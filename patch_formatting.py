import re

with open("src/pipeline.py", "r") as f:
    content = f.read()

# Fix "Too many blank lines (3)" at line 1813 in src/pipeline.py
content = re.sub(r"\n\n\n\ndef check_disk_space", r"\n\ndef check_disk_space", content)
content = re.sub(r"\n\n\ndef check_disk_space", r"\n\ndef check_disk_space", content)

with open("src/pipeline.py", "w") as f:
    f.write(content)

with open("tests/test_stability.py", "r") as f:
    content = f.read()

# Fix formatting issues in tests/test_stability.py
# 1. Too many blank lines at line 1357
content = re.sub(r"\n\n\n\ndef test_export_reel_aborts", r"\n\ndef test_export_reel_aborts", content)
content = re.sub(r"\n\n\ndef test_export_reel_aborts", r"\n\ndef test_export_reel_aborts", content)

# 2. Fix unsorted imports at line 1358
search = """    from unittest.mock import MagicMock, patch
    from src.pipeline import ClipInfo, Segment, export_reel
    from src.styling import GradeConfig"""
replace = """    from unittest.mock import MagicMock, patch

    from src.pipeline import ClipInfo, Segment, export_reel
    from src.styling import GradeConfig"""
content = content.replace(search, replace)

# 3. Two spaces before inline comment
search = """    seg = Segment(clip=clip, start=0.0, end=60.0) # 60 seconds"""
replace = """    seg = Segment(clip=clip, start=0.0, end=60.0)  # 60 seconds"""
content = content.replace(search, replace)

# 4. Blank lines top level
search = """            mock_render.assert_not_called()

def test_export_reel_proceeds_with_sufficient_disk_space"""
replace = """            mock_render.assert_not_called()


def test_export_reel_proceeds_with_sufficient_disk_space"""
content = content.replace(search, replace)

search = """            mock_render.assert_called_once()

def test_watcher_duplicate_events_prevented_by_lock"""
replace = """            mock_render.assert_called_once()


def test_watcher_duplicate_events_prevented_by_lock"""
content = content.replace(search, replace)

# 5. Fix unused assignment `target`
search = """            with patch("pathlib.Path.replace"):
                target = export_reel("""
replace = """            with patch("pathlib.Path.replace"):
                export_reel("""
content = content.replace(search, replace)


with open("tests/test_stability.py", "w") as f:
    f.write(content)

print("done")
