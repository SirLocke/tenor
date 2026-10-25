from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw, ImageFont
from src.styling import (
    GradeConfig,
    _download_font,
    _fit_line,
    _line_height,
    _resolve_font_path_impl,
    _text_width,
    build_subtitle,
    build_text_overlay,
    center_crop_resize,
    escape_drawtext_text,
    format_coordinates,
    target_size,
    wrap_text,
)


def test_format_coordinates():
    assert format_coordinates(53.5510, 9.9930) == "53.5510° N, 9.9930° E"
    assert format_coordinates(-53.5510, -9.9930) == "53.5510° S, 9.9930° W"
    assert format_coordinates(0.0, 0.0) == "0.0000° N, 0.0000° E"


def test_escape_drawtext_text():
    assert escape_drawtext_text(None) == ""
    assert escape_drawtext_text("Hello") == "Hello"
    assert escape_drawtext_text(123) == "123"
    assert escape_drawtext_text("a\\b") == "a\\\\b"
    assert escape_drawtext_text("a:b") == "a\\:b"
    assert escape_drawtext_text("a,b") == "a\\,b"
    assert escape_drawtext_text("a;b") == "a\\;b"
    assert escape_drawtext_text("a'b") == "a\\'b"
    assert escape_drawtext_text("a\nb") == "a\\nb"
    assert escape_drawtext_text("a\rb") == "a\\rb"
    # combined
    assert escape_drawtext_text("a:b,c'd\n") == "a\\:b\\,c\\'d\\n"


def test_build_subtitle():
    assert build_subtitle("Berlin", "Today") == "Berlin  ·  Today"
    assert build_subtitle("Berlin", "") == "Berlin"
    assert build_subtitle(None, "Today") == "Today"
    assert build_subtitle(None, "") == ""


def test_target_size():
    cfg = {"video": {"width": 720, "height": 1280}}
    assert target_size(cfg) == (720, 1280)

    empty_cfg = {}
    assert target_size(empty_cfg) == (1080, 1920)


class MockClip:
    def __init__(self, w, h):
        self.size = (w, h)
        self.crop_kwargs = None
        self.resize_kwargs = None

    def resized(self, size):
        self.resize_kwargs = size
        return self

    def cropped(self, **kwargs):
        self.crop_kwargs = kwargs
        return self


def test_center_crop_resize():
    # Test valid width/height combinations

    # 1. Exact ratio (9:16)
    clip = MockClip(1080, 1920)
    center_crop_resize(clip, 1080, 1920)
    assert clip.crop_kwargs is None
    assert clip.resize_kwargs == (1080, 1920)

    # 2. Too wide (e.g. 16:9 source, crop width)
    clip = MockClip(1920, 1080)
    center_crop_resize(clip, 1080, 1920)
    assert clip.crop_kwargs is not None
    # target ratio = 1080/1920 = 9/16
    # h = 1080. new_width = 1080 * (9/16) = 607.5 -> 608
    # 608 % 2 == 0.
    # w = 1920, center_x = 960.
    # x1 = 960 - 304 = 656
    # x2 = 656 + 608 = 1264
    assert clip.crop_kwargs == {"x1": 656, "y1": 0, "x2": 1264, "y2": 1080}
    assert clip.resize_kwargs == (1080, 1920)

    # 3. Too tall (e.g. 9:20 source, crop height)
    clip = MockClip(1080, 2400)
    center_crop_resize(clip, 1080, 1920)
    assert clip.crop_kwargs is not None
    # target ratio = 9/16
    # w = 1080. new_height = 1080 / (9/16) = 1920
    # h = 2400, center_y = 1200
    # y1 = 1200 - 960 = 240
    # y2 = 240 + 1920 = 2160
    assert clip.crop_kwargs == {"x1": 0, "y1": 240, "x2": 1080, "y2": 2160}
    assert clip.resize_kwargs == (1080, 1920)

    # 4. Zero width or height
    clip = MockClip(0, 1080)
    center_crop_resize(clip, 1080, 1920)
    assert clip.crop_kwargs is None
    assert clip.resize_kwargs == (1080, 1920)


def test_gradeconfig_from_config():
    # Test preset override
    cfg = {"style": {"preset": "cinematic", "gamma": 0.5}}
    config = GradeConfig.from_config(cfg)
    assert config.preset == "cinematic"
    assert config.gamma == 0.5
    assert config.contrast == 0.18  # Cinematic default

    # Test natural default fallback omission
    cfg_natural = {"style": {"preset": "cinematic", "gamma": 1.0}}  # natural default for gamma
    config_natural = GradeConfig.from_config(cfg_natural)
    # Since 1.0 is natural default but preset is cinematic, it should use cinematic default or the user explicitly set 1.0
    # Wait, the code says: if val == natural_default, ignore it (continue).
    # Therefore it uses base_params['gamma'] which is cinematic default (0.94).
    assert config_natural.gamma == 0.94

    # Test explicit preset arg overrides config
    config_override = GradeConfig.from_config(cfg, preset="original")
    assert config_override.preset == "original"
    # Overrides are ignored when preset is explicitly passed
    assert config_override.gamma == 1.0  # Original default


def test_download_font(tmp_path):
    target = tmp_path / "testfont.ttf"

    # Test valid download
    with patch("src.styling.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = b"A" * 5000  # > 4096 bytes
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        assert _download_font("http://example.com/font.ttf", target)
        assert target.exists()

    # Test invalid download (too small)
    target_small = tmp_path / "small.ttf"
    with patch("src.styling.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = b"A" * 1000  # < 4096 bytes
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        assert not _download_font("http://example.com/font.ttf", target_small)
        assert not target_small.exists()

    # Test download error
    target_err = tmp_path / "err.ttf"
    with patch("src.styling.urlopen", side_effect=ValueError("Failed")):
        assert not _download_font("http://example.com/font.ttf", target_err)
        assert not target_err.exists()


def test_resolve_font_path_impl(tmp_path):
    # Test configured path exists
    configured_font = tmp_path / "conf.ttf"
    configured_font.touch()

    with patch("src.styling.expand_path", return_value=configured_font):
        path = _resolve_font_path_impl(str(configured_font), False, (), "bold")
        assert path == configured_font

    # Test auto download
    # We mock _ensure_font
    with patch("src.styling._ensure_font", return_value=True):
        non_existent = tmp_path / "nonexistent.ttf"
        with patch("src.styling.expand_path", return_value=non_existent):
            path = _resolve_font_path_impl(str(non_existent), True, (), "bold")
            assert path == non_existent

    # Test fallbacks
    fallback_font = tmp_path / "fallback.ttf"
    fallback_font.touch()
    path = _resolve_font_path_impl(None, False, (str(fallback_font),), "bold")
    assert path == fallback_font

    # Test none found
    path = _resolve_font_path_impl(None, False, (), "bold")
    assert path is None


def test_typography_measurements():
    img = Image.new("RGBA", (100, 100))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    # _text_width
    width = _text_width(draw, "Test", font, tracking=2)
    assert width > 0
    assert _text_width(draw, "", font, 2) == 0.0

    # _line_height
    height = _line_height(font)
    assert height > 0

    # _fit_line
    assert _fit_line(draw, "Short", font, tracking=0, max_width=1000) == "Short"
    fitted = _fit_line(draw, "Very long text that needs to be truncated", font, tracking=0, max_width=20)
    assert fitted.endswith("…")

    # wrap_text
    lines = wrap_text(draw, "Word1 Word2 Word3", font, tracking=0, max_width=1000)
    assert lines == ["Word1 Word2 Word3"]

    lines_narrow = wrap_text(draw, "Word1 Word2 Word3", font, tracking=0, max_width=10)
    # Should aggressively wrap or truncate depending on width, but max_lines=3 limits it
    assert len(lines_narrow) <= 3


def test_build_text_overlay():
    cfg = {}

    # Test empty output
    overlay = build_text_overlay(cfg, title="", subtitle="", geo_tag="")
    # Check if empty (all transparent) - Image.getextrema might be useful but we just verify it runs
    assert overlay.size == (1080, 1920)

    # Test with text
    # Need to patch load_font because system might not have the web fonts or DejaVu
    with patch("src.styling.load_font", return_value=ImageFont.load_default()):
        overlay = build_text_overlay(cfg, title="My Title", subtitle="Subtitle", geo_tag="Hamburg")
        assert overlay.size == (1080, 1920)

        # Test offset edge cases and uppercase
        cfg_custom = {
            "style": {
                "text": {
                    "shadow_offset": [5],  # Should auto pad to [5.0, 0.0]
                    "shadow_alpha": 1.0,
                    "uppercase_title": False,
                    "shadow_blur": 2,
                }
            }
        }
        overlay_custom = build_text_overlay(cfg_custom, title="lower case title", subtitle="", geo_tag="")
        assert overlay_custom.size == (1080, 1920)
