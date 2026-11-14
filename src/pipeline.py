"""Schnitt, Beat-Matching, Dark-Film-Rendering und Export.

CLI-Beispiel::

    python -m src.pipeline -i ~/video-cut/ingest -o ~/video-cut/output -t "Madeira 2026"
    python -m src.pipeline -i clip.MOV -o out/ -t "Madeira" -m song.mp3
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shlex
import signal
import statistics
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
    find_highlights,
    plan_edge,
    plan_windows,
    spread_windows,
)
from .styling import (
    GradeConfig,
    build_subtitle,
    build_text_overlay,
    center_crop_resize,
    fit_clip_resize,
    format_coordinates,
    make_grader,
    resolve_fit_mode,
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


def _record_error(errors: list[str], message: str, *args: Any) -> str:
    """Formatiert eine Fehlermeldung, loggt sie und speichert sie in ``errors``."""
    formatted = message % args if args else message
    log.error("%s", formatted)
    errors.append(formatted)
    return formatted


# --------------------------------------------------------------------------- #
# Beat-Matching
# --------------------------------------------------------------------------- #


def _cleanup_ffmpeg_children() -> None:
    """Findet und killt verwaiste ffmpeg-Kindprozesse, um Zombies zu verhindern."""
    mypid = os.getpid()
    try:
        proc = subprocess.Popen(
            ["ps", "-o", "pid,ppid,command", "--no-headers"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        out, _ = proc.communicate()
        candidates = []
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) >= 3:
                pid_str, ppid_str, cmd = parts
                if int(ppid_str) == mypid and "ffmpeg" in cmd.lower():
                    candidates.append(int(pid_str))

        for pid in candidates:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

        if candidates:
            import time

            time.sleep(0.1)
            proc2 = subprocess.Popen(
                ["ps", "-o", "pid,ppid,command", "--no-headers"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            out2, _ = proc2.communicate()
            for line in out2.decode("utf-8", "replace").splitlines():
                parts = line.strip().split(None, 2)
                if len(parts) >= 3:
                    pid_str, ppid_str, cmd = parts
                    pid = int(pid_str)
                    if pid in candidates and int(ppid_str) == mypid and "ffmpeg" in cmd.lower():
                        try:
                            os.kill(pid, signal.SIGKILL)
                            os.waitpid(pid, os.WNOHANG)
                        except OSError:
                            pass
    except Exception:
        pass


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
    beats = sorted({round(b, 4) for b in beats if b >= 0.0})
    if len(beats) < 3:
        log.warning("Zu wenige Beats in %s (%d) - Fallback greift", music.name, len(beats))
        return []

    intervals = [b - a for a, b in zip(beats, beats[1:], strict=False) if b - a > 0.05]
    if not intervals:
        return []
    median_beat = float(statistics.median(intervals))
    if median_beat <= 0:
        return []
    group = max(1, int(round(target / median_beat)))
    cut_len = median_beat * group
    log.info(
        "Beat-Grid: Tempo ~%.0f BPM, Beat-Abstand %.2fs -> Gruppe %d (Cut ~%.2fs)",
        60.0 / median_beat if median_beat else 0.0,
        median_beat,
        group,
        cut_len,
    )

    grid = beats[::group]
    durations: list[float] = []
    for start, end in zip(grid, grid[1:], strict=False):
        span = end - start
        if span <= 0:
            continue
        durations.append(min(max(span, t_min), t_max))
    if not durations:
        durations = [min(max(cut_len, t_min), t_max)]
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

    valid_clips = [c for c in clips if c.duration > 0.0 and c.duration >= max(min_source_duration, t_min)]

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
        for start, length in zip(starts, wanted, strict=False):
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
            segments.append(Segment(clip=clip, start=round(plan.start, 3), end=round(plan.end, 3)))
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
            f" ({100.0 * used_in_clip / planned_seconds:.0f}% der Planung)" if planned_seconds > 0 else "",
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
def is_valid_video(path: Path) -> bool:
    """Prüft mit ffprobe, ob eine Videodatei lesbar und nicht korrupt ist."""
    from .metadata import FFPROBE, FFPROBE_TIMEOUT, RAW_VIDEO_SUFFIXES

    try:
        cmd = [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
        ]
        if path.suffix.lower() in RAW_VIDEO_SUFFIXES:
            cmd.extend(["-f", "hevc"])
        cmd.extend(["--", str(path)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=FFPROBE_TIMEOUT,
                check=True,
            )
        except BaseException:
            _cleanup_ffmpeg_children()
            raise
        return "video" in result.stdout.decode().strip()
    except subprocess.TimeoutExpired:
        return False
    except (subprocess.SubprocessError, OSError, ValueError):
        return False


def has_audio_stream(path: Path) -> bool:
    """Prüft, ob die fertige Datei wirklich eine Tonspur enthält.

    moviepy legt bei ``audio=True`` auch dann eine Datei an, wenn kein Ton
    vorhanden war - der Stream fehlt dann aber. Für den Export-Log ist der
    Unterschied der ganze Punkt: entweder das Reel ist hörbar oder nicht.
    """
    from .metadata import FFPROBE, FFPROBE_TIMEOUT

    try:
        try:
            result = subprocess.run(
                [
                    FFPROBE,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    "--",
                    str(path),
                ],
                capture_output=True,
                timeout=FFPROBE_TIMEOUT,
                check=True,
            )
        except BaseException:
            _cleanup_ffmpeg_children()
            raise
        return result.stdout.decode().strip() == "audio"
    except subprocess.TimeoutExpired:
        return False
    except (subprocess.SubprocessError, OSError, ValueError):
        return False


def probe_duration(path: Path) -> float | None:
    """Dauer einer fertigen Datei in Sekunden, oder ``None``.

    Genauer als der Schnittplan, wenn einzelne Cuts ausgefallen sind.
    """
    from .metadata import FFPROBE, FFPROBE_TIMEOUT

    try:
        try:
            result = subprocess.run(
                [
                    FFPROBE,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nw=1:nk=1",
                    "--",
                    str(path),
                ],
                capture_output=True,
                timeout=FFPROBE_TIMEOUT,
                check=True,
            )
        except BaseException:
            _cleanup_ffmpeg_children()
            raise
        value = float(result.stdout.decode().strip())
        return value if math.isfinite(value) and value > 0 else None
    except subprocess.TimeoutExpired:
        return None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _is_10bit_pix_fmt(pix_fmt: str | None) -> bool:
    """Erkennt gängige 10-Bit-HEVC-Pixelformate, die 8-Bit-Transkodierung brauchen."""
    return bool(pix_fmt and ("yuv420p10le" in pix_fmt or "p010le" in pix_fmt))


def _probe_pix_fmt(path: Path) -> str:
    """Liest das Pixelformat des ersten Videostreams via ffprobe."""
    from .metadata import FFPROBE, FFPROBE_TIMEOUT, RAW_VIDEO_SUFFIXES

    probe_cmd = [
        FFPROBE,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=pix_fmt",
        "-of",
        "csv=p=0",
    ]
    if path.suffix.lower() in RAW_VIDEO_SUFFIXES:
        probe_cmd.extend(["-f", "hevc"])
    probe_cmd.extend(["--", str(path)])
    try:
        try:
            result = subprocess.run(probe_cmd, capture_output=True, timeout=FFPROBE_TIMEOUT, check=True)
        except BaseException:
            _cleanup_ffmpeg_children()
            raise
        return result.stdout.decode().strip()
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"ffprobe timeout für {path}") from exc
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def _transcode_to_8bit(path: Path, *, with_audio: bool) -> Path:
    """Transkodiert einen 10-Bit-HEVC-Clip temporär nach 8-Bit H.264."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    transcode_cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(path),
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "22",
    ]
    if with_audio:
        # Ton nur mitnehmen, wenn er danach wirklich gebraucht wird.
        transcode_cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        transcode_cmd += ["-an"]
    transcode_cmd.append(str(tmp_path))

    try:
        subprocess.run(transcode_cmd, timeout=120, check=True)
    except BaseException:
        _cleanup_ffmpeg_children()
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _unlink_files(paths: Sequence[Path]) -> None:
    """Löscht temporäre Dateien fehlertolerant."""
    for path in paths:
        path.unlink(missing_ok=True)


def _open_source(
    path: Path,
    *,
    with_audio: bool = False,
    prepared_path: Path | None = None,
    pix_fmt: str | None = None,
) -> Any:
    """Öffnet eine Videoquelle; beschädigte Dateien werfen eine lesbare Exception.

    10-Bit-HEVC (yuv420p10le) wird via ffmpeg nach 8-Bit (yuv420p)
    transkodiert, damit moviepy/OpenCV die Frames verarbeiten können.

    ``with_audio`` steuert, ob die Tonspur geöffnet wird. Sie ist per Default
    aus, weil die Reels stumm exportiert werden - der Ton eines Rohclips wird
    nur mit ``video.audio: true`` mitgenommen.
    """
    from moviepy import VideoFileClip

    def _validated(clip: Any) -> Any:
        """Größenprüfung - der Reader ist ab hier schon offen (ffmpeg läuft)."""
        size = clip.size
        if size is None or size[0] < 2 or size[1] < 2:
            raise ValueError(f"ungültige Bildgröße {size}")
        return clip

    if prepared_path is not None:
        clip = VideoFileClip(str(prepared_path), audio=with_audio)
        try:
            return _validated(clip)
        except BaseException:
            with contextlib.suppress(Exception):  # pragma: no cover
                clip.close()
            raise

    resolved_pix_fmt = pix_fmt if pix_fmt is not None else _probe_pix_fmt(path)
    if _is_10bit_pix_fmt(resolved_pix_fmt):
        tmp_path: Path | None = None
        try:
            tmp_path = _transcode_to_8bit(path, with_audio=with_audio)
            clip = VideoFileClip(str(tmp_path), audio=with_audio)
            try:
                _validated(clip)
            except BaseException:
                with contextlib.suppress(Exception):  # pragma: no cover
                    clip.close()
                raise
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as exc:
            # WICHTIG: bei jedem Fehlschlag das Zwischenfile löschen. moviepy
            # räumt nichts auf (Clip.__del__ ist absichtlich ein no-op), sonst
            # wächst /tmp bei jeder kaputten Datei um ein Vielfaches.
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise ValueError(f"ffprobe timeout für {path}") from exc
        except BaseException:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise
        else:
            original_close = clip.close
            scratch = tmp_path

            def close_and_cleanup(_close: Any = original_close, _tmp: Path = scratch) -> None:
                try:
                    _close()
                finally:
                    _tmp.unlink(missing_ok=True)

            clip.close = close_and_cleanup
            return clip

    clip = VideoFileClip(str(path), audio=with_audio)
    try:
        return _validated(clip)
    except BaseException:
        # Reader (und damit der ffmpeg-Kindprozess) ist schon offen.
        with contextlib.suppress(Exception):  # pragma: no cover
            clip.close()
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
    prepared_path: Path | None = None,
    pix_fmt: str | None = None,
) -> Any:
    width &= ~1
    height &= ~1
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

    source = _open_source(
        segment.clip.path,
        with_audio=with_audio,
        prepared_path=prepared_path,
        pix_fmt=pix_fmt,
    )
    if sources is not None:
        sources.append(source)

    source_duration = float(source.duration or 0.0)
    frame = 1.0 / fps

    aligned_start = round(segment.start * fps) / fps
    aligned_end = round(segment.end * fps) / fps

    start = max(min(aligned_start, max(source_duration - 0.05, 0.0)), 0.0)
    # Obergrenze gewinnt: das min()-Clamp darf NICHT von einem
    # start+frame-Floor überstimmt werden, sonst liest subclipped über EOF.
    end = min(aligned_end, source_duration)
    if end <= start:
        raise ValueError(
            f"Cut {aligned_start:.2f}-{aligned_end:.2f}s liegt außerhalb von "
            f"{segment.clip.name} (Dauer {source_duration:.2f}s)"
        )
    end = max(end, min(start + frame, source_duration))
    piece = source.subclipped(start, end)
    piece = piece.with_duration(max(end - start, frame))

    # Ton des Rohclips: derselbe Ausschnitt, unabhängig vom Bild-Pfad. Wird
    # vor dem Crop/Grading abgezweigt, weil die Bild-Transformationen den
    # Audio-Teil des Clips nicht mitnehmen.
    source_audio = getattr(source, "audio", None) if with_audio else None

    fit_mode = resolve_fit_mode(cfg)
    grader = make_grader(grade)
    if fit_mode == "crop":
        piece = center_crop_resize(piece, width, height)
        if grader is not None:
            piece = piece.image_transform(grader)
    else:
        piece = fit_clip_resize(piece, width, height, fit_mode=fit_mode, frame_transform=grader)

    if overlay_png is not None:
        text = ImageClip(overlay_png, duration=piece.duration)
        piece = CompositeVideoClip([piece, text], size=(width, height))

    if source_audio is not None:
        try:
            piece = piece.with_audio(source_audio.subclipped(start, end).with_duration(piece.duration))
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
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from moviepy import concatenate_videoclips

    video_cfg = get(cfg, "video", {}) or {}
    fps = int(video_cfg.get("fps", 30))
    width, height = target_size(cfg)
    width &= ~1
    height &= ~1
    failures = on_error if on_error is not None else []

    sources: list[Any] = []
    sources_lock = threading.Lock()
    failures_lock = threading.Lock()
    cache_meta_lock = threading.Lock()
    key_locks: dict[tuple[Path, bool], threading.Lock] = {}
    prepared_cache: dict[tuple[Path, bool], tuple[Path | None, str]] = {}
    failed_keys: dict[tuple[Path, bool], Exception] = {}
    temp_files: list[Path] = []
    temp_files_lock = threading.Lock()

    def _prepare_source(segment: Segment) -> tuple[Path | None, str]:
        src_path = segment.clip.path
        key = (src_path.resolve(), with_audio)
        with cache_meta_lock:
            lock = key_locks.setdefault(key, threading.Lock())
        with lock:
            if key in failed_keys:
                raise failed_keys[key]
            if key in prepared_cache:
                return prepared_cache[key]

            extra = getattr(segment.clip, "extra", None)
            raw_pix_fmt = extra.get("pix_fmt") if isinstance(extra, Mapping) else None
            try:
                resolved_pix_fmt = str(raw_pix_fmt) if raw_pix_fmt else _probe_pix_fmt(src_path)
                prepared_path: Path | None = None
                if _is_10bit_pix_fmt(resolved_pix_fmt):
                    prepared_path = _transcode_to_8bit(src_path, with_audio=with_audio)
                    with temp_files_lock:
                        temp_files.append(prepared_path)
            except Exception as exc:
                failed_keys[key] = exc
                raise

            entry = (prepared_path, resolved_pix_fmt)
            prepared_cache[key] = entry
            return entry

    rendered_array: list[Any | None] = [None] * len(segments)

    def _render_one(index: int, segment: Segment) -> None:
        local_sources: list[Any] = []
        success = False
        try:
            # Text-Overlay nur auf den ersten N Cuts (Instagram-Reels-Konvention:
            # der Hook trägt die Beschriftung, der Rest bleibt clean).
            segment_overlay = overlay_png
            if text_on_first_cuts > 0 and index > text_on_first_cuts:
                segment_overlay = None

            prepared_path, pix_fmt = _prepare_source(segment)
            clip = build_segment_clip(
                segment,
                cfg,
                grade,
                segment_overlay,
                fps=fps,
                width=width,
                height=height,
                sources=local_sources,
                with_audio=with_audio,
                prepared_path=prepared_path,
                pix_fmt=pix_fmt,
            )

            with sources_lock:
                sources.extend(local_sources)

            rendered_array[index - 1] = clip
            log.info(
                "Cut %d/%d: %s %.2f-%.2fs (%.2fs)",
                index,
                len(segments),
                segment.clip.name,
                segment.start,
                segment.end,
                segment.duration,
            )
            success = True
        except Exception as exc:
            # Nur der fehlgeschlagene Cut wird verworfen. Die bereits
            # gerenderten Cuts bleiben erhalten - sonst hätte das Reel vorn
            # ein Loch und der Schnittplan passte nicht zum Ergebnis.
            message = f"Cut {index} aus {segment.clip.name} fehlgeschlagen: {exc}"
            log.error("%s", message)
            with failures_lock:
                failures.append(message)
        finally:
            if not success:
                _close_all(local_sources)

    try:
        # Use ThreadPoolExecutor for parallel rendering
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as executor:
            futures = []
            for i, seg in enumerate(segments, start=1):
                futures.append(executor.submit(_render_one, i, seg))
            for future in futures:
                future.result()

        rendered = [c for c in rendered_array if c is not None]

        if not rendered:
            _close_all(sources)
            _unlink_files(temp_files)
            return []

        # concatenate_videoclips kann bei gemischten Größen/fehlerhaften
        # n_frames werfen. Ohne den finally-Block bleiben dann ALLE geöffneten
        # Reader (je ein ffmpeg-Prozess) bis zum nächsten GC-Cycle hängen.
        final = concatenate_videoclips(rendered, method="chain")
        if not with_audio:
            # Ohne Tonwunsch wird die Spur abgeworfen, damit der Export nicht
            # versehentlich eine mitgeschleppte Tonspur enthält.
            with contextlib.suppress(Exception):  # pragma: no cover
                final = final.without_audio()
        elif getattr(final, "audio", None) is None:
            log.warning(
                "Kein einziger Cut hatte eine Tonspur - der Export bleibt stumm (rohvideo.audio_codec=%s)",
                get(cfg, "video.audio_codec", "aac"),
            )
        # Quell-Reader leben bis nach write_videofile weiter
        final.tenor_sources = sources  # type: ignore[attr-defined]
        final.tenor_temp_files = temp_files  # type: ignore[attr-defined]
        if temp_files:
            original_close = final.close

            def close_and_cleanup(
                _close: Any = original_close,
                _sources: list[Any] = sources,
                _temps: list[Path] = temp_files,
            ) -> None:
                try:
                    _close()
                finally:
                    _close_all(_sources)
                    _unlink_files(_temps)

            final.close = close_and_cleanup  # type: ignore[method-assign]
        return [final]
    except BaseException:
        _close_all(sources)
        _close_all([c for c in rendered_array if c is not None])
        _unlink_files(temp_files)
        raise


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
        with contextlib.suppress(Exception):  # pragma: no cover
            clip.close()


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

    success = False
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
            "Musik %r gemischt (Originalton bei %.0f%%, Musik bei %.0f%%, %d Kanal/%d Hz, Ausblende %.1fs)",
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
        success = True
        return result
    except Exception as exc:
        log.warning("Musik konnte nicht gemischt werden (%s) - Reel bleibt stumm", exc)
        return None
    finally:
        if not success:
            _close_all([track])


def make_overlay_array(cfg: Mapping[str, Any], title: str, subtitle: str, geo_tag: str = "") -> np.ndarray:
    """Text-Overlay als RGBA-Numpy-Array für ``ImageClip``."""
    width, height = target_size(cfg)
    width &= ~1
    height &= ~1
    image = build_text_overlay(
        cfg, title=title, subtitle=subtitle, geo_tag=geo_tag, width=width, height=height
    )
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
    width &= ~1
    height &= ~1

    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{max(at, 0.0):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        "-q:v",
        "2",
        str(cover),
    ]
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        except BaseException:
            _cleanup_ffmpeg_children()
            raise
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
        raw_name = template.format(**values)
    except KeyError as exc:
        log.warning("Unbekannter Platzhalter %s in naming.template - Fallback", exc)
        raw_name = f"{values['date']}_{values['slug']}_{values['time']}"

    cleaned = re.sub(r"[^\w\s-]", "", raw_name, flags=re.UNICODE)
    cleaned = re.sub(r"[\s_]+", "_", cleaned).strip(" _-")
    return cleaned or "reel"


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
                "music.file ist leer oder zeigt auf keine Datei - kein Beat-Matching aus der Konfiguration"
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
            log.warning("spotify.track_file %s nicht gefunden - Fallback auf music_dir", path)

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


def _fetch_spotify_beats(track_uri: str, cfg: Mapping[str, Any]) -> tuple[list[float] | None, str]:
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

    from . import expand_path

    try:
        cache_dir = expand_path(spotify_cfg.get("cache_dir", ".cache/spotify_beats"))
        ttl = float(spotify_cfg.get("cache_ttl", 604800))
        beats = fetch_spotify_beats(track_uri, cache_dir=cache_dir, ttl=ttl)
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

    intervals = [b - a for a, b in zip(beats, beats[1:], strict=False) if b - a > 0.05]
    if not intervals:
        return None, "Spotify-Beats zu unregelmäßig"
    median_beat = float(statistics.median(intervals))
    group = max(1, int(round(median_beat / target)))
    durations: list[float] = []
    for start, end in zip(beats[::group], beats[::group][1:], strict=False):
        span = end - start
        if span > 0:
            durations.append(min(max(span, t_min), t_max))
    log.info(
        "Spotify-Grid: Beat %.2fs -> Gruppe %d (Cut ~%.2fs)",
        median_beat,
        group,
        median_beat * group,
    )
    if not durations:
        return None, "Spotify-Beats ergeben keine verwertbaren Cuts"
    return durations, ""


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


def group_clips_by_day(clips: Sequence[ClipInfo], merge_days: bool = False) -> dict[str, list[ClipInfo]]:
    """Gruppiert Clips nach Aufnahmetag - oder bündelt sie in einer Gruppe.

    Standard: ein Reel pro Tag, damit ``output/<trip>/<tag>/`` wirklich nur
    Material dieses Tages enthält. Mit ``merge_days`` (CLI ``--single-reel``)
    landen alle Clips bewusst in *einem* Reel. Der Gruppenname wird dann zu
    einem Tagesbereich (``2026-09-27_2026-09-30``), damit die Ablage weiter
    erkennbar bleibt; ohne brauchbares Datum bleibt ``_ohne-datum``.
    """
    if not merge_days:
        groups: dict[str, list[ClipInfo]] = {}
        for clip in clips:
            groups.setdefault(clip.date_label or "_ohne-datum", []).append(clip)
        return groups

    labels = sorted({c.date_label for c in clips if c.date_label})
    if not labels:
        label = "_ohne-datum"
    elif len(labels) == 1:
        label = labels[0]
    else:
        label = f"{labels[0]}_{labels[-1]}"
    return {label: list(clips)}


def _apply_run_overrides(
    cfg: dict[str, Any],
    *,
    output_dir: str | Path | None,
    lossless: bool,
    no_geocoding: bool,
    duration: float | None,
    audio: bool | None,
    grade: str | None,
    bpm: float | None,
) -> None:
    """Wendet Laufzeit-Overrides (CLI/Watcher) auf die geladene Config an."""
    if output_dir is not None:
        cfg["output_dir"] = str(output_dir)
    if lossless:
        video = cfg.setdefault("video", {})
        if not isinstance(video, dict):
            video = dict(video)
            cfg["video"] = video
        video["lossless"] = True
    if no_geocoding:
        geocoding = cfg.setdefault("geocoding", {})
        cfg["geocoding"] = {**(geocoding if isinstance(geocoding, Mapping) else {}), "enabled": False}
    if duration is not None:
        video = cfg.setdefault("video", {})
        if not isinstance(video, dict):
            video = dict(video)
            cfg["video"] = video
        video["max_duration"] = float(duration)
    _apply_audio_override(cfg, audio)
    if grade is not None:
        style = cfg.setdefault("style", {})
        if not isinstance(style, dict):
            style = dict(style)
            cfg["style"] = style
        style["preset"] = str(grade).lower()
    if bpm is not None:
        cut = cfg.setdefault("cut", {})
        if not isinstance(cut, dict):
            cut = dict(cut)
            cfg["cut"] = cut
        cut["fallback_bpm"] = float(bpm)


def _apply_pace_settings(
    cut_cfg: dict[str, Any],
    video_cfg: Mapping[str, Any],
    pace: str | None,
) -> None:
    """Passt Schnitt-Zielwerte anhand von ``pace`` oder ``video.max_duration`` an."""
    effective_pace = pace
    if not effective_pace:
        max_dur = float(get(video_cfg, "max_duration", 0) or 0)
        if 0 < max_dur <= 16:
            effective_pace = "fast"
        elif max_dur > 0:
            effective_pace = "medium"

    pace_presets: dict[str, tuple[float, float, float]] = {
        "fast": (1.6, 1.3, 2.0),
        "medium": (2.3, 1.7, 2.9),
        "slow": (3.2, 2.4, 4.0),
    }
    if effective_pace in pace_presets:
        target, target_min, target_max = pace_presets[effective_pace]
        cut_cfg["target"] = target
        cut_cfg["target_min"] = target_min
        cut_cfg["target_max"] = target_max


def _collect_and_validate_clips(
    sources: Sequence[str | Path],
    cfg: Mapping[str, Any],
    geocoder: Geocoder,
    *,
    limit: int,
    errors: list[str] | None = None,
) -> list[ClipInfo]:
    """Sammelt Clips, normalisiert 90/270-Rotationen und filtert ungültige Dateien aus."""
    clips = collect_clips(
        sources,
        geocoder=geocoder,
        recursive=bool(get(cfg, "watcher.recursive", False)),
        ignore_dirs=list(get(cfg, "watcher.ignore_dirs", [])),
        extensions=list(get(cfg, "extensions", [".mov", ".mp4"])),
        future_tolerance=float(get(cfg, "metadata.future_tolerance", 86400.0)),
    )
    geocoder.save_cache()

    for c in clips:
        if c.rotation in (90, 270):
            c.width, c.height = c.height, c.width
            log.info(
                "Clip %s rotiert um %d Grad: Display-Größe ist %dx%d", c.name, c.rotation, c.width, c.height
            )
            # Mark it so we don't swap it again (idempotent helper)
            c.rotation = 0

    if limit > 0:
        clips = clips[:limit]
    if not clips:
        message = "Keine verwertbaren Eingabedateien gefunden: %s"
        if errors is None:
            log.error(message, ", ".join(str(s) for s in sources))
        else:
            _record_error(errors, message, ", ".join(str(s) for s in sources))
        return []

    min_source = float(get(cfg, "watcher.min_clip_duration", 0.0) or 0.0)
    usable: list[ClipInfo] = []
    for c in clips:
        if c.duration < min_source:
            continue
        if c.error:
            log.warning("Datei übersprungen (Metadaten-Fehler): %s - %s", c.path, c.error)
            continue
        if not (getattr(c, "extra", None) or {}).get("has_video") and not is_valid_video(c.path):
            log.warning("Datei übersprungen (korrupt oder nicht lesbar): %s", c.path)
            continue
        usable.append(c)

    skipped = len(clips) - len(usable)
    if skipped:
        log.info("%d Datei(en) übersprungen (kürzer als %.1fs oder korrupt)", skipped, min_source)
    if not usable:
        message = "Alle Eingaben sind ungültig (kürzer als %.1fs oder korrupt)"
        if errors is None:
            log.error(message, min_source)
        else:
            _record_error(errors, message, min_source)
        return []
    return usable


def _first_geo_tag(segments: Sequence[Segment]) -> str:
    """Liefert formatierte Koordinaten des ersten Segments mit gültigen Geo-Daten."""
    for segment in segments:
        if segment.clip.has_geo and segment.clip.latitude is not None and segment.clip.longitude is not None:
            return format_coordinates(segment.clip.latitude, segment.clip.longitude)
    return ""


def _delete_source_clips(clips: Sequence[ClipInfo]) -> None:
    """Löscht verarbeitete Rohclips von der Platte (ohne Duplikate)."""
    for clip_path in dict.fromkeys(c.path for c in clips):
        try:
            clip_path.unlink()
            log.info("Rohclip gelöscht: %s", clip_path.name)
        except OSError as exc:
            log.warning("Rohclip nicht löschbar (%s): %s", clip_path.name, exc)


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
    fit_mode: str | None = None,
    pace: str | None = None,
    lossless: bool = False,
    audio: bool | None = None,
    bpm: float | None = None,
    merge_days: bool = False,
) -> RenderResult:
    """Kompletter Pipeline-Lauf: Metadaten -> Schnitt -> Render -> Export.

    ``audio`` überschreibt ``video.audio``: ``True`` nimmt den Originalton der
    Rohclips mit, ``False`` exportiert stumm, ``None`` folgt der Config. Die
    Musik wird in keinem Fall untergemischt.
    """
    cfg = load_config(config_path)
    # Overrides explizit durchreichen, damit der Watcher nicht darauf angewiesen
    # ist, dass die Datei auf der Platte schon die richtigen Werte enthält.
    _apply_run_overrides(
        cfg,
        output_dir=output_dir,
        lossless=lossless,
        no_geocoding=no_geocoding,
        duration=duration,
        audio=audio,
        grade=grade,
        bpm=bpm,
    )
    if fit_mode is not None:
        cfg.setdefault("video", {})
        cfg["video"]["fit_mode"] = str(fit_mode).strip().lower()

    video_cfg = get(cfg, "video", {}) or {}
    cut_cfg = dict(get(cfg, "cut", {}) or {})
    _apply_pace_settings(cut_cfg, video_cfg, pace)

    result = RenderResult()
    if not sources:
        sources = [str(expand_path(get(cfg, "ingest_dir", ".")))]

    geocoder = Geocoder(cfg)
    usable = _collect_and_validate_clips(sources, cfg, geocoder, limit=limit, errors=result.errors)
    if not usable:
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
        if result.beat_source.startswith("beats:"):
            log.info("Musik %r liefert nur den Schnitttakt (kein Ton im Reel)", music)
        else:
            log.warning(
                "Musik %r nicht nutzbar - Export ohne Musikton (Beat-Quelle: %s)",
                music,
                result.beat_source,
            )

    # Qualitätsanalyse einmalig über alle nutzbaren Clips. Das Ergebnis liegt
    # auf Platte (Key aus Pfad/mtime/Größe), wiederholte Läufe kosten also nur
    # noch einen Blick in die Datei.
    quality_cfg = QualityConfig.from_config(get(cfg, "quality", {}))
    analyses = analyse_clips(usable, quality_cfg, cache_dir=expand_path(quality_cfg.cache_dir))
    min_source = float(get(cfg, "watcher.min_clip_duration", 0.0) or 0.0)

    # Clips nach Aufnahmetag gruppieren: jeder Tag wird ein eigenes Reel in
    # <trip>/<tag>/. Mit --single-reel wird stattdessen bewusst alles in einem
    # Reel gebündelt (Mehrfachauswahl mehrerer Tage im Homelab).
    groups = group_clips_by_day(usable, merge_days=merge_days)
    if len(groups) > 1:
        log.info(
            "Material verteilt sich auf %d Tage: %s",
            len(groups),
            ", ".join(f"{k} ({len(v)} Dateien)" for k, v in sorted(groups.items())),
        )
    elif merge_days:
        log.info(
            "Tage werden in einem Reel zusammengefasst: %s",
            next(iter(groups), ""),
        )

    day_plans: list[tuple[str, list[ClipInfo], list[Segment]]] = []
    for day, day_clips in sorted(groups.items()):
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
        day_plans.append((day, day_clips, day_segments))

    planned_segments = [seg for _, _, day_segments in day_plans for seg in day_segments]
    result.segments = planned_segments
    result.duration = sum(s.duration for s in planned_segments)
    if not planned_segments:
        _record_error(result.errors, "Kein einziger Cut zustande gekommen - Export abgebrochen")
        return result

    log.info(
        "Schnittplan: %d Cuts, %.1fs, Titel=%r, Unterzeile=%r, Beat-Quelle=%s",
        len(planned_segments),
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

    grade_obj = GradeConfig.from_config(cfg, preset=grade)
    outputs: list[Path] = []
    covers: list[Path] = []
    exported_segments: list[Segment] = []
    total_duration = 0.0

    for day, day_clips, day_segments in day_plans:
        day_place = dominant_place(day_clips)
        day_title = title or trip or primary_place(day_place) or day
        day_subtitle = build_subtitle(day_place, day)
        day_geo_tag = _first_geo_tag(day_segments)

        target = export_reel(
            day_segments,
            cfg,
            base_out,
            title=day_title,
            subtitle=day_subtitle,
            geo_tag=day_geo_tag,
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
        exported_segments.extend(day_segments)
        total_duration += sum(s.duration for s in day_segments)
        if dry_run:
            # Im Dry-Run existiert nichts - nur den Plan protokollieren.
            continue
        outputs.append(target)
        covers.append(target.with_suffix(".jpg"))

    result.duration = total_duration
    result.segments = exported_segments

    if dry_run:
        return result

    if not outputs:
        if not result.errors:
            _record_error(result.errors, "Kein einziges Reel zustande gekommen")
        else:
            log.error("Kein einziges Reel zustande gekommen")
        return result

    result.outputs = outputs
    result.output = outputs[0]
    result.covers = [c for c in covers if c.is_file()]
    result.cover = result.covers[0] if result.covers else None

    if delete_after:
        _delete_source_clips(usable)

    return result


def check_disk_space(
    video_cfg: Mapping[str, Any],
    duration: float,
    out_dir: Path,
    want_audio: bool,
    music: str | Path | None,
) -> bool:
    import shutil

    if not out_dir.exists():
        return True

    try:
        usage = shutil.disk_usage(out_dir)
    except OSError as exc:
        log.warning("Konnte freien Speicherplatz für %s nicht ermitteln: %s", out_dir, exc)
        return True

    is_lossless = bool(video_cfg.get("lossless"))
    width = int(video_cfg.get("width", 1080))
    height = int(video_cfg.get("height", 1920))
    fps = int(video_cfg.get("fps", 30))

    if is_lossless:
        # Grobe Schätzung für unkomprimiert / CRF 0: ~1.5 Bytes pro Pixel (YUV420) geteilt durch 3 als x264-Lossless Floor
        video_bps = width * height * fps * 1.5 * 8 / 3
    else:
        crf = float(video_cfg.get("crf", 19) or 19)
        bpp = 0.20 * 2.0 ** ((19.0 - crf) / 6.0)
        video_bps = width * height * fps * bpp

    audio_bps = 0.0
    if want_audio or music is not None:
        audio_bitrate_str = str(video_cfg.get("audio_bitrate", "192k"))
        if audio_bitrate_str.endswith("k"):
            try:
                audio_bps = float(audio_bitrate_str.replace("k", "")) * 1000
            except ValueError:
                audio_bps = 192000.0
        else:
            try:
                audio_bps = float(audio_bitrate_str)
            except ValueError:
                audio_bps = 192000.0

    bytes_per_second = (video_bps + audio_bps) / 8
    estimated_bytes = bytes_per_second * duration

    # Sicherheitsmarge: 20% oder mindestens 200 MB
    margin = max(estimated_bytes * 0.2, 200 * 1024 * 1024)
    required_bytes = estimated_bytes + margin

    log.info(
        "Speicherplatz-Check (%s): geschätzt %.1f MB (inkl. Reserve), verfügbar %.1f MB",
        out_dir,
        required_bytes / 1024 / 1024,
        usage.free / 1024 / 1024,
    )

    if usage.free < required_bytes:
        log.error(
            "Nicht genug freier Speicher auf %s: geschätzt %.1f MB (inkl. Reserve), verfügbar %.1f MB",
            out_dir,
            required_bytes / 1024 / 1024,
            usage.free / 1024 / 1024,
        )
        return False

    return True


def _compute_fingerprint(
    segments: Sequence[Segment],
    music: str | Path | None,
    grade: GradeConfig,
    title: str,
    subtitle: str,
    fit_mode: str = "crop",
) -> str:
    """Berechnet einen eindeutigen Hash für diesen Export-Job (Quellen und Beat-Konfiguration)."""
    payload = []
    for s in segments:
        try:
            stat = s.clip.path.stat()
            stamp = f"{s.clip.path.resolve()}|{stat.st_mtime:.3f}|{stat.st_size}|{s.start:.3f}|{s.end:.3f}"
        except OSError:
            stamp = f"{s.clip.path}|{s.start:.3f}|{s.end:.3f}"
        payload.append(stamp)

    payload.append(str(music))
    payload.append(str(getattr(grade, "preset", "")))
    payload.append(title)
    payload.append(subtitle)
    normalized_fit = str(fit_mode or "crop").strip().lower()
    if normalized_fit != "crop":
        payload.append(normalized_fit)

    return hashlib.sha256("||".join(payload).encode("utf-8")).hexdigest()[:32]


def _find_existing_by_fingerprint(out_dir: Path, fingerprint: str) -> Path | None:
    """Sucht ein bereits exportiertes, valides Video mit identischem Fingerprint."""
    for fp_file in out_dir.glob("*.fingerprint"):
        try:
            if fp_file.read_text().strip() == fingerprint:
                candidate = fp_file.with_suffix("")
                if candidate.exists() and is_valid_video(candidate):
                    log.info(
                        "Überspringe Export: Valides Video %s existiert bereits für dieselbe Quelle und Beat-Konfiguration.",
                        candidate.name,
                    )
                    return candidate
        except OSError:
            continue
    return None


def _cleanup_export_temps(partial: Path, temp_audio: Path, beat_caches: Sequence[Path] = ()) -> None:
    """Entfernt temporäre Export-Artefakte (.part, .part_audio.mp4, .beat-cache)."""
    partial.unlink(missing_ok=True)
    temp_audio.unlink(missing_ok=True)
    for bc in beat_caches:
        bc.unlink(missing_ok=True)


def _build_write_kwargs(
    video_cfg: Mapping[str, Any],
    *,
    want_audio: bool,
    has_audio_track: bool,
    temp_audio: Path,
    is_lossless: bool,
) -> dict[str, Any]:
    """Erstellt die Parameter für ``moviepy.VideoClip.write_videofile()``."""
    ffmpeg_params = ["-pix_fmt", str(video_cfg.get("pixel_format", "yuv420p"))]
    # Eine gemischte Musikspur ist Ton im fertigen Reel und braucht deshalb
    # dieselben Codec-Parameter wie der Originalton - sonst schrieb moviepy
    # stumm bzw. mit libmp3lame und Instagram hätte nichts abzuspielen.
    with_track = bool(want_audio) or bool(has_audio_track)
    crf = video_cfg.get("crf")
    preset = str(video_cfg.get("preset", "medium"))

    if is_lossless:
        crf = 0
        preset = "ultrafast"

    if crf is not None:
        ffmpeg_params.extend(["-crf", str(crf)])
    if bool(video_cfg.get("faststart", True)):
        ffmpeg_params.extend(["-movflags", "+faststart"])
    threads = int(video_cfg.get("threads", 0) or 0)
    if threads:
        ffmpeg_params.extend(["-threads", str(threads)])

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
        write_kwargs["temp_audiofile"] = str(temp_audio)

    return write_kwargs


def _execute_export(
    final: Any,
    partial: Path,
    temp_audio: Path,
    beat_caches: Sequence[Path],
    video_cfg: Mapping[str, Any],
    *,
    want_audio: bool,
    dry_run: bool,
    errors_seen: list[str] | None = None,
) -> bool:
    """Führt Dry-Run-Iteration oder Export (inkl. Lossless-Fallback) aus."""
    has_audio_track = getattr(final, "audio", None) is not None

    def _attempt(is_lossless: bool) -> None:
        write_kwargs = _build_write_kwargs(
            video_cfg,
            want_audio=want_audio,
            has_audio_track=has_audio_track,
            temp_audio=temp_audio,
            is_lossless=is_lossless,
        )
        if dry_run:
            log.info("DRY-RUN: Rendere Video-Frames ohne Export")
            for _ in final.iter_frames(fps=write_kwargs["fps"], dtype="uint8"):
                pass
            if write_kwargs["audio"] and getattr(final, "audio", None) is not None:
                log.info("DRY-RUN: Rendere Audio-Frames ohne Export")
                for _ in final.audio.iter_chunks(fps=write_kwargs["audio_fps"]):
                    pass
        else:
            final.write_videofile(str(partial), **write_kwargs)

    try:
        _attempt(bool(video_cfg.get("lossless")))
        return True
    except Exception as exc:
        _cleanup_export_temps(partial, temp_audio, beat_caches)
        if bool(video_cfg.get("lossless")):
            log.warning("Lossless-Export fehlgeschlagen, versuche Standard-Export: %s", exc)
            try:
                _attempt(False)
                return True
            except Exception as exc2:
                log.exception("Export nach %s fehlgeschlagen: %s", partial, exc2)
                _cleanup_export_temps(partial, temp_audio, beat_caches)
                if errors_seen is not None:
                    errors_seen.append(f"Export fehlgeschlagen: {exc2}")
                return False
        log.exception("Export nach %s fehlgeschlagen: %s", partial, exc)
        if errors_seen is not None:
            errors_seen.append(f"Export fehlgeschlagen: {exc}")
        return False
    except BaseException:
        _cleanup_export_temps(partial, temp_audio, beat_caches)
        raise


def _finalize_export(
    partial: Path,
    target: Path,
    out_dir: Path,
    cfg: Mapping[str, Any],
    video_cfg: Mapping[str, Any],
    *,
    fingerprint: str,
    planned_duration: float,
    want_audio: bool,
    music_mixed: bool,
    dry_run: bool,
    no_cover: bool,
) -> float:
    """Schließt den Export atomar ab, schreibt Fingerprint/Cover und triggert Nextcloud."""
    if dry_run:
        log.info("Export fertig: %s (%.1fs Video)", target, planned_duration)
        return planned_duration

    partial.replace(target)
    try:
        target.with_suffix(f"{target.suffix}.fingerprint").write_text(fingerprint)
    except OSError as exc:
        log.warning("Konnte Fingerprint nicht schreiben: %s", exc)

    # Tatsächliche Dauer aus der fertigen Datei, nicht aus dem Plan: es
    # können Cuts ausgefallen sein.
    actual = probe_duration(target) or planned_duration
    log.info("Export fertig: %s (%.1fs Video)", target, actual)

    if (want_audio or music_mixed) and not has_audio_stream(target):
        if music_mixed:
            # Genau der Fall, den der Nutzer gemeldet hat: Musik war gewählt,
            # das Ergebnis war trotzdem stumm.
            log.error(
                "%s enthält trotz gemischter Musik keine Tonspur - der Ton wurde nicht geschrieben",
                target.name,
            )
        else:
            log.warning(
                "%s enthält trotz video.audio=true keine Tonspur - vermutlich hatten die Quellen keinen Ton",
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
    scan_nextcloud(cfg, out_dir)
    return actual


def export_reel(
    segments: Sequence[Segment],
    cfg: Mapping[str, Any],
    base_out: Path,
    *,
    title: str,
    subtitle: str,
    geo_tag: str = "",
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

    out_dir = build_output_dir(base_out, cfg, title, when, day)
    if not dry_run:
        out_dir = ensure_dir(out_dir)
    log.info("Ausgabeordner: %s", out_dir)

    fit_mode = resolve_fit_mode(cfg)
    fingerprint = _compute_fingerprint(segments, music, grade, title, subtitle, fit_mode=fit_mode)

    if not dry_run:
        existing = _find_existing_by_fingerprint(out_dir, fingerprint)
        if existing is not None:
            return existing

    if dry_run:
        for segment in segments:
            log.info(
                "PLAN %s %.2f-%.2fs (%.2fs)",
                segment.clip.name,
                segment.start,
                segment.end,
                segment.duration,
            )

    stem = build_output_name(cfg, title, when, day)
    stem = f"{stem}_{fingerprint[:8]}"
    suffix = str(get(cfg, "naming.extension", ".mp4") or ".mp4")
    target = unique_path(out_dir, stem, suffix)
    # Staging-Datei trägt dieselbe Endung wie das Ziel, sonst räumt
    # cleanup_partials() sie nicht mehr auf.
    partial = target.parent / f"{target.stem}.part{suffix}"

    want_audio = bool(video_cfg.get("audio", False))
    total_duration = sum(s.duration for s in segments)

    has_space = check_disk_space(video_cfg, total_duration, out_dir, want_audio, music)
    if not dry_run and not has_space:
        if errors_seen is not None:
            errors_seen.extend(["Zu wenig Speicherplatz für den Export"])
        return None

    if not dry_run:
        partial.unlink(missing_ok=True)

    try:
        overlay = make_overlay_array(cfg, title, subtitle, geo_tag)
    except Exception as exc:
        log.error("Text-Overlay konnte nicht gerendert werden (%s) - exportiere ohne Text", exc)
        overlay = None

    errors: list[str] = []
    # Text nur auf den ersten N Cuts. 0 = auf allen (Default 2 = Hook only).
    text_on_first_cuts = int(get(cfg, "text_on_first_cuts", 2) or 0)
    if text_on_first_cuts > 0 and len(segments) > text_on_first_cuts:
        log.info("Text-Overlay nur auf den ersten %d von %d Cuts", text_on_first_cuts, len(segments))
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
        if errors_seen is not None:
            errors_seen.extend(errors)
        return None

    if errors_seen is not None:
        errors_seen.extend(errors)
    rendered_clip = rendered[0]
    final = rendered_clip
    temp_audio = partial.with_name(f"{partial.stem}.part_audio.mp4")
    beat_caches = list(partial.parent.glob("*.beat-cache"))
    music_mixed = False
    try:
        if music is not None:
            mixed = mix_music(rendered_clip, music, cfg, video_cfg)
            if mixed is not None:
                final = mixed
                music_mixed = True

        if not _execute_export(
            final,
            partial,
            temp_audio,
            beat_caches,
            video_cfg,
            want_audio=want_audio,
            dry_run=dry_run,
            errors_seen=errors_seen,
        ):
            return None
    except BaseException:
        # SIGTERM/Stromausfall: unfertige Datei nicht liegen lassen
        _cleanup_export_temps(partial, temp_audio, beat_caches)
        raise
    finally:
        clips_to_close = [final] if final is rendered_clip else [final, rendered_clip]
        _close_all(clips_to_close)

        sources_to_close: list[Any] = []
        seen_ids: set[int] = set()
        for clip_obj in clips_to_close:
            raw_sources = getattr(clip_obj, "tenor_sources", None)
            if isinstance(raw_sources, (list, tuple)):
                for src in raw_sources:
                    if src is not None and id(src) not in seen_ids:
                        seen_ids.add(id(src))
                        sources_to_close.append(src)
        _close_all(sources_to_close)

        temp_files: list[Path] = []
        for clip_obj in clips_to_close:
            raw_temp_files = getattr(clip_obj, "tenor_temp_files", None)
            if isinstance(raw_temp_files, (list, tuple)):
                temp_files.extend(raw_temp_files)
        _unlink_files(temp_files)
        _close_all([getattr(final, "tenor_music_reader", None)])

        _cleanup_ffmpeg_children()
        temp_audio.unlink(missing_ok=True)

    _finalize_export(
        partial,
        target,
        out_dir,
        cfg,
        video_cfg,
        fingerprint=fingerprint,
        planned_duration=total_duration,
        want_audio=want_audio,
        music_mixed=music_mixed,
        dry_run=dry_run,
        no_cover=no_cover,
    )

    return target


def scan_nextcloud(cfg: Mapping[str, Any], output_dir: Path) -> None:
    """Führt den konfigurierten Nextcloud-Scan-Befehl aus."""
    nc_cfg = get(cfg, "nextcloud", {}) or {}
    if not nc_cfg.get("enabled", False):
        return
    cmd_template = nc_cfg.get("scan_cmd")
    if not cmd_template or not isinstance(cmd_template, str):
        return
    try:
        cmd_tokens = shlex.split(cmd_template)
    except ValueError as exc:
        log.warning("Nextcloud-Scan Befehl ungültig: %s", exc)
        return
    if not cmd_tokens:
        return
    cmd = [tok.format(output_dir=output_dir) for tok in cmd_tokens]
    timeout = int(nc_cfg.get("timeout", 30) or 0)
    log.info("Nextcloud-Scan: %s", " ".join(cmd))
    try:
        try:
            subprocess.run(
                cmd,
                shell=False,
                timeout=timeout,
                check=False,
                capture_output=True,
            )
        except BaseException:
            _cleanup_ffmpeg_children()
            raise
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
        "--fit",
        choices=["crop", "blur", "pad"],
        default=None,
        help="Rahmung nicht-9:16-Clips (crop = Zuschneiden, blur = unscharfer Hintergrund, pad = schwarze Balken)",
    )
    parser.add_argument(
        "--pace",
        choices=["fast", "medium", "slow"],
        default=None,
        help="Schnitt-Tempo (fast = ~1.5s, medium = ~2.3s, slow = ~3.2s)",
    )
    parser.add_argument(
        "--bpm",
        type=float,
        default=None,
        help="Expliziter BPM-Wert (überschreibt cut.fallback_bpm)",
    )
    parser.add_argument(
        "--single-reel",
        "--merge-days",
        dest="merge_days",
        action="store_true",
        help="alle Clips in EIN Reel bündeln statt eines Reels pro Aufnahmetag",
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


def _validate_cli_numbers(
    *,
    bpm: float | None,
    duration: float | None,
    limit: int | None,
) -> list[str]:
    """Prüft numerische CLI-Parameter auf gültige Wertebereiche."""
    errors: list[str] = []
    if bpm is not None and (not math.isfinite(bpm) or bpm <= 0.0):
        errors.append(f"Ungültiger Wert für --bpm: {bpm!r} (muss eine endliche Zahl > 0 sein)")
    if duration is not None and (not math.isfinite(duration) or duration < 0.0):
        errors.append(f"Ungültiger Wert für --duration: {duration!r} (muss eine endliche Zahl >= 0 sein)")
    if limit is not None and limit < 0:
        errors.append(f"Ungültiger Wert für --limit: {limit!r} (muss >= 0 sein)")
    return errors


def _json_payload(result: Any) -> dict[str, Any]:
    """Serialisiert ein Pipeline-Ergebnis (oder Test-Double) in das JSON-Format."""
    output = getattr(result, "output", None)
    outputs = getattr(result, "outputs", None) or []
    cover = getattr(result, "cover", None)
    covers = getattr(result, "covers", None) or []
    segments = getattr(result, "segments", None) or []
    duration = float(getattr(result, "duration", 0.0) or 0.0)
    title = getattr(result, "title", "") or ""
    subtitle = getattr(result, "subtitle", "") or ""
    place = getattr(result, "place", None)
    trip = getattr(result, "trip", None)
    beat_source = getattr(result, "beat_source", "fallback")
    if beat_source is None:
        beat_source = "fallback"
    beat_note = getattr(result, "beat_note", "") or ""
    errors = list(getattr(result, "errors", None) or [])
    return {
        "output": str(output) if output else None,
        "outputs": [str(p) for p in outputs],
        "cover": str(cover) if cover else None,
        "covers": [str(p) for p in covers],
        "cuts": len(segments),
        "duration": round(duration, 2),
        "title": title,
        "subtitle": subtitle,
        "place": place,
        "trip": trip,
        "output_dir": str(Path(output).parent) if output else None,
        "beat_source": beat_source,
        "beat_note": beat_note,
        "errors": errors,
    }


def _print_json(result: Any) -> None:
    """Gibt das JSON-Ergebnis formatiert auf stdout aus."""
    print(json.dumps(_json_payload(result), indent=2, ensure_ascii=False))  # noqa: T201


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    validation_errors = _validate_cli_numbers(
        bpm=args.bpm,
        duration=args.duration,
        limit=args.limit,
    )
    if validation_errors:
        for err in validation_errors:
            print(err, file=sys.stderr)  # noqa: T201
        if args.json:
            _print_json(RenderResult(errors=validation_errors))
        return 2

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        msg = f"Konfigurationsfehler: {exc}"
        print(msg, file=sys.stderr)  # noqa: T201
        if args.json:
            _print_json(RenderResult(errors=[msg]))
        return 2
    setup_logging(cfg, quiet=args.quiet)

    if not args.input:
        ingest = expand_path(get(cfg, "ingest_dir", "."))
        if not ingest.is_dir():
            msg = f"Kein -i angegeben und ingest_dir {ingest} existiert nicht"
            log.error("%s", msg)
            if args.json:
                _print_json(RenderResult(errors=[msg]))
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
        fit_mode=args.fit,
        pace=args.pace,
        lossless=args.lossless,
        audio=args.audio,
        bpm=args.bpm,
        merge_days=args.merge_days,
    )

    if args.json:
        _print_json(result)

    if args.dry_run:
        segments = getattr(result, "segments", None) or []
        if not segments:
            return 1
        if not args.json:
            log.info(
                "Dry-Run Zusammenfassung: %d Cuts, %.1fs, Beat-Quelle=%s, Titel=%r",
                len(segments),
                float(getattr(result, "duration", 0.0) or 0.0),
                getattr(result, "beat_source", "fallback"),
                getattr(result, "title", ""),
            )
        return 0

    if not getattr(result, "output", None):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
