from src.pipeline import select_bitrate

def test_select_bitrate():
    # instagram
    assert select_bitrate("instagram", 1920) == 3500
    assert select_bitrate("instagram", 1080) == 3500
    assert select_bitrate("instagram", 720) == 2500
    assert select_bitrate("instagram", 480) == 1000
    assert select_bitrate("instagram", 360) == 1000

    # tiktok
    assert select_bitrate("tiktok", 1080) == 4000
    assert select_bitrate("tiktok", 720) == 3000
    assert select_bitrate("tiktok", 480) == 1500

    # youtube_shorts
    assert select_bitrate("youtube_shorts", 1080) == 5000
    assert select_bitrate("youtube_shorts", 720) == 3500
    assert select_bitrate("youtube_shorts", 480) == 2000

    # generic
    assert select_bitrate("generic", 1080) == 4500
    assert select_bitrate("generic", 720) == 3000
    assert select_bitrate("generic", 480) == 1500

    # fallback (unknown platform -> generic)
    assert select_bitrate("unknown_platform", 1080) == 4500

    # intermediate resolutions
    assert select_bitrate("instagram", 1000) == 2500
    assert select_bitrate("instagram", 800) == 2500
    assert select_bitrate("youtube_shorts", 719) == 2000
