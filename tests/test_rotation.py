from unittest.mock import patch

import pytest
from src.metadata import probe_clip


@pytest.fixture
def mock_ffprobe_json():
    with patch("src.metadata.ffprobe_json") as m:
        yield m


def test_clip_rotation_parsing(mock_ffprobe_json, tmp_path):
    p = tmp_path / "test.mp4"
    p.touch()

    # Test rotate in stream tags
    mock_ffprobe_json.return_value = {
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080, "tags": {"rotate": "90"}}],
        "format": {"duration": "1.0"},
    }
    clip = probe_clip(p)
    assert clip.rotation == 90
    assert clip.width == 1920
    assert clip.height == 1080

    # Test rotate in display matrix
    mock_ffprobe_json.return_value = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
            }
        ],
        "format": {"duration": "1.0"},
    }
    clip = probe_clip(p)
    assert clip.rotation == 270

    # Test rotate in format tags
    mock_ffprobe_json.return_value = {
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
        "format": {"duration": "1.0", "tags": {"rotate": "180"}},
    }
    clip = probe_clip(p)
    assert clip.rotation == 180


def test_pipeline_swap(tmp_path):
    p = tmp_path / "test2.mp4"
    import subprocess

    # Make a video longer than target_min (1.4) so segments can be built
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=3", "-c:v", "libx264", str(p)],
        capture_output=True,
    )

    from src.pipeline import run

    with patch("src.pipeline.collect_clips") as m_collect:
        from src.metadata import ClipInfo

        info = ClipInfo(path=p)
        info.width = 1920
        info.height = 1080
        info.duration = 3.0
        info.rotation = 90
        info.fps = 25.0
        m_collect.return_value = [info]

        # Test the pipeline
        res = run([str(p)], limit=1, dry_run=True)
        assert res.segments
        assert res.segments[0].clip.width == 1080
        assert res.segments[0].clip.height == 1920
        assert res.segments[0].clip.rotation == 0
