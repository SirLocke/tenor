"""Performance-Tests für Subprozess-Deduplizierung und Cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from src.metadata import ClipInfo
from src.pipeline import (
    Segment,
    _open_source,
    _probe_pix_fmt,
    _transcode_to_8bit,
    export_reel,
    has_audio_stream,
    is_valid_video,
    probe_duration,
    render_segments,
    run,
    scan_nextcloud,
    write_cover,
)
from src.styling import GradeConfig


class _FakePiece:
    def __init__(self, duration: float = 1.5) -> None:
        self.duration = duration
        self.audio = None

    def with_duration(self, value: float) -> _FakePiece:
        self.duration = value
        return self

    def image_transform(self, _fn: Any) -> _FakePiece:
        return self

    def with_audio(self, _audio: Any) -> _FakePiece:
        return self

    def with_fps(self, _fps: int) -> _FakePiece:
        return self

    def close(self) -> None:
        pass


class _FakeVideoClip:
    def __init__(self, path: str, *, close_log: list[str] | None = None) -> None:
        self.path = path
        self.duration = 10.0
        self.size = (1920, 1080)
        self.audio = None
        self.closed = False
        self._close_log = close_log

    def subclipped(self, start: float, end: float) -> _FakePiece:
        return _FakePiece(max(end - start, 0.1))

    def close(self) -> None:
        self.closed = True
        if self._close_log is not None:
            self._close_log.append(f"close:{self.path}")


class _FakeFinalClip:
    def __init__(self, *, temp_check: Path | None = None, events: list[str] | None = None) -> None:
        self.duration = 4.5
        self.audio = None
        self._temp_check = temp_check
        self._events = events

    def without_audio(self) -> _FakeFinalClip:
        return self

    def write_videofile(self, path: str, **_kwargs: Any) -> None:
        if self._temp_check is not None and self._events is not None:
            self._events.append(f"write:exists={self._temp_check.exists()}")
        Path(path).write_bytes(b"fake-video")

    def close(self) -> None:
        if self._events is not None:
            self._events.append("close_final")


def test_run_skips_is_valid_video_when_has_video_recorded(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"dummy")
    info = ClipInfo(
        path=clip_path,
        duration=3.0,
        width=1920,
        height=1080,
        fps=30.0,
        extra={"has_video": True, "pix_fmt": "yuv420p"},
    )
    with (
        patch("src.pipeline.collect_clips", return_value=[info]),
        patch("src.pipeline.is_valid_video") as mock_valid,
    ):
        res = run([str(clip_path)], limit=1, dry_run=True)

    mock_valid.assert_not_called()
    assert res.segments


def test_run_calls_is_valid_video_when_has_video_missing(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"dummy")
    info = ClipInfo(
        path=clip_path,
        duration=3.0,
        width=1920,
        height=1080,
        fps=30.0,
    )
    with (
        patch("src.pipeline.collect_clips", return_value=[info]),
        patch("src.pipeline.is_valid_video", return_value=True) as mock_valid,
    ):
        res = run([str(clip_path)], limit=1, dry_run=True)

    mock_valid.assert_called_once_with(clip_path)
    assert res.segments


def test_render_segments_deduplicates_10bit_transcode_and_cleans_up_on_close(tmp_path: Path) -> None:
    src_path = tmp_path / "hevc10.mov"
    src_path.write_bytes(b"raw")
    clip = ClipInfo(
        path=src_path,
        duration=10.0,
        width=1920,
        height=1080,
        fps=30.0,
        extra={"has_video": True, "pix_fmt": "yuv420p10le"},
    )
    segments = [
        Segment(clip=clip, start=0.0, end=1.5),
        Segment(clip=clip, start=2.0, end=3.5),
        Segment(clip=clip, start=4.0, end=5.5),
    ]

    transcoded_tmp = tmp_path / "shared_8bit.mp4"
    opened_clips: list[_FakeVideoClip] = []
    events: list[str] = []

    def fake_transcode(_path: Path, *, with_audio: bool) -> Path:
        assert with_audio is False
        transcoded_tmp.write_bytes(b"8bit")
        return transcoded_tmp

    def fake_video_clip(path: str, **_kwargs: Any) -> _FakeVideoClip:
        vc = _FakeVideoClip(path, close_log=events)
        opened_clips.append(vc)
        return vc

    fake_final = _FakeFinalClip(temp_check=transcoded_tmp, events=events)

    with (
        patch("src.pipeline._probe_pix_fmt") as mock_probe,
        patch("src.pipeline._transcode_to_8bit", side_effect=fake_transcode) as mock_transcode,
        patch("moviepy.VideoFileClip", side_effect=fake_video_clip),
        patch("moviepy.concatenate_videoclips", return_value=fake_final),
        patch("src.pipeline.center_crop_resize", side_effect=lambda piece, _w, _h: piece),
        patch("src.pipeline.make_grader", return_value=None),
    ):
        rendered = render_segments(segments, {}, grade=GradeConfig(), overlay_png=None)
        assert len(rendered) == 1
        final = rendered[0]

        mock_probe.assert_not_called()
        mock_transcode.assert_called_once_with(src_path, with_audio=False)
        assert len(opened_clips) == 3
        assert len({id(c) for c in opened_clips}) == 3
        assert all(c.path == str(transcoded_tmp) for c in opened_clips)
        assert transcoded_tmp.exists()
        assert final.tenor_temp_files == [transcoded_tmp]

        final.close()

    assert not transcoded_tmp.exists()
    assert all(c.closed for c in opened_clips)
    assert events.index("close_final") < events.index(f"close:{transcoded_tmp}")


def test_render_segments_deduplicates_probe_when_pix_fmt_missing(tmp_path: Path) -> None:
    src_path = tmp_path / "hevc10_unknown.mov"
    src_path.write_bytes(b"raw")
    clip = ClipInfo(path=src_path, duration=10.0)
    segments = [
        Segment(clip=clip, start=0.0, end=1.5),
        Segment(clip=clip, start=2.0, end=3.5),
    ]
    transcoded_tmp = tmp_path / "shared_p010.mp4"

    def fake_transcode(_path: Path, *, with_audio: bool) -> Path:
        transcoded_tmp.write_bytes(b"8bit")
        return transcoded_tmp

    with (
        patch("src.pipeline._probe_pix_fmt", return_value="p010le") as mock_probe,
        patch("src.pipeline._transcode_to_8bit", side_effect=fake_transcode) as mock_transcode,
        patch("moviepy.VideoFileClip", side_effect=lambda p, **_kw: _FakeVideoClip(p)),
        patch("moviepy.concatenate_videoclips", return_value=_FakeFinalClip()),
        patch("src.pipeline.center_crop_resize", side_effect=lambda piece, _w, _h: piece),
        patch("src.pipeline.make_grader", return_value=None),
    ):
        rendered = render_segments(segments, {}, grade=GradeConfig(), overlay_png=None)
        assert len(rendered) == 1
        mock_probe.assert_called_once_with(src_path)
        mock_transcode.assert_called_once_with(src_path, with_audio=False)
        rendered[0].close()

    assert not transcoded_tmp.exists()


def test_render_segments_unlinks_temp_files_when_all_cuts_fail(tmp_path: Path) -> None:
    src_path = tmp_path / "hevc10.mov"
    src_path.write_bytes(b"raw")
    clip = ClipInfo(path=src_path, duration=10.0, extra={"pix_fmt": "yuv420p10le"})
    segments = [Segment(clip=clip, start=0.0, end=1.5)]
    transcoded_tmp = tmp_path / "shared_fail.mp4"

    def fake_transcode(_path: Path, *, with_audio: bool) -> Path:
        transcoded_tmp.write_bytes(b"8bit")
        return transcoded_tmp

    with (
        patch("src.pipeline._transcode_to_8bit", side_effect=fake_transcode),
        patch("src.pipeline.build_segment_clip", side_effect=ValueError("bad cut")),
    ):
        errors: list[str] = []
        rendered = render_segments(segments, {}, grade=GradeConfig(), overlay_png=None, on_error=errors)

    assert rendered == []
    assert len(errors) == 1
    assert not transcoded_tmp.exists()


def test_render_segments_unlinks_temp_files_on_concatenate_exception(tmp_path: Path) -> None:
    src_path = tmp_path / "hevc10.mov"
    src_path.write_bytes(b"raw")
    clip = ClipInfo(path=src_path, duration=10.0, extra={"pix_fmt": "yuv420p10le"})
    segments = [Segment(clip=clip, start=0.0, end=1.5)]
    transcoded_tmp = tmp_path / "shared_concat_err.mp4"

    def fake_transcode(_path: Path, *, with_audio: bool) -> Path:
        transcoded_tmp.write_bytes(b"8bit")
        return transcoded_tmp

    with (
        patch("src.pipeline._transcode_to_8bit", side_effect=fake_transcode),
        patch("moviepy.VideoFileClip", side_effect=lambda p, **_kw: _FakeVideoClip(p)),
        patch("moviepy.concatenate_videoclips", side_effect=RuntimeError("concat failed")),
        patch("src.pipeline.center_crop_resize", side_effect=lambda piece, _w, _h: piece),
        patch("src.pipeline.make_grader", return_value=None),
        pytest.raises(RuntimeError, match="concat failed"),
    ):
        render_segments(segments, {}, grade=GradeConfig(), overlay_png=None)

    assert not transcoded_tmp.exists()


def test_export_reel_unlinks_temp_files_after_write_and_source_close(tmp_path: Path) -> None:
    src_path = tmp_path / "hevc10.mov"
    src_path.write_bytes(b"raw")
    clip = ClipInfo(path=src_path, duration=10.0, extra={"has_video": True, "pix_fmt": "yuv420p10le"})
    segments = [
        Segment(clip=clip, start=0.0, end=1.5),
        Segment(clip=clip, start=2.0, end=3.5),
    ]
    transcoded_tmp = tmp_path / "shared_export.mp4"
    events: list[str] = []

    def fake_transcode(_path: Path, *, with_audio: bool) -> Path:
        transcoded_tmp.write_bytes(b"8bit")
        return transcoded_tmp

    fake_final = _FakeFinalClip(temp_check=transcoded_tmp, events=events)
    cfg = {"naming": {"folder_template": ""}, "video": {"audio": False, "cover": False}}
    base_out = tmp_path / "output"

    with (
        patch("src.pipeline._transcode_to_8bit", side_effect=fake_transcode),
        patch("moviepy.VideoFileClip", side_effect=lambda p, **_kw: _FakeVideoClip(p, close_log=events)),
        patch("moviepy.concatenate_videoclips", return_value=fake_final),
        patch("src.pipeline.center_crop_resize", side_effect=lambda piece, _w, _h: piece),
        patch("src.pipeline.make_grader", return_value=None),
        patch("src.pipeline.make_overlay_array", return_value=None),
        patch("src.pipeline.probe_duration", return_value=3.0),
        patch("src.pipeline._cleanup_ffmpeg_children"),
    ):
        target = export_reel(
            segments,
            cfg,
            base_out,
            title="Test",
            subtitle="Sub",
            day="2026-09-11",
            grade=GradeConfig(),
            when=1600000000.0,
            dry_run=False,
            no_cover=True,
            errors_seen=[],
        )

    assert target is not None and target.exists()
    assert "write:exists=True" in events
    assert not transcoded_tmp.exists()
    assert events.count(f"close:{transcoded_tmp}") >= 2


def test_transcode_to_8bit_unlinks_partial_temp_on_failure(tmp_path: Path) -> None:
    created_paths: list[Path] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> None:
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"partial")
        created_paths.append(out_path)
        raise subprocess.CalledProcessError(1, cmd)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("src.pipeline._cleanup_ffmpeg_children") as mock_cleanup,
        pytest.raises(subprocess.CalledProcessError),
    ):
        _transcode_to_8bit(tmp_path / "broken.mov", with_audio=False)

    assert len(created_paths) == 1
    assert not created_paths[0].exists()
    mock_cleanup.assert_called_once()


def test_open_source_with_prepared_path_skips_probe_transcode_and_unlink(tmp_path: Path) -> None:
    prepared = tmp_path / "prepared.mp4"
    prepared.write_bytes(b"8bit")
    fake_vc = _FakeVideoClip(str(prepared))

    with (
        patch("src.pipeline._probe_pix_fmt") as mock_probe,
        patch("src.pipeline._transcode_to_8bit") as mock_transcode,
        patch("moviepy.VideoFileClip", return_value=fake_vc) as mock_vfc,
    ):
        opened = _open_source(tmp_path / "orig.mov", with_audio=True, prepared_path=prepared)
        mock_probe.assert_not_called()
        mock_transcode.assert_not_called()
        mock_vfc.assert_called_once_with(str(prepared), audio=True)
        opened.close()

    assert prepared.exists()
    assert fake_vc.closed


def test_open_source_direct_with_pix_fmt_skips_probe_and_unlinks_on_close(tmp_path: Path) -> None:
    transcoded_tmp = tmp_path / "direct_8bit.mp4"
    transcoded_tmp.write_bytes(b"8bit")

    with (
        patch("src.pipeline._probe_pix_fmt") as mock_probe,
        patch("src.pipeline._transcode_to_8bit", return_value=transcoded_tmp) as mock_transcode,
        patch("moviepy.VideoFileClip", side_effect=lambda p, **_kw: _FakeVideoClip(p)),
    ):
        opened = _open_source(tmp_path / "orig.mov", with_audio=False, pix_fmt="yuv420p10le")
        mock_probe.assert_not_called()
        mock_transcode.assert_called_once_with(tmp_path / "orig.mov", with_audio=False)
        assert transcoded_tmp.exists()
        opened.close()

    assert not transcoded_tmp.exists()


def test_cleanup_ffmpeg_children_skipped_on_clean_subprocess_run(tmp_path: Path) -> None:
    video = tmp_path / "ok.mp4"
    video.write_bytes(b"ok")
    cover = tmp_path / "cover.jpg"

    def fake_run(cmd: list[str], **_kwargs: Any) -> MagicMock:
        if cmd[0] == "ffmpeg" and "-frames:v" in cmd:
            cover.write_bytes(b"jpg")
        if cmd[0] == "ffmpeg" and cmd[-1].endswith(".mp4"):
            Path(cmd[-1]).write_bytes(b"mp4")
        # is_valid_video: -select_streams v:0 -show_entries stream=codec_type:format=duration
        if "v:0" in cmd and "stream=codec_type:format=duration" in cmd:
            return MagicMock(stdout=b"video\n3.5\n", stderr=b"", returncode=0)
        # has_audio_stream: -select_streams a:0 -show_entries stream=codec_type
        if "a:0" in cmd and "stream=codec_type" in cmd:
            return MagicMock(stdout=b"audio\n", stderr=b"", returncode=0)
        # probe_duration: -show_entries format=duration (no stream=codec_type)
        if "format=duration" in cmd and "stream=codec_type" not in cmd:
            return MagicMock(stdout=b"3.5\n", stderr=b"", returncode=0)
        # _probe_pix_fmt: -show_entries stream=pix_fmt
        if "stream=pix_fmt" in cmd:
            return MagicMock(stdout=b"yuv420p\n", stderr=b"", returncode=0)
        if "-show_entries" in cmd and "stream=codec_type" in cmd and "a:0" in cmd:
            return MagicMock(stdout=b"audio\n", stderr=b"", returncode=0)
        if "-show_entries" in cmd and "format=duration" in cmd:
            return MagicMock(stdout=b"3.5\n", stderr=b"", returncode=0)
        if "-show_entries" in cmd and "stream=pix_fmt" in cmd:
            return MagicMock(stdout=b"yuv420p\n", stderr=b"", returncode=0)
        return MagicMock(stdout=b"", stderr=b"", returncode=0)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("src.pipeline._cleanup_ffmpeg_children") as mock_cleanup,
    ):
        assert is_valid_video(video) is True
        assert has_audio_stream(video) is True
        assert probe_duration(video) == 3.5
        assert _probe_pix_fmt(video) == "yuv420p"
        tmp_out = _transcode_to_8bit(video, with_audio=False)
        tmp_out.unlink(missing_ok=True)
        assert write_cover(video, cover) == cover
        scan_nextcloud({"nextcloud": {"enabled": True, "scan_cmd": "echo {output_dir}"}}, tmp_path)

    mock_cleanup.assert_not_called()


def test_cleanup_ffmpeg_children_called_on_subprocess_timeout(tmp_path: Path) -> None:
    video = tmp_path / "hang.mp4"
    video.write_bytes(b"hang")
    cover = tmp_path / "cover.jpg"

    with (
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=30)),
        patch("src.pipeline._cleanup_ffmpeg_children") as mock_cleanup,
    ):
        assert is_valid_video(video) is False
        assert has_audio_stream(video) is False
        assert probe_duration(video) is None
        with pytest.raises(ValueError, match="ffprobe timeout"):
            _probe_pix_fmt(video)
        assert write_cover(video, cover) is None
        scan_nextcloud({"nextcloud": {"enabled": True, "scan_cmd": "echo {output_dir}"}}, tmp_path)

    assert mock_cleanup.call_count == 6
