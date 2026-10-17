"""Tests for src/spotify_beats.py."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from src.spotify_beats import (
    SpotifyAccessError,
    _auth_get,
    _cache,
    _TokenCache,
    extract_track_id,
    fetch_spotify_beats,
    get_token,
)


class TestExtractTrackId:
    """Tests for extract_track_id."""

    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            # Spotify Web URLs
            ("https://open.spotify.com/track/4uLU612A35POGL3A3X3iie?si=123456", "4uLU612A35POGL3A3X3iie"),
            ("https://open.spotify.com/track/4uLU612A35POGL3A3X3iie", "4uLU612A35POGL3A3X3iie"),
            ("http://open.spotify.com/track/4uLU612A35POGL3A3X3iie", "4uLU612A35POGL3A3X3iie"),
            # Spotify URIs and variations
            ("spotify:track:4uLU612A35POGL3A3X3iie", "4uLU612A35POGL3A3X3iie"),
            ("track:4uLU612A35POGL3A3X3iie", "4uLU612A35POGL3A3X3iie"),
            ("track/4uLU612A35POGL3A3X3iie", "4uLU612A35POGL3A3X3iie"),
            # Bare 22-character IDs
            ("4uLU612A35POGL3A3X3iie", "4uLU612A35POGL3A3X3iie"),
            ("0123456789abcdefABCDEF", "0123456789abcdefABCDEF"),
            ("  4uLU612A35POGL3A3X3iie \n\t", "4uLU612A35POGL3A3X3iie"),
            # Invalid or unsupported inputs
            (None, None),
            ("", None),
            ("   ", None),
            ("invalid_track_id", None),
            ("http://example.com", None),
            ("spotify:artist:4uLU612A35POGL3A3X3iie", None),
            ("spotify:album:4uLU612A35POGL3A3X3iie", None),
            ("4uLU612A35POGL3A3X3ii", None),  # 21 characters (too short)
            ("4uLU612A35POGL3A3X3iieX", None),  # 23 characters (too long)
            ("4uLU612A35POGL3A3X!iiie", None),  # Contains invalid character
        ],
    )
    def test_extract_track_id(self, input_val: str | None, expected: str | None) -> None:
        assert extract_track_id(input_val) == expected  # type: ignore[arg-type]


class TestTokenCache:
    """Tests for _TokenCache."""

    def test_cache_set_and_get(self) -> None:
        cache = _TokenCache()
        assert cache.get() is None

        cache.set("test_token", 3600)
        assert cache.get() == "test_token"

    def test_cache_expired_token(self) -> None:
        cache = _TokenCache()
        cache.set("expired_token", 3600)
        cache._expires = time.time() - 10
        assert cache.get() is None

    def test_cache_clear(self) -> None:
        cache = _TokenCache()
        cache.set("test_token", 3600)
        cache.clear()
        assert cache.get() is None


class TestGetToken:
    """Tests for get_token."""

    def setup_method(self) -> None:
        _cache.clear()

    def test_get_cached_token(self) -> None:
        _cache.set("cached_abc", 3600)
        assert get_token(force=False) == "cached_abc"

    def test_get_token_missing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
        assert get_token() is None

    def test_get_token_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "dummy_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "dummy_secret")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new_token_123", "expires_in": 3600}

        with patch("src.spotify_beats._requests") as mock_req_fn:
            mock_requests = MagicMock()
            mock_requests.post.return_value = mock_resp
            mock_req_fn.return_value = mock_requests

            token = get_token(force=True)
            assert token == "new_token_123"
            assert _cache.get() == "new_token_123"

    def test_get_token_auth_error_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "bad_id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "bad_secret")

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("src.spotify_beats._requests") as mock_req_fn:
            mock_requests = MagicMock()
            mock_requests.post.return_value = mock_resp
            mock_req_fn.return_value = mock_requests

            assert get_token(force=True) is None

    @pytest.mark.parametrize(
        "exception_instance",
        [
            TimeoutError("Request timed out"),
            ConnectionError("Failed to connect"),
            ValueError("Invalid JSON returned"),
            KeyError("access_token"),
            RuntimeError("Unexpected error"),
        ],
    )
    def test_get_token_exception_handling(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        exception_instance: Exception,
    ) -> None:
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")

        with patch("src.spotify_beats._requests") as mock_req_fn:
            mock_requests = MagicMock()
            mock_requests.post.side_effect = exception_instance
            mock_req_fn.return_value = mock_requests

            with caplog.at_level("WARNING"):
                assert get_token(force=True) is None

            assert "Spotify-Token nicht erhältlich:" in caplog.text
            assert str(exception_instance) in caplog.text or repr(exception_instance) in caplog.text

    def test_get_token_raise_for_status_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        http_error = RuntimeError("500 Server Error")
        mock_resp.raise_for_status.side_effect = http_error

        with patch("src.spotify_beats._requests") as mock_req_fn:
            mock_requests = MagicMock()
            mock_requests.post.return_value = mock_resp
            mock_req_fn.return_value = mock_requests

            with caplog.at_level("WARNING"):
                assert get_token(force=True) is None

            assert "Spotify-Token nicht erhältlich: 500 Server Error" in caplog.text

    def test_get_token_invalid_schema_key_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"error": "invalid_grant"}

        with patch("src.spotify_beats._requests") as mock_req_fn:
            mock_requests = MagicMock()
            mock_requests.post.return_value = mock_resp
            mock_req_fn.return_value = mock_requests

            with caplog.at_level("WARNING"):
                assert get_token(force=True) is None

            assert "Spotify-Token nicht erhältlich: 'access_token'" in caplog.text


class TestAuthGet:
    """Tests for _auth_get."""

    def setup_method(self) -> None:
        _cache.clear()

    def test_auth_get_no_requests(self) -> None:
        with patch("src.spotify_beats._requests", return_value=None):
            assert _auth_get("https://api.spotify.com/v1/test") is None

    def test_auth_get_no_token(self) -> None:
        with patch("src.spotify_beats.get_token", return_value=None):
            assert _auth_get("https://api.spotify.com/v1/test") is None

    def test_auth_get_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {"key": "value"}

        with (
            patch("src.spotify_beats.get_token", return_value="valid_token"),
            patch("src.spotify_beats._requests") as mock_req_fn,
        ):
            mock_req = MagicMock()
            mock_req.get.return_value = mock_resp
            mock_req_fn.return_value = mock_req

            res = _auth_get("https://api.spotify.com/v1/test")
            assert res == {"key": "value"}

    def test_auth_get_401_retry_clears_cache(self) -> None:
        resp_401 = MagicMock()
        resp_401.status_code = 401

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.ok = True
        resp_200.json.return_value = {"success": True}

        _cache.set("old_token", 3600)

        def mock_get_token(force: bool = False) -> str | None:
            return "new_token" if force else "old_token"

        with (
            patch("src.spotify_beats.get_token", side_effect=mock_get_token),
            patch("src.spotify_beats._requests") as mock_req_fn,
        ):
            mock_req = MagicMock()
            mock_req.get.side_effect = [resp_401, resp_200]
            mock_req_fn.return_value = mock_req

            res = _auth_get("https://api.spotify.com/v1/test")
            assert res == {"success": True}

    def test_auth_get_404(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with (
            patch("src.spotify_beats.get_token", return_value="token"),
            patch("src.spotify_beats._requests") as mock_req_fn,
        ):
            mock_req = MagicMock()
            mock_req.get.return_value = mock_resp
            mock_req_fn.return_value = mock_req

            assert _auth_get("https://api.spotify.com/v1/test") is None

    def test_auth_get_403_quota_locked(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with (
            patch("src.spotify_beats.get_token", return_value="token"),
            patch("src.spotify_beats._requests") as mock_req_fn,
        ):
            mock_req = MagicMock()
            mock_req.get.return_value = mock_resp
            mock_req_fn.return_value = mock_req

            with pytest.raises(SpotifyAccessError):
                _auth_get("https://api.spotify.com/v1/audio-analysis/123", quota_locked=True)

    def test_auth_get_429_backoff(self) -> None:
        resp_429_1 = MagicMock()
        resp_429_1.status_code = 429
        resp_429_1.headers = {"Retry-After": "1"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.ok = True
        resp_200.json.return_value = {"ok": True}

        with (
            patch("src.spotify_beats.get_token", return_value="token"),
            patch("src.spotify_beats._requests") as mock_req_fn,
        ):
            mock_req = MagicMock()
            mock_req.get.side_effect = [resp_429_1, resp_200]
            mock_req_fn.return_value = mock_req

            with patch("time.sleep") as mock_sleep:
                res = _auth_get("https://api.spotify.com/v1/test")
                assert res == {"ok": True}
                mock_sleep.assert_called_once_with(1)

    def test_auth_get_429_max_retries_exceeded(self) -> None:
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "1"}

        with (
            patch("src.spotify_beats.get_token", return_value="token"),
            patch("src.spotify_beats._requests") as mock_req_fn,
        ):
            mock_req = MagicMock()
            mock_req.get.return_value = resp_429
            mock_req_fn.return_value = mock_req

            with patch("time.sleep"):
                res = _auth_get("https://api.spotify.com/v1/test")
                assert res is None

    def test_auth_get_invalid_json(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.side_effect = ValueError("Invalid JSON")

        with (
            patch("src.spotify_beats.get_token", return_value="token"),
            patch("src.spotify_beats._requests") as mock_req_fn,
        ):
            mock_req = MagicMock()
            mock_req.get.return_value = mock_resp
            mock_req_fn.return_value = mock_req

            assert _auth_get("https://api.spotify.com/v1/test") is None


class TestFetchSpotifyBeats:
    """Tests for fetch_spotify_beats."""

    def test_fetch_spotify_beats_invalid_uri(self) -> None:
        assert fetch_spotify_beats("invalid") is None

    def test_fetch_spotify_beats_success(self) -> None:
        data = {
            "beats": [
                {"start": 1.5},
                {"start": 0.5},
                {"start": -0.2},  # Should be filtered out (< 0)
                {"start": float("nan")},  # Should be filtered out (NaN)
                {"start": "2.5"},
                {"invalid_entry": True},
            ],
            "track": {"tempo": 120.0},
        }
        with patch("src.spotify_beats._auth_get", return_value=data):
            beats = fetch_spotify_beats("4uLU612A35POGL3A3X3iie")
            assert beats == [0.5, 1.5, 2.5]

    def test_fetch_spotify_beats_empty_beats(self) -> None:
        data = {"beats": [], "track": {"tempo": 120.0}}
        with patch("src.spotify_beats._auth_get", return_value=data):
            assert fetch_spotify_beats("4uLU612A35POGL3A3X3iie") is None

    def test_fetch_spotify_beats_access_error(self) -> None:
        with (
            patch("src.spotify_beats._auth_get", side_effect=SpotifyAccessError("Quota locked")),
            pytest.raises(SpotifyAccessError),
        ):
            fetch_spotify_beats("4uLU612A35POGL3A3X3iie")

    def test_fetch_spotify_beats_generic_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("src.spotify_beats._auth_get", side_effect=Exception("API connection failure")):
            with caplog.at_level("WARNING"):
                res = fetch_spotify_beats("4uLU612A35POGL3A3X3iie")
                assert res is None

            assert "Fehler beim Abruf der Spotify-Beats: API connection failure" in caplog.text
