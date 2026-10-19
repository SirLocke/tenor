"""Spotify-Audio-Analysis für Beat-Timestamps.

Die Pipeline exportiert standardmäßig still; die Musik kommt später in Instagram
dazu. Hier werden daher nur die *Beat-Zeitpunkte* eines Spotify-Tracks geholt,
nicht die Audio-Datei selbst.

Aktivierung: ``SPOTIFY_CLIENT_ID`` und ``SPOTIFY_CLIENT_SECRET`` als
Environment-Variablen setzen und in ``config.yaml`` unter ``spotify.enabled``
aktivieren. Fehlt eine der Voraussetzungen, wird sauber auf den BPM-Fallback
zurückgefallen - die Pipeline läuft dann ohne Spotify weiter.

.. warning::

   Seit dem **27.11.2024** beantworten ``/v1/audio-analysis`` und
   ``/v1/audio-features`` nur noch für Apps mit damals genehmigter
   "Extended Quota" die Anfrage. Für jede später registrierte App liefern sie
   dauerhaft HTTP 403 - unabhängig von Zugangsdaten und Token. Diese App
   bekommt deshalb :class:`SpotifyAccessError` statt Beats, und der Aufrufer
   nutzt eine lokale Audiodatei aus ``music_dir`` oder den BPM-Fallback.
   ``/v1/tracks/`` ist nicht betroffen, deshalb funktioniert die Anzeige des
   Tracknamens weiterhin.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import expand_path

log = logging.getLogger("tenor.spotify")

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_ANALYSIS_URL = "https://api.spotify.com/v1/audio-analysis/{track_id}"

TRACK_ID_RE = re.compile(r"(?:track[:/]|open\.spotify\.com/track/)([A-Za-z0-9]{22})")

#: Erklärung, die bei HTTP 403 auf den quota-gesperrten Endpoints ausgegeben wird.
QUOTA_HINT = (
    "Spotify verweigert /v1/audio-analysis für diese App (HTTP 403). Seit dem "
    "27.11.2024 ist der Endpoint nur noch für Apps mit vor diesem Datum "
    "genehmigter Extended Quota freigeschaltet - neue Apps bekommen dauerhaft "
    "403, auch mit korrekten Zugangsdaten. Das lässt sich nicht in der "
    "Konfiguration beheben: als Beat-Quelle eine lokale Audiodatei über "
    "spotify.track_file (bzw. music_dir) angeben oder den BPM-Fallback nutzen."
)


class SpotifyAccessError(RuntimeError):
    """Die Spotify-App darf den angefragten Endpoint nicht benutzen (HTTP 403).

    Wird nur für die quota-gesperrten Endpoints (:data:`QUOTA_HINT`) geworfen,
    damit der Aufrufer die Einschränkung *einmal* mit Ursache melden kann,
    statt bei jedem Lauf still in den BPM-Fallback zu fallen.
    """


class _TokenCache:
    """Thread-sicherer OAuth-Token-Cache.

    Ohne Lock könnten zwei parallele Pipeline-Läufe beide ein Token anfordern
    und sich gegenseitig mit veralteten Ablaufzeiten überschreiben.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires: float = 0.0

    def get(self) -> str | None:
        with self._lock:
            if self._token and self._expires > time.time() + 30:
                return self._token
            return None

    def set(self, token: str, expires_in: float) -> None:
        with self._lock:
            self._token = token
            self._expires = time.time() + max(expires_in, 60)

    def clear(self) -> None:
        with self._lock:
            self._token = None
            self._expires = 0.0


_cache = _TokenCache()

_beat_cache_lock = threading.Lock()
CACHE_TTL = 7 * 24 * 3600


def extract_track_id(uri: str) -> str | None:
    """Track-ID aus URI, URL oder nackter ID ziehen."""
    match = TRACK_ID_RE.search(uri or "")
    if match:
        return match.group(1)
    bare = re.fullmatch(r"([A-Za-z0-9]{22})", (uri or "").strip())
    return bare.group(1) if bare else None


def _requests() -> Any:
    """``requests`` erst hier importieren.

    So bleibt ``import src.pipeline`` auch ohne requests möglich - die
    Abhängigkeit ist nur für Spotify nötig.
    """
    try:
        import requests
    except ImportError:  # pragma: no cover
        log.warning("requests fehlt - Spotify Beat-Detection nicht verfügbar")
        return None
    return requests


def get_token(force: bool = False) -> str | None:
    """Client-Credentials-Token (gültig ca. 1 Stunde)."""
    if not force:
        cached = _cache.get()
        if cached:
            return cached

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        log.debug("SPOTIFY_CLIENT_ID/SECRET nicht gesetzt - Spotify deaktiviert")
        return None

    requests = _requests()
    if requests is None:
        return None

    try:
        response = requests.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=10,
        )
        if response.status_code in (400, 401):
            log.warning("Spotify-Zugangsdaten ungültig (HTTP %s)", response.status_code)
            return None
        response.raise_for_status()
        data = response.json()
        token = str(data["access_token"])
        _cache.set(token, float(data.get("expires_in", 3600)))
        log.info(
            "Spotify-Token bezogen (gültig %.0f min)",
            float(data.get("expires_in", 3600)) / 60,
        )
        return token
    except Exception as exc:  # Netzwerk, JSON, Timeouts - alles nicht kritisch
        log.warning("Spotify-Token nicht erhältlich: %s", exc)
        return None


def _auth_get(url: str, *, quota_locked: bool = False) -> Any | None:
    """GET mit Bearer-Token, einmaliger Retry bei abgelaufenem Token.

    Unterstützt exponentielles Backoff bei HTTP 429 (Rate Limit).
    ``quota_locked`` markiert Endpoints, die seit 27.11.2024 für neue Apps
    gesperrt sind. Ein 403 wird dort als :class:`SpotifyAccessError` gemeldet
    (mit Ursache), statt es als generischen Fehler zu verschlucken.
    """
    requests = _requests()
    if requests is None:
        return None

    max_429_retries = 3
    retries_429 = 0

    for attempt in (0, 1):
        token = get_token(force=attempt == 1)
        if not token:
            return None

        while True:
            try:
                response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            except Exception as exc:
                log.warning("Spotify-Anfrage fehlgeschlagen: %s", exc)
                return None

            if response.status_code == 429:
                if retries_429 >= max_429_retries:
                    retry_after = response.headers.get("Retry-After", "?")
                    log.warning(
                        "Spotify-Drosselung erreicht (Retry-After=%s), maximale Retries überschritten",
                        retry_after,
                    )
                    return None

                retry_after_header = response.headers.get("Retry-After")
                if retry_after_header and retry_after_header.isdigit():
                    wait_time = int(retry_after_header)
                else:
                    wait_time = 2**retries_429

                log.info(
                    "Spotify HTTP 429: Warte %ds (Versuch %d/%d)",
                    wait_time,
                    retries_429 + 1,
                    max_429_retries,
                )
                time.sleep(wait_time)
                retries_429 += 1
                continue

            break  # Exit the 429 loop for other responses

        if response.status_code == 401 and attempt == 0:
            _cache.clear()
            continue
        if response.status_code == 404:
            return None
        if response.status_code == 403 and quota_locked:
            raise SpotifyAccessError(QUOTA_HINT)
        if not response.ok:
            log.warning("Spotify HTTP %s bei %s", response.status_code, url.rsplit("/", 1)[-1])
            return None
        try:
            return response.json()
        except ValueError as exc:
            log.warning("Spotify-Antwort kein gültiges JSON: %s", exc)
            return None
    return None


def _evict_stale(directory: Path, ttl: float) -> int:
    """Entfernt abgelaufene oder ungültige Cache-Einträge.

    Liest die JSON-Dateien und vergleicht `stored_at` (oder mtime) mit dem TTL.
    Fehlerhafte Dateien werden gelöscht (werden beim nächsten Lauf neu geladen).
    """
    if not directory.is_dir():
        return 0

    now = time.time()
    evicted = 0
    for file in directory.glob("*.json"):
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
            stored_at = float(data.get("stored_at", file.stat().st_mtime))
            if now - stored_at > ttl:
                file.unlink(missing_ok=True)
                evicted += 1
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
            # Ungültige Datei: löschen und neu holen
            file.unlink(missing_ok=True)
            evicted += 1

    if evicted > 0:
        log.debug("Spotify-Beat-Cache: %d abgelaufene Einträge entfernt", evicted)
    return evicted


def fetch_spotify_beats(
    track_uri: str,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    ttl: float | None = None,
) -> list[float] | None:
    """Beat-Startzeiten in Sekunden aus Spotify Audio Analysis."""
    track_id = extract_track_id(track_uri)
    if not track_id:
        log.warning("Ungültige Spotify-Referenz: %r", track_uri)
        return None

    actual_cache_dir = Path(cache_dir) if cache_dir is not None else expand_path(".cache/spotify_beats")
    actual_ttl = ttl if ttl is not None else CACHE_TTL
    cache_file = actual_cache_dir / f"{track_id}.json"

    with _beat_cache_lock:
        try:
            if cache_file.is_file():
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)

                stored_at = float(cached_data.get("stored_at", cache_file.stat().st_mtime))
                if time.time() - stored_at <= actual_ttl:
                    beats_hit = cached_data.get("beats")

                    # Try to refresh the mtime/stored_at on hit
                    try:
                        cached_data["stored_at"] = time.time()
                        temp_file = cache_file.with_name(f"{cache_file.name}.tmp")
                        with open(temp_file, "w", encoding="utf-8") as f:
                            json.dump(cached_data, f)
                        temp_file.replace(cache_file)
                    except OSError:
                        pass # Ignore if cache dir is read-only

                    return beats_hit
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
            pass # Invalid cache file, refetch

        # Evict stale entries on miss
        try:
            _evict_stale(actual_cache_dir, actual_ttl)
        except OSError:
            pass

    try:
        data = _auth_get(SPOTIFY_ANALYSIS_URL.format(track_id=track_id), quota_locked=True)
        if data is None:
            return None

        raw = data.get("beats") if isinstance(data, dict) else None
        if not isinstance(raw, list) or not raw:
            log.warning("Spotify lieferte keine Beats für %s", track_uri)
            return None

        beats: list[float] = []
        for entry in raw:
            try:
                value = float(entry["start"])
            except (KeyError, TypeError, ValueError):
                continue
            if value >= 0.0 and value == value:  # NaN aussortieren
                beats.append(value)
        beats.sort()

        tempo = 0.0
        track = data.get("track") if isinstance(data, dict) else None
        if isinstance(track, dict):
            try:
                tempo = float(track.get("tempo") or 0.0)
            except (TypeError, ValueError):
                tempo = 0.0
        log.info("Spotify: %d Beats, Tempo ~%.1f BPM für %s", len(beats), tempo, track_uri)

        result = beats or None

        if result:
            with _beat_cache_lock:
                try:
                    actual_cache_dir.mkdir(parents=True, exist_ok=True)
                    temp_file = cache_file.with_name(f"{cache_file.name}.tmp")
                    with open(temp_file, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "stored_at": time.time(),
                                "tempo": tempo,
                                "beats": result,
                            },
                            f,
                        )
                    temp_file.replace(cache_file)
                except OSError as exc:
                    log.warning("Spotify-Beat-Cache nicht schreibbar: %s", exc)

        return result
    except SpotifyAccessError:
        raise
    except Exception as exc:
        log.warning("Fehler beim Abruf der Spotify-Beats: %s", exc)
        return None
