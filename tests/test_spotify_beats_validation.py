from unittest.mock import MagicMock, patch

from src.spotify_beats import fetch_spotify_beats


def test_spotify_beats_strictly_increasing_and_within_duration(tmp_path):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "track": {"tempo": 120.0, "duration": 10.0},
        "beats": [
            {"start": 1.0},
            {"start": 2.0},
            {"start": 2.0},  # duplicate
            {"start": 1.5},  # out of order, will be sorted
            {"start": 11.0},  # exceeds duration
            {"start": -1.0},  # negative
            {"start": "invalid"},  # invalid
        ],
    }

    with (
        patch("src.spotify_beats._requests") as mock_req,
        patch("src.spotify_beats.get_token", return_value="dummy_token"),
    ):
        mock_requests_module = MagicMock()
        mock_requests_module.get.return_value = mock_response
        mock_req.return_value = mock_requests_module

        cache_dir = tmp_path / "cache"
        beats = fetch_spotify_beats("track:1234567890123456789012", cache_dir=cache_dir)

        assert beats == [1.0, 1.5, 2.0]


def test_spotify_beats_no_duration(tmp_path):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "track": {"tempo": 120.0},
        "beats": [
            {"start": 1.0},
            {"start": 2.0},
            {"start": 2.0},  # duplicate
            {"start": 1.5},  # out of order, will be sorted
            {"start": 11.0},  # should be kept as no duration is provided
        ],
    }

    with (
        patch("src.spotify_beats._requests") as mock_req,
        patch("src.spotify_beats.get_token", return_value="dummy_token"),
    ):
        mock_requests_module = MagicMock()
        mock_requests_module.get.return_value = mock_response
        mock_req.return_value = mock_requests_module

        cache_dir = tmp_path / "cache"
        beats = fetch_spotify_beats("track:1234567890123456789012", cache_dir=cache_dir)

        assert beats == [1.0, 1.5, 2.0, 11.0]
