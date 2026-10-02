"""Dark-Film-Grading, Center-Crop und Typografie-Overlay.

Das Grading arbeitet in zwei Stufen:

1. eine 1D-Tonwertkurve (Gamma, S-Kurve, Matte Black Toe, Highlight-Roll-Off),
   die als 256er-LUT via OpenCV auf das ganze Bild gelegt wird,
2. eine Farbstufe (Entsättigung, Cine-Teal in den Schatten, kühle Highlights)
   als vektorisierte Numpy-Operation.

Die Typografie wird als transparente RGBA-Overlay-Bild gerendert (Pillow) und
rechtsbündig verankert - ohne Box, mit weichem Drop-Shadow.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from logging import getLogger
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import expand_path, get

log = getLogger("tenor.styling")

try:  # OpenCV ist nur für die LUT-Anwendung nötig, numpy-Fallback existiert
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

#: Font-Quellen, in Reihenfolge probiert. Bewährte Mirrors mit Apache-2.0/OFL-Lizenz.
FONT_URLS: dict[str, tuple[tuple[str, float | None], ...]] = {
    # (URL, weight fuer Variable Fonts)
    "bold": (
        ("https://raw.githubusercontent.com/googlefonts/roboto-2/main/src/hinted/Roboto-Bold.ttf", None),
        ("https://raw.githubusercontent.com/google/fonts/main/ofl/roboto/Roboto%5Bwdth,wght%5D.ttf", 700.0),
    ),
    "regular": (
        ("https://raw.githubusercontent.com/googlefonts/roboto-2/main/src/hinted/Roboto-Regular.ttf", None),
        ("https://raw.githubusercontent.com/google/fonts/main/ofl/roboto/Roboto%5Bwdth,wght%5D.ttf", 400.0),
    ),
    "mono": (
        (
            "https://raw.githubusercontent.com/google/fonts/main"
            "/ofl/robotomono/RobotoMono%5Bwght%5D.ttf",
            400.0,
        ),
    ),
}


GRADE_PRESETS: dict[str, dict[str, float]] = {
    "natural": {
        "gamma": 1.0,
        "contrast": 0.02,
        "black_lift": 0.0,
        "black_toe": 1.0,
        "highlight_ceiling": 1.0,
        "saturation": 1.03,
        "shadow_desaturate": 0.0,
        "teal_red": 0.0,
        "teal_green": 0.0,
        "teal_blue": 0.0,
        "highlight_cool": 0.0,
        "skin_damp": 0.0,
    },
    "original": {
        "gamma": 1.0,
        "contrast": 0.0,
        "black_lift": 0.0,
        "black_toe": 1.0,
        "highlight_ceiling": 1.0,
        "saturation": 1.0,
        "shadow_desaturate": 0.0,
        "teal_red": 0.0,
        "teal_green": 0.0,
        "teal_blue": 0.0,
        "highlight_cool": 0.0,
        "skin_damp": 0.0,
    },
    "cinematic": {
        "gamma": 0.94,
        "contrast": 0.18,
        "black_lift": 0.015,
        "black_toe": 2.0,
        "highlight_ceiling": 0.94,
        "saturation": 0.92,
        "shadow_desaturate": 0.10,
        "teal_red": -0.004,
        "teal_green": 0.008,
        "teal_blue": 0.018,
        "highlight_cool": 0.008,
        "skin_damp": 0.005,
    },
}


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GradeConfig:
    """Color-Grading-Parameter, eingelesen aus ``style:`` in der config.yaml."""

    gamma: float = 1.0
    contrast: float = 0.0
    black_lift: float = 0.0
    black_toe: float = 1.0
    highlight_ceiling: float = 1.0
    saturation: float = 1.0
    shadow_desaturate: float = 0.0
    teal_red: float = 0.0
    teal_green: float = 0.0
    teal_blue: float = 0.0
    highlight_cool: float = 0.0
    skin_damp: float = 0.0
    preset: str = "original"

    @property
    def is_identity(self) -> bool:
        """Prüft, ob das Grading wirkungslos (Identitätsabbildung) ist."""
        return (
            abs(self.gamma - 1.0) < 1e-4
            and abs(self.contrast) < 1e-4
            and self.black_lift < 1e-4
            and self.highlight_ceiling >= 0.999
            and abs(self.saturation - 1.0) < 1e-4
            and self.shadow_desaturate < 1e-4
            and abs(self.teal_red) < 1e-4
            and abs(self.teal_green) < 1e-4
            and abs(self.teal_blue) < 1e-4
            and self.highlight_cool < 1e-4
            and self.skin_damp < 1e-4
        )

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], preset: str | None = None) -> GradeConfig:
        style = cfg.get("style") if isinstance(cfg.get("style"), Mapping) else {}
        style = style or {}
        chosen_preset = (preset or str(style.get("preset") or "original")).lower()
        base_params = dict(GRADE_PRESETS.get(chosen_preset, GRADE_PRESETS["original"]))
        known = {f for f in cls.__dataclass_fields__ if f != "preset"}  # type: ignore[attr-defined]
        for k, v in style.items():
            if k in known and v is not None and preset is None:
                try:
                    val = float(v)
                    # Wenn die config.yaml die Standardwerte des "natural" Presets enthält,
                    # und ein anderes Preset gewählt ist, ignorieren wir diese Werte,
                    # es sei denn, sie wurden vom Nutzer explizit geändert.
                    if chosen_preset != "natural":
                        natural_default = GRADE_PRESETS.get("natural", {}).get(k)
                        if natural_default is not None and abs(val - natural_default) < 1e-6:
                            continue
                    base_params[k] = val
                except (ValueError, TypeError):
                    pass
        return cls(preset=chosen_preset, **base_params)

    def tone_curve(self, samples: int = 256) -> np.ndarray:
        """1D-Tonwertkurve auf 0..1, 255 Stützstellen.

        Reihenfolge: Gamma -> S-Kurve -> Matte Black Toe -> Highlight-Roll-Off.

        ``gamma`` ist dabei als *Belichtungsregler* zu lesen: Werte **unter** 1.0
        unterbelichten die Mitteltöne (0.93 entspricht ca. -0,1 Blende), Werte
        über 1.0 hellen auf. Technisch wird ``x ** (1/gamma)`` gerechnet, damit
        0.92-0.95 wie im Plan eine düstere Grundstimmung erzeugt.
        """
        x = np.linspace(0.0, 1.0, samples, dtype=np.float32)

        # 1) Mitteltöne leicht unterbelichten (gamma < 1 => dunkler)
        gamma = self.gamma if self.gamma > 1e-6 else 1.0
        x = np.power(np.clip(x, 0.0, 1.0), 1.0 / gamma, dtype=np.float32)

        # 2) sanfte S-Kurve für denKontrast (smoothstep als Basis)
        smooth = x * x * (3.0 - 2.0 * x)
        blend = float(np.clip(self.contrast, 0.0, 1.0))
        x = x + blend * (smooth - x)

        # 3) Matte Black Toe: leicht angehobener, matter Schwarzpunkt
        lift = max(self.black_lift, 0.0)
        if lift:
            toe = max(self.black_toe, 1.0)
            x = x + lift * np.power(np.clip(1.0 - x, 0.0, 1.0), toe, dtype=np.float32)

        # 4) Highlight-Roll-Off: weiches Klemmen statt hartem Abschneiden
        ceiling = float(np.clip(self.highlight_ceiling, 0.5, 1.0))
        knee = ceiling * 0.72
        if ceiling < 1.0:
            headroom = max(ceiling - knee, 1e-4)
            over = x > knee
            if np.any(over):
                x = np.array(x, dtype=np.float32, copy=True)
                x[over] = knee + headroom * np.tanh((x[over] - knee) / headroom)

        return np.clip(x, 0.0, 1.0).astype(np.float32)

    def lut8(self) -> np.ndarray:
        """Kurve als uint8-LUT für :func:`cv2.LUT`."""
        return np.clip(np.round(self.tone_curve() * 255.0), 0, 255).astype(np.uint8)


def _build_lut(cfg: GradeConfig) -> np.ndarray:
    return cfg.lut8()


def apply_grade(frame: np.ndarray, lut: np.ndarray, grade: GradeConfig) -> np.ndarray:
    """Wendet Dark-Film-Grading auf ein einzelnes RGB-Frame (uint8, HxWx3) an.

    Erwartet und liefert ``np.ndarray`` mit ``dtype=uint8``.
    """
    array = np.asarray(frame)
    if array.ndim == 2:  # Graustufen
        array = np.repeat(array[:, :, None], 3, axis=2)
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    if array.shape[2] == 4:  # RGBA -> RGB
        array = array[:, :, :3]

    # --- Stufe 1: Tonwertkurve via LUT ---------------------------------- #
    if cv2 is not None:
        toned = cv2.LUT(array, lut)
    else:  # numpy-Fallback
        toned = lut[array]

    # --- Stufe 2: Farbstufe --------------------------------------------- #
    img = toned.astype(np.float32) * (1.0 / 255.0)
    luma = img[:, :, 0] * 0.2126 + img[:, :, 1] * 0.7152 + img[:, :, 2] * 0.0722

    # Schatten: entsättigen und kühlen (Cine-Teal/Slate)
    shadow_w = np.square(1.0 - luma)
    sat = np.clip(
        grade.saturation * (1.0 - grade.shadow_desaturate * shadow_w), 0.0, 2.0
    )[:, :, None]
    luma3 = luma[:, :, None]
    img = luma3 + sat * (img - luma3)

    img[:, :, 0] += grade.teal_red * shadow_w
    img[:, :, 1] += grade.teal_green * shadow_w
    img[:, :, 2] += grade.teal_blue * shadow_w

    # Highlights leicht kühlen, damit der Himmel nicht digital ausbrennt
    img[:, :, 2] += grade.highlight_cool * np.power(luma, 3.0)

    # Haut-/Erdtöne in den Mitteltönen dezent dämpfen
    img[:, :, 0] -= grade.skin_damp * (4.0 * luma * np.power(1.0 - luma, 3.0))

    np.clip(img, 0.0, 1.0, out=img)
    return np.round(img * 255.0).astype(np.uint8)


def make_grader(grade: GradeConfig) -> Any:
    """Erzeugt eine moviepy-taugliche ``image_func`` mit gecachter LUT.

    Gibt ``None`` zurück, wenn das Grading eine Identitätsabbildung ist
    (z. B. Preset 'original' oder neutrale Werte), um unnötige Bildkopien zu sparen.
    """
    if grade.is_identity:
        return None

    lut = _build_lut(grade)

    def grade_frame(frame: np.ndarray) -> np.ndarray:
        return apply_grade(frame, lut, grade)

    return grade_frame


# --------------------------------------------------------------------------- #
# Crop / Skalierung
# --------------------------------------------------------------------------- #
def target_size(cfg: Mapping[str, Any]) -> tuple[int, int]:
    return (int(get(cfg, "video.width", 1080)), int(get(cfg, "video.height", 1920)))


def center_crop_resize(clip: Any, width: int, height: int) -> Any:
    """Center-Crop auf 9:16: skaliert so, dass die kleinere Seite passt, dann mittig beschneiden."""
    target_ratio = width / height
    w, h = clip.size
    if w <= 0 or h <= 0:
        return clip.resized((width, height))

    source_ratio = w / h
    if abs(source_ratio - target_ratio) < 1e-4:
        return clip.resized((width, height))

    if source_ratio > target_ratio:  # zu breit -> links/rechts beschneiden
        new_width = int(round(h * target_ratio))
        if new_width % 2:
            new_width -= 1
        x_center = w / 2
        x1 = max(int(round(x_center - new_width / 2)), 0)
        x2 = min(x1 + new_width, w)
        cropped = clip.cropped(x1=x1, y1=0, x2=x2, y2=h)
    else:  # zu hoch/zu schmal -> oben/unten beschneiden
        new_height = int(round(w / target_ratio))
        if new_height % 2:
            new_height -= 1
        y_center = h / 2
        y1 = max(int(round(y_center - new_height / 2)), 0)
        y2 = min(y1 + new_height, h)
        cropped = clip.cropped(x1=0, y1=y1, x2=w, y2=y2)

    return cropped.resized((width, height))


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
def _download_font(url: str, target: Path) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "tenor/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - feste Mirror-URLs
            payload = response.read()
        if len(payload) < 4096:
            log.warning("Font-Download zu klein, wird verworfen: %s", url)
            return False
        tmp.write_bytes(payload)
        tmp.replace(target)
        log.info("Font geladen: %s", target)
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("Font-Download fehlgeschlagen (%s): %s", url, exc)
        tmp.unlink(missing_ok=True)
        return False


def _ensure_font(kind: str, target: Path) -> bool:
    """Lädt einen Font, probiert alle Mirror nacheinander."""
    for url, _weight in FONT_URLS.get(kind, ()):
        if _download_font(url, target):
            return True
    log.warning("Kein Download für '%s' möglich - Systemfont wird genutzt", kind)
    return False


def resolve_font_path(cfg: Mapping[str, Any], kind: str) -> Path | None:
    """Findet (oder lädt) die passende Font-Datei für ``bold``/``regular``/``mono``."""
    configured = get(cfg, f"fonts.{kind}")
    if configured:
        candidate = expand_path(configured)
        if candidate.is_file():
            return candidate
        if bool(get(cfg, "fonts.auto_download", True)) and kind in FONT_URLS:
            if _ensure_font(kind, candidate):
                return candidate

    fallbacks = get(cfg, "fonts.system_fallbacks", []) or []
    for fallback in fallbacks:
        path = Path(str(fallback)).expanduser()
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=32)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def load_font(cfg: Mapping[str, Any], kind: str, size: int) -> ImageFont.FreeTypeFont:
    """Font laden; als letzte Option der DejaVu-Fallback von Pillow."""
    path = resolve_font_path(cfg, kind)
    if path is not None:
        try:
            return _load_font(str(path), int(size))
        except OSError as exc:
            log.warning("Font %s nicht ladbar (%s), nutze Fallback", path, exc)
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if kind == "bold" else "DejaVuSans.ttf", int(size))
    except OSError:  # pragma: no cover
        return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# Typografie
# --------------------------------------------------------------------------- #
def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, tracking: int) -> float:
    if not text:
        return 0.0
    widths = [draw.textlength(ch, font=font) for ch in text]
    return float(sum(widths) + tracking * max(len(text) - 1, 0))


def _draw_tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Any,
    tracking: int,
    anchor_right: float | None = None,
) -> float:
    """Zeichnet Text zeichenweise (für Letter-Spacing), rechtsbündig verankert."""
    if not text:
        return 0.0
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = float(sum(widths) + tracking * max(len(text) - 1, 0))
    x = (anchor_right - total) if anchor_right is not None else xy[0]
    y = xy[1]
    for char, width in zip(text, widths):
        draw.text((x, y), char, font=font, fill=fill, anchor="lt")
        x += width + tracking
    return total


def _fit_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    tracking: int,
    max_width: float,
) -> str:
    """Kürzt einen Text zeichenweise mit Ellipse auf ``max_width``."""
    if _text_width(draw, text, font, tracking) <= max_width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and _text_width(draw, trimmed + ellipsis, font, tracking) > max_width:
        trimmed = trimmed[:-1].rstrip()
    return f"{trimmed}{ellipsis}" if trimmed else ellipsis


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    tracking: int,
    max_width: float,
    max_lines: int = 3,
) -> list[str]:
    """Bricht Text an Wortgrenzen auf ``max_width`` um (Zeichen-Tracking berücksichtigt)."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for index, word in enumerate(words[1:], start=1):
        candidate = f"{current} {word}"
        if _text_width(draw, candidate, font, tracking) <= max_width:
            current = candidate
            continue
        lines.append(current)
        if len(lines) >= max_lines:
            return [_fit_line(draw, " ".join(words[index:]), font, tracking, max_width)]
        current = word
    lines.append(current)
    return lines[:max_lines]


def _line_height(font: ImageFont.FreeTypeFont) -> float:
    bbox = font.getbbox("Ag")
    return float(bbox[3] - bbox[1])


def build_text_overlay(
    cfg: Mapping[str, Any],
    *,
    title: str,
    subtitle: str = "",
    width: int | None = None,
    height: int | None = None,
) -> Image.Image:
    """Rendert das rechtsbündige Text-Overlay (RGBA, 1080x1920) mit Drop-Shadow.

    Keine Boxen, kein Badge - nur Typografie plus weicher Schatten für Kontrast
    über hellen Bildstellen.
    """
    out_w = width or target_size(cfg)[0]
    out_h = height or target_size(cfg)[1]
    image = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))

    text_cfg = get(cfg, "style.text", {}) or {}
    anchor_x = float(text_cfg.get("anchor_x", out_w - 180))
    y_title = float(text_cfg.get("y_title", 1445))
    max_width = float(text_cfg.get("max_width", max(anchor_x - 120, 200)))
    line_gap = float(text_cfg.get("line_gap", 16))
    safe_bottom = float(text_cfg.get("safe_bottom", 1540))
    offset = [float(v) for v in (text_cfg.get("shadow_offset", [2, 2]) or [2, 2])]
    while len(offset) < 2:
        offset.append(0.0)
    shadow_blur = float(text_cfg.get("shadow_blur", 4) or 0)
    shadow_alpha = float(np.clip(text_cfg.get("shadow_alpha", 0.7), 0.0, 1.0))
    uppercase = bool(text_cfg.get("uppercase_title", True))

    title_text = (title or "").strip()
    if uppercase and title_text:
        title_text = title_text.upper()
    subtitle_text = (subtitle or "").strip()

    title_font = load_font(cfg, "bold", int(text_cfg.get("title_size", 36)))
    subtitle_font = load_font(cfg, "regular", int(text_cfg.get("subtitle_size", 24)))
    title_tracking = int(text_cfg.get("title_tracking", 3))
    subtitle_tracking = int(text_cfg.get("subtitle_tracking", 1))

    measure = ImageDraw.Draw(image)
    title_lines = wrap_text(measure, title_text, title_font, title_tracking, max_width) if title_text else []
    subtitle_lines = (
        wrap_text(measure, subtitle_text, subtitle_font, subtitle_tracking, max_width, max_lines=2)
        if subtitle_text
        else []
    )
    if not title_lines and not subtitle_lines:
        return image

    # (Zeilen, Font, Farbe, Tracking, Zeilenhöhe)
    blocks: list[tuple[list[str], Any, str, int, float]] = []
    if title_lines:
        blocks.append(
            (
                title_lines,
                title_font,
                str(text_cfg.get("title_color", "#FFFFFF")),
                title_tracking,
                _line_height(title_font),
            )
        )
    if subtitle_lines:
        blocks.append(
            (
                subtitle_lines,
                subtitle_font,
                str(text_cfg.get("subtitle_color", "#D0D5DD")),
                subtitle_tracking,
                _line_height(subtitle_font),
            )
        )

    total_height = sum(line_h * len(lines) for lines, _f, _c, _t, line_h in blocks)
    total_height += line_gap * (len(blocks) - 1)

    # Textgruppe in der Safe Zone halten (1440-1540 px), sonst nach oben schieben
    y_start = y_title
    if y_start + total_height > safe_bottom:
        y_start = safe_bottom - total_height
    y_start = max(y_start, 80.0)

    # Zeilen mit Position auflösen
    placed: list[tuple[str, Any, str, int, float]] = []
    y = y_start
    for lines, font, color, tracking, line_h in blocks:
        for line in lines:
            placed.append((line, font, color, tracking, y))
            y += line_h
        y += line_gap

    # Schatten-Layer (unscharf, schwarz, Alpha aus shadow_alpha)
    if shadow_alpha > 0.0:
        shadow_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shadow_fill = (0, 0, 0, int(round(255 * shadow_alpha)))
        for line, font, _color, tracking, line_y in placed:
            _draw_tracked(
                shadow_draw,
                (0, line_y + offset[1]),
                line,
                font,
                shadow_fill,
                tracking,
                anchor_right=anchor_x - offset[0],
            )
        if shadow_blur > 0:
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
        image = Image.alpha_composite(image, shadow_layer)

    # Text-Layer
    text_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    for line, font, color, tracking, line_y in placed:
        _draw_tracked(text_draw, (0, line_y), line, font, color, tracking, anchor_right=anchor_x)
    return Image.alpha_composite(image, text_layer)


def build_subtitle(place: str | None, date_label: str) -> str:
    """Zeile 2: Ort + Datum, jeweils optional."""
    parts = [p for p in (place, date_label) if p]
    return "  ·  ".join(parts)

