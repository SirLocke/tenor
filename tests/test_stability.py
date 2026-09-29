"""Regressionstests für die Stabilitäts-Fixes.

Ohne diese Tests war der Watcher nach dem ersten Dateisystem-Event taub
(``IngestHandler`` griff auf ein nicht existierendes Attribut zu) - ein Fehler,
der im Betrieb nur als "der Watcher verarbeitet nichts mehr" auffällt.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from watchdog.events import FileSystemEvent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import DEFAULTS  # noqa: E402
from src.pipeline import _get_fallback_bpm, _safe_bpm, durations_from_bpm  # noqa: E402
from src.spotify_beats import extract_track_id  # noqa: E402
from src.watcher import (  # noqa: E402
    IngestHandler,
    IngestProcessor,
    WatcherState,
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
        "watcher": {**DEFAULTS["watcher"], "debounce_seconds": 0.1, "batch_window": 0.1},
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
        json.dumps({"processed": {"/good.mov": 1.0, "/bad.mov": "kein-float", "/nan.mov": float("nan")}}),
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
@pytest.mark.parametrize(
    "bpm", [60, 80, 90, 100, 110, 120, 124, 128, 132, 140, 160, 174, 200]
)
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
    cfg = {"cut": {}, "spotify": {"fallback_bpm": {"default": 90, "pop": 120, "pop remix": 140}}}
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
    cfg = {"cut": {}, "spotify": {"fallback_bpm": {"broken": "kein-float", "default": 128}}}
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

        def subclipped(self, start: float, end: float) -> "_FakePiece":
            self.requests.append((start, end))
            return _FakePiece(self, start, end)

    class _FakePiece:
        def __init__(self, source: _FakeSource, start: float, end: float) -> None:
            self.source = source
            self.start = start
            self.end = end
            self.duration = max(end - start, 1 / 30)

        def with_duration(self, value: float) -> "_FakePiece":
            self.duration = value
            return self

        def with_fps(self, _fps: int) -> "_FakePiece":
            return self

        def image_transform(self, _fn: Any) -> "_FakePiece":
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
    pipeline_module._open_source = lambda _p: fake  # type: ignore[assignment]
    original_crop = pipeline_module.center_crop_resize
    pipeline_module.center_crop_resize = lambda piece, _w, _h: piece  # type: ignore[assignment]
    try:
        with pytest.raises(ValueError, match="außerhalb"):
            build_segment_clip(segment, {}, GradeConfig(), None, fps=30, width=1080, height=1920)
    finally:
        pipeline_module._open_source = original  # type: ignore[assignment]
        pipeline_module.center_crop_resize = original_crop  # type: ignore[assignment]
