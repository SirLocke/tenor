from pathlib import Path
from unittest.mock import MagicMock, patch

from src.pipeline import ClipInfo, Segment, export_reel
from src.styling import GradeConfig


def test_export_reel_appends_fingerprint_to_filename(tmp_path: Path) -> None:
    """Ensure that export_reel appends the 8-character fingerprint prefix to the stem."""
    cfg = {"naming": {"folder_template": "", "template": "{slug}"}, "video": {"audio": False}}
    base_out = tmp_path / "output"

    clip_info = ClipInfo(path=Path("dummy.mov"), duration=2.0)
    seg = Segment(clip=clip_info, start=0.0, end=2.0)

    # Use dry_run=True to skip actual rendering
    with patch("src.pipeline.render_segments") as mock_render:
        mock_final = MagicMock()
        mock_final.duration = 2.0
        mock_render.return_value = [mock_final]

        target = export_reel(
            segments=[seg],
            cfg=cfg,
            base_out=base_out,
            title="CollisionTest",
            subtitle="",
            day="2023-01-01",
            grade=GradeConfig(),
            when=1672531200.0,
            dry_run=True,
            errors_seen=[],
        )

    assert target is not None
    # Check that it ends with _{fingerprint[:8]}.mp4
    # The stem should be the collision test name and the fingerprint part
    assert "_collisiontest_" in target.name.lower() or target.name.lower().startswith("collisiontest_")

    # We can also dynamically compute what fingerprint we expect or just verify the hash pattern
    # It should look like CollisionTest_1a2b3c4d.mp4 (8 hex chars)
    import re

    assert re.search(r"_[a-f0-9]{8}\.mp4$", target.name), f"Expected fingerprint in name, got {target.name}"
