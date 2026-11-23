from pathlib import Path

from src import iter_media_files


def test_iter_media_files_excludes_multipart_temp_intermediates(tmp_path: Path) -> None:
    """Scans skip files with multipart temp suffixes like video.part.mp4."""
    (tmp_path / "video.mp4").write_text("x")
    (tmp_path / "video.part.mp4").write_text("x")
    (tmp_path / "video.tmp.mp4").write_text("x")
    (tmp_path / ".crdownload.mp4").write_text("x")

    found = {p.name for p in iter_media_files(tmp_path, [".mp4"])}

    assert found == {"video.mp4"}
