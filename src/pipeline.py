"""Schnitt, Beat-Matching, Dark-Film-Rendering und Export.

CLI-Beispiel::

    python -m src.pipeline -i /srv/nas/nils/videos -o /srv/nas/nils/videos/output -t "Madeira 2026"
    python -m src.pipeline -i clip.MOV -o out/ -t "Madeira" -m song.mp3
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import (
    ConfigError,
    ensure_dir,
    expand_path,
    get,
    load_config,
    setup_logging,
    slugify,
)
from .metadata import ClipInfo, Geocoder, collect_clips, dominant_place, primary_place
from .styling import (
    GradeConfig,
    build_subtitle,
    build_text_overlay,
    center_crop_resize,
    make_grader,
    target_size,
)

log = logging.getLogger("tenor.pipeline")


@dataclass
class Segment:
    """Ein geplanter Cut: Ausschnitt aus einer Rohdatei."""

    clip: ClipInfo
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)


@dataclass
class RenderResult:
    """Ergebnis eines Laufs."""

    output: Path | None = None
    cover: Path | None = None
    outputs: list[Path] = field(default_factory=list)
    covers: list[Path] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    title: str = ""
    subtitle: str = ""
    duration: float = 0.0
    place: str | None = None
    trip: str | None = None
    beat_source: str = "fallback"
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.output is not None and self.output.is_file()


# --------------------------------------------------------------------------- #
# Beat-Matching
# --------------------------------------------------------------------------- #
def detect_beat_durations(music: Path, cut_cfg: Mapping[str, Any]) -> list[float]:
    """Beat-Tracking via librosa -> Liste von Cut-Längen in Sekunden.

    Beats werden zu "Downbeats" gruppiert, sodass ein Schnitt zwischen
    ``target_min`` und ``target_max`` liegt (Ziel: 1,4-2,0 s).
    """
    t_min = float(cut_cfg.get("target_min", 1.4))
    t_max = float(cut_cfg.get("target_max", 2.0))
    target = float(cut_cfg.get("target", (t_min + t_max) / 2))
    target = min(max(target, t_min), t_max)

    try:
        import librosa
    except ImportError:  # pragma: no cover
        log.warning("librosa fehlt - es gilt das Fallback-Intervall")
        return []

    try:
        y, sr = librosa.load(str(music), sr=22050, mono=True)
        if y.size == 0:
            raise ValueError("leere Audiodatei")
        _tempo, frames = librosa.beat.beat_track(y=y, sr=sr)
        times = librosa.frames_to_time(frames, sr=sr)
    except Exception as exc:
        log.warning("Beat-Tracking für %s fehlgeschlagen (%s) - Fallback greift", music.name, exc)
        return []

    beats = [0.0] + [float(t) for t in np.asarray(times).ravel() if math.isfinite(float(t))]
    beats = sorted(set(round(b, 4) for b in beats if b >= 0.0))
    if len(beats) < 3:
        log.warning("Zu wenige Beats in %s (%d) - Fallback greift", music.name, len(beats))
        return []

    intervals = [b - a for a, b in zip(beats, beats[1:]) if b - a > 0.05]
    if not intervals:
        return []
    median_beat = float(sorted(intervals)[len(intervals) // 2])
    group = max(1, int(round(median_beat / target)))
    log.info(
        "Beat-Grid: Tempo ~%.0f BPM, Beat-Abstand %.2fs -> Gruppe %d (Cut ~%.2fs)",
        60.0 / median_beat if median_beat else 0.0,
        median_beat,
        group,
        median_beat * group,
    )

    grid = beats[::group]
    durations: list[float] = []
    for start, end in zip(grid, grid[1:]):
        span = end - start
        if span <= 0:
            continue
        durations.append(min(max(span, t_min), t_max))
    if not durations:
        durations = [min(max(median_beat * group, t_min), t_max)]
    return durations


def _iter_durations(durations: Sequence[float], fallback: float, limit: int = 0) -> Iterator[float]:
    """Endloser Iterator über Cut-Längen (Beat-Grid, sonst fester Takt)."""
    pool = list(durations) if durations else []
    index = 0
    produced = 0
    while limit <= 0 or produced < limit:
        yield pool[index % len(pool)] if pool else fallback
        index += 1
        produced += 1


# --------------------------------------------------------------------------- #
# Schnittplan
# --------------------------------------------------------------------------- #
def build_segments(
    clips: Sequence[ClipInfo],
    cut_cfg: Mapping[str, Any],
    video_cfg: Mapping[str, Any],
    *,
    durations: Sequence[float] = (),
    min_source_duration: float = 0.0,
) -> list[Segment]:
    """Erzeugt chronologisch geordnete Cuts aus den Rohclips.

    Zu lange Rohclips werden zentriert gekürzt; ein Rest unterhalb der
    minimalen Cutlänge wird verworfen (besser ein sauberer Beat-Cut als ein
    0,3-Sekunden-Fetzen).
    """
    t_min = float(cut_cfg.get("target_min", 1.4))
    fallback = float(cut_cfg.get("fallback_interval", 1.8))
    max_cuts = int(cut_cfg.get("max_cuts", 0) or 0)
    max_per_clip = int(cut_cfg.get("max_per_clip", 0) or 0)
    max_total = float(get(video_cfg, "max_duration", 0) or 0)

    segments: list[Segment] = []
    total = 0.0
    duration_iter = _iter_durations(durations, fallback, max_cuts)

    for clip in clips:
        source_duration = clip.duration
        if source_duration <= 0.0:
            log.warning("%s: keine Dauer ermittelbar - wird übersprungen", clip.name)
            continue
        if source_duration < min_source_duration or source_duration < t_min:
            log.info(
                "%s: nur %.2fs Material (<%.1fs) - übersprungen",
                clip.name,
                source_duration,
                max(min_source_duration, t_min),
            )
            continue

        cursor = 0.0
        used_in_clip = 0.0
        cuts_from_clip = 0
        while True:
            if max_cuts and len(segments) >= max_cuts:
                break
            if max_per_clip and cuts_from_clip >= max_per_clip:
                break
            if max_total and total >= max_total:
                break

            remaining = source_duration - cursor
            if remaining < t_min:
                break

            want = next(duration_iter)
            if max_total and total + want > max_total:
                want = max_total - total
                if want < t_min:
                    break

            take = min(want, remaining)
            # Zentriert kürzen: subclip(mid - dur/2, mid + dur/2)
            mid = cursor + remaining / 2.0
            start = max(mid - take / 2.0, 0.0)
            end = min(start + take, source_duration)
            if end - start < t_min - 1e-3:
                break

            segments.append(Segment(clip=clip, start=round(start, 3), end=round(end, 3)))
            total += end - start
            used_in_clip += end - start
            cursor = end
            cuts_from_clip += 1

        log.info(
            "%s: %d Cuts, %.1fs von %.1fs Material genutzt",
            clip.name,
            cuts_from_clip,
            used_in_clip,
            source_duration,
        )

    return segments


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _open_source(path: Path) -> Any:
    """Öffnet eine Videoquelle; beschädigte Dateien werfen eine lesbare Exception.

    10-Bit-HEVC (yuv420p10le) wird via ffmpeg nach 8-Bit (yuv420p)
    transkodiert, damit moviepy/OpenCV die Frames verarbeiten können.
    """
    import tempfile
    import subprocess
    from moviepy import VideoFileClip

    # Prüfen ob 10-Bit-HEVC -> dann nach 8-Bit transkodieren
    probe_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt,codec_name",
        "-of", "csv=p=0", str(path)
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, timeout=10, check=True)
        out = result.stdout.decode().strip()
        if "yuv420p10le" in out or "p010le" in out:
            # 10-Bit -> temporär nach 8-Bit transkodieren
            tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            tmp.close()
            transcode_cmd = [
                "ffmpeg", "-v", "error", "-y",
                "-i", str(path),
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                "-an",  # Audio weg (Pipeline exportiert stumm)
                tmp.name
            ]
            subprocess.run(transcode_cmd, timeout=120, check=True)
            clip = VideoFileClip(tmp.name, audio=False)
            # Temporäre Datei beim Schließen löschen
            clip.close = lambda: (VideoFileClip.close(clip), __import__("os").unlink(tmp.name))
            return clip
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        pass  # Fallback: normal öffnen

    clip = VideoFileClip(str(path), audio=False)
    if clip.size is None or clip.size[0] < 2 or clip.size[1] < 2:
        raise ValueError(f"ungültige Bildgröße {clip.size}")
    return clip


def build_segment_clip(
    segment: Segment,
    cfg: Mapping[str, Any],
    grade: GradeConfig,
    overlay_png: Any,
    *,
    fps: int,
    width: int,
    height: int,
    sources: list[Any] | None = None,
) -> Any:
    """Rendert einen Cut: Crop -> Grading -> Text-Overlay.

    ``sources`` sammelt die geöffneten Quell-Clips. moviepy liest Frames
    verzögert, die Reader dürfen erst nach ``write_videofile`` geschlossen
    werden - deshalb wird hier *nicht* geschlossen.
    """
    from moviepy import CompositeVideoClip, ImageClip

    source = _open_source(segment.clip.path)
    if sources is not None:
        sources.append(source)

    source_duration = float(source.duration or 0.0)
    start = max(min(segment.start, max(source_duration - 0.05, 0.0)), 0.0)
    end = max(min(segment.end, source_duration), start + 1.0 / fps)
    piece = source.subclipped(start, end)
    piece = piece.with_duration(max(end - start, 1.0 / fps))

    piece = center_crop_resize(piece, width, height)
    piece = piece.with_fps(fps)
    piece = piece.image_transform(make_grader(grade))

    if overlay_png is not None:
        text = ImageClip(overlay_png, duration=piece.duration)
        piece = CompositeVideoClip([piece, text], size=(width, height))
    return piece.with_fps(fps)


def render_segments(
    segments: Sequence[Segment],
    cfg: Mapping[str, Any],
    *,
    grade: GradeConfig,
    overlay_png: Any,
    on_error: list[str] | None = None,
) -> list[Any]:
    """Rendert alle Cuts. Fehlerhafte Clips werden übersprungen, nicht abgebrochen."""
    from moviepy import concatenate_videoclips

    video_cfg = get(cfg, "video", {}) or {}
    fps = int(video_cfg.get("fps", 30))
    width, height = target_size(cfg)
    failures = on_error if on_error is not None else []
    rendered: list[Any] = []
    sources: list[Any] = []

    for index, segment in enumerate(segments, start=1):
        try:
            clip = build_segment_clip(
                segment,
                cfg,
                grade,
                overlay_png,
                fps=fps,
                width=width,
                height=height,
                sources=sources,
            )
            rendered.append(clip)
            log.info(
                "Cut %d/%d: %s %.2f-%.2fs (%.2fs)",
                index,
                len(segments),
                segment.clip.name,
                segment.start,
                segment.end,
                segment.duration,
            )
        except Exception as exc:
            message = f"Cut {index} aus {segment.clip.name} fehlgeschlagen: {exc}"
            log.error("%s", message)
            failures.append(message)
            _close_all(sources)
            sources = []
            if rendered:
                _close_all(rendered)
                rendered = []

    if not rendered:
        _close_all(sources)
        return []

    final = concatenate_videoclips(rendered, method="chain")
    try:
        final = final.without_audio()
    except Exception:  # pragma: no cover
        pass
    # Quell-Reader leben bis nach write_videofile weiter
    final.tenor_sources = sources  # type: ignore[attr-defined]
    return [final]


def _close_all(clips: Sequence[Any]) -> None:
    """Schließt Clips und ignoriert Fehler (z. B. bereits geschlossene Reader)."""
    for clip in clips:
        try:
            clip.close()
        except Exception:  # pragma: no cover
            pass


def make_overlay_array(cfg: Mapping[str, Any], title: str, subtitle: str) -> np.ndarray:
    """Text-Overlay als RGBA-Numpy-Array für ``ImageClip``."""
    width, height = target_size(cfg)
    image = build_text_overlay(cfg, title=title, subtitle=subtitle, width=width, height=height)
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    return np.asarray(image, dtype=np.uint8)


# --------------------------------------------------------------------------- #
# Cover
# --------------------------------------------------------------------------- #
def write_cover(
    video: Path,
    cover: Path,
    *,
    at: float = 0.5,
    width: int = 1080,
    height: int = 1920,
) -> Path | None:
    """Erzeugt ein 1080x1920 Cover-Frame bei Sekunde ``at`` (via ffmpeg)."""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{max(at, 0.0):.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        "-q:v", "2",
        str(cover),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    except FileNotFoundError:
        log.error("ffmpeg nicht gefunden - kein Cover erzeugt")
        return None
    except subprocess.TimeoutExpired:
        log.error("ffmpeg-Timeout beim Cover von %s", video.name)
        return None
    if proc.returncode != 0 or not cover.is_file():
        log.error(
            "Cover konnte nicht erzeugt werden (%s): %s",
            video.name,
            proc.stderr.decode("utf-8", "replace").strip()[:300],
        )
        return None
    log.info("Cover: %s", cover)
    return cover


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #
def trip_from_path(
    paths: Sequence[str | Path],
    bases: Sequence[str | Path] = (),
    ingest_dir: str | Path | None = None,
) -> str | None:
    """Errät den Tripnamen aus dem Ingest-Unterordner.

    Legt man Clips nach ``ingest/Madeira 2026/`` ab, ist der Ordnername der
    Tripname. ``bases`` sind die Eingabewurzeln dieses Laufs (CLI-``-i``).
    ``ingest_dir`` ist der konfigurierte Ingest-Ordner (Fallback für Watcher).
    Gibt es keinen gemeinsamen Unterordner, kommt ``None`` zurück.
    """
    if not paths:
        return None

    roots: list[Path] = []
    # Zuerst: explizit übergebene Bases (CLI -i)
    for raw_base in bases:
        try:
            candidate = expand_path(raw_base).resolve()
        except OSError:  # pragma: no cover
            continue
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)

    # Fallback: konfiguriertes ingest_dir (für Watcher, der Dateipfade als sources übergibt)
    if not roots and ingest_dir:
        try:
            candidate = expand_path(ingest_dir).resolve()
        except OSError:
            candidate = None
        if candidate and candidate.is_dir():
            roots.append(candidate)

    names: set[str] = set()
    fallback: str | None = None
    for raw in paths:
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError:  # pragma: no cover
            resolved = Path(raw)
        if fallback is None:
            fallback = resolved.parent.name or None

        for root in roots:
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            # Direkt im Ingest-Ordner -> kein Trip-Unterordner.
            if len(relative.parts) >= 2:
                names.add(relative.parts[0])
            break
        else:
            # Clip liegt unter keiner bekannten Wurzel: Elternordner nehmen,
            # aber nur wenn alle Clips dort liegen ( unten über names geprüft ).
            if roots:
                continue
            names.add(resolved.parent.name)

    names.discard("")
    if len(names) == 1:
        return names.pop()
    if not roots and fallback and len(names) == 1:
        return fallback
    return None


def _naming_values(cfg: Mapping[str, Any], title: str, when: float, day: str) -> dict[str, str]:
    """Platzhalter für Dateinamen und Unterordner."""
    stamp = time.localtime(when)
    return {
        "date": time.strftime("%Y-%m-%d", stamp),
        "time": time.strftime("%H%M%S", stamp),
        "slug": slugify(title),
        "title": slugify(title, max_length=80),
        "day": day or time.strftime("%Y-%m-%d", stamp),
        "trip": slugify(title, max_length=60),
    }


def build_output_name(cfg: Mapping[str, Any], title: str, when: float, day: str = "") -> str:
    naming = get(cfg, "naming", {}) or {}
    template = str(naming.get("template", "{date}_{slug}_{time}"))
    values = _naming_values(cfg, title, when, day)
    try:
        return template.format(**values)
    except KeyError as exc:
        log.warning("Unbekannter Platzhalter %s in naming.template - Fallback", exc)
        return f"{values['date']}_{values['slug']}_{values['time']}"


def build_output_dir(
    base: Path,
    cfg: Mapping[str, Any],
    title: str,
    when: float,
    day: str = "",
) -> Path:
    """Baut ``<output_dir>/<trip>/<day>/`` gemäß ``naming.folder_template``.

    ``naming.folder_template: ""`` deaktiviert die Unterordner komplett,
    dann wird direkt in ``base`` geschrieben.
    """
    naming = get(cfg, "naming", {}) or {}
    template = str(naming.get("folder_template", "{trip}/{day}")).strip()
    if not template:
        return base

    values = _naming_values(cfg, title, when, day)
    try:
        relative = template.format(**values)
    except KeyError as exc:
        log.warning("Unbekannter Platzhalter %s in naming.folder_template - Fallback", exc)
        relative = f"{values['trip']}/{values['day']}"

    # Jedes Segment gegen Traversal und leere Komponenten absichern
    parts: list[str] = []
    for raw_segment in relative.replace("\\", "/").split("/"):
        segment = raw_segment.strip()
        if not segment or segment in (".", ".."):
            continue
        if segment != segment.strip("/") or "/" in segment:
            continue
        slug = slugify(segment, max_length=60)
        # Direkt aufeinanderfolgende Dubletten (z. B. Titel == Tag) zusammenfassen,
        # sonst entstehen sinnlose Ordner wie 2026-09-11/2026-09-11/.
        if slug and (not parts or parts[-1] != slug):
            parts.append(slug)
    if not parts:
        return base
    return base.joinpath(*parts)


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Vergibt ``stem``, ``stem_2``, ... falls der Name schon existiert."""
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


# --------------------------------------------------------------------------- #
# Orchestrierung
# --------------------------------------------------------------------------- #
def _pick_music(music: str | Path | None, cfg: Mapping[str, Any]) -> Path | None:
    if music:
        path = Path(str(music)).expanduser()
        return path if path.is_file() else None
    music_dir = get(cfg, "music_dir")
    if not music_dir:
        return None
    directory = expand_path(music_dir)
    if not directory.is_dir():
        return None
    candidates = sorted(
        (p for p in directory.iterdir() if p.suffix.lower() == ".mp3" and p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def run(
    sources: Sequence[str | Path],
    *,
    config_path: str | None = None,
    output_dir: str | Path | None = None,
    title: str | None = None,
    music: str | Path | None = None,
    no_cover: bool = False,
    no_geocoding: bool = False,
    limit: int = 0,
    dry_run: bool = False,
    delete_after: bool = False,
) -> RenderResult:
    """Kompletter Pipeline-Lauf: Metadaten -> Schnitt -> Render -> Export."""
    cfg = load_config(config_path)
    video_cfg = get(cfg, "video", {}) or {}
    cut_cfg = get(cfg, "cut", {}) or {}

    result = RenderResult()
    if not sources:
        sources = [str(expand_path(get(cfg, "ingest_dir", ".")))]

    if no_geocoding:
        cfg.setdefault("geocoding", {})
        cfg["geocoding"]["enabled"] = False
    geocoder = Geocoder(cfg)

    clips = collect_clips(
        sources,
        geocoder=geocoder,
        recursive=bool(get(cfg, "watcher.recursive", False)),
    )
    geocoder.save_cache()

    if limit > 0:
        clips = clips[:limit]
    if not clips:
        log.error("Keine verwertbaren Eingabedateien gefunden: %s", ", ".join(str(s) for s in sources))
        return result

    min_source = float(get(cfg, "watcher.min_clip_duration", 0.0) or 0.0)
    usable = [c for c in clips if c.duration >= min_source]
    skipped = len(clips) - len(usable)
    if skipped:
        log.info("%d Datei(en) kürzer als %.1fs - übersprungen", skipped, min_source)
    if not usable:
        log.error("Alle Eingaben sind kürzer als %.1fs", min_source)
        return result

    place = dominant_place(usable)
    result.place = place
    reference = usable[0]
    # Ohne expliziten Titel: Tripname aus dem Ingest-Unterordner, dann
    # Ortsname (Kurzform), dann Datum, sonst "Tenor".
    trip = trip_from_path([str(c.path) for c in usable], sources, get(cfg, "ingest_dir"))
    effective_title = title or trip or primary_place(place) or reference.date_label or "Tenor"
    subtitle = build_subtitle(place, reference.date_label)
    result.title = effective_title
    result.trip = trip
    result.subtitle = subtitle
    if trip and not title:
        log.info("Tripname aus Ingest-Unterordner erkannt: %r", trip)

    music_path = _pick_music(music, cfg)
    durations: list[float] = []
    if music_path is not None:
        durations = detect_beat_durations(music_path, cut_cfg)
        result.beat_source = f"beats:{music_path.name}" if durations else "fallback"
    else:
        log.info("Keine Musik gefunden - fester Takt von %.1fs", float(cut_cfg.get("fallback_interval", 1.8)))

    segments = build_segments(
        usable,
        cut_cfg,
        video_cfg,
        durations=durations,
        min_source_duration=min_source,
    )
    result.segments = segments
    result.duration = sum(s.duration for s in segments)
    if not segments:
        log.error("Kein einziger Cut zustande gekommen - Export abgebrochen")
        return result

    log.info(
        "Schnittplan: %d Cuts, %.1fs, Titel=%r, Unterzeile=%r, Beat-Quelle=%s",
        len(segments),
        result.duration,
        effective_title,
        subtitle,
        result.beat_source,
    )

    now = time.time()
    base_out = expand_path(output_dir or get(cfg, "output_dir", "output"))
    if not dry_run:
        base_out = ensure_dir(base_out)

    # Clips nach Aufnahmetag gruppieren: jeder Tag wird ein eigenes Reel in
    # <trip>/<tag>/. Clips ohne Datum landen gemeinsam in "_ohne-datum".
    groups: dict[str, list[ClipInfo]] = {}
    for clip in usable:
        groups.setdefault(clip.date_label or "_ohne-datum", []).append(clip)
    if len(groups) > 1:
        log.info(
            "Material verteilt sich auf %d Tage: %s",
            len(groups),
            ", ".join(f"{k} ({len(v)} Dateien)" for k, v in sorted(groups.items())),
        )

    grade = GradeConfig.from_config(cfg)
    outputs: list[Path] = []
    covers: list[Path] = []
    total_duration = 0.0

    for day, day_clips in sorted(groups.items()):
        # Bei einem einzigen Tag wurde der Schnittplan oben schon gebaut.
        if len(groups) == 1:
            day_segments = segments
        else:
            day_segments = build_segments(
                day_clips,
                cut_cfg,
                video_cfg,
                durations=durations,
                min_source_duration=min_source,
            )
        if not day_segments:
            log.warning("Kein Cut für den Tag %s - übersprungen", day)
            continue

        day_place = dominant_place(day_clips)
        day_title = title or trip or primary_place(day_place) or day
        day_subtitle = build_subtitle(day_place, day)

        target = export_reel(
            day_segments,
            cfg,
            base_out,
            title=day_title,
            subtitle=day_subtitle,
            day=day,
            grade=grade,
            when=now,
            dry_run=dry_run,
            no_cover=no_cover,
        )
        if target is None:
            continue
        if dry_run:
            # Im Dry-Run existiert nichts - nur den Plan protokollieren.
            total_duration += sum(s.duration for s in day_segments)
            continue
        outputs.append(target)
        covers.append(target.with_suffix(".jpg"))
        total_duration += sum(s.duration for s in day_segments)

    result.duration = total_duration
    result.segments = segments

    if dry_run:
        return result

    if not outputs:
        log.error("Kein einziges Reel zustande gekommen")
        return result

    result.outputs = outputs
    result.output = outputs[0]
    result.covers = [c for c in covers if c.is_file()]
    result.cover = result.covers[0] if result.covers else None

    if delete_after:
        for clip in dict.fromkeys(c.path for c in usable):
            try:
                clip.unlink()
                log.info("Rohclip gelöscht: %s", clip.name)
            except OSError as exc:
                log.warning("Rohclip nicht löschbar (%s): %s", clip.name, exc)

    return result


def export_reel(
    segments: Sequence[Segment],
    cfg: Mapping[str, Any],
    base_out: Path,
    *,
    title: str,
    subtitle: str,
    day: str,
    grade: GradeConfig,
    when: float,
    dry_run: bool = False,
    no_cover: bool = False,
) -> Path | None:
    """Rendert und exportiert einen Satz von Cuts als ein Reel.

    Gibt den fertigen Pfad zurück, oder ``None`` wenn nichts exportiert wurde.
    """
    video_cfg = get(cfg, "video", {}) or {}

    if dry_run:
        # Nichts anlegen, nichts löschen - nur den Zielpfad vorhersagen.
        out_dir = build_output_dir(base_out, cfg, title, when, day)
        stem = build_output_name(cfg, title, when, day)
        suffix = str(get(cfg, "naming.extension", ".mp4") or ".mp4")
        log.info("Ausgabeordner: %s", out_dir)
        for segment in segments:
            log.info(
                "PLAN %s %.2f-%.2fs (%.2fs)",
                segment.clip.name,
                segment.start,
                segment.end,
                segment.duration,
            )
        return out_dir / f"{stem}{suffix}"

    out_dir = ensure_dir(build_output_dir(base_out, cfg, title, when, day))
    log.info("Ausgabeordner: %s", out_dir)

    stem = build_output_name(cfg, title, when, day)
    suffix = str(get(cfg, "naming.extension", ".mp4") or ".mp4")
    target = unique_path(out_dir, stem, suffix)
    partial = target.parent / f"{target.stem}.part.mp4"
    partial.unlink(missing_ok=True)

    try:
        overlay = make_overlay_array(cfg, title, subtitle)
    except Exception as exc:
        log.error("Text-Overlay konnte nicht gerendert werden (%s) - exportiere ohne Text", exc)
        overlay = None

    errors: list[str] = []
    rendered = render_segments(
        segments,
        cfg,
        grade=grade,
        overlay_png=overlay,
        on_error=errors,
    )
    for message in errors:
        log.error("%s", message)

    if not rendered:
        log.error("Kein Cut konnte gerendert werden - Abbruch ohne Export")
        return None

    final = rendered[0]
    ffmpeg_params = ["-pix_fmt", str(video_cfg.get("pixel_format", "yuv420p"))]
    crf = video_cfg.get("crf")
    if crf is not None:
        ffmpeg_params += ["-crf", str(crf)]
    if bool(video_cfg.get("faststart", True)):
        ffmpeg_params += ["-movflags", "+faststart"]
    threads = int(video_cfg.get("threads", 0) or 0)
    if threads:
        ffmpeg_params += ["-threads", str(threads)]

    try:
        final.write_videofile(
            str(partial),
            fps=int(video_cfg.get("fps", 30)),
            codec=str(video_cfg.get("codec", "libx264")),
            audio=bool(video_cfg.get("audio", False)),
            preset=str(video_cfg.get("preset", "medium")),
            threads=threads or None,
            ffmpeg_params=ffmpeg_params,
            logger=None,
        )
    except Exception as exc:
        log.exception("Export nach %s fehlgeschlagen: %s", partial, exc)
        partial.unlink(missing_ok=True)
        return None
    except BaseException:
        # SIGTERM/Stromausfall: unfertige Datei nicht liegen lassen
        partial.unlink(missing_ok=True)
        raise
    finally:
        _close_all([final])
        _close_all(getattr(final, "tenor_sources", []) or [])

    partial.replace(target)
    log.info("Export fertig: %s (%.1fs Video)", target, sum(s.duration for s in segments))

    if not no_cover and bool(video_cfg.get("cover", True)):
        write_cover(
            target,
            target.with_suffix(".jpg"),
            at=float(video_cfg.get("cover_at", 0.5)),
            width=target_size(cfg)[0],
            height=target_size(cfg)[1],
        )

    # Optional: Nextcloud-Scan nach Export (indiziert neue Dateien)
    if not dry_run:
        scan_nextcloud(cfg, out_dir)

    return target


def scan_nextcloud(cfg: Mapping[str, Any], output_dir: Path) -> None:
    """Führt den konfigurierten Nextcloud-Scan-Befehl aus."""
    nc_cfg = get(cfg, "nextcloud", {}) or {}
    if not nc_cfg.get("enabled", False):
        return
    cmd_template = nc_cfg.get("scan_cmd")
    if not cmd_template:
        return
    # Platzhalter {output_dir} ersetzen
    cmd = cmd_template.format(output_dir=output_dir)
    timeout = int(nc_cfg.get("timeout", 30) or 0)
    log.info("Nextcloud-Scan: %s", cmd)
    try:
        subprocess.run(
            cmd,
            shell=True,
            timeout=timeout,
            check=False,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        log.warning("Nextcloud-Scan Timeout nach %ss", timeout)
    except Exception as exc:  # pragma: no cover
        log.warning("Nextcloud-Scan fehlgeschlagen: %s", exc)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.pipeline",
        description="Rohclips chronologisch schneiden, dark-film graden und als 9:16-Reel exportieren.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-i", "--input", nargs="+", help="Eingangsdatei(en) oder -ordner")
    parser.add_argument("-o", "--output", help="Ausgabeordner (Default: output_dir aus config.yaml)")
    parser.add_argument("-t", "--title", help="Titelzeile des Overlays, z. B. 'Madeira 2026'")
    parser.add_argument("-m", "--music", help="MP3 für Beat-Matching")
    parser.add_argument("-c", "--config", help="Pfad zur config.yaml")
    parser.add_argument("--no-cover", action="store_true", help="kein Cover-Thumbnail erzeugen")
    parser.add_argument("--no-geo", action="store_true", help="Reverse-Geocoding überspringen")
    parser.add_argument("--limit", type=int, default=0, help="max. Anzahl Eingabedateien (0 = alle)")
    parser.add_argument("--dry-run", action="store_true", help="nur Schnittplan ausgeben, kein Export")
    parser.add_argument("--delete-after", action="store_true", help="Rohclips nach Export löschen")
    parser.add_argument("--json", action="store_true", help="Ergebnis als JSON auf stdout")
    parser.add_argument("-q", "--quiet", action="store_true", help="nur Warnungen und Fehler loggen")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg, quiet=args.quiet)

    if not args.input:
        ingest = expand_path(get(cfg, "ingest_dir", "."))
        if not ingest.is_dir():
            log.error("Kein -i angegeben und ingest_dir %s existiert nicht", ingest)
            return 2
        args.input = [str(ingest)]

    result = run(
        args.input,
        config_path=args.config,
        output_dir=args.output,
        title=args.title,
        music=args.music,
        no_cover=args.no_cover,
        no_geocoding=args.no_geo,
        limit=args.limit,
        dry_run=args.dry_run,
        delete_after=args.delete_after,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "output": str(result.output) if result.output else None,
                    "outputs": [str(p) for p in result.outputs],
                    "cover": str(result.cover) if result.cover else None,
                    "covers": [str(p) for p in result.covers],
                    "cuts": len(result.segments),
                    "duration": round(result.duration, 2),
                    "title": result.title,
                    "subtitle": result.subtitle,
                    "place": result.place,
                    "trip": result.trip,
                    "output_dir": str(result.output.parent) if result.output else None,
                    "beat_source": result.beat_source,
                    "errors": result.errors,
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if not result.output and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
