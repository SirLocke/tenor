import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.pipeline import is_valid_video


@patch("subprocess.run")
def test_is_valid_video_success(mock_run):
    mock_run.return_value = MagicMock(stdout=b"video\n")
    assert is_valid_video(Path("test.mp4")) is True
    args, _ = mock_run.call_args
    cmd = args[0]
    assert "--" in cmd
    assert cmd[cmd.index("--") + 1] == "test.mp4"


@patch("subprocess.run")
def test_is_valid_video_failure(mock_run):
    mock_run.return_value = MagicMock(stdout=b"")
    assert is_valid_video(Path("test.mp4")) is False


@patch("subprocess.run")
def test_is_valid_video_exception(mock_run):
    mock_run.side_effect = subprocess.SubprocessError
    assert is_valid_video(Path("test.mp4")) is False


@patch("subprocess.run")
def test_is_valid_video_hevc(mock_run):
    mock_run.return_value = MagicMock(stdout=b"video\n")
    assert is_valid_video(Path("test.hevc")) is True
    args, kwargs = mock_run.call_args
    assert "-f" in args[0]
    assert "hevc" in args[0]
    assert "--" in args[0]
    assert args[0][args[0].index("--") + 1] == "test.hevc"
