import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch

from src.metadata import ClipInfo, _collect_tags, dominant_place, probe_clip

def test_collect_tags_malformed():
    """Test _collect_tags with malformed metadata (e.g., format or streams are the wrong type)."""
    # format is string instead of dict
    res1 = _collect_tags({"format": "not_a_dict"})
    assert res1 == {}

    # streams is integer instead of list
    res2 = _collect_tags({"streams": 123})
    assert res2 == {}

    # streams is a string instead of a list of dicts
    res3 = _collect_tags({"streams": "string_streams"})
    assert res3 == {}

    # valid case
    res4 = _collect_tags({"format": {"tags": {"a": "1"}}, "streams": [{"tags": {"b": "2"}}]})
    assert res4 == {"a": "1", "b": "2"}


@patch("src.metadata.ffprobe_json")
def test_probe_clip_malformed(mock_ffprobe):
    """Test probe_clip with malformed ffprobe metadata."""
    # format is string, streams is integer
    mock_ffprobe.return_value = {"format": "not_a_dict", "streams": 123}

    clip = probe_clip("dummy.mp4")
    assert clip.error is None
    assert clip.duration == 0.0

    # streams is string
    mock_ffprobe.return_value = {"format": {}, "streams": "string_streams"}
    clip2 = probe_clip("dummy.mp4")
    assert clip2.error is None
    assert clip2.duration == 0.0


def test_dominant_place():
    """Test calculation of dominant_place with various clip combinations."""
    # 1. Empty clip list
    assert dominant_place([]) is None

    # 2. Clips with no place set (place is None)
    clips_no_place = [ClipInfo(path=Path("a.mp4")), ClipInfo(path=Path("b.mp4"))]
    assert dominant_place(clips_no_place) is None

    # 3. Mixed clips where some have place=None
    clips_mixed_none = [
        ClipInfo(path=Path("a.mp4"), place="Berlin, Germany"),
        ClipInfo(path=Path("b.mp4"), place=None),
        ClipInfo(path=Path("c.mp4"), place="Berlin, Germany"),
    ]
    assert dominant_place(clips_mixed_none) == "Berlin, Germany"

    # 4. Clear majority place
    clips_majority = [
        ClipInfo(path=Path("a.mp4"), place="Berlin, Germany"),
        ClipInfo(path=Path("b.mp4"), place="Paris, France"),
        ClipInfo(path=Path("c.mp4"), place="Berlin, Germany"),
        ClipInfo(path=Path("d.mp4"), place="Tokyo, Japan"),
    ]
    assert dominant_place(clips_majority) == "Berlin, Germany"

    # 5. Tie-breaking (first encountered place among tied ones should win)
    clips_tie_1 = [
        ClipInfo(path=Path("a.mp4"), place="Berlin, Germany"),
        ClipInfo(path=Path("b.mp4"), place="Paris, France"),
        ClipInfo(path=Path("c.mp4"), place="Paris, France"),
        ClipInfo(path=Path("d.mp4"), place="Berlin, Germany"),
    ]
    assert dominant_place(clips_tie_1) == "Berlin, Germany"

    clips_tie_2 = [
        ClipInfo(path=Path("a.mp4"), place="Paris, France"),
        ClipInfo(path=Path("b.mp4"), place="Berlin, Germany"),
        ClipInfo(path=Path("c.mp4"), place="Berlin, Germany"),
        ClipInfo(path=Path("d.mp4"), place="Paris, France"),
    ]
    assert dominant_place(clips_tie_2) == "Paris, France"
