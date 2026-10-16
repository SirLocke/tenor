
import pytest
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
from src.pipeline import is_valid_video

@patch("subprocess.run")
def test_is_valid_video_success(mock_run):
    mock_run.return_value = MagicMock(stdout=b"video\n")
    assert is_valid_video(Path("test.mp4")) is True

@patch("subprocess.run")
def test_is_valid_video_failure(mock_run):
    mock_run.return_value = MagicMock(stdout=b"")
    assert is_valid_video(Path("test.mp4")) is False

@patch("subprocess.run")
def test_is_valid_video_exception(mock_run):
    mock_run.side_effect = subprocess.SubprocessError
    assert is_valid_video(Path("test.mp4")) is False
