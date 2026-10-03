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
from unittest.mock import patch
from types import SimpleNamespace
from typing import Any

import pytest
from watchdog.events import FileSystemEvent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import DEFAULTS  # noqa: E402
from src.pipeline import (  # noqa: E402
    _get_fallback_bpm,
    _pick_music,
    _safe_bpm,
    build_parser,
    durations_from_bpm,
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
        json.dumps(
            {
                "processed": {
                    "/good.mov": 1.0,
                    "/bad.mov": "kein-float",
                    "/nan.mov": float("nan"),
                }
            }
        ),
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
    assert (
        durations_from_bpm(bpm, {"target_min": 1.4, "target_max": 2.0, "target": 1.7})
        == []
    )


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

    clips = collect_clips(
        [tmp_path], extensions=[".mov"], ignore_dirs=["imgest"], recursive=True
    )
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
    pipeline_module._open_source = lambda _p, **_kw: fake  # type: ignore[assignment]
    original_crop = pipeline_module.center_crop_resize
    pipeline_module.center_crop_resize = lambda piece, _w, _h: piece  # type: ignore[assignment]
    try:
        with pytest.raises(ValueError, match="außerhalb"):
            build_segment_clip(
                segment, {}, GradeConfig(), None, fps=30, width=1080, height=1920
            )
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
        return SimpleNamespace(
            output=None, outputs=[], covers=[], segments=[], errors=[]
        )

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
        return SimpleNamespace(
            ok=True, output=None, outputs=[], covers=[], segments=[], errors=[]
        )

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
        pipeline_module._fetch_spotify_beats, pipeline_module.detect_beat_durations = (
            originals  # type: ignore[assignment]
        )

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
        _durations, source, _note = pipeline_module._resolve_beats(
            str(song), cfg, cfg["cut"]
        )
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
        durations = pipeline_module.detect_beat_durations(dummy_music, {"target": 1.7, "target_min": 1.4, "target_max": 2.0})

    assert len(durations) > 0
    assert all(1.4 <= d <= 2.0 for d in durations)


from unittest.mock import Mock


@patch("src.spotify_beats.get_token")
@patch("src.spotify_beats._requests")
@patch("src.spotify_beats.time.sleep")
def test_spotify_429_exponential_backoff(
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
def test_spotify_429_retry_after_header(
    mock_sleep: Mock, mock_requests: Mock, mock_get_token: Mock
) -> None:
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
