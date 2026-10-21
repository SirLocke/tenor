import re

with open("tests/test_stability.py", "r") as f:
    content = f.read()

new_tests = """
def test_export_reel_aborts_on_low_disk_space(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch
    from src.pipeline import ClipInfo, Segment, export_reel
    from src.styling import GradeConfig

    cfg = {"naming": {"folder_template": ""}, "video": {"audio": True, "crf": 19}}
    base_out = tmp_path / "output"

    clip_path = tmp_path / "fake.mp4"
    clip_path.write_bytes(b"dummy")
    clip = ClipInfo(path=clip_path, duration=5.0)
    seg = Segment(clip=clip, start=0.0, end=60.0) # 60 seconds

    grade = GradeConfig()
    errors = []

    with patch("shutil.disk_usage") as mock_usage:
        mock_usage_obj = MagicMock()
        mock_usage_obj.free = 100 * 1024 * 1024  # 100 MB free (less than 200MB margin)
        mock_usage.return_value = mock_usage_obj

        with patch("src.pipeline.render_segments") as mock_render:
            target = export_reel(
                segments=[seg],
                cfg=cfg,
                base_out=base_out,
                title="LowSpaceTest",
                subtitle="Subtitle",
                day="2026-09-11",
                grade=grade,
                when=1600000000.0,
                dry_run=False,
                errors_seen=errors,
            )

            assert target is None
            assert len(errors) == 1
            assert errors[0] == "Zu wenig Speicherplatz für den Export"
            mock_render.assert_not_called()

def test_export_reel_proceeds_with_sufficient_disk_space(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch
    from src.pipeline import ClipInfo, Segment, export_reel
    from src.styling import GradeConfig

    cfg = {"naming": {"folder_template": ""}, "video": {"audio": True, "crf": 19}}
    base_out = tmp_path / "output"

    clip_path = tmp_path / "fake.mp4"
    clip_path.write_bytes(b"dummy")
    clip = ClipInfo(path=clip_path, duration=5.0)
    seg = Segment(clip=clip, start=0.0, end=60.0) # 60 seconds

    grade = GradeConfig()
    errors = []

    with patch("shutil.disk_usage") as mock_usage:
        mock_usage_obj = MagicMock()
        mock_usage_obj.free = 1000 * 1024 * 1024 * 1024  # 1000 GB free
        mock_usage.return_value = mock_usage_obj

        with patch("src.pipeline.render_segments") as mock_render:
            mock_final = MagicMock()
            mock_final.duration = 60.0
            mock_audio = MagicMock()
            mock_final.audio = mock_audio
            mock_render.return_value = [mock_final]

            target = export_reel(
                segments=[seg],
                cfg=cfg,
                base_out=base_out,
                title="EnoughSpaceTest",
                subtitle="Subtitle",
                day="2026-09-11",
                grade=grade,
                when=1600000000.0,
                dry_run=False,
                errors_seen=errors,
            )

            # Not returning None due to disk space, mock_render should be called
            mock_render.assert_called_once()
"""

# Insert right before test_watcher_duplicate_events_prevented_by_lock
content = content.replace("def test_watcher_duplicate_events_prevented_by_lock", new_tests + "\ndef test_watcher_duplicate_events_prevented_by_lock")

with open("tests/test_stability.py", "w") as f:
    f.write(content)

print("done")
