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

import logging
import os
import re
import threading
import time
from typing import Any

log = logging.getLogger("tenor.spotify")

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_ANALYSIS_URL = "https://api.spotify.com/v1/audio-analysis/{track_id}"
SPOTIFY_TRACK_URL = "https://api.spotify.com/v1/tracks/{track_id}"

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
        log.info("Spotify-Token bezogen (gültig %.0f min)", float(data.get("expires_in", 3600)) / 60)
        return token
    except Exception as exc:  # Netzwerk, JSON, Timeouts - alles nicht kritisch
        log.warning("Spotify-Token nicht erhältlich: %s", exc)
        return None


def _auth_get(url: str, *, quota_locked: bool = False) -> Any | None:
    """GET mit Bearer-Token, einmaliger Retry bei abgelaufenem Token.

    ``quota_locked`` markiert Endpoints, die seit 27.11.2024 für neue Apps
    gesperrt sind. Ein 403 wird dort als :class:`SpotifyAccessError` gemeldet
    (mit Ursache), statt es als generischen Fehler zu verschlucken.
    """
    requests = _requests()
    if requests is None:
        return None
    for attempt in (0, 1):
        token = get_token(force=attempt == 1)
        if not token:
            return None
        try:
            response = requests.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=20
            )
        except Exception as exc:
            log.warning("Spotify-Anfrage fehlgeschlagen: %s", exc)
            return None
        if response.status_code == 401 and attempt == 0:
            _cache.clear()
            continue
        if response.status_code == 404:
            return None
        if response.status_code == 403 and quota_locked:
            raise SpotifyAccessError(QUOTA_HINT)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "?")
            log.warning("Spotify-Drosselung erreicht (Retry-After=%s)", retry_after)
            return None
        if not response.ok:
            log.warning("Spotify HTTP %s bei %s", response.status_code, url.rsplit("/", 1)[-1])
            return None
        try:
            return response.json()
        except ValueError as exc:
            log.warning("Spotify-Antwort kein gültiges JSON: %s", exc)
            return None
    return None


def track_name(track_uri: str) -> str | None:
    """``artist - title`` für Logging/Beat-Source."""
    track_id = extract_track_id(track_uri)
    if not track_id:
        return None
    data = _auth_get(SPOTIFY_TRACK_URL.format(track_id=track_id))
    if not isinstance(data, dict):
        return None
    title = str(data.get("name") or "").strip()
    artists = data.get("artists") or []
    names = [str(a.get("name", "")).strip() for a in artists if isinstance(a, dict)]
    artist = ", ".join(n for n in names if n)
    return f"{artist} - {title}" if artist and title else (title or None)


def fetch_spotify_beats(track_uri: str) -> list[float] | None:
    """Beat-Startzeiten in Sekunden aus Spotify Audio Analysis."""
    track_id = extract_track_id(track_uri)
    if not track_id:
        log.warning("Ungültige Spotify-Referenz: %r", track_uri)
        return None

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
    log.info(
        "Spotify: %d Beats, Tempo ~%.1f BPM für %s", len(beats), tempo, track_uri
    )
    return beats or None
