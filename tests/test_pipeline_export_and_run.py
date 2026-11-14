"""Regressionstests für ``pipeline.export_reel()`` und ``pipeline.run()``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from src.metadata import ClipInfo
from src.pipeline import Segment, export_reel, run
from src.styling import GradeConfig


def _make_clip(
    tmp_path: Path,
    name: str,
    day: str = "2026-09-27",
    *,
    place: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> ClipInfo:
    path = tmp_path / name
    path.write_bytes(b"dummy")
    year, month, dom = (int(part) for part in day.split("-"))
    return ClipInfo(
        path=path,
        created=datetime(year, month, dom, 12, 0, tzinfo=UTC),
        duration=5.0,
        width=1920,
        height=1080,
        place=place,
        latitude=latitude,
        longitude=longitude,
    )


def _base_cfg() -> dict[str, Any]:
    return {
        "naming": {"folder_template": "{day}", "template": "{slug}", "extension": ".mp4"},
        "video": {"audio": False, "crf": 19, "preset": "medium", "cover": True},
        "quality": {"enabled": False},
        "cut": {"fallback_bpm": 120},
    }


def test_export_reel_without_errors_seen_handles_low_disk_space(tmp_path: Path) -> None:
    cfg = _base_cfg()
    clip = _make_clip(tmp_path, "a.mp4")
    seg = Segment(clip=clip, start=0.0, end=2.0)

    with (
        patch("src.pipeline.check_disk_space", return_value=False),
        patch("src.pipeline.render_segments") as mock_render,
    ):
        target = export_reel(
            [seg],
            cfg,
            tmp_path / "out",
            title="Test",
            subtitle="",
            day="2026-09-27",
            grade=GradeConfig(),
            when=1700000000.0,
        )

    assert target is None
    mock_render.assert_not_called()


def test_export_reel_without_errors_seen_handles_empty_render(tmp_path: Path) -> None:
    cfg = _base_cfg()
    clip = _make_clip(tmp_path, "a.mp4")
    seg = Segment(clip=clip, start=0.0, end=2.0)

    def _fail_render(*_args: Any, **kwargs: Any) -> list[Any]:
        kwargs["on_error"].append("Cut 1 fehlgeschlagen")
        return []

    with (
        patch("src.pipeline.check_disk_space", return_value=True),
        patch("src.pipeline.make_overlay_array", return_value=None),
        patch("src.pipeline.render_segments", side_effect=_fail_render),
    ):
        target = export_reel(
            [seg],
            cfg,
            tmp_path / "out",
            title="Test",
            subtitle="",
            day="2026-09-27",
            grade=GradeConfig(),
            when=1700000000.0,
        )

    assert target is None


def test_export_reel_without_errors_seen_propagates_partial_errors_and_write_failure(
    tmp_path: Path,
) -> None:
    cfg = _base_cfg()
    clip = _make_clip(tmp_path, "a.mp4")
    seg = Segment(clip=clip, start=0.0, end=2.0)

    rendered_clip = MagicMock()
    rendered_clip.audio = None
    rendered_clip.tenor_sources = []
    rendered_clip.tenor_music_reader = None
    rendered_clip.write_videofile.side_effect = RuntimeError("ffmpeg crashed")

    def _partial_render(*_args: Any, **kwargs: Any) -> list[Any]:
        kwargs["on_error"].append("Cut 2 übersprungen")
        return [rendered_clip]

    with (
        patch("src.pipeline.check_disk_space", return_value=True),
        patch("src.pipeline.make_overlay_array", return_value=None),
        patch("src.pipeline.render_segments", side_effect=_partial_render),
        patch("src.pipeline._cleanup_ffmpeg_children"),
    ):
        target = export_reel(
            [seg],
            cfg,
            tmp_path / "out",
            title="Test",
            subtitle="",
            day="2026-09-27",
            grade=GradeConfig(),
            when=1700000000.0,
        )

    assert target is None
    rendered_clip.close.assert_called_once()


def test_export_reel_lossless_fallback_to_standard_and_closes_both_clips(tmp_path: Path) -> None:
    cfg = _base_cfg()
    cfg["video"]["lossless"] = True
    cfg["video"]["cover"] = False
    clip = _make_clip(tmp_path, "a.mp4")
    seg = Segment(clip=clip, start=0.0, end=2.0)
    music_path = tmp_path / "song.mp3"
    music_path.write_bytes(b"mp3")

    src_reader = MagicMock()
    rendered_clip = MagicMock()
    rendered_clip.audio = None
    rendered_clip.tenor_sources = [src_reader]
    rendered_clip.tenor_music_reader = None

    music_reader = MagicMock()
    mixed_clip = MagicMock()
    mixed_clip.audio = MagicMock()
    # Real moviepy ``with_audio()`` does not copy custom attributes onto the wrapper.
    mixed_clip.tenor_sources = None
    mixed_clip.tenor_music_reader = music_reader

    write_calls: list[dict[str, Any]] = []

    def _write_videofile(path_str: str, **kwargs: Any) -> None:
        write_calls.append(kwargs)
        partial_path = Path(path_str)
        temp_audio_path = Path(kwargs["temp_audiofile"])
        temp_audio_path.write_bytes(b"temp-audio")
        if len(write_calls) == 1:
            partial_path.write_bytes(b"broken-lossless")
            raise RuntimeError("lossless failed")
        partial_path.write_bytes(b"valid-standard")

    mixed_clip.write_videofile.side_effect = _write_videofile

    out_dir = tmp_path / "out" / "2026-09-27"
    out_dir.mkdir(parents=True)
    beat_cache = out_dir / "track.beat-cache"
    beat_cache.write_text("cached")

    with (
        patch("src.pipeline.check_disk_space", return_value=True),
        patch("src.pipeline.make_overlay_array", return_value=None),
        patch("src.pipeline.render_segments", return_value=[rendered_clip]),
        patch("src.pipeline.mix_music", return_value=mixed_clip),
        patch("src.pipeline.probe_duration", return_value=2.0),
        patch("src.pipeline.has_audio_stream", return_value=True),
        patch("src.pipeline.scan_nextcloud"),
        patch("src.pipeline._cleanup_ffmpeg_children"),
    ):
        target = export_reel(
            [seg],
            cfg,
            tmp_path / "out",
            title="Test",
            subtitle="",
            day="2026-09-27",
            grade=GradeConfig(),
            when=1700000000.0,
            music=music_path,
        )

    assert target is not None
    assert target.is_file()
    assert target.read_bytes() == b"valid-standard"
    assert len(write_calls) == 2
    assert write_calls[0]["preset"] == "ultrafast"
    assert "-crf" in write_calls[0]["ffmpeg_params"]
    assert write_calls[0]["ffmpeg_params"][write_calls[0]["ffmpeg_params"].index("-crf") + 1] == "0"
    assert write_calls[1]["preset"] == "medium"
    assert not beat_cache.exists()
    assert not list(out_dir.glob("*.part*"))

    rendered_clip.close.assert_called_once()
    mixed_clip.close.assert_called_once()
    src_reader.close.assert_called_once()
    music_reader.close.assert_called_once()


def test_export_reel_cleans_temp_files_on_failure_and_base_exception(tmp_path: Path) -> None:
    cfg = _base_cfg()
    cfg["video"]["audio"] = True
    clip = _make_clip(tmp_path, "a.mp4")
    seg = Segment(clip=clip, start=0.0, end=2.0)

    out_dir = tmp_path / "out" / "2026-09-27"
    out_dir.mkdir(parents=True)

    for exc_to_raise in (RuntimeError("disk io"), KeyboardInterrupt()):
        beat_cache = out_dir / "song.beat-cache"
        beat_cache.write_text("cache")

        rendered_clip = MagicMock()
        rendered_clip.audio = MagicMock()
        rendered_clip.tenor_sources = []
        rendered_clip.tenor_music_reader = None

        def _failing_write(path_str: str, _exc: BaseException = exc_to_raise, **kwargs: Any) -> None:
            Path(path_str).write_bytes(b"partial")
            Path(kwargs["temp_audiofile"]).write_bytes(b"partial-audio")
            raise _exc

        rendered_clip.write_videofile.side_effect = _failing_write

        with (
            patch("src.pipeline.check_disk_space", return_value=True),
            patch("src.pipeline.make_overlay_array", return_value=None),
            patch("src.pipeline.render_segments", return_value=[rendered_clip]),
            patch("src.pipeline._cleanup_ffmpeg_children"),
        ):
            if isinstance(exc_to_raise, KeyboardInterrupt):
                with pytest.raises(KeyboardInterrupt):
                    export_reel(
                        [seg],
                        cfg,
                        tmp_path / "out",
                        title="Test",
                        subtitle="",
                        day="2026-09-27",
                        grade=GradeConfig(),
                        when=1700000000.0,
                    )
            else:
                errors: list[str] = []
                res = export_reel(
                    [seg],
                    cfg,
                    tmp_path / "out",
                    title="Test",
                    subtitle="",
                    day="2026-09-27",
                    grade=GradeConfig(),
                    when=1700000000.0,
                    errors_seen=errors,
                )
                assert res is None
                assert len(errors) == 1
                assert "disk io" in errors[0]

        assert not beat_cache.exists()
        assert not list(out_dir.glob("*.part*"))
        rendered_clip.close.assert_called_once()


def test_run_multiday_plans_once_per_day_and_populates_result_segments(tmp_path: Path) -> None:
    ingest = tmp_path / "ingest" / "Madeira"
    ingest.mkdir(parents=True)
    out_base = tmp_path / "output"

    clip_day1 = _make_clip(
        ingest, "d1.mov", day="2026-09-27", place="Funchal", latitude=32.6, longitude=-16.9
    )
    clip_day2 = _make_clip(ingest, "d2.mov", day="2026-09-28", place="Porto Moniz")

    cfg = _base_cfg()
    cfg["ingest_dir"] = str(tmp_path / "ingest")
    cfg["output_dir"] = str(out_base)

    build_calls: list[list[str]] = []

    def _fake_build_segments(day_clips: list[ClipInfo], *_args: Any, **_kwargs: Any) -> list[Segment]:
        build_calls.append([c.name for c in day_clips])
        return [Segment(clip=c, start=0.0, end=2.5) for c in day_clips]

    def _fake_export_reel(
        day_segments: list[Segment],
        _cfg: Any,
        base_out: Path,
        *,
        day: str,
        **_kwargs: Any,
    ) -> Path:
        out_file = base_out / f"{day}.mp4"
        out_file.write_bytes(b"video")
        out_file.with_suffix(".jpg").write_bytes(b"cover")
        assert len(day_segments) == 1
        return out_file

    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.Geocoder", return_value=SimpleNamespace(save_cache=lambda: None)),
        patch("src.pipeline.collect_clips", return_value=[clip_day1, clip_day2]),
        patch("src.pipeline.is_valid_video", return_value=True),
        patch("src.pipeline._resolve_beats", return_value=([2.5], "fallback:120bpm", "note")),
        patch("src.pipeline._resolve_music_file", return_value=None),
        patch("src.pipeline.analyse_clips", return_value={}),
        patch("src.pipeline.build_segments", side_effect=_fake_build_segments),
        patch("src.pipeline.export_reel", side_effect=_fake_export_reel),
    ):
        result = run([str(tmp_path / "ingest")])

    assert build_calls == [["d1.mov"], ["d2.mov"]]
    assert [s.clip.name for s in result.segments] == ["d1.mov", "d2.mov"]
    assert result.duration == pytest.approx(5.0)
    assert len(result.outputs) == 2
    assert result.output == result.outputs[0]
    assert len(result.covers) == 2
    assert result.cover == result.covers[0]
    assert result.title == "Madeira"
    assert result.subtitle == "Funchal  ·  2026-09-27"
    assert result.trip == "Madeira"
    assert result.place == "Funchal"
    assert result.beat_source == "fallback:120bpm"
    assert result.beat_note == "note"


def test_run_accumulates_only_exported_day_segments_when_one_day_fails(tmp_path: Path) -> None:
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    out_base = tmp_path / "output"

    clip_day1 = _make_clip(ingest, "d1.mov", day="2026-09-27")
    clip_day2 = _make_clip(ingest, "d2.mov", day="2026-09-28")

    cfg = _base_cfg()
    cfg["ingest_dir"] = str(ingest)
    cfg["output_dir"] = str(out_base)

    def _fake_build_segments(day_clips: list[ClipInfo], *_args: Any, **_kwargs: Any) -> list[Segment]:
        return [Segment(clip=c, start=0.0, end=2.0) for c in day_clips]

    def _fake_export_reel(
        _day_segments: list[Segment],
        _cfg: Any,
        base_out: Path,
        *,
        day: str,
        **_kwargs: Any,
    ) -> Path | None:
        if day == "2026-09-28":
            return None
        out_file = base_out / f"{day}.mp4"
        out_file.write_bytes(b"video")
        return out_file

    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.Geocoder", return_value=SimpleNamespace(save_cache=lambda: None)),
        patch("src.pipeline.collect_clips", return_value=[clip_day1, clip_day2]),
        patch("src.pipeline.is_valid_video", return_value=True),
        patch("src.pipeline._resolve_beats", return_value=([2.0], "fallback:120bpm", "")),
        patch("src.pipeline._resolve_music_file", return_value=None),
        patch("src.pipeline.analyse_clips", return_value={}),
        patch("src.pipeline.build_segments", side_effect=_fake_build_segments),
        patch("src.pipeline.export_reel", side_effect=_fake_export_reel),
    ):
        result = run([str(ingest)])

    assert [s.clip.name for s in result.segments] == ["d1.mov"]
    assert result.duration == pytest.approx(2.0)
    assert len(result.outputs) == 1


def test_run_empty_plan_or_dry_run_does_not_create_output_dir(tmp_path: Path) -> None:
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    out_base = tmp_path / "output_never_created"

    clip = _make_clip(ingest, "a.mov", day="2026-09-27")
    cfg = _base_cfg()
    cfg["ingest_dir"] = str(ingest)
    cfg["output_dir"] = str(out_base)

    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.Geocoder", return_value=SimpleNamespace(save_cache=lambda: None)),
        patch("src.pipeline.collect_clips", return_value=[clip]),
        patch("src.pipeline.is_valid_video", return_value=True),
        patch("src.pipeline._resolve_beats", return_value=([2.0], "fallback:120bpm", "")),
        patch("src.pipeline._resolve_music_file", return_value=None),
        patch("src.pipeline.analyse_clips", return_value={}),
        patch("src.pipeline.build_segments", return_value=[]),
    ):
        empty_result = run([str(ingest)])

    assert empty_result.segments == []
    assert empty_result.outputs == []
    assert not out_base.exists()

    planned_seg = Segment(clip=clip, start=0.0, end=2.0)
    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.Geocoder", return_value=SimpleNamespace(save_cache=lambda: None)),
        patch("src.pipeline.collect_clips", return_value=[clip]),
        patch("src.pipeline.is_valid_video", return_value=True),
        patch("src.pipeline._resolve_beats", return_value=([2.0], "fallback:120bpm", "")),
        patch("src.pipeline._resolve_music_file", return_value=None),
        patch("src.pipeline.analyse_clips", return_value={}),
        patch("src.pipeline.build_segments", return_value=[planned_seg]),
        patch("src.pipeline.export_reel", return_value=out_base / "2026-09-27" / "dry.mp4"),
    ):
        dry_result = run([str(ingest)], dry_run=True)

    assert not out_base.exists()
    assert dry_result.outputs == []
    assert dry_result.duration == pytest.approx(2.0)
    assert dry_result.segments == [planned_seg]
