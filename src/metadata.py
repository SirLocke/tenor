"""Metadaten-Extraktion, Geocoding und chronologische Sortierung.

Alle Funktionen sind fehlertolerant: kaputte oder unlesbare Dateien liefern
``None`` bzw. leere Strukturen, damit ein Batch-Lauf nie abbricht.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    ConfigError,
    expand_path,
    get,
    iter_media_files,
    load_config,
    setup_logging,
)

log = logging.getLogger("tenor.metadata")

FFPROBE = "ffprobe"
FFPROBE_TIMEOUT = 30  # Sekunden - ein hängendes ffprobe darf den Lauf nicht blockieren

# Rohbitstream-Endungen ohne Container (kein Moov-Atom -> ffprobe braucht -f hevc)
RAW_VIDEO_SUFFIXES = {".hevc", ".h265", ".265", ".h264", ".264", ".avc", ".av1"}

#: ISO-6709 mit Vorzeichenpaaren, z. B. ``+53.4702-006.1153+023.5/``
_SIGNED_PAIR = re.compile(r"([+-]\d{1,3}(?:\.\d+)?)\s*([+-]\d{1,3}(?:\.\d+)?)")
#: ISO-6709 Kurzform ``+5328-00607/``
_SHORT_PAIR = re.compile(r"([+-]\d{2})(\d{2}(?:\.\d+)?)\s*([+-]\d{3})(\d{2}(?:\.\d+)?)")
#: Grad/Minuten/Sekunden mit Hemisphären, z. B. ``53°28'12.7"N 6°06'54.9"W``
_DMS = re.compile(
    r"""(\d{1,3})\s*[°d:\s]\s*(\d{1,2})\s*['m:\s]\s*([\d.]+)?\s*["s]?\s*([NSEW])""",
    re.IGNORECASE,
)
_EMBEDDED_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")

#: Reihenfolge der Tag-Priorität für den Aufnahmezeitpunkt.
CREATION_TAGS = (
    "com.apple.quicktime.contentcreationdate",
    "com.apple.quicktime.creationdate",
    "creation_time",
    "date",
    "date_time_original",
    "DateTimeOriginal",
    "Creation Time",
)

LOCATION_TAGS = (
    "com.apple.quicktime.location.ISO6709",
    "com.apple.quicktime.location.iso6709",
    "location",
    "LocationISO6709",
    "GPSCoordinates",
)


@dataclass
class ClipInfo:
    """Metadaten eines Rohclips."""

    path: Path
    created: datetime | None = None
    duration: float = 0.0
    latitude: float | None = None
    longitude: float | None = None
    place: str | None = None
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_geo: bool = False
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def sort_key(self) -> tuple[float, str]:
        """Chronologischer Schlüssel: Aufnahmezeit, sonst mtime, dann Name."""
        if self.created is not None:
            return (self.created.timestamp(), self.name)
        try:
            return (self.path.stat().st_mtime, self.name)
        except OSError:
            return (0.0, self.name)

    @property
    def date_label(self) -> str:
        """Datum für die Unterzeile (``JJJJ-MM-TT``) oder leerer String."""
        when = self.created or self._mtime()
        if when is None:
            return ""
        return when.strftime("%Y-%m-%d")

    @property
    def time_label(self) -> str:
        when = self.created or self._mtime()
        if when is None:
            return ""
        return when.strftime("%H%M%S")

    def _mtime(self) -> datetime | None:
        try:
            return datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "created": self.created.isoformat() if self.created else None,
            "duration": round(self.duration, 3),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "place": self.place,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "has_geo": self.has_geo,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# ffprobe
# --------------------------------------------------------------------------- #
def ffprobe_json(path: str | Path) -> dict[str, Any] | None:
    """Ruft ffprobe im JSON-Modus auf. Fehler -> ``None`` (nie eine Exception).

    Rohbitstreams ohne Container (``.hevc``/``.h265``) bekommen den passenden
    Demuxer explizit gesetzt, sonst findet ffprobe keine Streams.
    """
    cmd = [FFPROBE, "-v", "quiet"]
    if Path(path).suffix.lower() in RAW_VIDEO_SUFFIXES:
        cmd += ["-f", "hevc"]
    cmd += [
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=FFPROBE_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        log.error("ffprobe nicht gefunden - bitte 'apt install ffmpeg' ausführen")
        return None
    except subprocess.TimeoutExpired:
        log.warning("ffprobe-Timeout nach %ss für %s", FFPROBE_TIMEOUT, path)
        return None
    except OSError as exc:  # pragma: no cover
        log.warning("ffprobe konnte %s nicht starten: %s", path, exc)
        return None

    if proc.returncode != 0 or not proc.stdout:
        log.warning(
            "ffprobe meldet Fehler für %s (rc=%s): %s",
            path,
            proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip()[:200],
        )
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        log.warning("ffprobe-JSON unlesbar für %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _collect_tags(probe: Mapping[str, Any] | None) -> dict[str, str]:
    """Sammelt Format- und Stream-Tags in ein flaches Dict (case-insensitiv)."""
    tags: dict[str, str] = {}
    if not probe:
        return tags
    containers: list[Mapping[str, Any]] = [probe.get("format") or {}]
    containers.extend(st for st in (probe.get("streams") or []) if isinstance(st, Mapping))
    for container in containers:
        raw = container.get("tags")
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                text = str(value).strip()
                if text and key not in tags:
                    tags[key] = text
    return tags


def _tag(tags: Mapping[str, str], *names: str) -> str | None:
    lowered = {k.lower(): v for k, v in tags.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _parse_datetime(value: str | None) -> datetime | None:
    """Parst QuickTime/ffprobe-Zeitstempel zu einem timezone-aware datetime."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # "2023-07-15T10:23:45.000000Z" -> "+00:00"
    normalized = re.sub(r"Z$", "+00:00", text)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text[: len(fmt) + 8].strip(), fmt)
                break
            except ValueError:
                continue
        else:
            log.debug("Unbekannter Zeitstempel: %r", text)
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# --------------------------------------------------------------------------- #
# Geodaten
# --------------------------------------------------------------------------- #
def parse_coordinates(value: str | None) -> tuple[float | None, float | None]:
    """Liest ISO-6709 / QuickTime-Koordinaten -> ``(lat, lon)``.

    Unterstützt ``+53.4702-006.1153+023.5/``, die Kurzform ``+5328-00607/``
    sowie Grad/Minuten/Sekunden mit Hemisphärenkennzeichen.
    """
    if not value:
        return (None, None)
    text = str(value).strip()
    if not text:
        return (None, None)

    match = _SIGNED_PAIR.search(text)
    if match:
        lat, lon = _to_float(match.group(1)), _to_float(match.group(2))
        if _valid_coords(lat, lon):
            return (lat, lon)

    match = _SHORT_PAIR.search(text)
    if match:
        lat = _to_float(match.group(1) + match.group(2))
        lon = _to_float(match.group(3) + match.group(4))
        if _valid_coords(lat, lon):
            return (lat, lon)

    dms = _DMS.findall(text)
    if len(dms) >= 2:
        lat = _dms_to_deg(*dms[0])
        lon = _dms_to_deg(*dms[1])
        if _valid_coords(lat, lon):
            return (lat, lon)

    return (None, None)


def _dms_to_deg(degrees: str, minutes: str, seconds: str, hemisphere: str) -> float:
    value = float(degrees) + float(minutes or 0) / 60.0 + float(seconds or 0) / 3600.0
    if hemisphere.upper() in ("S", "W"):
        value = -value
    return value


def _valid_coords(lat: float, lon: float) -> bool:
    if any(v != v for v in (lat, lon)):  # NaN
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0)


def embedded_datetime(value: str | None) -> datetime | None:
    """Datumsanteil aus einem ISO-6709-String (iPhone legt ihn mit ab)."""
    if not value:
        return None
    match = _EMBEDDED_DATE.search(str(value))
    if not match:
        return None
    return _parse_datetime(f"{match.group(1)}T{match.group(2)}Z")


# --------------------------------------------------------------------------- #
# Geocoding
# --------------------------------------------------------------------------- #
class Geocoder:
    """Lazyer, gecachter Reverse-Geocoder (Nominatim) mit eigenem User-Agent."""

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.enabled = bool(get(cfg, "geocoding.enabled", True))
        self.timeout = float(get(cfg, "geocoding.timeout", 5) or 5)
        self.zoom = int(get(cfg, "geocoding.zoom", 10) or 10)
        self.language = str(get(cfg, "geocoding.language", "de") or "de")
        self.user_agent = str(get(cfg, "geocoding.user_agent", "tenor/1.0"))
        self.precision = int(get(cfg, "geocoding.cache_precision", 3) or 3)
        self.cache_file = expand_path(get(cfg, "geocoding.cache_file", ".cache/geocoding.json"))
        self._cache: dict[str, str | None] = self._load_cache()
        self._client: Any = None
        self._disabled_reason: str | None = None

    # -- Cache ----------------------------------------------------------- #
    def _load_cache(self) -> dict[str, str | None]:
        try:
            with self.cache_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return {str(k): (v if isinstance(v, str) or v is None else None) for k, v in data.items()}
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Geocoding-Cache nicht lesbar (%s): %s", self.cache_file, exc)
        return {}

    def save_cache(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, ensure_ascii=False, indent=1, sort_keys=True)
            tmp.replace(self.cache_file)
        except OSError as exc:
            log.warning("Geocoding-Cache nicht schreibbar: %s", exc)

    def _key(self, lat: float, lon: float) -> str:
        p = self.precision
        return f"{round(lat, p):.{p}f},{round(lon, p):.{p}f}"

    # -- Client ---------------------------------------------------------- #
    def _get_client(self) -> Any:
        if self._client is not None or self._disabled_reason:
            return self._client
        try:
            from geopy.geocoders import Nominatim  # lazy import
        except ImportError:
            self._disabled_reason = "geopy ist nicht installiert"
            log.warning("Geocoding deaktiviert: %s", self._disabled_reason)
            return None
        try:
            self._client = Nominatim(
                user_agent=self.user_agent,
                timeout=self.timeout,
            )
        except Exception as exc:  # pragma: no cover
            self._disabled_reason = str(exc)
            log.warning("Nominatim-Client nicht initialisierbar: %s", exc)
            return None
        return self._client

    def reverse(self, lat: float | None, lon: float | None) -> str | None:
        """Weltkoordinaten -> ``"City, Land"`` (oder ``None``)."""
        if not self.enabled or lat is None or lon is None:
            return None
        if not _valid_coords(lat, lon):
            return None
        key = self._key(lat, lon)
        if key in self._cache:
            return self._cache[key]

        client = self._get_client()
        if client is None:
            return None
        try:
            location = client.reverse(
                (lat, lon),
                zoom=self.zoom,
                language=self.language,
                exactly_one=True,
                timeout=self.timeout,
            )
        except Exception as exc:
            # Netzwerkfehler/Timeout/429 -> Cache negative, Batch läuft weiter.
            log.warning("Reverse-Geocoding für %s fehlgeschlagen: %s", key, exc)
            self._cache[key] = None
            return None

        if location is None or not location.raw:
            self._cache[key] = None
            return None

        place = self._format_place(location.raw)
        self._cache[key] = place
        return place

    @staticmethod
    def _format_place(raw: Mapping[str, Any]) -> str | None:
        def first(*keys: str) -> str | None:
            for key in keys:
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        city = first("city", "town", "village", "municipality", "suburb", "hamlet")
        country = first("country")
        state = first("state", "region", "county")
        if city and country and city != country:
            return f"{city}, {country}"
        if city:
            return city
        if state and country and state != country:
            return f"{state}, {country}"
        return country or first("display_name", "label")


# --------------------------------------------------------------------------- #
# Sammlung
# --------------------------------------------------------------------------- #
def probe_clip(path: str | Path) -> ClipInfo:
    """Liest alle für die Pipeline relevanten Metadaten einer Datei."""
    clip = ClipInfo(path=Path(path))
    probe = ffprobe_json(path)
    if probe is None:
        clip.error = "ffprobe fehlgeschlagen"
        return clip

    tags = _collect_tags(probe)

    created = _parse_datetime(_tag(tags, *CREATION_TAGS))
    geo_raw = _tag(tags, *LOCATION_TAGS)
    if created is None:
        # Manche iPhone-Dateien tragen das Datum nur im Location-Tag.
        created = embedded_datetime(geo_raw)
    clip.created = created

    fmt = probe.get("format") or {}
    duration = _to_float(fmt.get("duration"))
    if duration != duration:  # NaN
        duration = 0.0
    clip.duration = max(duration, 0.0)

    for stream in probe.get("streams") or []:
        if not isinstance(stream, Mapping) or stream.get("codec_type") != "video":
            continue
        if clip.width == 0:
            clip.width = int(_to_float(stream.get("width")) or 0)
            clip.height = int(_to_float(stream.get("height")) or 0)
        if clip.fps == 0.0:
            rate = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1")
            try:
                num, _, den = rate.partition("/")
                den_value = float(den or 0)
                clip.fps = float(num) / den_value if den_value else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                clip.fps = 0.0
        if clip.duration == 0.0:
            stream_dur = _to_float(stream.get("duration"))
            clip.duration = max(0.0 if stream_dur != stream_dur else stream_dur, 0.0)
            # Rohbitstream ohne Container: Dauer via Frame-Count berechnen.
            if clip.duration == 0.0 and clip.fps > 0.0:
                try:
                    count_cmd = [
                        FFPROBE, "-v", "quiet",
                        "-select_streams", "v:0",
                        "-count_frames",
                        "-show_entries", "stream=nb_read_frames",
                        "-of", "csv=p=0",
                    ]
                    if Path(path).suffix.lower() in RAW_VIDEO_SUFFIXES:
                        count_cmd += ["-f", "hevc"]
                    count_cmd.append(str(path))
                    proc = subprocess.run(count_cmd, capture_output=True, timeout=30, check=False)
                    if proc.returncode == 0 and proc.stdout.strip():
                        frames = int(proc.stdout.strip())
                        clip.duration = frames / clip.fps
                except (ValueError, subprocess.TimeoutExpired, OSError):
                    pass
        break

    lat, lon = parse_coordinates(geo_raw)
    if lat is None:
        lat, lon = _from_gps_tags(tags)
    clip.latitude, clip.longitude = lat, lon
    clip.has_geo = lat is not None and lon is not None
    clip.extra = {"geo_raw": geo_raw} if geo_raw else {}
    return clip


def _from_gps_tags(tags: Mapping[str, str]) -> tuple[float | None, float | None]:
    """Fallback: einzelne GPS-Tags oder XMP-Exif-Exif-GPSFelder."""
    lat = _to_float(_tag(tags, "GPSLatitude") or "")
    lon = _to_float(_tag(tags, "GPSLongitude") or "")
    ref_lat = (_tag(tags, "GPSLatitudeRef") or "N").strip().upper()
    ref_lon = (_tag(tags, "GPSLongitudeRef") or "E").strip().upper()
    if ref_lat in ("S", "W"):
        lat = -abs(lat)
    if ref_lon in ("S", "W"):
        lon = -abs(lon)
    if _valid_coords(lat, lon):
        return (lat, lon)
    return (None, None)


def collect_clips(
    sources: Sequence[str | Path],
    *,
    geocoder: Geocoder | None = None,
    recursive: bool = False,
    ignore_dirs: Sequence[str] = (),
) -> list[ClipInfo]:
    """Sammelt Clips aus Dateien und/oder Verzeichnissen, chronologisch sortiert."""
    files: list[Path] = []
    for source in sources:
        path = Path(source).expanduser()
        if path.is_dir():
            extensions = (".mov", ".mp4", ".m4v", ".hevc", ".h265")
            files.extend(iter_media_files(path, extensions, recursive=recursive, ignore_dirs=ignore_dirs))
        elif path.exists():
            files.append(path)
        else:
            log.warning("Quelle existiert nicht, wird übersprungen: %s", path)

    # Duplikate entfernen, Reihenfolge stabil halten
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)

    clips: list[ClipInfo] = []
    for path in unique:
        try:
            clip = probe_clip(path)
        except Exception as exc:  # defensiv: Metadaten dürfen nie den Lauf killen
            log.exception("Unerwarteter Fehler beim Lesen von %s: %s", path, exc)
            clip = ClipInfo(path=path, error=str(exc))
        clips.append(clip)
        if clip.error:
            log.warning("%s: %s - wird mit mtime einsortiert", path.name, clip.error)
        else:
            log.info(
                "%s | %s | %.1fs | %sx%s | %s",
                clip.name,
                clip.created.isoformat() if clip.created else "kein Datum",
                clip.duration,
                clip.width or "?",
                clip.height or "?",
                f"{clip.latitude:.4f},{clip.longitude:.4f}" if clip.has_geo else "keine Geodaten",
            )

    if geocoder is not None and geocoder.enabled:
        for clip in clips:
            if clip.has_geo and not clip.place:
                try:
                    clip.place = geocoder.reverse(clip.latitude, clip.longitude)
                except Exception as exc:  # pragma: no cover
                    log.warning("Geocoding für %s fehlgeschlagen: %s", clip.name, exc)

    clips.sort(key=lambda c: c.sort_key)
    return clips


def dominant_place(clips: Iterable[ClipInfo]) -> str | None:
    """Häufigster Ort in einer Clipliste (Votes über Mehrheitsbeschluss)."""
    places = [c.place for c in clips if c.place]
    if not places:
        return None
    counts: dict[str, int] = {}
    for place in places:
        counts[place] = counts.get(place, 0) + 1
    return max(counts.items(), key=lambda item: (item[1], -places.index(item[0])))[0]


def primary_place(place: str | None) -> str | None:
    """Kürzester sinnvoller Ortsname aus ``"São Roque, Funchal, Madeira, PT"``.

    Wird als Titelzeile verwendet, damit Zeile 1 nicht mit dem kompletten
    Adress-String überfrachtet wird.
    """
    if not place:
        return None
    first = place.split(",")[0].strip()
    return first or None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``python -m src.metadata -i /pfad/zum/ordner`` - Metadaten auflisten."""
    parser = argparse.ArgumentParser(
        prog="python -m src.metadata",
        description="Metadaten der Rohclips anzeigen (Debug-Hilfe).",
    )
    parser.add_argument("-i", "--input", nargs="+", required=True, help="Datei oder Verzeichnis")
    parser.add_argument("-c", "--config", help="Pfad zur config.yaml")
    parser.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    parser.add_argument("--no-geo", action="store_true", help="Reverse-Geocoding überspringen")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg)
    geocoder = None if args.no_geo else Geocoder(cfg)
    clips = collect_clips(
        args.input,
        geocoder=geocoder,
        recursive=bool(get(cfg, "watcher.recursive", False)),
    )
    if geocoder is not None:
        geocoder.save_cache()

    if args.json:
        print(json.dumps([c.to_dict() for c in clips], indent=2, ensure_ascii=False))
    else:
        for clip in clips:
            print(
                f"{clip.date_label or '----------'} {clip.time_label or '------'} | "
                f"{clip.name} | {clip.duration:7.1f}s | "
                f"{clip.place or (f'{clip.latitude:.4f},{clip.longitude:.4f}' if clip.has_geo else '-')} "
                f"| {clip.error or 'ok'}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
