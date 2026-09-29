"""Spotify Audio Analysis beat detection.

Requires SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET env vars.
Fetches beat timestamps from Spotify's Audio Analysis endpoint.
"""
import os
import time
import logging
from typing import List

import requests

log = logging.getLogger("tenor.spotify")

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_ANALYSIS_URL = "https://api.spotify.com/v1/audio-analysis/{track_id}"

_token_cache: dict = {"token": None, "expires": 0}


def _get_token() -> str | None:
    """Client-credentials OAuth token (valid ~1 hour)."""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires"] > now + 30:
        return _token_cache["token"]

    cid = os.getenv("SPOTIFY_CLIENT_ID")
    secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        log.warning("SPOTIFY_CLIENT_ID/SECRET nicht gesetzt – Spotify Beat-Detection deaktiviert")
        return None

    try:
        r = requests.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(cid, secret),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires"] = now + data.get("expires_in", 3600)
        return _token_cache["token"]
    except Exception as exc:
        log.warning("Spotify Token Fehler: %s", exc)
        return None


def _extract_track_id(uri: str) -> str | None:
    """Extrahiert Track-ID aus Spotify URI/URL."""
    # spotify:track:4cOdK2wGLETKBW3PvgPWqT
    # https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT
    import re
    m = re.search(r"(?:track[:/])([a-zA-Z0-9]{22})", uri)
    return m.group(1) if m else None


def fetch_spotify_beats(track_uri: str) -> List[float] | None:
    """Gibt Beat-Startzeiten (Sekunden) aus Spotify Audio Analysis zurück."""
    token = _get_token()
    if not token:
        return None

    track_id = _extract_track_id(track_uri)
    if not track_id:
        log.warning("Ungültige Spotify URI: %s", track_uri)
        return None

    try:
        r = requests.get(
            SPOTIFY_ANALYSIS_URL.format(track_id=track_id),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code == 404:
            log.warning("Spotify Track nicht gefunden: %s", track_uri)
            return None
        r.raise_for_status()
        data = r.json()
        beats = [b["start"] for b in data.get("beats", []) if "start" in b]
        log.info("Spotify Beats: %d Beats für %s (Tempo ~%.1f BPM)",
                 len(beats), track_uri, data.get("track", {}).get("tempo", 0))
        return beats
    except Exception as exc:
        log.warning("Spotify Audio Analysis Fehler: %s", exc)
        return None
