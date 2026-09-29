"""tenor - automatische Video-Pipeline (Ingest -> Beat-Schnitt -> Dark Film -> 9:16 Export).

Das Paket stellt die gemeinsame Konfigurations-Infrastruktur bereit. Die
Fachmodule liegen in:

* :mod:`src.metadata`  - ffprobe-Metadaten, ISO-6709-Geodaten, Reverse-Geocoding
* :mod:`src.styling`   - Center-Crop, Dark-Film-Grading, Typografie-Overlay
* :mod:`src.pipeline`  - Beat-Matching, Schnitt, Export, Cover (CLI)
* :mod:`src.watcher`   - Ingest-Watcher (CLI, systemd-tauglich)
"""

from __future__ import annotations

import copy
import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "__version__",
    "REPO_ROOT",
    "DEFAULTS",
    "ConfigError",
    "find_config",
    "load_config",
    "get",
    "expand_path",
    "ensure_dir",
    "setup_logging",
    "slugify",
]

__version__ = "1.0.0"

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Vorgabewerte. ``config.yaml`` wird tief in diese Struktur hinein gemergt,
#: fehlende Keys bleiben dadurch immer befüllt.
DEFAULTS: dict[str, Any] = {
    "ingest_dir": "/srv/nas/Nils/video-cut/ingest",
    "output_dir": "/srv/nas/Nils/video-cut/output",
    "music_dir": "/srv/nas/Nils/video-cut/music",
    "extensions": [".mov", ".mp4", ".m4v", ".hevc", ".h265"],
    "video": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "codec": "libx264",
        "crf": 19,
        "preset": "medium",
        "pixel_format": "yuv420p",
        "audio": False,
        "faststart": True,
        "max_duration": 0,
    },
    "cut": {
        "target_min": 1.4,
        "target_max": 2.0,
        "target": 1.7,
        "fallback_bpm": 120,
        "fallback_interval": 1.8,
        "max_cuts": 0,
        "max_per_clip": 0,
    },
    # Text nur auf den ersten N Cuts (0 = alle).
    "text_on_first_cuts": 2,
    # Bildqualitäts-Gate für die Cut-Auswahl. Schwellen an echten
    # 4K-iPhone-Clips kalibriert (s. config.yaml für die Begründung).
    "quality": {
        "enabled": True,
        "sample_fps": 4.0,
        "edge_trim": 0.25,
        "min_sharpness": 2500.0,
        "min_luma": 35.0,
        "max_luma": 242.0,
        "max_black_ratio": 0.35,
        "max_white_ratio": 0.25,
        "duplicate_threshold": 20.0,
        "duplicate_memory": 3,
        "search_offsets": [0.3, -0.3, 0.6, -0.6, 0.9, -0.9],
        "drop_duplicates": True,
        "keep_imperfect": True,
        "cache_dir": ".cache/quality",
        "timeout": 60.0,
    },
    "spotify": {
        "enabled": False,
        "track_uri": "",
        "default_bpm": 120,
        "fallback_bpm": {
            "default": 120,
            "pop": 120,
            "edm": 128,
            "hiphop": 90,
            "rock": 110,
            "reggae": 80,
            "house": 124,
            "techno": 132,
        },
    },
    "style": {
        "gamma": 0.93,
        "contrast": 0.22,
        "black_lift": 0.035,
        "black_toe": 2.2,
        "highlight_ceiling": 0.94,
        "saturation": 0.90,
        "shadow_desaturate": 0.18,
        "teal_red": -0.006,
        "teal_green": 0.012,
        "teal_blue": 0.026,
        "highlight_cool": 0.010,
        "skin_damp": 0.008,
        "text": {
            "anchor_x": 900,
            "y_title": 1445,
            "max_width": 780,
            "title_size": 36,
            "subtitle_size": 24,
            "title_tracking": 3,
            "subtitle_tracking": 1,
            "line_gap": 16,
            "title_color": "#FFFFFF",
            "subtitle_color": "#D0D5DD",
            "uppercase_title": True,
            "safe_bottom": 1540,
            "shadow_offset": [2, 2],
            "shadow_blur": 4,
            "shadow_alpha": 0.7,
        },
    },
    "fonts": {
        "bold": "assets/fonts/Roboto-Bold.ttf",
        "regular": "assets/fonts/Roboto-Regular.ttf",
        "mono": "assets/fonts/RobotoMono-Regular.ttf",
        "auto_download": True,
        "system_fallbacks": [
            "/usr/share/fonts/truetype/roboto/unhinted/Roboto-Bold.ttf",
            "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ],
    },
    "geocoding": {
        "enabled": True,
        "timeout": 5,
        "zoom": 10,
        "language": "de",
        "user_agent": f"tenor/{__version__} (self-hosted video pipeline)",
        "cache_file": ".cache/geocoding.json",
        "cache_precision": 3,
    },
    "watcher": {
        "debounce_seconds": 5,
        "batch_window": 12,
        "recursive": True,
        "ignore_dirs": ["imgest"],
        "process_existing_on_start": True,
        "delete_after": False,
        "min_clip_duration": 0.6,
        "retry_limit": 3,
        "retry_backoff": 300,
    },
    "naming": {
        "template": "{date}_{slug}_{time}",
        "folder_template": "{trip}/{day}",
        "extension": ".mp4",
    },
    "logging": {
        "level": "INFO",
        "file": ".cache/tenor.log",
    },
}

_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(RuntimeError):
    """Konfiguration konnte nicht gelesen oder interpretiert werden."""


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``override`` rekursiv in eine Kopie von ``base``."""
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _substitute(value: Any) -> Any:
    """Ersetzt ``${VAR}`` durch Umgebungsvariablen (rekursiv)."""
    if isinstance(value, str):
        return _ENV_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, Mapping):
        return {k: _substitute(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v) for v in value]
    return value


def find_config(path: str | os.PathLike[str] | None = None) -> Path:
    """Ermittelt die zu verwendende Konfigurationsdatei.

    Reihenfolge: expliziter Pfad -> ``TENOR_CONFIG`` -> ``./config.yaml`` ->
    ``../config.yaml`` relativ zum Paket (Projekt-Root).
    """
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"Konfiguration nicht gefunden: {candidate}")
        return candidate.resolve()

    env = os.environ.get("TENOR_CONFIG")
    if env:
        candidate = Path(env).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"TENOR_CONFIG zeigt auf eine fehlende Datei: {candidate}")
        return candidate.resolve()

    for candidate in (Path.cwd() / "config.yaml", REPO_ROOT / "config.yaml"):
        if candidate.is_file():
            return candidate.resolve()

    # Kein config.yaml vorhanden: Defaults aus dem Repo-Root verwenden.
    return REPO_ROOT / "config.yaml"


def _coerce(value: str, template: Any) -> Any:
    """Wandelt einen Env-String in den Typ des bestehenden Config-Werts."""
    if isinstance(template, bool):
        return value.strip().lower() in ("1", "true", "yes", "on", "ja")
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(value)
        except ValueError:
            return value
    if isinstance(template, float):
        try:
            return float(value)
        except ValueError:
            return value
    if isinstance(template, list):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _apply_env_overrides(cfg: dict[str, Any], prefix: str = "TENOR_") -> None:
    """Überschreibt Config-Werte aus Umgebungsvariablen (in place).

    ``video.fps: 30`` lässt sich als ``TENOR_VIDEO_FPS=60`` überschreiben,
    ``ingest_dir`` als ``TENOR_INGEST_DIR``. Der Typ wird vom bestehenden
    Wert abgeleitet, damit z. B. ``true``/``false`` als Boolean ankommt.
    """
    for key, value in list(cfg.items()):
        if isinstance(value, Mapping):
            _apply_env_overrides(value, prefix + key.upper() + "_")
            continue
        env_name = f"{prefix}{key.upper()}"
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        cfg[key] = _coerce(raw, value)


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Lädt die Konfiguration, merged sie mit den Defaults und substituiert ``${VAR}``.

    Reihenfolge der Priorität (steigend): Defaults < config.yaml < explizite
    ``overrides`` < ``TENOR_*``-Umgebungsvariablen.
    """
    config_path = find_config(path)
    raw: dict[str, Any] = {}

    if config_path.is_file():
        try:
            import yaml  # lazy import, damit Defaults auch ohne PyYAML laden
        except ImportError as exc:  # pragma: no cover
            raise ConfigError("PyYAML fehlt - 'pip install -r requirements.txt'") from exc
        try:
            with config_path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
        except Exception as exc:
            raise ConfigError(f"{config_path} ist kein gültiges YAML: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path} muss ein YAML-Mapping enthalten")
        raw = loaded
    else:
        logging.getLogger(__name__).warning(
            "Keine config.yaml gefunden - verwende eingebaute Vorgabewerte."
        )

    merged = _deep_merge(DEFAULTS, raw)
    if overrides:
        merged = _deep_merge(merged, overrides)
    merged = _substitute(merged)
    _apply_env_overrides(merged)
    merged["_config_path"] = str(config_path)
    return merged


def get(cfg: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    """Liest ``cfg["video"]["fps"]`` über ``get(cfg, "video.fps", 30)``."""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def expand_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """Wandelt einen Konfigurationspfad in einen absoluten :class:`Path`.

    ``~``, ``${VAR}`` und relative Pfade (relativ zu ``base``, sonst
    ``REPO_ROOT``) werden aufgelöst.
    """
    raw = _substitute(str(value))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base or REPO_ROOT) / path
    return path


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    """Legt ein Verzeichnis an (inklusive Eltern) und gibt es zurück."""
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def setup_logging(cfg: Mapping[str, Any] | None = None, *, quiet: bool = False) -> logging.Logger:
    """Konfiguriert Logging auf stdout (und optional in eine Logdatei)."""
    cfg = cfg or {}
    level_name = str(get(cfg, "logging.level", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    if quiet:
        level = logging.WARNING

    root = logging.getLogger("tenor")
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root.addHandler(stream)

    log_file = get(cfg, "logging.file")
    if log_file:
        try:
            target = expand_path(log_file)
            target.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(target, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(fmt))
            root.addHandler(file_handler)
        except OSError as exc:  # Logdatei darf den Lauf nie killen
            root.warning("Logdatei nicht schreibbar (%s): %s", log_file, exc)

    root.propagate = False
    return root


def slugify(value: str, *, max_length: int = 48) -> str:
    """Macht aus einem Titel einen dateisystem-sicheren Dateinamen-Baustein."""
    cleaned = _substitute(str(value or "reel")).strip()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"[\s_-]+", "-", cleaned).strip("-")
    return (cleaned or "reel")[:max_length].strip("-")


def iter_media_files(
    directory: str | os.PathLike[str],
    extensions: Iterable[str],
    *,
    recursive: bool = False,
    ignore_dirs: Iterable[str] = (),
) -> list[Path]:
    """Listet Mediendateien in ``directory`` (sortiert, Temp-Dateien ausgeschlossen).

    ``ignore_dirs``: Verzeichnisnamen, die komplett ignoriert werden (z. B. ``["imgest"]``).
    """
    base = Path(directory).expanduser()
    if not base.is_dir():
        return []
    wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    ignore = {d.lower() for d in ignore_dirs}
    pattern = "**/*" if recursive else "*"
    found: list[Path] = []
    for path in base.glob(pattern):
        if not path.is_file() or path.suffix.lower() not in wanted:
            continue
        # Ignorierte Ordner in der Pfadstruktur prüfen
        if any(part.lower() in ignore for part in path.parts):
            continue
        name = path.name
        if name.startswith(".") or name.endswith((".part", ".tmp", ".crdownload", "~")):
            continue
        found.append(path)
    return sorted(found, key=lambda p: p.name.lower())
