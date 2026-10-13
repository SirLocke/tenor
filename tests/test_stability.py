"""Regressionstests für die Stabilitäts-Fixes.

Ohne diese Tests war der Watcher nach dem ersten Dateisystem-Event taub
(``IngestHandler`` griff auf ein nicht existierendes Attribut zu) - ein Fehler,
der im Betrieb nur als "der Watcher verarbeitet nichts mehr" auffällt.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from watchdog.events import FileSystemEvent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import DEFAULTS  # noqa: E402
from src.pipeline import (  # noqa: E402
    _close_all,
    _get_fallback_bpm,
    _match_audio_channels,
    _pick_music,
    _resolve_music_file,
    _safe_bpm,
    build_parser,
    durations_from_bpm,
    mix_music,
    unique_path,
)
from src.spotify_beats import extract_track_id  # noqa: E402
from src.watcher import (  # noqa: E402
    IngestHandler,
    IngestProcessor,
    WatcherState,
    size_stable,
    wait_until_stable,
)


# --------------------------------------------------------------------------- #
# Watchdog-Handler: der Bug, der den Watcher taub machte
# --------------------------------------------------------------------------- #
def _processor(tmp_path: Path, **overrides: Any) -> IngestProcessor:
    cfg: dict[str, Any] = {
        **DEFAULTS,
        "ingest_dir": str(tmp_path / "ingest"),
        "output_dir": str(tmp_path / "out"),
        "watcher": {
            **DEFAULTS["watcher"],
            "debounce_seconds": 0.1,
            "batch_window": 0.1,
        },
    }
    cfg["watcher"].update(overrides)
    (tmp_path / "ingest").mkdir(parents=True, exist_ok=True)
    return IngestProcessor(cfg)


def test_handler_never_raises_on_event(tmp_path: Path) -> None:
    """Regression: ``self.ingest_dir`` existierte auf dem Handler nicht.

    Die Exception tötete den inotify-Thread des Watchdogs - der Prozess lief
    weiter, verarbeitete aber nie wieder etwas.
    """
    processor = _processor(tmp_path)
    seen: list[Path] = []
    processor.on_file_event = seen.append  # type: ignore[method-assign]

    handler = IngestHandler(processor)
    clip = tmp_path / "ingest" / "Madeira" / "a.mov"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.touch()

    for event_type in ("on_created", "on_modified"):
        event = FileSystemEvent(str(clip))
        getattr(handler, event_type)(event)  # darf nicht werfen
    handler.on_moved(FileSystemEvent(str(clip)))

    assert len(seen) == 3


def test_handler_survives_broken_processor(tmp_path: Path) -> None:
    """Auch ein Fehler in der Weiterleitung darf den Handler nicht killen."""
    processor = _processor(tmp_path)

    def boom(_path: Path) -> None:
        raise RuntimeError("absichtlicher Fehler")

    processor.on_file_event = boom  # type: ignore[method-assign]
    handler = IngestHandler(processor)
    clip = tmp_path / "ingest" / "a.mov"
    clip.touch()

    # Kein pytest.raises: genau das Deregeln des Threads ist der Bug.
    handler.on_created(FileSystemEvent(str(clip)))


def test_handler_respects_ignore_dirs(tmp_path: Path) -> None:
    processor = _processor(tmp_path, ignore_dirs=["imgest"])
    seen: list[Path] = []
    processor.on_file_event = seen.append  # type: ignore[method-assign]
    handler = IngestHandler(processor)

    ignored = tmp_path / "ingest" / "imgest" / "Madeira" / "a.mov"
    wanted = tmp_path / "ingest" / "Madeira" / "b.mov"
    for path in (ignored, wanted):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    handler.on_created(FileSystemEvent(str(ignored)))
    handler.on_created(FileSystemEvent(str(wanted)))
    assert [p.name for p in seen] == ["b.mov"]


# --------------------------------------------------------------------------- #
# WatcherState
# --------------------------------------------------------------------------- #
def test_state_survives_corrupt_json_via_backup(tmp_path: Path) -> None:
    """Kaputte State-Datei darf die Historie nicht komplett vernichten."""
    state_path = tmp_path / ".tenor-state.json"
    state = WatcherState.load(state_path)
    state.processed = {"/a.mov": 1000.0, "/b.mov": 2000.0}
    state.save()

    # Erst ein Backup erzeugen, dann die Hauptdatei zerstören.
    state.save()
    state_path.write_text("{kaputt", encoding="utf-8")

    recovered = WatcherState.load(state_path)
    assert recovered.processed == {"/a.mov": 1000.0, "/b.mov": 2000.0}


def test_state_keeps_good_entries_when_one_is_bad(tmp_path: Path) -> None:
    state_path = tmp_path / ".tenor-state.json"
    state_path.write_text(
        json.dumps({
            "processed": {
                "/good.mov": 1.0,
                "/bad.mov": "kein-float",
                "/nan.mov": float("nan"),
            }
        }),
        encoding="utf-8",
    )
    loaded = WatcherState.load(state_path)
    assert loaded.processed == {"/good.mov": 1.0}
    assert all(math.isfinite(v) for v in loaded.processed.values())


def test_state_missing_file_is_not_an_error(tmp_path: Path) -> None:
    loaded = WatcherState.load(tmp_path / "gibtsnicht.json")
    assert loaded.processed == {}


def test_state_is_processed_handles_nan_and_mtime(tmp_path: Path) -> None:
    clip = tmp_path / "a.mov"
    clip.touch()
    state = WatcherState.load(tmp_path / ".state.json")
    assert not state.is_processed(clip)
    state.mark(clip)
    assert state.is_processed(clip)

    # Geänderte mtime => neu verarbeiten. touch() kann within derselben
    # Timestamp-Auflösung liegen, deshalb kurz warten.
    time.sleep(0.02)
    clip.touch()
    assert not state.is_processed(clip)


def test_concurrent_state_saves_do_not_corrupt(tmp_path: Path) -> None:
    """Parallele Saves dürfen kein halbes JSON erzeugen."""
    state_path = tmp_path / ".tenor-state.json"
    state = WatcherState.load(state_path)
    errors: list[BaseException] = []

    def worker(offset: int) -> None:
        try:
            for i in range(40):
                state.processed[f"/clip{offset}-{i}.mov"] = float(i)
                state.save()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["processed"]) == 160


def test_state_no_temp_files_left_behind(tmp_path: Path) -> None:
    state_path = tmp_path / ".tenor-state.json"
    state = WatcherState.load(state_path)
    state.processed = {"/a.mov": 1.0}
    for _ in range(10):
        state.save()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# --------------------------------------------------------------------------- #
# Debounce
# --------------------------------------------------------------------------- #
def test_wait_until_stable_accepts_settled_file(tmp_path: Path) -> None:
    clip = tmp_path / "a.mov"
    clip.write_bytes(b"x" * 4096)
    assert wait_until_stable(clip, 0.2, poll=0.05)


def test_wait_until_stable_rejects_missing_file(tmp_path: Path) -> None:
    assert not wait_until_stable(tmp_path / "weg.mov", 0.1, poll=0.05)


def test_wait_until_stable_survives_transient_oserror(tmp_path: Path) -> None:
    clip = tmp_path / "transient.mov"
    clip.write_bytes(b"x" * 4096)

    original_stat = clip.stat
    call_count = [0]

    def mocked_stat(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("Transient Network Error")
        return original_stat(*args, **kwargs)

    with patch.object(Path, "stat", side_effect=mocked_stat):
        assert wait_until_stable(clip, 0.1, poll=0.05)


def test_size_stable_survives_transient_oserror(tmp_path: Path) -> None:
    clip = tmp_path / "transient.mov"
    clip.write_bytes(b"x" * 4096)

    original_stat = clip.stat
    call_count = [0]

    def mocked_stat(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("Transient Network Error")
        return original_stat(*args, **kwargs)

    with patch.object(Path, "stat", side_effect=mocked_stat):
        assert size_stable(clip, 2, poll=0.05)


def test_size_stable_returns_false_on_persistent_oserror(tmp_path: Path) -> None:
    clip = tmp_path / "persistent.mov"
    clip.write_bytes(b"x" * 4096)

    with patch("src.watcher.get_stat", side_effect=OSError("Persistent OS error")):
        assert not size_stable(clip, checks=2, poll=0.0)


def test_size_stable_returns_false_on_oserror_during_subsequent_check(tmp_path: Path) -> None:
    clip = tmp_path / "subsequent_fail.mov"
    clip.write_bytes(b"x" * 4096)

    original_get_stat = clip.stat
    call_count = [0]

    def mocked_get_stat(path: Path) -> os.stat_result:
        call_count[0] += 1
        if call_count[0] > 1:
            raise OSError("Fail on check 2")
        return original_get_stat()

    with patch("src.watcher.get_stat", side_effect=mocked_get_stat):
        assert not size_stable(clip, checks=2, poll=0.0)


def test_size_stable_returns_true_when_stable(tmp_path: Path) -> None:
    clip = tmp_path / "stable.mov"
    clip.write_bytes(b"x" * 1024)
    assert size_stable(clip, checks=3, poll=0.0)


def test_size_stable_returns_false_when_unstable(tmp_path: Path) -> None:
    clip = tmp_path / "growing.mov"
    clip.write_bytes(b"x" * 1024)

    sizes = [1024, 2048]
    call_count = [0]

    class FakeStat:
        def __init__(self, size: int):
            self.st_size = size
            self.st_mtime = 0.0

    def mocked_get_stat(path: Path):
        idx = min(call_count[0], len(sizes) - 1)
        call_count[0] += 1
        return FakeStat(sizes[idx])

    with patch("src.watcher.get_stat", side_effect=mocked_get_stat):
        assert not size_stable(clip, checks=2, poll=0.0)


def test_wait_until_stable_gives_up_eventually(tmp_path: Path) -> None:
    """Eine endlos wachsende Datei darf den Thread nicht ewig blockieren."""
    clip = tmp_path / "grow.mov"
    clip.write_bytes(b"0")
    stop = threading.Event()

    def grow() -> None:
        while not stop.is_set():
            try:
                with clip.open("ab") as fh:
                    fh.write(b"0" * 64)
            except OSError:
                return
            time.sleep(0.02)

    thread = threading.Thread(target=grow, daemon=True)
    thread.start()
    try:
        start = time.monotonic()
        assert not wait_until_stable(clip, 0.1, poll=0.05, max_wait=1.0)
        assert time.monotonic() - start < 15
    finally:
        stop.set()


# --------------------------------------------------------------------------- #
# BPM-Fallback
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bpm", [60, 80, 90, 100, 110, 120, 124, 128, 132, 140, 160, 174, 200])
def test_durations_from_bpm_respects_target_range(bpm: float) -> None:
    cut = {"target_min": 1.4, "target_max": 2.0, "target": 1.7}
    durations = durations_from_bpm(bpm, cut)
    assert durations
    for value in durations:
        assert 1.4 <= value <= 2.0, (bpm, value)


@pytest.mark.parametrize("bpm", [0, -1, float("nan")])
def test_durations_from_bpm_rejects_bad_input(bpm: float) -> None:
    assert durations_from_bpm(bpm, {"target_min": 1.4, "target_max": 2.0, "target": 1.7}) == []


def test_fallback_bpm_prefers_explicit_cut_setting() -> None:
    cfg = {
        "cut": {"fallback_bpm": 124},
        "spotify": {"fallback_bpm": {"default": 90}},
    }
    assert _get_fallback_bpm(cfg, None) == 124.0


def test_fallback_bpm_matches_music_hint() -> None:
    cfg = {"cut": {}, "spotify": {"fallback_bpm": {"default": 90, "edm": 128}}}
    assert _get_fallback_bpm(cfg, "my-edm-set.mp3") == 128.0


def test_fallback_bpm_longest_key_wins() -> None:
    """'pop' darf ein spezifischeres 'pop remix' nicht verdecken."""
    cfg = {
        "cut": {},
        "spotify": {"fallback_bpm": {"default": 90, "pop": 120, "pop remix": 140}},
    }
    assert _get_fallback_bpm(cfg, "Nightdrive (Pop Remix).mp3") == 140.0


def test_fallback_bpm_uses_default_sentinel() -> None:
    cfg = {"cut": {}, "spotify": {"fallback_bpm": {"house": 124, "default": 90}}}
    assert _get_fallback_bpm(cfg, "unbekannt.mp3") == 90.0


def test_fallback_bpm_does_not_depend_on_yaml_order() -> None:
    """Regression: früher hing der Wert an der Reihenfolge der YAML-Keys."""
    a = {"cut": {}, "spotify": {"fallback_bpm": {"pop": 120, "default": 90}}}
    b = {"cut": {}, "spotify": {"fallback_bpm": {"default": 90, "pop": 120}}}
    assert _get_fallback_bpm(a, None) == _get_fallback_bpm(b, None) == 90.0


def test_fallback_bpm_default_without_config() -> None:
    assert _get_fallback_bpm({}, None) == 120.0


def test_fallback_bpm_skips_broken_entries() -> None:
    cfg = {
        "cut": {},
        "spotify": {"fallback_bpm": {"broken": "kein-float", "default": 128}},
    }
    assert _get_fallback_bpm(cfg, "broken.mp3") == 128.0


def test_safe_bpm_never_crashes() -> None:
    for bad in ("text", None, 0, -5, float("inf"), float("nan"), []):
        assert _safe_bpm(bad, 120.0) == 120.0


# --------------------------------------------------------------------------- #
# Spotify-Helfer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        "4cOdK2wGLETKBW3PvgPWqT",
    ],
)
def test_extract_track_id(value: str) -> None:
    assert extract_track_id(value) == "4cOdK2wGLETKBW3PvgPWqT"


@pytest.mark.parametrize("value", ["", "kein track", "spotify:playlist:abc", "xyz"])
def test_extract_track_id_rejects_junk(value: str) -> None:
    assert extract_track_id(value) is None


def test_spotify_token_cache_is_thread_safe() -> None:
    from src.spotify_beats import _TokenCache

    cache = _TokenCache()
    seen: list[str | None] = []

    def worker() -> None:
        for _ in range(200):
            cache.set("tok", 3600.0)
            seen.append(cache.get())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(v == "tok" for v in seen)


# --------------------------------------------------------------------------- #
# collect_clips
# --------------------------------------------------------------------------- #
def test_collect_clips_uses_given_extensions(tmp_path: Path) -> None:
    from src.metadata import collect_clips

    src = tmp_path / "in"
    src.mkdir()
    (src / "a.mov").touch()
    (src / "b.mkv").touch()
    (src / "c.hevc").touch()

    clips = collect_clips([src], extensions=[".mov", ".hevc"])
    assert {c.path.name for c in clips} == {"a.mov", "c.hevc"}


def test_collect_clips_honours_ignore_dirs(tmp_path: Path) -> None:
    from src.metadata import collect_clips

    (tmp_path / "imgest").mkdir()
    (tmp_path / "Madeira").mkdir()
    (tmp_path / "imgest" / "a.mov").touch()
    (tmp_path / "Madeira" / "b.mov").touch()

    clips = collect_clips([tmp_path], extensions=[".mov"], ignore_dirs=["imgest"], recursive=True)
    assert [c.path.name for c in clips] == ["b.mov"]


# --------------------------------------------------------------------------- #
# Cut-Position
# --------------------------------------------------------------------------- #
def test_build_segment_clip_never_exceeds_source_duration() -> None:
    """Regression: der start+frame-Floor konnte die Obergrenze aufheben."""
    from src.pipeline import build_segment_clip
    from src.styling import GradeConfig

    class _FakeSource:
        """Minimaler moviepy-Ersatz, der den subclip-Aufruf protokolliert."""

        duration = 0.0
        size = (1920, 1080)

        def __init__(self) -> None:
            self.requests: list[tuple[float, float]] = []

        def subclipped(self, start: float, end: float) -> _FakePiece:
            self.requests.append((start, end))
            return _FakePiece(self, start, end)

    class _FakePiece:
        def __init__(self, source: _FakeSource, start: float, end: float) -> None:
            self.source = source
            self.start = start
            self.end = end
            self.duration = max(end - start, 1 / 30)

        def with_duration(self, value: float) -> _FakePiece:
            self.duration = value
            return self

        def with_fps(self, _fps: int) -> _FakePiece:
            return self

        def image_transform(self, _fn: Any) -> _FakePiece:
            return self

    fake = _FakeSource()
    segment = SimpleNamespace(
        clip=SimpleNamespace(path=Path("/tmp/does-not-exist.mov"), name="x.mov"),
        start=0.0,
        end=5.0,
        duration=5.0,
    )
    import src.pipeline as pipeline_module

    original = pipeline_module._open_source
    pipeline_module._open_source = lambda _p, **_kw: fake  # type: ignore[assignment]
    original_crop = pipeline_module.center_crop_resize
    pipeline_module.center_crop_resize = lambda piece, _w, _h: piece  # type: ignore[assignment]
    try:
        with pytest.raises(ValueError, match="außerhalb"):
            build_segment_clip(segment, {}, GradeConfig(), None, fps=30, width=1080, height=1920)
    finally:
        pipeline_module._open_source = original  # type: ignore[assignment]
        pipeline_module.center_crop_resize = original_crop  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Audio: Ton mitnehmen, Musik nie untermischen
# --------------------------------------------------------------------------- #
def test_audio_is_opt_in_not_silent_by_default() -> None:
    """Ohne ``--audio`` bleibt das Reel stumm - der Ton kommt in Instagram dazu."""
    assert DEFAULTS["video"]["audio"] is False
    args = build_parser().parse_args(["-i", "clip.mov"])
    assert args.audio is None

    with_audio = build_parser().parse_args(["-i", "clip.mov", "--audio"])
    assert with_audio.audio is True


def test_audio_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["-i", "clip.mov", "--audio", "--no-audio"])


def test_audio_codec_is_aac_not_moviepy_default() -> None:
    """moviepy schreibt sonst libmp3lame - das lehnt Instagram ab."""
    assert DEFAULTS["video"]["audio_codec"] == "aac"
    assert DEFAULTS["video"]["audio_bitrate"]


def test_cli_audio_flag_reaches_run(tmp_path: Path) -> None:
    """--audio muss ``run()`` erreichen - sonst wäre die Config stumm schlaglos."""
    import src.pipeline as pipeline_module

    captured: dict[str, Any] = {}

    def _fake_run(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(output=None, outputs=[], covers=[], segments=[], errors=[])

    original = pipeline_module.run
    pipeline_module.run = _fake_run  # type: ignore[assignment]
    try:
        pipeline_module.main(["-i", str(tmp_path), "--audio"])
        assert captured["audio"] is True
        captured.clear()
        pipeline_module.main(["-i", str(tmp_path), "--no-audio"])
        assert captured["audio"] is False
        captured.clear()
        pipeline_module.main(["-i", str(tmp_path)])
        assert captured["audio"] is None
    finally:
        pipeline_module.run = original  # type: ignore[assignment]


def test_audio_override_beats_config() -> None:
    """``audio=True/False`` schlägt ``video.audio``, ``None`` lässt es in Ruhe."""
    from src.pipeline import _apply_audio_override

    cfg: dict[str, Any] = {"video": {"audio": False, "fps": 30}}
    _apply_audio_override(cfg, True)
    assert cfg["video"]["audio"] is True

    _apply_audio_override(cfg, False)
    assert cfg["video"]["audio"] is False

    _apply_audio_override(cfg, None)
    assert cfg["video"]["audio"] is False
    assert cfg["video"]["fps"] == 30  # nichts anderes angefasst

    empty: dict[str, Any] = {}
    _apply_audio_override(empty, True)
    assert empty["video"]["audio"] is True


def test_watcher_audio_flag_reaches_pipeline(tmp_path: Path) -> None:
    """Regression: ``watcher --audio`` landete nur in der Config-Kopie.

    ``pipeline.run()`` liest die Config aber erneut von der Platte und hätte
    den Opt-in nie gesehen - das Reel wäre trotz ``--audio`` stumm geblieben.
    """
    import src.pipeline as pipeline_module
    from src.watcher import IngestProcessor

    captured: dict[str, Any] = {}

    def _fake_run(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(ok=True, output=None, outputs=[], covers=[], segments=[], errors=[])

    cfg: dict[str, Any] = {
        **DEFAULTS,
        "ingest_dir": str(tmp_path / "ingest"),
        "output_dir": str(tmp_path / "out"),
        "video": {**DEFAULTS["video"], "audio": True},
    }
    (tmp_path / "ingest").mkdir(parents=True, exist_ok=True)
    clip = tmp_path / "ingest" / "clip.mp4"
    clip.write_bytes(b"stub")

    processor = IngestProcessor(cfg)
    assert processor.audio is True

    original = pipeline_module.run
    pipeline_module.run = _fake_run  # type: ignore[assignment]
    try:
        processor.process_batch([clip])
    finally:
        pipeline_module.run = original  # type: ignore[assignment]

    assert captured["audio"] is True


# --------------------------------------------------------------------------- #
# Musik im fertigen Reel
# --------------------------------------------------------------------------- #
# Regression: im Homelab wurde ein Track ausgewählt, das Reel war stumm. Zwei
# Ursachen, beide hier festgenagelt:
#   1. Der Export kannte nur `video.audio` (Originalton). Eine gemischte
#      Musikspur wurde nicht als Ton erkannt, also nicht mit AAC geschrieben.
#   2. Der WIP-Code benutzte die moviepy-1.x-Namen (`moviepy.editor`,
#      `.volumex`, `.set_audio`), die es in 2.x nicht mehr gibt.


def _music_file(tmp_path: Path, seconds: float = 1.5, freq: int = 440) -> Path:
    """Echte Audiodatei per ffmpeg - ein synthetischer Clip reicht nicht,
    weil genau der Leseweg aus moviepy geprüft wird.

    ``aevalsrc`` statt ``sine``: der ``sine``-Filter schreibt hier mit rund
    0,125 Amplitude und moviepy kommt mit einem weiteren Faktor 1/sqrt(2) darunter
    - Pegel, die nichts mit dem Mischen zu tun haben, aber jede absolute
    Schwelle in diesen Tests wackeln ließen. 0,9 Amplitude gibt einen Ton, der
    unabhängig von moviepy- und ffmpeg-Version klar im hörbaren Bereich bleibt.
    """
    import subprocess

    target = tmp_path / f"song-{seconds}-{freq}.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=0.9*sin(2*PI*{freq}*t):s=44100:d={seconds}",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


def _silent_video(duration: float = 3.0):
    import numpy as np
    from moviepy import ImageClip

    array = np.full((64, 64, 3), 120, dtype=np.uint8)
    return ImageClip(array, duration=duration).with_fps(30)


def _rms(clip, start: float, end: float, fps: int = 8000) -> float:
    """Effektiver Pegel eines Zeitfensters, ohne den ganzen Clip zu rendern.

    ``subclipped`` verschiebt die Zeitbasis: der Ausschnitt startet bei ``t=0``
    und liest intern ``absolut = t + start``. Deshalb wird hier die *lokale*
    Zeit abgefragt - mit ``start + i / fps`` läge die Abfrage um ``start`` zu
    weit hinten und der Ausschnitt lief ins Leere (bei Fenstern am Clip-Ende
    komplett aus dem Material heraus).
    """
    import numpy as np

    section = clip.subclipped(start, end)
    count = max(int(round((end - start) * fps)), 1)
    samples = np.array([np.asarray(section.get_frame(i / fps), dtype=float) for i in range(count)])
    return float(np.sqrt((samples**2).mean()))


def test_selected_music_ends_up_in_the_reel(tmp_path: Path) -> None:
    """Der Kern der Meldung: gewaehlte Musik muss hoerbar sein."""
    song = _music_file(tmp_path)
    mixed = mix_music(_silent_video(), song, {"music": {"gain": 1.0, "fade_out": 0.0}})

    assert mixed is not None
    assert mixed.audio is not None
    assert mixed.audio.duration == pytest.approx(3.0)
    assert _rms(mixed.audio, 0.2, 0.8) > 0.1


def test_music_loops_to_cover_the_reel(tmp_path: Path) -> None:
    """1,5 s Musik auf ein 3-s-Reel: muss loopen, sonst endet der Ton mitten drin."""
    song = _music_file(tmp_path, seconds=1.5)
    mixed = mix_music(_silent_video(duration=3.0), song, {"music": {"fade_out": 0.0}})
    assert mixed is not None and mixed.audio is not None
    # Auch hinter der Loop-Grenze ist noch Ton.
    assert _rms(mixed.audio, 1.6, 2.2) > 0.1
    assert _rms(mixed.audio, 2.4, 3.0) > 0.1


def test_music_longer_than_reel_is_trimmed(tmp_path: Path) -> None:
    song = _music_file(tmp_path, seconds=20.0)
    mixed = mix_music(_silent_video(duration=3.0), song, {"music": {"fade_out": 0.0}})
    assert mixed is not None and mixed.audio is not None
    assert mixed.audio.duration == pytest.approx(3.0)


def test_music_fades_out_at_the_end(tmp_path: Path) -> None:
    song = _music_file(tmp_path, seconds=1.5)
    mixed = mix_music(_silent_video(duration=4.0), song, {"music": {"fade_out": 1.0}})
    assert mixed is not None and mixed.audio is not None
    assert _rms(mixed.audio, 0.2, 0.6) > 0.1
    assert _rms(mixed.audio, 3.95, 4.0) < 0.02


def test_original_audio_is_ducked_under_music(tmp_path: Path) -> None:
    """--audio plus Musik: Originalton bleibt hoerbar, aber leise."""
    import numpy as np
    from moviepy import AudioClip

    song = _music_file(tmp_path, seconds=1.5)
    video = _silent_video(duration=3.0).with_audio(
        AudioClip(lambda t: np.sin(2 * np.pi * 220 * t), duration=3.0, fps=44100)
    )
    cfg = {"music": {"gain": 0.0, "fade_out": 0.0}, "video": {"original_gain": 0.25}}

    ducked = mix_music(video, song, cfg)
    loud = mix_music(video, song, {"music": {"gain": 0.0, "fade_out": 0.0}, "video": {"original_gain": 1.0}})
    assert ducked is not None and loud is not None
    # Bei Gain 0 hört man nur den Originalton: 0,25 ist rund ein Viertel.
    assert _rms(ducked.audio, 0.5, 1.0) == pytest.approx(_rms(loud.audio, 0.5, 1.0) * 0.25, rel=0.02)


def test_mix_uses_moviepy_v2_api_only(tmp_path: Path) -> None:
    """moviepy 2.x: `moviepy.editor`, `.volumex`, `.set_audio` existieren nicht.

    Der alte Stand scheiterte genau daran - mit `ModuleNotFoundError`, was als
    "Reel ohne Musik" endete.
    """
    import moviepy

    assert not hasattr(moviepy, "editor"), "moviepy 2.x hat kein moviepy.editor"

    song = _music_file(tmp_path)
    video = _silent_video(duration=3.0)
    mixed = mix_music(video, song, {"music": {"gain": 1.0, "fade_out": 0.5}})
    assert mixed is not None
    audio = mixed.audio
    assert hasattr(audio, "with_volume_scaled"), "v1-API auf 2.x übertragen"
    assert not hasattr(audio, "volumex")
    assert not hasattr(audio, "set_audio")


def test_mono_original_ton_unter_stereo_musik(tmp_path: Path) -> None:
    """iPhone-Clips sind mono, MP3s stereo - sonst bricht das Addieren.

    `CompositeAudioClip` addiert rohe Numpy-Arrays; mono liefert (n,), stereo
    (n, 2). Ohne Angleichung: "operands could not be broadcast together".
    """
    import numpy as np
    from moviepy import AudioClip

    song = _music_file(tmp_path, seconds=1.5)  # stereo WAV
    video = _silent_video(duration=3.0).with_audio(
        AudioClip(lambda t: np.sin(2 * np.pi * 220 * t), duration=3.0, fps=22050)  # mono
    )
    mixed = mix_music(
        video, song, {"music": {"gain": 0.5, "fade_out": 0.0}, "video": {"original_gain": 0.25}}
    )
    assert mixed is not None and mixed.audio is not None
    assert int(getattr(mixed.audio, "nchannels", 1)) >= 2
    assert _rms(mixed.audio, 0.5, 1.0) > 0.0


def test_match_audio_channels_shapes_frames() -> None:
    """Der Weg, den die Kanal-Angleichung geht."""
    import numpy as np
    from moviepy import AudioClip

    mono = AudioClip(lambda t: np.sin(2 * np.pi * 220 * t), duration=1.0, fps=22050)
    stereo = _match_audio_channels(mono, 2)
    assert int(stereo.nchannels) == 2
    frame = stereo.get_frame(np.array([0.0, 0.001]))
    assert frame.shape[-1] == 2, "zweite Kanalachse, nicht doppelte Länge"
    assert np.allclose(frame[..., 0], frame[..., 1])

    back = _match_audio_channels(stereo, 1)
    assert int(back.nchannels) == 1
    # Bereits passende Clips bleiben unverändert (kein Kopieren pro Frame).
    assert _match_audio_channels(stereo, 2) is stereo


def test_close_all_accepts_a_single_clip(tmp_path: Path) -> None:
    """Regression: der Export blieb beim Aufräumen hängen.

    Ein moviepy-Clip ist slicebar. `for clip in clip` iterierte deshalb über
    Frames, nicht über Clips - `close()` lief ins Rendern und der Export
    hing nach dem fertigen Video. Deshalb wird auf eine Liste normalisiert.
    """
    closed = []

    class Sliceable:
        n = 3

        def __getitem__(self, item):
            return "frame"

        def close(self):
            closed.append(self)

    single = Sliceable()
    _close_all(single)  # type: ignore[arg-type]
    assert len(closed) == 1

    _close_all([single, None])
    assert len(closed) == 2, "None darf den Close nicht abbrechen"
    _close_all(None)  # type: ignore[arg-type]


def test_missing_music_file_leaves_the_reel_silent(tmp_path: Path) -> None:
    """Keine Datei ist kein Fehler - der Export läuft stumm weiter."""
    assert mix_music(_silent_video(), tmp_path / "fehlt.mp3", {}) is None


def test_music_fails_gracefully_when_moviepy_rejects_it(tmp_path: Path) -> None:
    """Beschädigte Musik darf den Lauf nie abreißen."""
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio at all")
    assert mix_music(_silent_video(), broken, {}) is None


def test_only_deliberately_chosen_music_is_mixed(tmp_path: Path) -> None:
    """Der Watcher hat niemanden nach Musik gefragt -> Reel bleibt stumm.

    ``music_dir`` bzw. ``spotify.track_file`` liefert nur den Schnitttakt.
    """
    cfg = {"music_dir": str(tmp_path), "music": {"file": str(tmp_path / "a.mp3")}}
    (tmp_path / "a.mp3").write_bytes(b"stub")

    # Ohne CLI-Wunsch wird nichts gemischt.
    assert _resolve_music_file(None, cfg) is None
    # Eine Spotify-URI ist keine Audiodatei.
    assert _resolve_music_file("spotify:track:abc", cfg) is None
    # Explizite Datei gewinnt.
    explicit = tmp_path / "b.mp3"
    explicit.write_bytes(b"stub")
    assert _resolve_music_file(explicit, cfg) == explicit


def test_music_file_from_config_is_used_as_ton(tmp_path: Path) -> None:
    """`music.file` in der Config zählt als bewusste Wahl - mit `-m auto`."""
    song = _music_file(tmp_path)
    cfg = {"music": {"file": str(song)}}
    assert _resolve_music_file("auto", cfg) == song
    assert _resolve_music_file(None, cfg) is None


def test_auto_finds_the_configured_music_for_beats_too(tmp_path: Path) -> None:
    """`-m auto` muss Beat-Quelle *und* Ton liefern - nicht nur den Ton.

    Sonst sucht der Beat-Auflöser nach einer Datei namens "auto", verwarnt und
    fällt auf 120 BPM zurück, während der Export die konfigurierte Musik
    unterlegt - der Schnitt passte dann nicht mehr zum Track.
    """
    song = _music_file(tmp_path)
    cfg = {"music": {"file": str(song)}}
    assert _pick_music("auto", cfg) == song
    assert _pick_music(song, cfg) == song
    # Ohne music.file gibt es für "auto" nichts zu holen.
    assert _pick_music("auto", {}) is None
    assert _resolve_music_file("auto", {}) is None


def test_music_defaults_dont_break_loading() -> None:
    """Die neuen Keys sind in den Defaults dokumentiert."""
    assert DEFAULTS["music"]["gain"] == 1.0
    assert DEFAULTS["music"]["fade_out"] == 1.5
    assert DEFAULTS["video"]["original_gain"] == 0.25


def _base_cfg() -> dict[str, Any]:
    """Kopie der Defaults - unabhängig von der echten config.yaml."""
    import copy

    return copy.deepcopy(DEFAULTS)


def test_spotify_403_falls_back_to_music_dir(tmp_path: Path) -> None:
    """Spotify liefert für neue Apps 403 - die lokale Datei muss einspringen.

    Regression: die Auswahl eines Spotify-Tracks lief ins Leere und das Reel
    wurde stumpf auf 120 BPM geschnitten.
    """
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    song = music_dir / "chosen.mp3"
    song.write_bytes(b"not-really-mp3")

    cfg = _base_cfg()
    cfg["music_dir"] = str(music_dir)
    cfg["spotify"] = {
        "enabled": True,
        "track_uri": "spotify:track:0Hcr3kdSB9NVQmFckRsgSD",
    }

    assert _pick_music(None, cfg) == song


def test_spotify_track_file_is_pinned(tmp_path: Path) -> None:
    """track_file gewinnt vor der neuesten Datei aus music_dir."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    newest = music_dir / "newest.mp3"
    newest.write_bytes(b"a")
    pinned = tmp_path / "the_chosen_track.mp3"
    pinned.write_bytes(b"b")

    cfg = _base_cfg()
    cfg["music_dir"] = str(music_dir)
    cfg["spotify"] = {"enabled": True, "track_file": str(pinned)}

    assert _pick_music(None, cfg) == pinned


def test_spotify_track_file_missing_falls_back(tmp_path: Path) -> None:
    """Ein toter track_file darf den Lauf nicht abbrechen."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    song = music_dir / "only.mp3"
    song.write_bytes(b"a")

    cfg = _base_cfg()
    cfg["music_dir"] = str(music_dir)
    cfg["spotify"] = {"enabled": True, "track_file": str(tmp_path / "nope.mp3")}

    assert _pick_music(None, cfg) == song


def test_spotify_access_error_is_actionable() -> None:
    """Die 403-Meldung muss den Grund nennen, nicht nur 'HTTP 403'."""
    from src.spotify_beats import QUOTA_HINT

    assert "403" in QUOTA_HINT
    assert "27.11.2024" in QUOTA_HINT
    assert "track_file" in QUOTA_HINT


def test_music_selection_covers_common_formats(tmp_path: Path) -> None:
    """Nicht-MP3 (m4a/wav/flac) sind gültige Beat-Quellen."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    for name in ("a.mp3", "b.m4a", "c.wav", "d.flac"):
        (music_dir / name).write_bytes(b"x")

    cfg = _base_cfg()
    cfg["music_dir"] = str(music_dir)
    cfg["spotify"] = {"enabled": False}

    picked = _pick_music(None, cfg)
    assert picked is not None
    assert picked.suffix in (".mp3", ".m4a", ".wav", ".flac")


def test_resolve_beats_reports_blocked_spotify(tmp_path: Path) -> None:
    """Spotify 403 -> lokale Datei + Grund im Report.

    Das ist der eigentliche Fix: vorher blieb der konfigurierte Track stumm
    wirkungslos und das Reel wurde auf 120 BPM geschnitten, ohne dass man im
    Report sehen konnte, warum.
    """
    import src.pipeline as pipeline_module

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    song = music_dir / "song.mp3"
    song.write_bytes(b"x")

    cfg = _base_cfg()
    cfg["music_dir"] = str(music_dir)
    cfg["spotify"] = {
        "enabled": True,
        "track_uri": "spotify:track:0Hcr3kdSB9NVQmFckRsgSD",
    }

    originals = (
        pipeline_module._fetch_spotify_beats,
        pipeline_module.detect_beat_durations,
    )
    pipeline_module._fetch_spotify_beats = (  # type: ignore[assignment]
        lambda _uri, _cfg: (
            None,
            "Spotify blockiert die Audio-Analysis für diese App (HTTP 403)",
        )
    )
    pipeline_module.detect_beat_durations = (  # type: ignore[assignment]
        lambda _path, _cut: [1.5]
    )
    try:
        durations, source, note = pipeline_module._resolve_beats(None, cfg, cfg["cut"])
    finally:
        pipeline_module._fetch_spotify_beats, pipeline_module.detect_beat_durations = originals  # type: ignore[assignment]

    assert durations == [1.5]
    assert source == "beats:song.mp3"
    assert "403" in note


def test_resolve_beats_falls_back_to_bpm_with_reason(tmp_path: Path) -> None:
    """Ohne jede Audiodatei bleibt der BPM-Takt - mit Begründung, nicht stumm."""
    import src.pipeline as pipeline_module

    cfg = _base_cfg()
    cfg["music_dir"] = str(tmp_path / "empty-music")
    cfg["spotify"] = {"enabled": False}
    cfg["cut"]["fallback_bpm"] = 120

    durations, source, note = pipeline_module._resolve_beats(None, cfg, cfg["cut"])

    assert durations  # das Grid muss wirklich gesetzt sein, nicht nur geloggt
    assert source == "fallback:120bpm"
    assert "Takt-Fallback" in note
    assert all(1.4 <= d <= 2.0 for d in durations)


def test_resolve_beats_uses_track_file_before_music_dir(tmp_path: Path) -> None:
    """track_file (Audio des gewählten Tracks) schlägt music_dir."""
    import src.pipeline as pipeline_module

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "newest.mp3").write_bytes(b"a")
    pinned = tmp_path / "the_waves.mp3"
    pinned.write_bytes(b"b")

    cfg = _base_cfg()
    cfg["music_dir"] = str(music_dir)
    cfg["spotify"] = {"enabled": True, "track_file": str(pinned)}

    original = pipeline_module.detect_beat_durations
    pipeline_module.detect_beat_durations = lambda _path, _cut: [1.6]  # type: ignore[assignment]
    try:
        durations, source, _note = pipeline_module._resolve_beats(None, cfg, cfg["cut"])
    finally:
        pipeline_module.detect_beat_durations = original  # type: ignore[assignment]

    assert durations == [1.6]
    assert source == "beats:the_waves.mp3"


def test_resolve_beats_cli_file_wins_over_spotify(tmp_path: Path) -> None:
    """-m song.mp3 darf nicht von einem konfigurierten Spotify-Track überstimmt werden."""
    import src.pipeline as pipeline_module

    song = tmp_path / "cli.mp3"
    song.write_bytes(b"a")

    cfg = _base_cfg()
    cfg["spotify"] = {
        "enabled": True,
        "track_uri": "spotify:track:0Hcr3kdSB9NVQmFckRsgSD",
    }

    called: list[str] = []

    def _fake_spotify(uri: str, _cfg: Any) -> tuple[list[float] | None, str]:
        called.append(uri)
        return [9.9], ""

    original_fetch = pipeline_module._fetch_spotify_beats
    original_detect = pipeline_module.detect_beat_durations
    pipeline_module._fetch_spotify_beats = _fake_spotify  # type: ignore[assignment]
    pipeline_module.detect_beat_durations = lambda _path, _cut: [1.5]  # type: ignore[assignment]
    try:
        _durations, source, _note = pipeline_module._resolve_beats(str(song), cfg, cfg["cut"])
    finally:
        pipeline_module._fetch_spotify_beats = original_fetch  # type: ignore[assignment]
        pipeline_module.detect_beat_durations = original_detect  # type: ignore[assignment]

    assert source == "beats:cli.mp3"
    assert called == []


def test_detect_beat_durations_median_calculation(tmp_path: Path) -> None:
    """Prüft, dass detect_beat_durations mit Mock-Librosa Median-Intervalle korrekt berechnet."""
    import sys
    from unittest.mock import MagicMock

    import numpy as np
    import src.pipeline as pipeline_module

    fake_librosa = MagicMock()
    # Erzeuge synthetisches Audiosignal und Beats: 100 Beats mit 0.5s Abstand
    fake_librosa.load.return_value = (np.ones(22050), 22050)
    fake_librosa.beat.beat_track.return_value = (120.0, np.arange(0, 100))
    # 0, 0.5, 1.0, 1.5, ...
    fake_librosa.frames_to_time.return_value = np.arange(0, 100) * 0.5

    dummy_music = tmp_path / "test.mp3"
    dummy_music.touch()

    with patch.dict(sys.modules, {"librosa": fake_librosa}):
        durations = pipeline_module.detect_beat_durations(
            dummy_music, {"target": 1.7, "target_min": 1.4, "target_max": 2.0}
        )

    assert len(durations) > 0
    assert all(1.4 <= d <= 2.0 for d in durations)
    # 120 BPM (beat 0.5s), target 1.7 -> group round(1.7/0.5)=3 -> cut 1.5s
    assert any(abs(d - 1.5) < 1e-6 for d in durations)


@patch("src.spotify_beats.get_token")
@patch("src.spotify_beats._requests")
@patch("src.spotify_beats.time.sleep")
def test_spotify_429_exponential_backoff(mock_sleep: Mock, mock_requests: Mock, mock_get_token: Mock) -> None:
    from src.spotify_beats import _auth_get

    mock_get_token.return_value = "fake-token"
    mock_req_module = Mock()
    mock_requests.return_value = mock_req_module

    resp_429 = Mock()
    resp_429.status_code = 429
    resp_429.headers = {}
    resp_429.ok = False

    resp_200 = Mock()
    resp_200.status_code = 200
    resp_200.ok = True
    resp_200.json.return_value = {"success": True}

    mock_req_module.get.side_effect = [resp_429, resp_429, resp_429, resp_200]

    result = _auth_get("http://fake.url")
    assert result == {"success": True}
    assert mock_sleep.call_count == 3
    assert mock_sleep.call_args_list[0][0][0] == 1
    assert mock_sleep.call_args_list[1][0][0] == 2
    assert mock_sleep.call_args_list[2][0][0] == 4


@patch("src.spotify_beats.get_token")
@patch("src.spotify_beats._requests")
@patch("src.spotify_beats.time.sleep")
def test_spotify_429_retry_after_header(mock_sleep: Mock, mock_requests: Mock, mock_get_token: Mock) -> None:
    from src.spotify_beats import _auth_get

    mock_get_token.return_value = "fake-token"
    mock_req_module = Mock()
    mock_requests.return_value = mock_req_module

    resp_429 = Mock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "5"}
    resp_429.ok = False

    resp_200 = Mock()
    resp_200.status_code = 200
    resp_200.ok = True
    resp_200.json.return_value = {"success": True}

    mock_req_module.get.side_effect = [resp_429, resp_200]

    result = _auth_get("http://fake.url")
    assert result == {"success": True}
    assert mock_sleep.call_count == 1
    assert mock_sleep.call_args_list[0][0][0] == 5


@patch("src.spotify_beats.get_token")
@patch("src.spotify_beats._requests")
@patch("src.spotify_beats.time.sleep")
def test_spotify_429_max_retries_exceeded(
    mock_sleep: Mock, mock_requests: Mock, mock_get_token: Mock
) -> None:
    from src.spotify_beats import _auth_get

    mock_get_token.return_value = "fake-token"
    mock_req_module = Mock()
    mock_requests.return_value = mock_req_module

    resp_429 = Mock()
    resp_429.status_code = 429
    resp_429.headers = {}
    resp_429.ok = False

    mock_req_module.get.return_value = resp_429

    result = _auth_get("http://fake.url")
    assert result is None
    assert mock_sleep.call_count == 3


# --------------------------------------------------------------------------- #
# Naming / unique_path
# --------------------------------------------------------------------------- #
def test_unique_path_non_existent(tmp_path: Path) -> None:
    """Wenn die Zieldatei noch nicht existiert, wird stem + suffix direkt zurückgegeben."""
    res = unique_path(tmp_path, "reel", ".mp4")
    assert res == tmp_path / "reel.mp4"


def test_unique_path_single_collision(tmp_path: Path) -> None:
    """Wenn reel.mp4 bereits existiert, soll reel_2.mp4 zurückgegeben werden."""
    (tmp_path / "reel.mp4").touch()
    res = unique_path(tmp_path, "reel", ".mp4")
    assert res == tmp_path / "reel_2.mp4"


def test_unique_path_multiple_collisions(tmp_path: Path) -> None:
    """Wenn reel.mp4, reel_2.mp4, reel_3.mp4 existieren, wird reel_4.mp4 vergeben."""
    (tmp_path / "reel.mp4").touch()
    (tmp_path / "reel_2.mp4").touch()
    (tmp_path / "reel_3.mp4").touch()
    res = unique_path(tmp_path, "reel", ".mp4")
    assert res == tmp_path / "reel_4.mp4"


def test_unique_path_different_suffixes(tmp_path: Path) -> None:
    """Ebenso mit anderen Endungen/Suffixen (z. B. .jpg oder ohne Endung)."""
    (tmp_path / "cover.jpg").touch()
    res_jpg = unique_path(tmp_path, "cover", ".jpg")
    assert res_jpg == tmp_path / "cover_2.jpg"

    (tmp_path / "folder").touch()
    res_no_ext = unique_path(tmp_path, "folder", "")
    assert res_no_ext == tmp_path / "folder_2"


def test_dry_run_renders_without_writing(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from src.pipeline import ClipInfo, Segment, export_reel
    from src.styling import GradeConfig

    cfg = {"naming": {"folder_template": ""}, "video": {"audio": True}}
    base_out = tmp_path / "output"

    # Fake segment
    clip_path = tmp_path / "fake.mp4"
    clip_path.write_bytes(b"dummy")
    clip = ClipInfo(path=clip_path, duration=5.0)
    seg = Segment(clip=clip, start=0.0, end=2.0)

    grade = GradeConfig()

    # We mock render_segments so we don't need real video input
    with patch("src.pipeline.render_segments") as mock_render:
        mock_final = MagicMock()
        mock_final.duration = 2.0
        mock_audio = MagicMock()
        mock_final.audio = mock_audio
        mock_render.return_value = [mock_final]

        target = export_reel(
            segments=[seg],
            cfg=cfg,
            base_out=base_out,
            title="DryRunTest",
            subtitle="Subtitle",
            day="2026-09-11",
            grade=grade,
            when=1600000000.0,
            dry_run=True,
            errors_seen=[],
        )

        # In dry run mode, iter_frames should be called to force the pipeline to evaluate
        mock_final.iter_frames.assert_called_once()

        # And audio chunks should be evaluated too
        mock_audio.iter_chunks.assert_called_once()

        # But nothing should be written
        mock_final.write_videofile.assert_not_called()

        # Output directory should NOT exist (since folder_template is "" it won't create subdirs anyway, but base_out should not be created if it wasn't)
        assert target is not None
        assert not target.exists()


def test_watcher_duplicate_events_prevented_by_lock(tmp_path: Path) -> None:
    import threading
    import time
    from unittest.mock import patch

    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"debounce_seconds": 0.5, "batch_window": 0.5}}
    processor = IngestProcessor(cfg)
    target_path = tmp_path / "burst.mp4"
    target_path.touch()

    threads_spawned = 0
    orig_thread = threading.Thread

    def fake_thread(*args, **kwargs):
        nonlocal threads_spawned
        if kwargs.get("target").__name__ == "_settle_and_collect":
            threads_spawned += 1

            class Dummy:
                def start(self):
                    pass

                def join(self):
                    pass

            return Dummy()
        return orig_thread(*args, **kwargs)

    in_critical_section = False

    original_is_processed = processor.state.is_processed

    def slow_is_processed(p):
        nonlocal in_critical_section
        in_critical_section = True
        time.sleep(0.5)
        res = original_is_processed(p)
        in_critical_section = False
        return res

    processor.state.is_processed = slow_is_processed

    with patch("threading.Thread", side_effect=fake_thread):
        t1 = orig_thread(target=processor.on_file_event, args=(target_path,))
        t2 = orig_thread(target=processor.on_file_event, args=(target_path,))
        t1.start()
        time.sleep(0.1)
        t2.start()
        t1.join()
        t2.join()

    assert threads_spawned == 1
