import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.metadata import (
    ClipInfo,
    _dms_to_deg,
    _to_float,
    collect_clips,
    embedded_datetime,
    ffprobe_json,
)


def test_clipinfo_properties_with_created():
    path = Path("test_video.mp4")
    created_dt = datetime(2023, 10, 15, 14, 30, 45, tzinfo=UTC)
    clip = ClipInfo(path=path, created=created_dt)

    assert clip.name == "test_video.mp4"
    assert clip.sort_key == (created_dt.timestamp(), "test_video.mp4")
    assert clip.date_label == "2023-10-15"
    assert clip.time_label == "143045"

    d = clip.to_dict()
    assert d["path"] == "test_video.mp4"
    assert d["created"] == created_dt.isoformat()


@patch.object(Path, "stat")
def test_clipinfo_properties_with_mtime(mock_stat):
    path = Path("test_video.mp4")
    # Mocking os.stat_result
    mock_stat.return_value.st_mtime = 1697380245.0  # 2023-10-15 14:30:45 UTC
    clip = ClipInfo(path=path, created=None)

    assert clip.name == "test_video.mp4"
    assert clip.sort_key == (1697380245.0, "test_video.mp4")
    assert clip.date_label == "2023-10-15"
    assert clip.time_label == "143045"

    d = clip.to_dict()
    assert d["created"] is None


@patch.object(Path, "stat")
def test_clipinfo_properties_no_mtime(mock_stat):
    path = Path("test_video.mp4")
    mock_stat.side_effect = OSError("File not found")
    clip = ClipInfo(path=path, created=None)

    assert clip.sort_key == (0.0, "test_video.mp4")
    assert clip.date_label == ""
    assert clip.time_label == ""


@patch.object(Path, "stat")
def test_clipinfo_mtime_error(mock_stat):
    path = Path("test_video.mp4")
    mock_stat.side_effect = OSError("File not found")
    clip = ClipInfo(path=path, created=None)

    assert clip._mtime() is None


def test_to_float():
    assert _to_float(42.5) == 42.5
    assert _to_float("42.5") == 42.5
    assert _to_float(42) == 42.0

    # Invalid values should return NaN
    assert math.isnan(_to_float(None))
    assert math.isnan(_to_float("invalid"))
    assert math.isnan(_to_float([]))


def test_embedded_datetime():
    # Valid embedded date
    dt = embedded_datetime("some text 2023-10-15T14:30:45 and more")
    assert dt is not None
    assert dt.year == 2023
    assert dt.month == 10
    assert dt.day == 15
    assert dt.hour == 14
    assert dt.minute == 30
    assert dt.second == 45
    assert dt.tzinfo == UTC

    # With space instead of T
    dt2 = embedded_datetime("2023-10-15 14:30:45")
    assert dt2 is not None
    assert dt2.year == 2023

    # Invalid format
    assert embedded_datetime("no date here") is None
    assert embedded_datetime(None) is None


def test_dms_to_deg():
    # N / E (positive)
    assert _dms_to_deg("53", "28", "12.7", "N") == 53 + 28 / 60.0 + 12.7 / 3600.0
    assert _dms_to_deg("6", "6", "54.9", "E") == 6 + 6 / 60.0 + 54.9 / 3600.0

    # S / W (negative)
    assert _dms_to_deg("12", "30", "00", "S") == -(12 + 30 / 60.0 + 0 / 3600.0)
    assert _dms_to_deg("45", "15", "00", "w") == -(45 + 15 / 60.0 + 0 / 3600.0)

    # Empty minutes/seconds
    assert _dms_to_deg("53", "", "", "N") == 53.0


@patch("subprocess.run")
def test_ffprobe_json_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=b'{"format": {"duration": "10.0"}}')

    result = ffprobe_json("test.mp4")
    assert result == {"format": {"duration": "10.0"}}


@patch("subprocess.run")
def test_ffprobe_json_file_not_found(mock_run):
    mock_run.side_effect = FileNotFoundError()

    result = ffprobe_json("test.mp4")
    assert result is None


@patch("subprocess.run")
def test_ffprobe_json_timeout(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=30)

    result = ffprobe_json("test.mp4")
    assert result is None


@patch("subprocess.run")
def test_ffprobe_json_invalid_json(mock_run, caplog):
    mock_run.return_value = MagicMock(returncode=0, stdout=b"{invalid json")

    result = ffprobe_json("test.mp4")
    assert result is None
    assert "ffprobe-JSON unlesbar für test.mp4" in caplog.text


@patch("subprocess.run")
def test_ffprobe_json_non_zero_exit(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"error occurred")

    result = ffprobe_json("test.mp4")
    assert result is None


@patch("src.metadata.probe_clip")
def test_collect_clips_basic(mock_probe, tmp_path):
    # Setup some dummy files
    file1 = tmp_path / "video1.mp4"
    file2 = tmp_path / "video2.mov"
    file3 = tmp_path / "not_a_video.txt"

    file1.touch()
    file2.touch()
    file3.touch()

    # Mock probe_clip to return predictable ClipInfo
    def fake_probe(path, **kwargs):
        return ClipInfo(path=Path(path), duration=5.0)

    mock_probe.side_effect = fake_probe

    # We test both dir scanning and explicit file providing
    clips = collect_clips([str(tmp_path), str(file1)], extensions=(".mp4", ".mov"))

    # It should only contain video1 and video2, unique (file1 passed twice essentially)
    assert len(clips) == 2
    paths = [c.path for c in clips]
    assert file1 in paths
    assert file2 in paths
    assert file3 not in paths


@patch("src.metadata.probe_clip")
def test_collect_clips_probe_error(mock_probe, tmp_path):
    file1 = tmp_path / "video1.mp4"
    file1.touch()

    # probe_clip throws an exception
    mock_probe.side_effect = Exception("probe failed completely")

    clips = collect_clips([str(tmp_path)], extensions=(".mp4",))

    assert len(clips) == 1
    assert clips[0].path == file1
    assert "probe failed completely" in clips[0].error


def test_collect_clips_nonexistent_paths():
    # Should skip nonexistent gracefully
    clips = collect_clips(["/this/path/does/not/exist.mp4"])
    assert len(clips) == 0


def test_collect_clips_invalid_extension(tmp_path):
    # Pass a valid file explicitly but it has wrong extension
    file1 = tmp_path / "test.txt"
    file1.touch()
    clips = collect_clips([str(file1)], extensions=(".mp4",))
    assert len(clips) == 0

from src.metadata import sanitize_filename

def test_sanitize_filename_basic():
    assert sanitize_filename("my video title.mp4") == "my_video_title.mp4"
    assert sanitize_filename("no_extension") == "no_extension"

def test_sanitize_filename_fallback():
    assert sanitize_filename("") == "reel"
    assert sanitize_filename("!!!.mp4") == "reel.mp4"
    assert sanitize_filename("   .mp4") == "reel.mp4"

def test_sanitize_filename_multidot():
    assert sanitize_filename("archive.tar.gz") == "archivetar.gz"
    assert sanitize_filename("a.b.c.d") == "abc.d"

def test_sanitize_filename_truncation_ascii():
    long_base = "a" * 250
    result = sanitize_filename(f"{long_base}.mp4")
    assert result == ("a" * 196) + ".mp4"
    assert len(result.encode("utf-8")) <= 200

def test_sanitize_filename_truncation_unicode():
    long_base = "ä" * 150 # 'ä' is 2 bytes in UTF-8
    result = sanitize_filename(f"{long_base}.mp4")
    # Max bytes is 200. ".mp4" is 4 bytes. Allowed bytes for base is 196.
    # 196 / 2 = 98 'ä' characters.
    assert result == ("ä" * 98) + ".mp4"
    assert len(result.encode("utf-8")) <= 200

def test_sanitize_filename_long_extension():
    long_ext = "a" * 250
    result = sanitize_filename(f"base.{long_ext}")
    assert len(result.encode("utf-8")) <= 200
    assert result == "." + ("a" * 199)

def test_sanitize_filename_emoji():
    long_base = "🔥" * 100 # '🔥' is 4 bytes in UTF-8
    result = sanitize_filename(f"{long_base}.mp4")
    # Allowed base bytes = 196. 196 / 4 = 49 emojis.
    assert result == "reel.mp4"
    assert len(result.encode("utf-8")) <= 200
