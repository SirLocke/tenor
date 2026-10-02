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
import os
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
from .quality import (
    ClipAnalysis,
    QualityConfig,
    analyse_clips,
    clamp_start,
    plan_edge,
    plan_windows,
    spread_windows,
    find_highlights,
)
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
    #: Grund, falls die bevorzugte Beat-Quelle nicht nutzbar war (leer = ok).
    beat_note: str = ""
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


def durations_from_bpm(bpm: float, cut_cfg: Mapping[str, Any]) -> list[float]:
    """Erzeugt ein Beat-Grid aus einem reinen BPM-Wert.

    Wird benutzt, wenn keine echte Audio-Datei und keine Spotify-Beats
    vorliegen. Gruppiert den Beat-Takt auf ``cut.target`` (1,4-2,0 s), damit
    die Schnitte auf echte Takte fallen statt auf beliebige Intervalle.
    """
    t_min = float(cut_cfg.get("target_min", 1.4))
    t_max = float(cut_cfg.get("target_max", 2.0))
    target = float(cut_cfg.get("target", (t_min + t_max) / 2))
    target = min(max(target, t_min), t_max)
    if not isinstance(bpm, (int, float)) or isinstance(bpm, bool):
        return []
    if not math.isfinite(float(bpm)) or bpm <= 0:
        return []
    beat = 60.0 / bpm
    group = max(1, int(round(target / beat)))
    span = min(max(beat * group, t_min), t_max)
    # Ein 2-Takt-Loop klingt rhythmisch, ein 1-Takt wirkt wie Zufall.
    reps = 2 if span * 2 <= t_max + t_min else 1
    return [span] * reps


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
def _plan_clip_cuts(
    source_duration: float,
    t_min: float,
    duration_iter: Iterator[float],
    *,
    max_cuts: int,
    max_per_clip: int,
    max_total: float,
    already_planned: int,
    already_total: float,
    clip_budget: float = 0.0,
    decay: float = 2.0,
) -> list[float]:
    """Legt fest, wie viele Cuts ein Clip liefert und wie lang sie werden.

    Verteilt Schnitte proportional zum Material und zur Zielzeit.
    ``decay`` (Standard 1.8) steuert die Schrittweite im verbleibenden Material,
    sodass längere Clips proportional mehr Material beisteuern.
    """
    wanted: list[float] = []
    remaining = source_duration
    total = already_total

    while True:
        if max_cuts and already_planned + len(wanted) >= max_cuts:
            break
        if max_per_clip and len(wanted) >= max_per_clip:
            break
        if max_total and total >= max_total:
            break
        if clip_budget and sum(wanted) >= clip_budget:
            break
        if remaining < t_min:
            break

        want = next(duration_iter)
        if max_total and total + want > max_total:
            want = max_total - total
            if want < t_min:
                break
        if clip_budget and sum(wanted) + want > clip_budget:
            want = clip_budget - sum(wanted)
            if want < t_min:
                break

        take = min(want, remaining)
        wanted.append(take)
        total += take
        remaining = (remaining - take) / decay

    return wanted


def build_segments(
    clips: Sequence[ClipInfo],
    cut_cfg: Mapping[str, Any],
    video_cfg: Mapping[str, Any],
    *,
    durations: Sequence[float] = (),
    min_source_duration: float = 0.0,
    quality_cfg: QualityConfig | None = None,
    analyses: Mapping[Path, ClipAnalysis] | None = None,
) -> list[Segment]:
    """Erzeugt chronologisch geordnete Cuts aus den Rohclips.

    Wie viele Cuts ein Clip liefert, entscheidet der Beat-Takt. *Wo* im Clip
    sie liegen, entscheidet das Qualitäts-Gate: Die Fenster werden über den
    ganzen Clip verteilt und anschließend geprüft - auf Schärfe, Belichtung
    und darauf, ob sie denselben Moment schon einmal zeigen. Was durchfällt,
    wird verschoben, bis es passt.

    Ohne Gate (oder wenn die Analyse nicht auswertbar war) bleibt es bei der
    reinen Evenverteilung. Ein Rest unterhalb der minimalen Cutlänge wird
    verworfen - besser ein sauberer Beat-Cut als ein 0,3-Sekunden-Fetzen.
    """
    t_min = float(cut_cfg.get("target_min", 1.4))
    fallback = float(cut_cfg.get("fallback_interval", 1.8))
    max_cuts = int(cut_cfg.get("max_cuts", 0) or 0)
    max_per_clip = int(cut_cfg.get("max_per_clip", 0) or 0)
    max_total = float(get(video_cfg, "max_duration", 0) or 0)
    gate = quality_cfg if quality_cfg is not None and quality_cfg.enabled else None
    edge_trim = gate.edge_trim if gate else 0.0

    segments: list[Segment] = []
    total = 0.0
    duration_iter = _iter_durations(durations, fallback, max_cuts)
    history: list[np.ndarray] = []
    kept_total = dropped_total = shifted_total = 0

    valid_clips = [
        c for c in clips
        if c.duration > 0.0 and c.duration >= max(min_source_duration, t_min)
    ]

    for idx, clip in enumerate(valid_clips):
        source_duration = clip.duration

        # Wenn ein max_total vorgegeben ist (z. B. 30s): Budget pro Clip verteilen,
        # damit Clips über das gesamte Video gestreut werden statt am Anfang abzubrechen
        clip_budget = 0.0
        if max_total > 0:
            remaining_clips = len(valid_clips) - idx
            remaining_total = max(max_total - total, 0.0)
            if remaining_clips > 0:
                clip_budget = max(t_min, remaining_total / remaining_clips)

        wanted = _plan_clip_cuts(
            source_duration,
            t_min,
            duration_iter,
            max_cuts=max_cuts,
            max_per_clip=max_per_clip,
            max_total=max_total,
            already_planned=len(segments),
            already_total=total,
            clip_budget=clip_budget,
            decay=float(cut_cfg.get("decay", 2.0)),
        )
        if not wanted:
            continue

        analysis = analyses.get(clip.path) if analyses else None

        # Der Rand wird einmal je Clip aus dem *konkreten* Schnittplan berechnet
        # und danach an Verteilung, Clamp und Gate-Suche weitergereicht. Vorher
        # standen die drei Stellen auf unterschiedlichen Werten, sodass ein
        # durchgefallenes Fenster bis 0,9 s in die Kameraeinfahrt geschoben
        # werden konnte und der erste Cut bei 0,57 s begann.
        edge = plan_edge(edge_trim, source_duration, sum(wanted))

        if gate is not None and analysis is not None:
            starts = find_highlights(analysis, source_duration, wanted, gate, edge)
        else:
            starts = spread_windows(source_duration, wanted, edge)

        windows = []
        for start, length in zip(starts, wanted):
            start = clamp_start(start, length, source_duration, edge)
            windows.append((start, start + length))

        if gate is not None and (analysis is None or not analysis.usable):
            reason = "keine Analyse" if analysis is None else (analysis.error or "unbekannt")
            log.info("%s: Qualitätsprüfung übersprungen (%s)", clip.name, reason)

        plans, history = plan_windows(
            analysis,
            windows,
            gate or QualityConfig(enabled=False),
            source_duration=source_duration,
            reference=history,
            edge_trim=edge,
        )

        planned_seconds = sum(end - start for start, end in windows)
        used_in_clip = 0.0
        cuts_from_clip = 0
        for plan in plans:
            if not plan.kept or plan.length < t_min - 1e-3:
                dropped_total += 1
                continue
            if max_total and total + plan.length > max_total:
                break
            segments.append(
                Segment(clip=clip, start=round(plan.start, 3), end=round(plan.end, 3))
            )
            total += plan.length
            used_in_clip += plan.length
            cuts_from_clip += 1
            if abs(plan.shifted) > 1e-3:
                shifted_total += 1

        kept_total += cuts_from_clip
        log.info(
            "%s: %d Cuts, %.1fs von %.1fs Material genutzt%s",
            clip.name,
            cuts_from_clip,
            used_in_clip,
            source_duration,
            f" ({100.0 * used_in_clip / planned_seconds:.0f}% der Planung)"
            if planned_seconds > 0
            else "",
        )

    if gate is not None and (kept_total or dropped_total or shifted_total):
        log.info(
            "Qualitäts-Gate: %d Cuts behalten, %d verworfen, %d verschoben",
            kept_total,
            dropped_total,
            shifted_total,
        )

    return segments


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def has_audio_stream(path: Path) -> bool:
    """Prüft, ob die fertige Datei wirklich eine Tonspur enthält.

    moviepy legt bei ``audio=True`` auch dann eine Datei an, wenn kein Ton
    vorhanden war - der Stream fehlt dann aber. Für den Export-Log ist der
    Unterschied der ganze Punkt: entweder das Reel ist hörbar oder nicht.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            timeout=15,
            check=True,
        )
        return result.stdout.decode().strip() == "audio"
    except (subprocess.SubprocessError, OSError, ValueError):
        return False


def probe_duration(path: Path) -> float | None:
    """Dauer einer fertigen Datei in Sekunden, oder ``None``.

    Genauer als der Schnittplan, wenn einzelne Cuts ausgefallen sind.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path),
            ],
            capture_output=True,
            timeout=15,
            check=True,
        )
        value = float(result.stdout.decode().strip())
        return value if math.isfinite(value) and value > 0 else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _open_source(path: Path, *, with_audio: bool = False) -> Any:
    """Öffnet eine Videoquelle; beschädigte Dateien werfen eine lesbare Exception.

    10-Bit-HEVC (yuv420p10le) wird via ffmpeg nach 8-Bit (yuv420p)
    transkodiert, damit moviepy/OpenCV die Frames verarbeiten können.

    ``with_audio`` steuert, ob die Tonspur geöffnet wird. Sie ist per Default
    aus, weil die Reels stumm exportiert werden - der Ton eines Rohclips wird
    nur mit ``video.audio: true`` mitgenommen.
    """
    import tempfile
    import subprocess
    from moviepy import VideoFileClip

    def _validated(clip: Any) -> Any:
        """Größenprüfung - der Reader ist ab hier schon offen (ffmpeg läuft)."""
        size = clip.size
        if size is None or size[0] < 2 or size[1] < 2:
            raise ValueError(f"ungültige Bildgröße {size}")
        return clip

    # Prüfen ob 10-Bit-HEVC -> dann nach 8-Bit transkodieren
    probe_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt,codec_name",
        "-of", "csv=p=0", str(path)
    ]
    tmp_path: Path | None = None
    try:
        result = subprocess.run(probe_cmd, capture_output=True, timeout=10, check=True)
        out = result.stdout.decode().strip()
        if "yuv420p10le" in out or "p010le" in out:
            # 10-Bit -> temporär nach 8-Bit transkodieren
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            transcode_cmd = [
                "ffmpeg", "-v", "error", "-y",
                "-i", str(path),
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
            ]
            if with_audio:
                # Ton nur mitnehmen, wenn er danach wirklich gebraucht wird.
                transcode_cmd += ["-c:a", "aac", "-b:a", "192k"]
            else:
                transcode_cmd += ["-an"]
            transcode_cmd.append(str(tmp_path))
            subprocess.run(transcode_cmd, timeout=120, check=True)
            clip = _validated(VideoFileClip(str(tmp_path), audio=with_audio))
            original_close = clip.close
            scratch = tmp_path

            def close_and_cleanup(_close: Any = original_close, _tmp: Path = scratch) -> None:
                try:
                    _close()
                finally:
                    _tmp.unlink(missing_ok=True)

            clip.close = close_and_cleanup
            return clip
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        # WICHTIG: bei jedem Fehlschlag das Zwischenfile löschen. moviepy
        # räumt nichts auf (Clip.__del__ ist absichtlich ein no-op), sonst
        # wächst /tmp bei jeder kaputten Datei um ein Vielfaches.
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
            tmp_path = None
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
            tmp_path = None
        raise

    clip = VideoFileClip(str(path), audio=with_audio)
    try:
        return _validated(clip)
    except Exception:
        # Reader (und damit der ffmpeg-Kindprozess) ist schon offen.
        try:
            clip.close()
        except Exception:  # pragma: no cover
            pass
        raise


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
    with_audio: bool = False,
) -> Any:
    """Rendert einen Cut: Crop -> Grading -> Text-Overlay.

    ``sources`` sammelt die geöffneten Quell-Clips. moviepy liest Frames
    verzögert, die Reader dürfen erst nach ``write_videofile`` geschlossen
    werden - deshalb wird hier *nicht* geschlossen.

    ``with_audio`` hängt die Tonspur des Rohclips an den fertigen Cut. Der Ton
    wird als *eigener* Subclip aus der Quelle geschnitten, nicht aus dem
    Bild-Clip geerbt: ``image_transform``/``resized`` liefern einen Clip
    ohne ``audio``, und ein geerbter Ton wäre nach dem Grading ohnehin
    unbrauchbar. Die Auswahl bleibt damit eindeutig der Originalton des
    Clips - es wird nichts aus ``music_dir`` oder Spotify untergemischt.
    """
    from moviepy import CompositeVideoClip, ImageClip

    source = _open_source(segment.clip.path, with_audio=with_audio)
    if sources is not None:
        sources.append(source)

    source_duration = float(source.duration or 0.0)
    frame = 1.0 / fps
    start = max(min(segment.start, max(source_duration - 0.05, 0.0)), 0.0)
    # Obergrenze gewinnt: das min()-Clamp darf NICHT von einem
    # start+frame-Floor überstimmt werden, sonst liest subclipped über EOF.
    end = min(segment.end, source_duration)
    if end <= start:
        raise ValueError(
            f"Cut {segment.start:.2f}-{segment.end:.2f}s liegt außerhalb von "
            f"{segment.clip.name} (Dauer {source_duration:.2f}s)"
        )
    end = max(end, min(start + frame, source_duration))
    piece = source.subclipped(start, end)
    piece = piece.with_duration(max(end - start, frame))

    # Ton des Rohclips: derselbe Ausschnitt, unabhängig vom Bild-Pfad. Wird
    # vor dem Crop/Grading abgezweigt, weil die Bild-Transformationen den
    # Audio-Teil des Clips nicht mitnehmen.
    source_audio = getattr(source, "audio", None) if with_audio else None

    piece = center_crop_resize(piece, width, height)
    grader = make_grader(grade)
    if grader is not None:
        piece = piece.image_transform(grader)

    if overlay_png is not None:
        text = ImageClip(overlay_png, duration=piece.duration)
        piece = CompositeVideoClip([piece, text], size=(width, height))

    if source_audio is not None:
        try:
            piece = piece.with_audio(
                source_audio.subclipped(start, end).with_duration(piece.duration)
            )
        except Exception as exc:
            log.warning(
                "Ton von %s nicht übernehmbar (%.2f-%.2fs): %s",
                segment.clip.name,
                start,
                end,
                exc,
            )

    return piece.with_fps(fps)


def render_segments(
    segments: Sequence[Segment],
    cfg: Mapping[str, Any],
    *,
    grade: GradeConfig,
    overlay_png: Any,
    text_on_first_cuts: int = 0,
    with_audio: bool = False,
    on_error: list[str] | None = None,
) -> list[Any]:
    """Rendert alle Cuts. Fehlerhafte Clips werden übersprungen, nicht abgebrochen.

    ``with_audio`` nimmt den Originalton der Rohclips mit. Fehlt bei einzelnen
    Quellen eine Tonspur, bleibt der betreffende Cut stumm - der Reel bleibt
    dadurch in jedem Fall hörbar, wo Material mit Ton vorliegt.
    """
    from moviepy import concatenate_videoclips

    video_cfg = get(cfg, "video", {}) or {}
    fps = int(video_cfg.get("fps", 30))
    width, height = target_size(cfg)
    failures = on_error if on_error is not None else []
    rendered: list[Any] = []
    sources: list[Any] = []

    for index, segment in enumerate(segments, start=1):
        try:
            # Text-Overlay nur auf den ersten N Cuts (Instagram-Reels-Konvention:
            # der Hook trägt die Beschriftung, der Rest bleibt clean).
            segment_overlay = overlay_png
            if text_on_first_cuts > 0 and index > text_on_first_cuts:
                segment_overlay = None
            clip = build_segment_clip(
                segment,
                cfg,
                grade,
                segment_overlay,
                fps=fps,
                width=width,
                height=height,
                sources=sources,
                with_audio=with_audio,
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
            # Nur der fehlgeschlagene Cut wird verworfen. Die bereits
            # gerenderten Cuts bleiben erhalten - sonst hätte das Reel vorn
            # ein Loch und der Schnittplan passte nicht zum Ergebnis.
            message = f"Cut {index} aus {segment.clip.name} fehlgeschlagen: {exc}"
            log.error("%s", message)
            failures.append(message)
            _close_all(sources)
            sources = []

    if not rendered:
        _close_all(sources)
        return []

    # concatenate_videoclips kann bei gemischten Größen/fehlerhaften
    # n_frames werfen. Ohne den finally-Block bleiben dann ALLE geöffneten
    # Reader (je ein ffmpeg-Prozess) bis zum nächsten GC-Cycle hängen.
    try:
        final = concatenate_videoclips(rendered, method="chain")
    except BaseException:
        _close_all(sources)
        _close_all(rendered)
        raise
    if not with_audio:
        # Ohne Tonwunsch wird die Spur abgeworfen, damit der Export nicht
        # versehentlich eine mitgeschleppte Tonspur enthält.
        try:
            final = final.without_audio()
        except Exception:  # pragma: no cover
            pass
    elif getattr(final, "audio", None) is None:
        log.warning(
            "Kein einziger Cut hatte eine Tonspur - der Export bleibt stumm "
            "(rohvideo.audio_codec=%s)",
            get(cfg, "video.audio_codec", "aac"),
        )
    # Quell-Reader leben bis nach write_videofile weiter
    final.tenor_sources = sources  # type: ignore[attr-defined]
    return [final]


def _close_all(clips: Sequence[Any]) -> None:
    """Schließt Clips und ignoriert Fehler (z. B. bereits geschlossene Reader).

    Ein moviepy-Clip ist selbst slicebar (``clip[start:end]``). Wird ein
    einzelner Clip statt einer Liste übergeben, iteriert diese Schleife über
    seine *Frames* statt über Clips - der Aufruf rendert dann endlos Samples
    und blockiert. Deshalb wird hier auf eine Liste normalisiert.
    """
    if clips is None:
        return
    # Eine Liste/Tupel ist die Normalform; alles andere (inkl. eines einzelnen
    # Clips) wird zu einem Ein-Element-Container.
    if not isinstance(clips, (list, tuple)):
        clips = [clips]  # type: ignore[assignment]
    for clip in clips:
        if clip is None:
            continue
        try:
            clip.close()
        except Exception:  # pragma: no cover
            pass


def _match_audio_channels(clip: Any, nchannels: int) -> Any:
    """Bringt eine Tonspur auf eine feste Kanalzahl.

    ``CompositeAudioClip`` addiert die Teile direkt als Numpy-Arrays. Ein
    Mono-Originalton unter einer Stereo-Musik scheitert dabei mit
    ``operands could not be broadcast together``: der Mono-Teil liefert
    ``(n,)``, der Stereo-Teil ``(n, 2)``. Im Reel-Export fällt genau das auf,
    weil iPhone-Clips meist mono aufgenommen sind und MP3s stereo sind.
    """
    current = int(getattr(clip, "nchannels", 1) or 1)
    if current == nchannels:
        return clip

    get = clip.get_frame

    def widen(t: Any) -> Any:
        if current == 1:
            # [..., None] statt np.repeat direkt: auf 1-D wiederholt np.repeat
            # die Samples und erzeugt die doppelte Länge statt einer Kanalachse.
            return np.repeat(get(t)[..., None], nchannels, axis=-1)
        return get(t).mean(axis=-1, keepdims=True)

    matched = clip.with_updated_frame_function(widen)
    # `with_updated_frame_function` tauscht nur die Frame-Funktion. Ohne dieses
    # Nachziehen meldet die Spur weiter ihre alte Kanalzahl, und
    # `CompositeAudioClip` rechnet die Kanalzahl aus den Teilen hoch - der
    # gemischte Ton bliebe dann als Mono im Reel.
    matched.nchannels = nchannels
    return matched


def mix_music(
    video: Any,
    music: str | Path,
    cfg: Mapping[str, Any],
    video_cfg: Mapping[str, Any] | None = None,
) -> Any | None:
    """Legt eine Musikspur unter das gerenderte Reel.

    Bewusst gewählte Musik (``-m``, Homelab-Auswahl, ``music.file``) soll im
    fertigen Reel hörbar sein - vorher steuerte sie nur den Schnitttakt und
    das Ergebnis war stumm. Ist der Originalton aktiv, läuft er gedämpft
    darunter (``video.original_gain``, Default 0,25).

    moviepy 2.x wird hier bewusst über die v2-Namen gebaut: ``moviepy.editor``
    existiert in 2.x nicht mehr, und ``volumex``/``subclip``/``set_audio``/
    ``.fx()`` gibt es dort ebenfalls nicht mehr. Der alte WIP-Stand rief genau
    diese v1-Namen auf und wäre mit ``ModuleNotFoundError`` gescheitert.

    Gibt ``None`` zurück, wenn die Datei fehlt oder moviepy die Spur nicht
    öffnen konnte - der Export läuft dann stumm weiter, statt zu scheitern.
    """
    path = Path(os.path.expanduser(str(music)))
    if not path.is_file():
        log.warning("Musikdatei nicht gefunden, Reel bleibt stumm: %s", path)
        return None

    try:
        from moviepy import AudioFileClip, CompositeAudioClip
        from moviepy.audio.fx import AudioFadeIn, AudioFadeOut, AudioLoop
    except ImportError as exc:  # pragma: no cover
        log.warning("moviepy-Audio nicht verfügbar (%s) - Reel bleibt stumm", exc)
        return None

    music_cfg = get(cfg, "music", {}) or {}
    video_opts = video_cfg if video_cfg is not None else (get(cfg, "video", {}) or {})

    try:
        track = AudioFileClip(str(path))
    except Exception as exc:
        log.warning("Musikdatei %s nicht lesbar (%s) - Reel bleibt stumm", path.name, exc)
        return None

    try:
        target = float(video.duration or 0.0)
        if target <= 0.0:
            return None

        source_duration = float(track.duration or 0.0)
        if source_duration <= 0.0:
            log.warning("Musikdatei %s hat keine Dauer - Reel bleibt stumm", path.name)
            return None

        # Kürzer als das Reel -> in Schleifen bis über die Reel-Länge hinaus;
        # danach auf die exakte Länge zuschneiden. Die alte Logik vervielfachte
        # statt zu loopen, was den Track an jeder Grenze abrupt abschnitt.
        if source_duration < target:
            loops = int(math.ceil(target / source_duration))
            track = track.with_effects([AudioLoop(n_loops=loops)])
        if float(track.duration or 0.0) > target:
            track = track.subclipped(0.0, target)

        music_gain = float(music_cfg.get("gain", 1.0))
        if abs(music_gain - 1.0) > 1e-6:
            track = track.with_volume_scaled(music_gain)

        parts: list[Any] = []
        original = getattr(video, "audio", None)
        original_gain = float(video_opts.get("original_gain", 0.25))
        if original is not None:
            # Originalton bleibt hörbar (Atmosphäre, Sprache), verschwindet aber
            # nicht hinter der Musik.
            if abs(original_gain - 1.0) > 1e-6:
                original = original.with_volume_scaled(original_gain)
            parts.append(original)
        parts.append(track)

        fade_in = float(music_cfg.get("fade_in", 0.0))
        fade_out = float(music_cfg.get("fade_out", 1.5))

        # Kanalzahl und Abtastrate müssen vor dem Addieren zusammenpassen, sonst
        # bricht CompositeAudioClip mit einem Broadcast-Fehler ab und der Export
        # bliebe stumm (siehe _match_audio_channels).
        channels = max(int(getattr(p, "nchannels", 1) or 1) for p in parts)
        rate = max(int(getattr(p, "fps", 0) or 0) for p in parts)
        if channels > 1:
            parts = [_match_audio_channels(p, channels) for p in parts]
        if rate > 0:
            parts = [p.with_fps(rate) for p in parts]

        mixed = track if len(parts) == 1 else CompositeAudioClip(parts)
        if fade_in > 0.0:
            mixed = mixed.with_effects([AudioFadeIn(fade_in)])
        if fade_out > 0.0 and target > fade_out:
            mixed = mixed.with_effects([AudioFadeOut(fade_out)])

        log.info(
            "Musik %r gemischt (Originalton bei %.0f%%, Musik bei %.0f%%, "
            "%d Kanal/%d Hz, Ausblende %.1fs)",
            path.name,
            original_gain * 100.0 if original is not None else 0.0,
            music_gain * 100.0,
            channels,
            rate,
            fade_out,
        )
        result = video.with_audio(mixed.with_duration(target))
        # Der Musik-Reader gehört zum Export-Cleanup, sonst bleibt ein
        # ffmpeg-Prozess bis zum nächsten GC hängen - dasselbe Muster wie
        # ``tenor_sources`` im Rendern.
        result.tenor_music_reader = track  # type: ignore[attr-defined]
        return result
    except Exception as exc:
        log.warning("Musik konnte nicht gemischt werden (%s) - Reel bleibt stumm", exc)
        return None


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
#: Audio-Endungen, die als Beat-Quelle taugen. librosa liest sie über
#: soundfile/audioread; alles, was ffmpeg dekodieren kann, funktioniert auch.
MUSIC_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus")


def _explicit_music_path(hint: str, cfg: Mapping[str, Any]) -> Path | None:
    """Ein ``-m``-Hinweis auf eine echte Datei abbilden.

    ``auto`` ist kein Dateiname, sondern der Wunsch "nimm ``music.file`` aus der
    Config" - ohne diesen Zweig suchte der Beat-Auflöser vergeblich nach einer
    Datei namens ``auto`` und fiel auf 120 BPM zurück, während der Export
    durchaus die konfigurierte Musik gemischt hätte.

    Gibt ``None`` zurück, wenn der Hinweis auf nichts Brauchbares zeigt; das
    Loggen übernehmen die Aufrufer, weil beide Stufen unterschiedlich
    gewichten (Beat-Quelle vs. Ton im Reel).
    """
    if hint == "auto":
        music_cfg = get(cfg, "music") or {}
        configured = str(music_cfg.get("file") or "").strip()
        if not configured:
            return None
        return expand_path(configured)
    return expand_path(hint)


def _pick_music(music: str | Path | None, cfg: Mapping[str, Any]) -> Path | None:
    """Bestimmt die lokale Audiodatei für das Beat-Matching.

    Reihenfolge:

    1. expliziter Pfad (``-m song.mp3``, ``-m auto`` -> ``music.file`` aus der
       Config)
    2. ``spotify.track_file`` - Datei, die zum konfigurierten Track gehört,
       falls Spotify den Track nicht selbst analysieren darf
    3. die *neueste* Audiodatei in ``music_dir``

    Spotify liefert seit 27.11.2024 für neue Apps keine Beat-Zeitpunkte mehr
    (HTTP 403, s. :mod:`src.spotify_beats`). Damit die Auswahl dort nicht
    wirkungslos bleibt, kann zu ``spotify.track_uri`` eine lokale Datei
    zugeordnet werden. Ist beides gesetzt, gewinnt die lokal zugeordnete Datei.
    """
    if music:
        hint = str(music)
        path = _explicit_music_path(hint, cfg)
        if path is not None and path.is_file():
            return path
        if hint == "auto":
            log.warning(
                "music.file ist leer oder zeigt auf keine Datei - kein Beat-Matching "
                "aus der Konfiguration"
            )
        else:
            log.warning("Musikdatei nicht gefunden: %s", hint)
        return None

    # 1. Datei, die zum ausgewählten Spotify-Track gehört.
    spotify_cfg = get(cfg, "spotify", {}) or {}
    if spotify_cfg.get("enabled"):
        pinned = str(spotify_cfg.get("track_file") or "").strip()
        if pinned:
            path = expand_path(pinned)
            if path.is_file():
                return path
            log.warning(
                "spotify.track_file %s nicht gefunden - Fallback auf music_dir", path
            )

    music_dir = get(cfg, "music_dir")
    if not music_dir:
        return None
    directory = expand_path(music_dir)
    if not directory.is_dir():
        return None
    wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in MUSIC_EXTENSIONS}
    candidates = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in wanted),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _fetch_spotify_beats(
    track_uri: str, cfg: Mapping[str, Any]
) -> tuple[list[float] | None, str]:
    """Beat-Timestamps von Spotify holen und in Cut-Längen übersetzen.

    Gibt ``(Cut-Längen | None, Grund)`` zurück. Der Grund ist leer, wenn die
    Beats wirklich ankamen, und sonst die Ursache - so wird im ``--json``
    sichtbar, *warum* der gewählte Track nicht geschnitten wurde (typisch:
    HTTP 403, s. :mod:`src.spotify_beats`).

    Importiert ``spotify_beats`` erst hier, damit die Pipeline auch ohne das
    optionale Modul startet.
    """
    spotify_cfg = get(cfg, "spotify", {}) or {}
    if not spotify_cfg.get("enabled"):
        return None, "spotify.enabled ist false"
    try:
        from .spotify_beats import SpotifyAccessError, fetch_spotify_beats
    except ImportError as exc:  # pragma: no cover
        log.warning("Spotify-Modul nicht verfügbar (%s)", exc)
        return None, f"Spotify-Modul nicht verfügbar ({exc})"
    try:
        beats = fetch_spotify_beats(track_uri)
    except SpotifyAccessError as exc:
        # Kein Konfigurationsfehler, sondern eine API-Sperre: einmal deutlich
        # benennen und dann auf die lokale Datei bzw. den BPM-Fallback
        # zurückfallen, sonst wiederholt sich die Meldung bei jedem Batch.
        log.warning("Spotify-Beat-Detection gesperrt: %s", exc)
        return None, "Spotify blockiert die Audio-Analysis für diese App (HTTP 403)"
    except Exception as exc:  # pragma: no cover
        log.warning("Spotify Beat-Detection fehlgeschlagen: %s", exc)
        return None, f"Spotify-Fehler: {exc}"
    if not beats or len(beats) < 3:
        return None, "Spotify lieferte keine verwertbaren Beats"

    # Gleiche Gruppierung wie beim lokalen Beat-Tracking: Beats auf eine
    # Zylinderlänge von ~target gruppieren, damit Cuts auf Downbeats landen.
    cut_cfg = get(cfg, "cut", {}) or {}
    t_min = float(cut_cfg.get("target_min", 1.4))
    t_max = float(cut_cfg.get("target_max", 2.0))
    target = min(max(float(cut_cfg.get("target", (t_min + t_max) / 2)), t_min), t_max)

    intervals = [b - a for a, b in zip(beats, beats[1:]) if b - a > 0.05]
    if not intervals:
        return None, "Spotify-Beats zu unregelmäßig"
    median_beat = float(sorted(intervals)[len(intervals) // 2])
    group = max(1, int(round(median_beat / target)))
    durations: list[float] = []
    for start, end in zip(beats[::group], beats[::group][1:]):
        span = end - start
        if span > 0:
            durations.append(min(max(span, t_min), t_max))
    log.info(
        "Spotify-Grid: Beat %.2fs -> Gruppe %d (Cut ~%.2fs)",
        median_beat,
        group,
        median_beat * group,
    )
    return (durations or None), ""


def _get_fallback_bpm(cfg: Mapping[str, Any], music_hint: str | Path | None) -> float:
    """Ermittelt den BPM-Wert für den Takt-Fallback.

    Reihenfolge: expliziter ``cut.fallback_bpm`` -> Treffer im ``fallback_bpm``-Map
    über den Musik-Hinweis -> generisches ``pop``/``default`` aus der Map ->
    ``spotify.default_bpm`` -> 120. Ohne den Sentinel-Schritt wäre das Ergebnis
    von der Reihenfolge der YAML-Keys abhängig.
    """
    cut_cfg = get(cfg, "cut", {}) or {}
    spotify_cfg = get(cfg, "spotify", {}) or {}
    fallback_map = dict(spotify_cfg.get("fallback_bpm", {}) or {})

    # 1. Hart im cut-Block konfiguriert - gewinnt immer.
    if "fallback_bpm" in cut_cfg:
        return _safe_bpm(cut_cfg.get("fallback_bpm"), 120.0)

    def _lookup(name: str) -> float | None:
        for key, bpm in fallback_map.items():
            if str(key).strip().lower() == name:
                try:
                    return _safe_bpm(bpm, None)
                except (TypeError, ValueError):
                    log.warning("Ungültiger BPM-Wert für %r in spotify.fallback_bpm", key)
                    return None
        return None

    # 2. Substring-Treffer über den Hinweis (Dateiname/URI).
    #    Der "default"-Sentinel wird hier übersprungen - er gehört in Schritt 3.
    if music_hint:
        hint = str(music_hint).lower()
        # Spezifischste zuerst: längster passender Schlüssel gewinnt, damit
        # "pop" nicht den Eintrag "pop remix" verdeckt.
        matches: list[tuple[int, float]] = []
        for key, value in fallback_map.items():
            name = str(key).strip().lower()
            if name in ("", "default", "*") or name not in hint:
                continue
            try:
                matches.append((len(name), _safe_bpm(value, None)))
            except (TypeError, ValueError):
                log.warning("Ungültiger BPM-Wert für %r in spotify.fallback_bpm", key)
        if matches:
            return max(matches, key=lambda item: item[0])[1]

    # 3. Expliziter Default in der Map.
    for sentinel in ("default", "*", "pop"):
        found = _lookup(sentinel)
        if found:
            return found

    # 4. spotify.default_bpm, sonst 120 BPM.
    return _safe_bpm(spotify_cfg.get("default_bpm"), 120.0)


def _safe_bpm(value: Any, fallback: float | None) -> float:
    """BPM-Wert robust lesen; ``0``, ``None`` und Müll landen im Fallback."""
    try:
        bpm = float(value)
    except (TypeError, ValueError):
        if fallback is None:
            raise
        return fallback
    if not math.isfinite(bpm) or bpm <= 0.0:
        if fallback is None:
            raise ValueError(f"ungültiger BPM-Wert: {value!r}")
        return fallback
    return bpm


def _resolve_beats(
    music: str | Path | None,
    cfg: Mapping[str, Any],
    cut_cfg: Mapping[str, Any],
) -> tuple[list[float], str, str]:
    """Wählt das Beat-Grid: Spotify -> lokale Datei -> BPM-Fallback.

    Gibt ``(Cut-Längen, Quelle, Grund)`` zurück. Der Grund ist leer, wenn die
    bevorzugte Quelle direkt funktioniert hat, und beschreibt sonst jede
    Station des Abstiegs. Damit ist im ``--json`` und im Log sichtbar, warum
    etwa ein konfigurierter Spotify-Track nicht geschnitten wurde - vorher
    fiel die Pipeline in genau diesem Fall still auf 120 BPM zurück.
    """
    notes: list[str] = []
    durations: list[float] = []
    beat_source = ""

    explicit_uri = isinstance(music, str) and music.startswith("spotify:")
    explicit_file = bool(music) and not explicit_uri

    spotify_cfg = get(cfg, "spotify", {}) or {}
    track_uri = ""
    if explicit_uri:
        track_uri = str(music)
    elif not music and spotify_cfg.get("enabled"):
        track_uri = str(spotify_cfg.get("track_uri") or "")

    # 1. Spotify - auch wenn der Endpoint für neue Apps gesperrt ist, damit
    #    der Grund im Report landet statt still im BPM-Fallback zu verschwinden.
    if track_uri:
        found, reason = _fetch_spotify_beats(track_uri, cfg)
        durations = list(found or [])
        if durations:
            beat_source = f"spotify:{track_uri}"
        elif reason:
            notes.append(reason)

    def _try_file(path: Path | None, label: str) -> None:
        nonlocal durations, beat_source
        if path is None:
            notes.append(f"{label} nicht gefunden")
            return
        found = detect_beat_durations(path, cut_cfg)
        if found:
            durations = found
            beat_source = f"beats:{path.name}"
            return
        notes.append(f"{label} {path.name!r} lieferte keine verwertbaren Beats")

    # 2. Explizite Datei von der CLI.
    if not durations and explicit_file:
        _try_file(_pick_music(music, cfg), f"Musikdatei {str(music)!r}")

    # 3. `spotify.track_file` (Audio des gewählten Tracks) bzw. die neueste
    #    Datei im music_dir. Greift nicht bei expliziter CLI-Vorgabe.
    if not durations and not music:
        track_file_cfg = str(spotify_cfg.get("track_file") or "").strip()
        path = _pick_music(None, cfg)
        if track_file_cfg and path is not None:
            configured = expand_path(track_file_cfg)
            # Sagt der Report nicht, dass die Wunschdatei ersetzt wurde, sieht
            # man nur den Schnitt-Takt und sucht den Fehler in der falschen Ecke.
            if path != configured:
                notes.append(
                    f"spotify.track_file {configured.name!r} nicht nutzbar - "
                    f"verwende {path.name!r} aus music_dir"
                )
        _try_file(path, "spotify.track_file" if track_file_cfg else "Audiodatei")

    # 4. Reiner BPM-Takt. Wichtig: das Ergebnis muss als durations-Liste
    #    weitergegeben werden - der Log-Text allein steuert nichts.
    if not durations:
        fallback_bpm = _get_fallback_bpm(cfg, music)
        durations = durations_from_bpm(fallback_bpm, cut_cfg)
        beat_source = f"fallback:{fallback_bpm:g}bpm"
        notes.append(
            "keine echten Beats verfügbar - Takt-Fallback "
            f"{fallback_bpm:g} BPM (Beat {60.0 / fallback_bpm if fallback_bpm else 0.0:.2f}s, "
            f"Cut {durations[0] if durations else 0.0:.2f}s)"
        )

    return durations, beat_source, "; ".join(notes)


def _resolve_music_file(music: str | Path | None, cfg: Mapping[str, Any]) -> Path | None:
    """Lokale Audiodatei, die als *Ton* ins Reel gemischt werden soll.

    Bewusst gewählt heißt: ``-m song.mp3`` (bzw. die Homelab-Auswahl) oder
    ``-m auto``, das auf ``music.file`` aus der Config zeigt. Die automatisch
    ermittelte Quelle (``spotify.track_file``, neueste Datei im ``music_dir``)
    wird hier bewusst *nicht* zurückgegeben: ein Lauf ohne ``-m`` hat niemanden
    nach Musik gefragt, da soll es still bleiben.

    Eine Spotify-URI ist kein Ton - nur ihre Beat-Liste wird genutzt. Für Ton
    muss eine lokale Datei dazukommen (``-m spotify:track:…`` reicht nicht).
    """
    if not music:
        return None
    hint = str(music)
    if hint.startswith("spotify:"):
        return None

    path = _explicit_music_path(hint, cfg)
    if path is None:
        return None
    if not path.is_file():
        if hint == "auto":
            log.warning("music.file nicht gefunden: %s", path)
        return None
    return path


def _apply_audio_override(cfg: dict[str, Any], audio: bool | None) -> None:
    """Setzt ``video.audio`` aus einem expliziten CLI-Wunsch.

    ``None`` lässt die Config unangetastet, ``True``/``False`` schlägt sie. Ohne
    diesen Schritt bliebe ``--audio`` auf einem Server mit ``video.audio: false``
    wirkungslos - genau der Fall, in dem man den Ton zuschalten will.
    """
    if audio is None:
        return
    video = cfg.setdefault("video", {})
    if not isinstance(video, dict):
        video = dict(video)
        cfg["video"] = video
    video["audio"] = bool(audio)


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
    duration: float | None = None,
    grade: str | None = None,
    pace: str | None = None,
    lossless: bool = False,
    audio: bool | None = None,
) -> RenderResult:
    """Kompletter Pipeline-Lauf: Metadaten -> Schnitt -> Render -> Export.

    ``audio`` überschreibt ``video.audio``: ``True`` nimmt den Originalton der
    Rohclips mit, ``False`` exportiert stumm, ``None`` folgt der Config. Die
    Musik wird in keinem Fall untergemischt.
    """
    cfg = load_config(config_path)
    # Overrides explizit durchreichen, damit der Watcher nicht darauf angewiesen
    # ist, dass die Datei auf der Platte schon die richtigen Werte enthält.
    if output_dir is not None:
        cfg["output_dir"] = str(output_dir)
    if lossless:
        cfg.setdefault("video", {})
        cfg["video"]["lossless"] = True
    if no_geocoding:
        cfg.setdefault("geocoding", {})
        cfg["geocoding"] = {**cfg["geocoding"], "enabled": False}
    if duration is not None:
        cfg.setdefault("video", {})
        cfg["video"]["max_duration"] = float(duration)
    _apply_audio_override(cfg, audio)
    if grade is not None:
        cfg.setdefault("style", {})
        cfg["style"]["preset"] = str(grade).lower()

    video_cfg = get(cfg, "video", {}) or {}
    cut_cfg = dict(get(cfg, "cut", {}) or {})

    # Schnitt-Tempo / Pacing anpassen
    effective_pace = pace
    if not effective_pace:
        max_dur = float(get(video_cfg, "max_duration", 0) or 0)
        if 0 < max_dur <= 16:
            effective_pace = "fast"
        elif max_dur > 0:
            effective_pace = "medium"

    if effective_pace == "fast":
        cut_cfg["target"] = 1.6
        cut_cfg["target_min"] = 1.3
        cut_cfg["target_max"] = 2.0
    elif effective_pace == "medium":
        cut_cfg["target"] = 2.3
        cut_cfg["target_min"] = 1.7
        cut_cfg["target_max"] = 2.9
    elif effective_pace == "slow":
        cut_cfg["target"] = 3.2
        cut_cfg["target_min"] = 2.4
        cut_cfg["target_max"] = 4.0

    result = RenderResult()
    if not sources:
        sources = [str(expand_path(get(cfg, "ingest_dir", ".")))]

    geocoder = Geocoder(cfg)

    clips = collect_clips(
        sources,
        geocoder=geocoder,
        recursive=bool(get(cfg, "watcher.recursive", False)),
        ignore_dirs=list(get(cfg, "watcher.ignore_dirs", [])),
        extensions=list(get(cfg, "extensions", [".mov", ".mp4"])),
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

    # Musikquelle auflösen: Spotify, lokale Datei oder BPM-Fallback.
    durations, beat_source, beat_note = _resolve_beats(music, cfg, cut_cfg)
    result.beat_source = beat_source
    result.beat_note = beat_note
    if beat_note:
        log.info("Beat-Quelle: %s (%s)", beat_source, beat_note)

    # Für den *Ton* zählt nur bewusst Gewähltes. Ohne diesen zweiten Schritt
    # steuerte die Musik nur den Schnitt und das fertige Reel blieb stumm,
    # obwohl im Homelab ein Track ausgewählt war.
    music_file = _resolve_music_file(music, cfg)
    if music_file is not None:
        log.info("Musik für den Export: %s", music_file)
    elif music and not str(music).startswith("spotify:"):
        log.info("Musik %r liefert nur den Schnitttakt (kein Ton im Reel)", music)

    # Qualitätsanalyse einmalig über alle nutzbaren Clips. Das Ergebnis liegt
    # auf Platte (Key aus Pfad/mtime/Größe), wiederholte Läufe kosten also nur
    # noch einen Blick in die Datei.
    quality_cfg = QualityConfig.from_config(get(cfg, "quality", {}))
    analyses = analyse_clips(usable, quality_cfg, cache_dir=expand_path(quality_cfg.cache_dir))

    segments = build_segments(
        usable,
        cut_cfg,
        video_cfg,
        durations=durations,
        min_source_duration=min_source,
        quality_cfg=quality_cfg,
        analyses=analyses,
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
    if music_file is not None:
        if bool(get(video_cfg, "audio", False)):
            log.info("Audio: Musik wird gemischt, Originalton läuft gedämpft darunter")
        else:
            log.info("Audio: Musik wird ins Reel gemischt (kein Originalton)")
    elif bool(get(video_cfg, "audio", False)):
        log.info("Audio: Originalton der Rohclips wird mitgenommen")
    else:
        log.info("Audio: stumm (video.audio=false, keine Musik gewählt)")

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

    grade_obj = GradeConfig.from_config(cfg, preset=grade)
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
                quality_cfg=quality_cfg,
                analyses=analyses,
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
            grade=grade_obj,
            when=now,
            dry_run=dry_run,
            no_cover=no_cover,
            errors_seen=result.errors,
            music=music_file,
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
    music: str | Path | None = None,
    errors_seen: list[str] | None = None,
) -> Path | None:
    """Rendert und exportiert einen Satz von Cuts als ein Reel.

    Gibt den fertigen Pfad zurück, oder ``None`` wenn nichts exportiert wurde.
    ``errors_seen`` sammelt Cut-Fehler für den maschinenlesbaren Report.
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
    # Staging-Datei trägt dieselbe Endung wie das Ziel, sonst räumt
    # cleanup_partials() sie nicht mehr auf.
    partial = target.parent / f"{target.stem}.part{suffix}"
    partial.unlink(missing_ok=True)

    try:
        overlay = make_overlay_array(cfg, title, subtitle)
    except Exception as exc:
        log.error("Text-Overlay konnte nicht gerendert werden (%s) - exportiere ohne Text", exc)
        overlay = None

    errors: list[str] = []
    # Text nur auf den ersten N Cuts. 0 = auf allen (Default 2 = Hook only).
    text_on_first_cuts = int(get(cfg, "text_on_first_cuts", 2) or 0)
    if text_on_first_cuts > 0 and len(segments) > text_on_first_cuts:
        log.info(
            "Text-Overlay nur auf den ersten %d von %d Cuts", text_on_first_cuts, len(segments)
        )
    want_audio = bool(video_cfg.get("audio", False))
    rendered = render_segments(
        segments,
        cfg,
        grade=grade,
        overlay_png=overlay,
        text_on_first_cuts=text_on_first_cuts,
        with_audio=want_audio,
        on_error=errors,
    )
    for message in errors:
        log.error("%s", message)

    if not rendered:
        log.error("Kein Cut konnte gerendert werden - Abbruch ohne Export")
        errors_seen.extend(errors)
        return None

    errors_seen.extend(errors)
    final = rendered[0]
    music_mixed = False
    if music is not None:
        mixed = mix_music(final, music, cfg, video_cfg)
        if mixed is not None:
            final = mixed
            music_mixed = True

    def _attempt_export(is_lossless: bool) -> None:
        ffmpeg_params = ["-pix_fmt", str(video_cfg.get("pixel_format", "yuv420p"))]
        # Eine gemischte Musikspur ist Ton im fertigen Reel und braucht deshalb
        # dieselben Codec-Parameter wie der Originalton - sonst schrieb moviepy
        # stumm bzw. mit libmp3lame und Instagram hätte nichts abzuspielen.
        with_track = bool(want_audio) or bool(getattr(final, "audio", None) is not None)
        crf = video_cfg.get("crf")
        preset = str(video_cfg.get("preset", "medium"))

        if is_lossless:
            crf = 0
            preset = "ultrafast"

        if crf is not None:
            ffmpeg_params += ["-crf", str(crf)]
        if bool(video_cfg.get("faststart", True)):
            ffmpeg_params += ["-movflags", "+faststart"]
        threads = int(video_cfg.get("threads", 0) or 0)
        if threads:
            ffmpeg_params += ["-threads", str(threads)]

        write_kwargs: dict[str, Any] = {
            "fps": int(video_cfg.get("fps", 30)),
            "codec": str(video_cfg.get("codec", "libx264")),
            # `video.audio` entscheidet über den *Originalton*; eine gemischte
            # Musikspur macht das Reel aber unabhängig davon hörbar.
            "audio": bool(video_cfg.get("audio", False)) or with_track,
            "preset": preset,
            "threads": threads or None,
            "ffmpeg_params": ffmpeg_params,
            "logger": None,
        }
        if write_kwargs["audio"]:
            # Nur wenn wirklich eine Spur mitkommt: sonst würde moviepy eine
            # leere Audiodatei erzeugen und -copy in den Container muxen.
            # Instagram/WhatsApp akzeptieren nur AAC, nicht das moviepy-Default
            # libmp3lame.
            write_kwargs["audio_codec"] = str(video_cfg.get("audio_codec", "aac"))
            write_kwargs["audio_bitrate"] = str(video_cfg.get("audio_bitrate", "192k"))
            write_kwargs["audio_fps"] = int(video_cfg.get("audio_sample_rate", 44100))
            write_kwargs["audio_nbytes"] = int(video_cfg.get("audio_nbytes", 2))

        final.write_videofile(str(partial), **write_kwargs)

    try:
        _attempt_export(bool(video_cfg.get("lossless")))
    except Exception as exc:
        partial.unlink(missing_ok=True)
        if bool(video_cfg.get("lossless")):
            log.warning("Lossless-Export fehlgeschlagen, versuche Standard-Export: %s", exc)
            try:
                _attempt_export(False)
            except Exception as exc2:
                log.exception("Export nach %s fehlgeschlagen: %s", partial, exc2)
                partial.unlink(missing_ok=True)
                if errors_seen is not None:
                    errors_seen.append(f"Export fehlgeschlagen: {exc2}")
                return None
        else:
            log.exception("Export nach %s fehlgeschlagen: %s", partial, exc)
            if errors_seen is not None:
                errors_seen.append(f"Export fehlgeschlagen: {exc}")
            return None
    except BaseException:
        # SIGTERM/Stromausfall: unfertige Datei nicht liegen lassen
        partial.unlink(missing_ok=True)
        raise
    finally:
        _close_all([final])
        _close_all(getattr(final, "tenor_sources", []) or [])
        _close_all([getattr(final, "tenor_music_reader", None)])

    partial.replace(target)
    # Tatsächliche Dauer aus der fertigen Datei, nicht aus dem Plan: es
    # können Cuts ausgefallen sein.
    actual = probe_duration(target) or sum(s.duration for s in segments)
    log.info("Export fertig: %s (%.1fs Video)", target, actual)

    if (want_audio or music_mixed) and not has_audio_stream(target):
        if music_mixed:
            # Genau der Fall, den der Nutzer gemeldet hat: Musik war gewählt,
            # das Ergebnis war trotzdem stumm.
            log.error(
                "%s enthält trotz gemischter Musik keine Tonspur - der Ton "
                "wurde nicht geschrieben",
                target.name,
            )
        else:
            log.warning(
                "%s enthält trotz video.audio=true keine Tonspur - vermutlich "
                "hatten die Quellen keinen Ton",
                target.name,
            )

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
    parser.add_argument(
        "-m",
        "--music",
        help=(
            "Musikdatei: liefert den Takt UND landet im Reel. "
            "'auto' nimmt music.file aus der Config. Ohne -m bleibt das Reel "
            "stumm und die Musikquelle steuert nur den Schnitt."
        ),
    )
    parser.add_argument("-c", "--config", help="Pfad zur config.yaml")
    parser.add_argument("--no-cover", action="store_true", help="kein Cover-Thumbnail erzeugen")
    parser.add_argument("--no-geo", action="store_true", help="Reverse-Geocoding überspringen")
    parser.add_argument("--limit", type=int, default=0, help="max. Anzahl Eingabedateien (0 = alle)")
    parser.add_argument(
        "--duration",
        "--max-duration",
        type=float,
        default=None,
        dest="duration",
        help="Ziel-/Maximallänge des Reels in Sekunden (z. B. 15, 30, 45, 60; 0 = alle Clips)",
    )
    parser.add_argument(
        "--grade",
        choices=["natural", "original", "cinematic"],
        default=None,
        help="Farbfilter-Grading (natural = dezent/natürlich, original = kein Filter, cinematic = Dark-Film)",
    )
    parser.add_argument(
        "--pace",
        choices=["fast", "medium", "slow"],
        default=None,
        help="Schnitt-Tempo (fast = ~1.5s, medium = ~2.3s, slow = ~3.2s)",
    )
    audio_group = parser.add_mutually_exclusive_group()
    audio_group.add_argument(
        "--audio",
        dest="audio",
        action="store_true",
        default=None,
        help="Originalton der Rohclips ins Reel übernehmen (überschreibt video.audio)",
    )
    audio_group.add_argument(
        "--no-audio",
        dest="audio",
        action="store_false",
        default=None,
        help="stumm exportieren, auch wenn video.audio: true gesetzt ist",
    )
    parser.add_argument("--dry-run", action="store_true", help="nur Schnittplan ausgeben, kein Export")
    parser.add_argument("--delete-after", action="store_true", help="Rohclips nach Export löschen")
    parser.add_argument("--lossless", action="store_true", help="verlustfrei exportieren (CRF 0, ultrafast)")
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
        duration=args.duration,
        grade=args.grade,
        pace=args.pace,
        lossless=args.lossless,
        audio=args.audio,
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
                    "beat_note": result.beat_note,
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
