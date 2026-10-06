import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch

from pytest import approx

from src.metadata import (
    ClipInfo,
    _collect_tags,
    dominant_place,
    embedded_datetime,
    parse_coordinates,
    probe_clip,
)


def test_parse_coordinates_valid():
    """Test parse_coordinates with valid ISO-6709 signed pairs and DMS formats."""
    # Signed pair
    lat, lon = parse_coordinates("+53.4702-006.1153+023.5/")
    assert lat == approx(53.4702)
    assert lon == approx(-6.1153)

    # Signed pair negative latitude
    lat, lon = parse_coordinates("-23.5000+045.2500/")
    assert lat == approx(-23.5)
    assert lon == approx(45.25)

    # DMS format N / W
    lat, lon = parse_coordinates('53°28\'12.7"N 6°06\'54.9"W')
    assert lat is not None and lon is not None
    assert lat == approx(53 + 28 / 60 + 12.7 / 3600)
    assert lon == approx(-(6 + 6 / 60 + 54.9 / 3600))

    # DMS format S / E
    lat, lon = parse_coordinates('12°30\'00"S 45°15\'00"E')
    assert lat == approx(-(12 + 30 / 60))
    assert lon == approx(45 + 15 / 60)


def test_parse_coordinates_invalid():
    """Test parse_coordinates with invalid data, edge cases, and corrupt strings."""
    # None or empty/whitespace
    assert parse_coordinates(None) == (None, None)
    assert parse_coordinates("") == (None, None)
    assert parse_coordinates("   ") == (None, None)

    # Non-coordinate strings
    assert parse_coordinates("not coordinates") == (None, None)
    assert parse_coordinates("1234567") == (None, None)

    # Coordinates at 0.0, 0.0 (considered invalid/unset in _valid_coords)
    assert parse_coordinates("+00.0000+000.0000/") == (None, None)

    # Coordinates out of range
    assert parse_coordinates("+91.0000+000.0000/") == (None, None)
    assert parse_coordinates("-95.0000+000.0000/") == (None, None)
    assert parse_coordinates("+00.0000+181.0000/") == (None, None)
    assert parse_coordinates("+00.0000-185.0000/") == (None, None)

    # DMS out of range
    assert parse_coordinates('95°00\'00"N 10°00\'00"E') == (None, None)


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


def test_embedded_datetime_timezone_handling():
    """Test embedded_datetime with various ISO 8601 string variations and timezone handling."""
    # Invalid or empty inputs
    assert embedded_datetime(None) is None
    assert embedded_datetime("") is None
    assert embedded_datetime("   ") is None
    assert embedded_datetime("no date here") is None

    # Datetime with 'Z' UTC suffix
    dt_z = embedded_datetime("2023-07-15T10:23:45Z")
    assert dt_z == datetime(2023, 7, 15, 10, 23, 45, tzinfo=timezone.utc)

    # Space separated date and time
    dt_space = embedded_datetime("2023-07-15 10:23:45")
    assert dt_space == datetime(2023, 7, 15, 10, 23, 45, tzinfo=timezone.utc)

    # Embedded date inside location string (e.g. QuickTime location tag from iPhone)
    geo_string_utc = "+53.4702-006.1153+023.5/2023-07-15T10:23:45Z/"
    dt_geo = embedded_datetime(geo_string_utc)
    assert dt_geo == datetime(2023, 7, 15, 10, 23, 45, tzinfo=timezone.utc)

    # Embedded date with timezone offset in location string
    geo_string_offset = "+53.4702-006.1153+023.5/2023-07-15T10:23:45+02:00/"
    dt_geo_offset = embedded_datetime(geo_string_offset)
    assert dt_geo_offset == datetime(2023, 7, 15, 10, 23, 45, tzinfo=timezone.utc)
