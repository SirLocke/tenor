import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import UTC
from unittest.mock import MagicMock, patch

from pytest import approx
from src.metadata import (
    ClipInfo,
    Geocoder,
    _collect_tags,
    _parse_datetime,
    dominant_place,
    ffprobe_json,
    parse_coordinates,
    primary_place,
    probe_clip,
)


def test_parse_datetime_plausibility():
    """Test that _parse_datetime rejects implausible dates (e.g. default epoch timestamps) and accepts plausible ones."""
    # Implausible dates
    assert _parse_datetime("1904-01-01T00:00:00Z") is None
    assert _parse_datetime("1970-01-01T00:00:00Z") is None
    assert _parse_datetime("1989-12-31T23:59:59Z") is None
    assert _parse_datetime("2100-01-01T00:00:00Z") is None

    # Plausible dates
    dt_1990 = _parse_datetime("1990-01-01T00:00:00Z")
    assert dt_1990 is not None
    assert dt_1990.year == 1990
    assert dt_1990.tzinfo == UTC

    dt_2024 = _parse_datetime("2024-05-12T12:00:00Z")
    assert dt_2024 is not None
    assert dt_2024.year == 2024
    assert dt_2024.month == 5
    assert dt_2024.day == 12

    # A date far in the future will be rejected by default, unless we pass a large tolerance
    # or mock the current date. Here we just pass a huge future_tolerance.
    dt_2099 = _parse_datetime("2099-12-31T23:59:59Z", future_tolerance=3153600000) # 100 years
    assert dt_2099 is not None
    assert dt_2099.year == 2099


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
    lat, lon = parse_coordinates("53°28'12.7\"N 6°06'54.9\"W")
    assert lat is not None and lon is not None
    assert lat == approx(53 + 28 / 60 + 12.7 / 3600)
    assert lon == approx(-(6 + 6 / 60 + 54.9 / 3600))

    # DMS format S / E
    lat, lon = parse_coordinates("12°30'00\"S 45°15'00\"E")
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
    assert parse_coordinates("95°00'00\"N 10°00'00\"E") == (None, None)


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


def test_primary_place():
    """Test primary_place with None, empty strings, single locations, and edge cases."""
    # None or empty/whitespace inputs
    assert primary_place(None) is None
    assert primary_place("") is None
    assert primary_place("   ") is None

    # Single location without comma
    assert primary_place("Berlin") == "Berlin"
    assert primary_place("  Berlin  ") == "Berlin"

    # Multi-component location string
    assert primary_place("São Roque, Funchal, Madeira, PT") == "São Roque"
    assert primary_place("Paris, France") == "Paris"
    assert primary_place("Tokyo, Japan") == "Tokyo"

    # Edge cases with leading commas or empty first component
    assert primary_place(", Madeira") is None
    assert primary_place("  , Funchal") is None


def test_geocoder_load_cache_file_not_found(tmp_path):
    """Test Geocoder._load_cache when cache_file does not exist (FileNotFoundError)."""
    non_existent_cache = tmp_path / "non_existent_dir" / "cache.json"
    cfg = {"geocoding": {"cache_file": str(non_existent_cache)}}
    geocoder = Geocoder(cfg)
    assert geocoder._cache == {}


@patch("subprocess.run")
def test_ffprobe_json_option_injection_prevention(mock_run):
    """Verify ffprobe_json passes '--' before the input file path."""
    mock_run.return_value = MagicMock(returncode=0, stdout=b"{}")
    malicious_path = Path("-help.mp4")
    ffprobe_json(malicious_path)
    mock_run.assert_called_once()
    args, _ = mock_run.call_args
    cmd = args[0]
    assert "--" in cmd
    dash_dash_idx = cmd.index("--")
    assert cmd[dash_dash_idx + 1] == str(malicious_path)


@patch("src.metadata.ffprobe_json")
@patch("subprocess.run")
def test_probe_clip_raw_bitstream_option_injection_prevention(mock_run, mock_ffprobe_json):
    """Verify probe_clip raw bitstream frame counter passes '--' before the path."""
    mock_ffprobe_json.return_value = {
        "format": {},
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
                "duration": "0.0",
            }
        ],
    }
    mock_run.return_value = MagicMock(returncode=0, stdout=b"300\n")
    malicious_path = Path("-help.hevc")
    clip = probe_clip(malicious_path)
    mock_run.assert_called_once()
    args, _ = mock_run.call_args
    cmd = args[0]
    assert "--" in cmd
    dash_dash_idx = cmd.index("--")
    assert cmd[dash_dash_idx + 1] == str(malicious_path)
    assert clip.duration == 10.0
