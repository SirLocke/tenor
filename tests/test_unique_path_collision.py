from pathlib import Path
from src.pipeline import unique_path

def test_unique_path_infinite_loop(tmp_path):
    long_stem = "a" * 250
    suffix = ".mp4"

    # Create the first candidate to force a collision
    first_candidate = unique_path(tmp_path, long_stem, suffix)
    first_candidate.touch()

    # Should safely create the _2 file without infinite looping
    second_candidate = unique_path(tmp_path, long_stem, suffix)
    assert second_candidate.name != first_candidate.name
    assert second_candidate.name.endswith("_2.mp4")
    assert len(second_candidate.name.encode("utf-8")) <= 200
