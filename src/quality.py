"""Bildqualitäts-Analyse für die Cut-Auswahl.

Alle vergleichbaren Reel-Pipelines prüfen *vor* dem Schnitt, ob im Bild
überhaupt etwas zu sehen ist: schwarze Frames, verwackelte Schwenks und
angeschnittene Motive landen sonst genauso im Reel wie gute Aufnahmen. Hier
wird deshalb pro Rohclip ein schmaler Graustufen-Strip einmalig via ffmpeg
gezogen und daraus Fenster-Scores berechnet.

Zwei unabhängige Kriterien:

``Lokal`` (Schärfe / Belichtung)
    Laplacian-Varianz für Schärfe sowie mittlere Luminanz plus Schwarz-/
    Weißanteil. Gemessen wird **vor** dem Grading, weil das Dark-Film-Look
    Luma bewusst absenkt - sonst würde jedes brauchbare Material durchfallen.

``Global`` (Wiederholung)
    16x16-Signatur pro Fenster. Zwei fast identische Signaturen bedeuten
    denselben Moment zweimal im Reel - bei iPhone-Bursts häufig, weil
    mehrere Dateien denselben Sekundenbereich aufnehmen.

Die Scores sind reine NumPy-Operationen auf dem Strip. Nach dem einmaligen
ffmpeg-Durchlauf kostet das Verschieben eines Cuts nur noch Mikrosekunden,
deshalb durchsucht :func:`plan_windows` mehrere Varianten pro Cut.

``plan_windows`` ist bewusst so gebaut, dass es nie eine Ausnahme wirft:
eine kaputte Datei, ein fehlendes ffmpeg oder ein Timeout führen dazu, dass
das Gate übersprungen wird - nie dazu, dass Material verloren geht.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

log = logging.getLogger("tenor.quality")

# Zielhöhe des Strips. 90 px reicht für eine Laplacian-Varianz, kostet aber
# nur ~14 kB pro Frame - ein 10-s-Clip bei 4 fps ist rund 140 kB.
STRIP_HEIGHT = 90
# 16x16 = 256 Merkmale pro Frame. 8x8 trennte die Kalibrierungsdaten nur um
# Faktor 2,45, 16x16 um 2,72.
SIGNATURE_SIZE = 16

# Die Schwellen unten sind an den echten Clips ausgemessen, nicht geraten.
# Wertebereich der Referenzaufnahmen (11 iPhone-Clips, 4K HEVC):
#   Schärfe (Laplacian-Var.)  3 300 - 35 000
#   Luma                     99 - 187
#   Schwarzanteil            0 - 4 %
#   Weißanteil               0 - 10 %
# Die Grenzen liegen bewusst weit unterhalb/oberhalb, damit echtes Material
# nie durchfällt und nur wirklich kaputtes Material aussortiert wird.


@dataclass
class QualityConfig:
    """Schwellen für das Qualitäts-Gate. Alles ist über config.yaml steuerbar."""

    enabled: bool = True
    sample_fps: float = 4.0
    # Erste/letzte Sekunden eines Clips ausschließen: dort ist bei
    # iPhone-Clips meistens die Hand noch im Bild oder die Kamera richtet
    # sich erst noch aus.
    edge_trim: float = 0.25
    # Lokale Kriterien. Unter- bzw. Obergrenzen mit 0 sind aus; bei den
    # Anteilen ist der ausgeschaltete Wert 1,0 (100 % kann nicht übertroffen
    # werden), bei max_luma entsprechend 255.
    # Schärfe ist die Laplacian-Varianz des Graustufen-Strips - bei
    # gutem 4K-Material im dreistelligen Bereich, nicht in Prozent.
    min_sharpness: float = 2500.0
    min_luma: float = 35.0
    max_luma: float = 242.0
    max_black_ratio: float = 0.35
    max_white_ratio: float = 0.25
    # Neu: Metriken für visuelles Interesse (Bewegung, Gesichter, Farbvarianz).
    min_motion: float = 0.0
    min_faces: float = 0.0
    min_color_variance: float = 0.0
    # Global: mittlere absolute Differenz zweier 16x16-Signaturen (0..255).
    # Kalibriert, nicht geraten - gemessen an der tatsächlichen Schnittplanung
    # der 11 echten Madeira-Clips (15 Cuts, Vergleich gegen die drei
    # vorherigen):
    #   gleicher Moment, andere Datei (iPhone-Burst)   6,8 - 13,9
    #   zwei Cuts aus demselben statischen Clip        7,4 -  8,0
    #   verschiedene Clips / verschiedene Szenen      24,7 - 104,6
    # Beide unteren Gruppen sind echte Wiederholungen und gehören
    # raus; 20 liegt in der Lücke. 0 schaltet die Prüfung ab.
    duplicate_threshold: float = 20.0
    # Versuche, ein durchgefallenes Fenster zu verschieben (Sekunden).
    search_offsets: tuple[float, ...] = (0.3, -0.3, 0.6, -0.6, 0.9, -0.9)
    # Eine echte Wiederholung wird verworfen (sonst steht derselbe Moment
    # zweimal im Reel), unsauberes Bildmaterial wird behalten - bei einer
    # Reise ein weiches Bild ist besser als eine Lücke im Reel.
    drop_duplicates: bool = True
    keep_imperfect: bool = True
    # Gegen wie viele der zuletzt behaltenen Cuts wird verglichen? 1 erkennt
    # nur direkte Nachbarschaft, 3 fängt auch ein A-B-A-Muster ("Sandwich"),
    # bei dem dieselbe Szene mit einem anderen dazwischen wiederkehrt.
    duplicate_memory: int = 3
    cache_dir: str = ".cache/quality"
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if self.min_sharpness < 0:
            raise ValueError(f"min_sharpness darf nicht negativ sein (ist {self.min_sharpness})")
        if self.min_luma < 0:
            raise ValueError(f"min_luma darf nicht negativ sein (ist {self.min_luma})")
        if not (0 <= self.max_luma <= 255):
            raise ValueError(f"max_luma muss zwischen 0 und 255 liegen (ist {self.max_luma})")
        if not (0 <= self.max_black_ratio <= 1.0):
            raise ValueError(f"max_black_ratio muss zwischen 0 und 1 liegen (ist {self.max_black_ratio})")
        if not (0 <= self.max_white_ratio <= 1.0):
            raise ValueError(f"max_white_ratio muss zwischen 0 und 1 liegen (ist {self.max_white_ratio})")
        if self.min_motion < 0:
            raise ValueError(f"min_motion darf nicht negativ sein (ist {self.min_motion})")
        if self.min_faces < 0:
            raise ValueError(f"min_faces darf nicht negativ sein (ist {self.min_faces})")
        if self.min_color_variance < 0:
            raise ValueError(f"min_color_variance darf nicht negativ sein (ist {self.min_color_variance})")
        if self.duplicate_threshold < 0:
            raise ValueError(f"duplicate_threshold darf nicht negativ sein (ist {self.duplicate_threshold})")

    @classmethod
    def from_config(cls, cfg: Any) -> QualityConfig:
        raw = cfg if isinstance(cfg, dict) else {}

        def number(key: str, fallback: float) -> float:
            """Liest eine Zahl, ohne an einem verkorksten YAML zu scheitern.

            Ein Tippfehler in der Config darf nicht den ganzen Lauf kippen -
            der Default ist immer ein Wert, mit dem die Pipeline läuft.
            """
            value = raw.get(key, fallback)
            if isinstance(value, bool):
                return fallback
            try:
                number_value = float(value)
            except (TypeError, ValueError):
                log.warning("quality.%s: %r ist keine Zahl - nutze %g", key, value, fallback)
                return fallback
            if not math.isfinite(number_value):
                log.warning("quality.%s: %r ist nicht endlich - nutze %g", key, value, fallback)
                return fallback
            return number_value

        offsets = raw.get("search_offsets")
        if isinstance(offsets, (list, tuple)) and offsets:
            parsed: tuple[float, ...] = ()
            for value in offsets:
                try:
                    offset = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(offset) and offset:
                    parsed += (offset,)
        else:
            parsed = cls.search_offsets
        if not parsed:
            log.warning("quality.search_offsets ohne brauchbaren Wert - nutze %r", cls.search_offsets)
            parsed = cls.search_offsets

        return cls(
            enabled=bool(raw.get("enabled", True)),
            sample_fps=max(number("sample_fps", 4.0), 0.5),
            edge_trim=max(number("edge_trim", 0.25), 0.0),
            min_sharpness=max(number("min_sharpness", 2500.0), 0.0),
            min_luma=max(number("min_luma", 35.0), 0.0),
            max_luma=min(max(number("max_luma", 242.0), 0.0), 255.0),
            max_black_ratio=min(max(number("max_black_ratio", 0.35), 0.0), 1.0),
            max_white_ratio=min(max(number("max_white_ratio", 0.25), 0.0), 1.0),
            min_motion=max(number("min_motion", 0.0), 0.0),
            min_faces=max(number("min_faces", 0.0), 0.0),
            min_color_variance=max(number("min_color_variance", 0.0), 0.0),
            duplicate_threshold=max(number("duplicate_threshold", 20.0), 0.0),
            search_offsets=parsed,
            drop_duplicates=bool(raw.get("drop_duplicates", True)),
            keep_imperfect=bool(raw.get("keep_imperfect", True)),
            duplicate_memory=max(int(number("duplicate_memory", 3.0)), 1),
            cache_dir=str(raw.get("cache_dir") or ".cache/quality"),
            timeout=max(number("timeout", 60.0), 5.0),
        )


@dataclass
class ClipAnalysis:
    """Frame-Statistiken eines Rohclips, auf einem Zeitraster abgetastet."""

    path: Path
    duration: float
    sample_fps: float
    sharpness: np.ndarray  # (N,) float32 - Laplacian-Varianz
    luma: np.ndarray  # (N,) float32 - Mittelwert 0..255
    black_ratio: np.ndarray  # (N,) float32 - Anteil < 16
    white_ratio: np.ndarray  # (N,) float32 - Anteil > 240
    signatures: np.ndarray  # (N, 64) float32
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.error is None and self.luma.size > 0

    def times(self) -> np.ndarray:
        """Zeitstempel der abgetasteten Frames (Sekunden)."""
        if self.sample_fps <= 0:
            return np.zeros(0, dtype=np.float32)
        return np.arange(self.luma.size, dtype=np.float32) / self.sample_fps


def _strip_size(width: int, height: int) -> tuple[int, int]:
    """Zielgröße des Strips, Seitenverhältnis erhalten, gerade Zahlen."""
    if width <= 0 or height <= 0:
        return 160, STRIP_HEIGHT
    scaled_width = int(round(width / height * STRIP_HEIGHT))
    # gerade Zahl, damit ffmpeg keine Filter einfügen muss
    scaled_width = max(16, scaled_width - (scaled_width % 2))
    return scaled_width, STRIP_HEIGHT


def _empty_stats() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.zeros(0, dtype=np.float32),
        np.zeros(0, dtype=np.float32),
        np.zeros(0, dtype=np.float32),
        np.zeros(0, dtype=np.float32),
        np.zeros((0, SIGNATURE_SIZE * SIGNATURE_SIZE), dtype=np.float32),
    )


def _safe_path(path: Path | str) -> str:
    """Konvertiert einen Pfad in einen String und verhindert Option-Injection in CLI-Befehlen.

    Pfade, die mit einem Bindestrich ('-') beginnen, werden mit './' präfigiert,
    damit Werkzeuge wie ffmpeg sie als Dateipfad und nicht als CLI-Option interpretieren.
    """
    s = str(path)
    if s.startswith("-"):
        return f"./{s}"
    return s


def _extract_and_score_strip(
    path: Path, width: int, height: int, sample_fps: float, timeout: float
) -> tuple[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray], str | None]:
    """Ein OpenCV-Durchlauf, der Frames dekodiert und bewertet."""
    strip_w, strip_h = _strip_size(width, height)
    frame_size = strip_w * strip_h
    if frame_size <= 0:
        return _empty_stats(), "invalid strip size"

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return _empty_stats(), "VideoCapture konnte Datei nicht öffnen"

    all_sharpness = []
    all_luma = []
    all_black = []
    all_white = []
    all_signatures = []

    try:
        source_fps = cap.get(cv2.CAP_PROP_FPS)
        if source_fps <= 0:
            source_fps = 30.0

        # Berechne wie viele Source Frames für 1 Sample Frame übersprungen werden müssen.
        # Um bei sample_fps zu bleiben.
        frame_interval = max(1.0, source_fps / sample_fps)

        current_frame = 0.0
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (strip_w, strip_h), interpolation=cv2.INTER_AREA)

            laplacian = cv2.Laplacian(small, cv2.CV_32F, ksize=3)
            all_sharpness.append(float(laplacian.var()))
            all_luma.append(float(small.mean()))
            all_black.append(float((small < 16).mean()))
            all_white.append(float((small > 240).mean()))

            sig_small = cv2.resize(small, (SIGNATURE_SIZE, SIGNATURE_SIZE), interpolation=cv2.INTER_AREA)
            all_signatures.append(sig_small.astype(np.float32).ravel())

            current_frame += frame_interval

            # Skip frames using grab instead of cap.set for performance
            frames_to_skip = int(current_frame) - int(current_frame - frame_interval) - 1
            if frames_to_skip > 0:
                for _ in range(frames_to_skip):
                    if not cap.grab():
                        break

    except Exception as e:
        log.warning("Fehler beim Dekodieren von %s: %s", path.name, e)
    finally:
        cap.release()

    if len(all_sharpness) == 0:
        return _empty_stats(), "keine Frames dekodiert"

    res_sharpness = np.array(all_sharpness, dtype=np.float32)
    res_luma = np.array(all_luma, dtype=np.float32)
    res_black = np.array(all_black, dtype=np.float32)
    res_white = np.array(all_white, dtype=np.float32)
    res_signatures = np.array(all_signatures, dtype=np.float32)

    return (res_sharpness, res_luma, res_black, res_white, res_signatures), None


def _cache_key(path: Path, cfg: QualityConfig) -> str:
    """Cache-Key aus Pfad, mtime, Größe und Analyse-Parametern."""
    try:
        stat = path.stat()
        stamp = f"{path.resolve()}|{stat.st_mtime:.3f}|{stat.st_size}|{stat.st_ino}"
    except OSError:
        stamp = str(path)
    payload = f"{stamp}|{cfg.sample_fps}|{STRIP_HEIGHT}|{SIGNATURE_SIZE}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _cache_paths(cache_dir: Path, key: str) -> tuple[Path, Path]:
    return cache_dir / f"{key}.npz", cache_dir / f"{key}.json"


def _load_cache(npz: Path, meta: Path) -> tuple[ClipAnalysis, Path] | None:
    if not (npz.is_file() and meta.is_file()):
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        with np.load(npz) as data:
            analysis = ClipAnalysis(
                path=Path(info["path"]),
                duration=float(info["duration"]),
                sample_fps=float(info["sample_fps"]),
                sharpness=data["sharpness"],
                luma=data["luma"],
                black_ratio=data["black"],
                white_ratio=data["white"],
                signatures=data["signatures"],
            )
    except (OSError, ValueError, KeyError) as exc:
        log.debug("Qualitäts-Cache unbrauchbar (%s): %s", npz.name, exc)
        return None
    return analysis, info["path"]


def _store_cache(npz: Path, meta: Path, analysis: ClipAnalysis) -> None:
    try:
        npz.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _temp_name(npz)
        try:
            with os.fdopen(fd, "wb") as fh:
                np.savez(
                    fh,
                    sharpness=analysis.sharpness,
                    luma=analysis.luma,
                    black_ratio=analysis.black_ratio,
                    white_ratio=analysis.white_ratio,
                    signatures=analysis.signatures,
                )
            os.replace(tmp, npz)
            meta.write_text(
                json.dumps({
                    "path": str(analysis.path),
                    "duration": analysis.duration,
                    "sample_fps": analysis.sample_fps,
                }),
                encoding="utf-8",
            )
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.debug("Qualitäts-Cache nicht schreibbar: %s", exc)


def _temp_name(target: Path) -> tuple[int, str]:
    import tempfile

    return tempfile.mkstemp(dir=str(target.parent), prefix=target.name, suffix=".tmp")


def analyse_clip(
    path: Path,
    *,
    width: int = 0,
    height: int = 0,
    duration: float = 0.0,
    cfg: QualityConfig | None = None,
) -> ClipAnalysis:
    """Analysiert einen Rohclip. Scheitert sie, kommt ein fehlertragendes
    Ergebnis zurück - die Pipeline darf nie daran scheitern."""
    config = cfg or QualityConfig()
    cache_dir = Path(config.cache_dir).expanduser()
    key = _cache_key(path, config)
    npz, meta = _cache_paths(cache_dir, key)

    cached = _load_cache(npz, meta)
    if cached is not None:
        analysis, _ = cached
        # Pfad ist der autoritative Wert (Cache ist nur Metadaten-Liste).
        analysis.path = path
        log.debug("Qualitäts-Cache hit: %s (%d Frames)", path.name, analysis.luma.size)
        return analysis

    stats, error = _extract_and_score_strip(path, width, height, config.sample_fps, config.timeout)
    sharpness, luma, black, white, signatures = stats

    if error:
        log.warning("Qualitätsanalyse für %s fehlgeschlagen: %s", path.name, error)
        return ClipAnalysis(
            path=path,
            duration=duration,
            sample_fps=config.sample_fps,
            sharpness=sharpness,
            luma=luma,
            black_ratio=black,
            white_ratio=white,
            signatures=signatures,
            error=error,
        )

    analysis = ClipAnalysis(
        path=path,
        duration=duration,
        sample_fps=config.sample_fps,
        sharpness=sharpness,
        luma=luma,
        black_ratio=black,
        white_ratio=white,
        signatures=signatures,
    )
    _store_cache(npz, meta, analysis)
    log.info(
        "Qualität %s: %d Frames, Schärfe %.0f-%.0f, Luma %.0f-%.0f",
        path.name,
        luma.size,
        float(sharpness.min()) if sharpness.size else 0.0,
        float(sharpness.max()) if sharpness.size else 0.0,
        float(luma.min()) if luma.size else 0.0,
        float(luma.max()) if luma.size else 0.0,
    )
    return analysis


@dataclass
class WindowScore:
    """Aggregierte Kennzahlen eines Cut-Fensters."""

    start: float
    end: float
    frames: int
    sharpness: float
    luma: float
    black_ratio: float
    white_ratio: float
    signature: np.ndarray
    motion: float = 0.0
    faces: float = 0.0
    color_variance: float = 0.0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _safe_mean(arr: np.ndarray, default: float = 0.0) -> float:
    if arr.size == 0:
        return default
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        m = float(np.nanmean(arr))
    return default if np.isnan(m) else m


def _safe_mean_signature(arr: np.ndarray) -> np.ndarray:
    if arr.size == 0:
        return np.zeros(SIGNATURE_SIZE * SIGNATURE_SIZE, dtype=np.float32)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        m = np.nanmean(arr, axis=0)
    return np.nan_to_num(m, nan=0.0).astype(np.float32)


def window_score(analysis: ClipAnalysis, start: float, end: float, cfg: QualityConfig) -> WindowScore:
    """Mittelwerte aller abgetasteten Frames, die in ``[start, end)`` liegen."""
    empty = np.zeros(SIGNATURE_SIZE * SIGNATURE_SIZE, dtype=np.float32)
    if not analysis.usable:
        return WindowScore(start, end, 0, 0.0, 0.0, 0.0, 0.0, empty, problems=["keine Analyse"])

    times = analysis.times()
    # Mitte der Frames erwischen: ein Fenster 1.4-2.9s enthält bei 4 fps
    # die Frames bei 1.5, 2.0, 2.5 - nicht den bei 1.25, der schon
    # außerhalb liegt.
    lo = np.searchsorted(times, start, side="left")
    hi = np.searchsorted(times, end, side="left")
    if hi <= lo:
        # Fenster kürzer als das Abtastraster: nimm den nächstliegenden Frame.
        index = int(np.clip(np.searchsorted(times, (start + end) / 2.0), 0, times.size - 1))
        lo, hi = index, index + 1

    motion = (
        _safe_mean(getattr(analysis, "motion", np.zeros(times.size))[lo:hi])
        if hasattr(analysis, "motion")
        else 0.0
    )
    faces = (
        _safe_mean(getattr(analysis, "faces", np.zeros(times.size))[lo:hi])
        if hasattr(analysis, "faces")
        else 0.0
    )
    color_variance = (
        _safe_mean(getattr(analysis, "color_variance", np.zeros(times.size))[lo:hi])
        if hasattr(analysis, "color_variance")
        else 0.0
    )

    return WindowScore(
        start=start,
        end=end,
        frames=int(hi - lo),
        sharpness=_safe_mean(analysis.sharpness[lo:hi]),
        luma=_safe_mean(analysis.luma[lo:hi]),
        black_ratio=_safe_mean(analysis.black_ratio[lo:hi]),
        white_ratio=_safe_mean(analysis.white_ratio[lo:hi]),
        signature=_safe_mean_signature(analysis.signatures[lo:hi]),
        motion=motion,
        faces=faces,
        color_variance=color_variance,
    )


def _local_problems(score: WindowScore, cfg: QualityConfig) -> list[str]:
    problems = []
    if cfg.min_sharpness > 0 and score.sharpness < cfg.min_sharpness:
        problems.append(f"unscharf (Schärfe {score.sharpness:.0f} < {cfg.min_sharpness:.0f})")
    if cfg.min_luma > 0 and score.luma < cfg.min_luma:
        problems.append(f"zu dunkel (Luma {score.luma:.0f} < {cfg.min_luma:.0f})")
    if cfg.max_luma < 255 and score.luma > cfg.max_luma:
        problems.append(f"überbelichtet (Luma {score.luma:.0f} > {cfg.max_luma:.0f})")
    # Bei Anteilen ist 1,0 (bzw. 255) der ausgeschaltete Wert - mehr als
    # 100 % schwarze Pixel kann es nicht geben.
    if 0 < cfg.max_black_ratio < 1.0 and score.black_ratio > cfg.max_black_ratio:
        problems.append(f"zu viel Schwarz ({score.black_ratio:.0%})")
    if 0 < cfg.max_white_ratio < 1.0 and score.white_ratio > cfg.max_white_ratio:
        problems.append(f"zu viel Weiß ({score.white_ratio:.0%})")

    if cfg.min_motion > 0 and score.motion < cfg.min_motion:
        problems.append(f"zu wenig Bewegung ({score.motion:.2f} < {cfg.min_motion:.2f})")
    if cfg.min_faces > 0 and score.faces < cfg.min_faces:
        problems.append(f"zu wenig Gesichter ({score.faces:.2f} < {cfg.min_faces:.2f})")
    if cfg.min_color_variance > 0 and score.color_variance < cfg.min_color_variance:
        problems.append(f"zu wenig Farbvarianz ({score.color_variance:.2f} < {cfg.min_color_variance:.2f})")

    return problems


def signature_distance(first: np.ndarray, second: np.ndarray) -> float:
    """Mittlere absolute Differenz zweier Signaturen (0..255)."""
    if first.size == 0 or second.size == 0 or first.size != second.size:
        return 255.0  # ohne Vergleichsbasis lieber "verschieden" annehmen
    return float(np.abs(first - second).mean())


def check_window(
    analysis: ClipAnalysis,
    start: float,
    end: float,
    cfg: QualityConfig,
    *,
    reference: np.ndarray | list[np.ndarray] | None = None,
) -> WindowScore:
    """Bewertet ein Fenster und sammelt die Gründe für ein Scheitern.

    ``reference`` ist eine Signatur oder eine Liste bereits gezeigter
    Signaturen; das Fenster gilt als Wiederholung, wenn es einer davon zu
    ähnlich ist. ``cfg.enabled: False`` macht jede Bewertung unauffällig.
    """
    score = window_score(analysis, start, end, cfg)
    if not cfg.enabled:
        return score
    if not score.frames:
        score.problems.append("kein Frame im Fenster")
        return score
    score.problems.extend(_local_problems(score, cfg))
    if score.problems:
        return score
    if cfg.duplicate_threshold > 0 and reference is not None:
        bases = [reference] if isinstance(reference, np.ndarray) else list(reference)
        for basis in bases:
            distance = signature_distance(score.signature, basis)
            if distance < cfg.duplicate_threshold:
                score.problems.append(
                    f"Wiederholung (Abstand {distance:.0f} < {cfg.duplicate_threshold:.0f})"
                )
                break
    return score


#: Sicherheitsabstand an Anfang und Ende eines Clips in Sekunden. Dort sitzen
#: Kameraeinstellungen, unscharfe erste Frames und abgebrochene Bewegungen - ein
#: Cut, der genau dort beginnt oder endet, sieht im Reel kaputt aus. Der
#: konfigurierte ``quality.edge_trim`` ist ein *Mindestwert*; dieser Wert ist
#: die Untergrenze, solange das Material für alle geplanten Fenster reicht.
MIN_SAFE_EDGE = 1.0


def plan_edge(edge_trim: float, duration: float, total: float) -> float:
    """Effektiver Rand, mit dem *alle* Fenster eines Clips geplant werden.

    ``edge_trim`` ist ein Mindestabstand, kein fester Wert: passt das geplante
    Material nicht daneben (typisch bei den 3-6-s-Clips, die im Reel landen),
    schrumpft der Rand auf die größtmögliche Hälfte des Restmaterials. Ohne diese
    Anpassung hätte ein einzelnes Fenster entweder über das Clip-Ende hinaus
    geplant oder gar keine Position mehr - beides führte zu Cuts direkt am
    Anfang bzw. Ende der Quelle.

    ``edge_trim <= 0`` (Gate aus) lässt den Rand vollständig weg, dann gilt das
    alte Verhalten ohne jeden Abstand.
    """
    if edge_trim <= 0.0:
        return 0.0
    wanted = max(edge_trim, MIN_SAFE_EDGE)
    return max(min(wanted, max(duration - total, 0.0) / 2.0), 0.0)


def find_highlights(
    analysis: ClipAnalysis,
    duration: float,
    lengths: list[float],
    cfg: QualityConfig,
    edge_trim: float,
) -> list[float]:
    """Findet die besten Schnitte in einem Clip anhand der Bildqualität.

    Verhindert Schnitte am Anfang und Ende des Clips (Kameraeinstellungen) über
    :func:`plan_edge`. Gesucht wird dann nach den Fenstern mit der höchsten
    Schärfe und den wenigsten Belichtungsproblemen. Zwei Schnitte im selben
    Clip müssen einen Mindestabstand haben (gap).

    Fallback auf `spread_windows`, wenn der Clip zu kurz für die Suche ist -
    mit demselben Rand, sonst läge der Fallback genau wieder am Clip-Anfang.
    """
    if not lengths:
        return []

    total_len = sum(lengths)
    safe_edge = plan_edge(edge_trim, duration, total_len)

    # Abstand zwischen zwei Cuts aus demselben Clip
    gap = 0.5
    needed_span = total_len + (len(lengths) - 1) * gap

    usable = duration - 2.0 * safe_edge

    if not analysis.usable or usable < needed_span or not cfg.enabled:
        # Fallback auf einfaches Spread, wenn zu wenig Platz oder keine Analyse
        return spread_windows(duration, lengths, safe_edge)

    candidates: list[dict[str, Any]] = []
    # Teste alle 0.5 Sekunden ein Fenster der durchschnittlichen Länge
    avg_length = total_len / len(lengths)

    current = safe_edge
    while current + avg_length <= duration - safe_edge:
        score = window_score(analysis, current, current + avg_length, cfg)
        if score.frames > 0:
            problems = _local_problems(score, cfg)
            candidates.append({
                "start": current,
                "end": current + avg_length,
                "problems": len(problems),
                "sharpness": score.sharpness,
            })
        current += 0.5

    # Sortiere Kandidaten: am wenigsten Probleme zuerst, dann höchste Schärfe
    candidates.sort(key=lambda c: (c["problems"], -c["sharpness"]))

    chosen_starts: list[float] = []
    max_length = max(lengths)

    for _ in lengths:
        placed = False
        for cand in candidates:
            cand_start = float(cand["start"])
            cand_end = cand_start + max_length

            # Prüfen ob die MAXIMALE Länge über den safe_edge hinausragt
            if cand_end > duration - safe_edge + 1e-4:
                continue

            # Prüfen ob es mit bereits gewählten kollidiert (inkl. gap)
            # Wir blockieren immer max_length, damit später egal welche Länge zugeordnet wird,
            # der Platz ausreicht.
            conflict = False
            for chosen in chosen_starts:
                if not (cand_end + gap <= chosen + 1e-4 or cand_start >= chosen + max_length + gap - 1e-4):
                    conflict = True
                    break

            if not conflict:
                chosen_starts.append(cand_start)
                placed = True
                break

        if not placed:
            # Falls wir keinen Platz mehr finden, brechen wir ab und
            # delegieren komplett ans klassische Fallback
            return spread_windows(duration, lengths, safe_edge)

    # Wichtig: Chronologische Reihenfolge für die Pipeline!
    return sorted(chosen_starts)


def spread_windows(duration: float, lengths: list[float], edge_trim: float) -> list[float]:
    """Verteilt ``lengths``-Fenster gleichmäßig über den ganzen Clip.

    Die alte Logik kürzte immer zentriert und ließ dabei Material links und
    rechts liegen: aus einem 10-s-Clip mit zwei 1,5-s-Cuts wurden nur die
    Sekunden 4,3-5,8 und 7,1-8,6 benutzt, also 30 % des Clips. Hier
    entstehen gleich große Abstände zwischen den Fenstern, wodurch das
    Material besser ausgeschöpft wird.

    Bei genau einem Cut ist das Verhalten unverändert (Mitte des Clips).

    ``edge_trim`` ist ein Mindestabstand zu Anfang und Ende (Kamerainfahrt) und
    wird über :func:`plan_edge` auf das vorhandene Material eingepasst - vorher
    stand das erste Fenster bei knappem Material direkt auf 0,5 s.
    """
    total = sum(lengths)
    margin = plan_edge(edge_trim, duration, total) if lengths else 0.0
    usable = max(duration - 2.0 * margin, total)
    if usable <= 0 or not lengths:
        return [0.0] * len(lengths)

    # Der Rest wird in n+1 gleich große Lücken aufgeteilt: eine vor dem
    # ersten Fenster, n-1 dazwischen, eine nach dem letzten. Damit füllen
    # die Fenster exakt den nutzbaren Bereich und überlappen nie.
    # Bei einem einzigen Cut ist das Zentrieren - verhält sich wie vorher.
    slack = max(usable - total, 0.0)
    gap = slack / (len(lengths) + 1)
    lead = margin + gap

    starts: list[float] = []
    cursor = lead
    for length in lengths:
        starts.append(cursor)
        cursor += length + gap
    return starts


def clamp_start(start: float, length: float, source_duration: float, edge: float = 0.0) -> float:
    """Hält ein Fenster in die Quelldatei, ohne seine Länge zu verändern.

    ``edge`` hält zusätzlich Abstand zum Anfang und Ende der Quelle. Ist der
    Rand für dieses eine Fenster nicht passend (Clip kürzer als Rand + Fenster),
    wird er symmetrisch auf das einzig Mögliche geschrumpft - sonst verlöre der
    Cut seine Länge oder läge außerhalb der Datei.
    """
    if source_duration <= 0.0:
        return max(start, 0.0)
    low = max(min(edge, max(source_duration - length, 0.0) / 2.0), 0.0)
    high = max(source_duration - low - length, low)
    return min(max(start, low), high)


@dataclass
class WindowPlan:
    """Ergebnis der Gate-Prüfung für ein geplantes Fenster."""

    start: float
    end: float
    kept: bool = True
    shifted: float = 0.0
    reason: str = ""

    @property
    def length(self) -> float:
        return max(self.end - self.start, 0.0)


def plan_windows(
    analysis: ClipAnalysis | None,
    windows: Sequence[tuple[float, float]],
    cfg: QualityConfig,
    *,
    source_duration: float = 0.0,
    reference: Sequence[np.ndarray] = (),
    edge_trim: float = 0.0,
) -> tuple[list[WindowPlan], list[np.ndarray]]:
    """Prüft geplante Fenster gegen das Qualitäts-Gate.

    Pro Fenster wird zuerst die Originalposition bewertet, danach dieselbe
    Länge an den in ``search_offsets`` konfigurierten Positionen. Die erste
    Variante ohne Beanstandung gewinnt.

    ``edge_trim`` ist der Mindestabstand zu Anfang und Ende der Quelle (s.
    :func:`plan_edge`) und gilt für *jede* Suchposition: ohne ihn konnte ein
    durchgefallenes Fenster mit ``-0.9`` bis auf 0,1 s an den Clip-Anfang
    geschoben werden und der Kameraanlauf landete mitten im Reel.

    Bleiben alle Varianten durchgefallen, entscheidet die Art des Mangels:

    * echte Wiederholung -> verwerfen (``drop_duplicates``), sonst stünde
      derselbe Moment zweimal im Reel
    * lokale Bildqualität -> behalten (``keep_imperfect``), das beste
      verbleibende Fenster. Eine Reise mit einem weichen Bild ist besser
      als eine Lücke im Reel.

    Ohne verwertbare Analyse (kaputte Datei, Timeout) wird nicht bewertet,
    sondern jedes Fenster unverändert übernommen: was nicht messbar ist,
    wird nicht verworfen.

    Gibt die Pläne und die Signaturen der zuletzt behaltenen Cuts zurück
    (so viele, wie ``duplicate_memory`` verlangt); der Aufrufer reicht sie
    beim nächsten Clip wieder herein.
    """
    if not cfg.enabled or analysis is None or not analysis.usable:
        return [WindowPlan(start, end) for start, end in windows], list(reference)

    def _is_duplicate(score: WindowScore) -> bool:
        return any(p.startswith("Wiederholung") for p in score.problems)

    plans: list[WindowPlan] = []
    history: list[np.ndarray] = list(reference)
    for start, end in windows:
        length = max(end - start, 0.0)
        tried: list[WindowScore] = []
        chosen: WindowScore | None = None
        duplicate = False

        # Vergleichsbasis sind die zuletzt *behaltenen* Cuts des Reels - ein
        # verworfener darf die Schwelle für die folgenden nicht verschieben.
        # Der Vergleich läuft gegen die letzten ``duplicate_memory`` Signaturen,
        # damit auch ein A-B-A-Muster auffällt, nicht nur direkte Nachbarschaft.
        basis: list[np.ndarray] = []
        for recent in reversed(history):
            basis.append(recent)
            if len(basis) >= cfg.duplicate_memory:
                break

        # O(1) set lookup for membership testing when deduplicating search offsets
        seen: set[float] = set()
        for offset in (0.0, *cfg.search_offsets):
            candidate = clamp_start(start + offset, length, source_duration, edge_trim)
            key = round(candidate, 3)
            if key in seen:
                continue
            seen.add(key)
            score = check_window(analysis, candidate, candidate + length, cfg, reference=basis)
            tried.append(score)
            if score.ok:
                chosen = score
                break
            if _is_duplicate(score):
                # Eine Wiederholung lässt sich nicht wegschieben: 0,3 s
                # weiter steht bei einem Burst immer noch derselbe Moment.
                # Deshalb wird hier nicht weiter gesucht - genau das hatte
                # die Kalibrierung aufgedeckt.
                duplicate = True
                break

        if chosen is None:
            # Nichts hat bestanden: das dichteste Problem gewinnt, bzw. das
            # schärfste Fenster, wenn alle gleich viele Mängel haben.
            base = tried[0] if tried else None
            best = min(
                tried,
                key=lambda s: (len(s.problems), -s.sharpness, s.start),
                default=None,
            )
            if duplicate and cfg.drop_duplicates:
                reason = next(
                    (p for p in (base.problems if base else []) if p.startswith("Wiederholung")),
                    "Wiederholung",
                )
                log.info("  Fenster %.2f-%.2fs verworfen: %s", start, end, reason)
                plans.append(WindowPlan(start, end, kept=False, reason=reason))
                continue
            if best is None or not cfg.keep_imperfect:
                reason = "; ".join(base.problems) if base else "kein Frame im Fenster"
                log.info("  Fenster %.2f-%.2fs verworfen: %s", start, end, reason)
                plans.append(WindowPlan(start, end, kept=False, reason=reason))
                continue
            chosen = best
            reason = "; ".join(chosen.problems)
            log.info("  Fenster %.2f-%.2fs bleibt trotz %s", start, end, reason)
        else:
            reason = ""

        shift = chosen.start - start
        if abs(shift) > 1e-3:
            log.info(
                "  Fenster %.2f-%.2fs -> %.2f-%.2fs verschoben (%.2fs)",
                start,
                end,
                chosen.start,
                chosen.start + length,
                shift,
            )
        plans.append(WindowPlan(chosen.start, chosen.start + length, shifted=shift, reason=reason))
        history.append(chosen.signature)

    return plans, history[-cfg.duplicate_memory :]


def analyse_clips(
    clips: Sequence[Any],
    cfg: QualityConfig,
    *,
    cache_dir: Path | None = None,
) -> dict[Path, ClipAnalysis]:
    """Analysiert alle Rohclips einmalig und liefert die Ergebnisse je Pfad.

    Fehlgeschlagene Analysen landen ebenfalls im Ergebnis - als Objekt mit
    gesetztem ``error``. So entscheidet der Aufrufer sichtbar, dass er nicht
    urteilt, und der Schnittplan bleibt vollständig.
    """
    if not cfg.enabled:
        return {}

    target = cache_dir if cache_dir is not None else Path(cfg.cache_dir).expanduser()
    local = QualityConfig(**{**cfg.__dict__, "cache_dir": str(target)})

    analyses: dict[Path, ClipAnalysis] = {}
    for clip in clips:
        path = Path(getattr(clip, "path", clip))
        if path in analyses:
            continue
        width = int(getattr(clip, "width", 0) or 0)
        height = int(getattr(clip, "height", 0) or 0)
        duration = float(getattr(clip, "duration", 0.0) or 0.0)
        try:
            analyses[path] = analyse_clip(path, width=width, height=height, duration=duration, cfg=local)
        except Exception as exc:  # Analyse darf den Lauf nie stoppen
            log.warning("Qualitätsanalyse für %s abgebrochen: %s", path.name, exc)
            analyses[path] = ClipAnalysis(
                path=path,
                duration=duration,
                sample_fps=local.sample_fps,
                sharpness=np.zeros(0, dtype=np.float32),
                luma=np.zeros(0, dtype=np.float32),
                black_ratio=np.zeros(0, dtype=np.float32),
                white_ratio=np.zeros(0, dtype=np.float32),
                signatures=np.zeros((0, SIGNATURE_SIZE * SIGNATURE_SIZE), dtype=np.float32),
                error=str(exc),
            )
    usable = sum(1 for a in analyses.values() if a.usable)
    log.info("Qualitätsanalyse: %d/%d Clips auswertbar", usable, len(analyses))
    return analyses
