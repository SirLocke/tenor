"""Tests für das Bildqualitäts-Gate der Cut-Auswahl.

Zwei Dinge sind hier besonders zu schützen, weil sie am echten Material
kalibriert und nicht hergeleitet sind:

1. Die Schwelle zwischen "dieselbe Szene zweimal" und "zwei verschiedene
   Aufnahmen". Wird sie zu niedrig gewählt, verschwinden echte Schnitte aus
   dem Reel; zu hoch, kommen Wiederholungen durch.
2. Die Zusicherung, dass das Gate nie Material verliert, das es nicht
   beurteilen kann. Eine kaputte Datei oder ein fehlendes ffmpeg darf keine
   Lücke ins Reel reißen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.metadata import ClipInfo  # noqa: E402
from src.pipeline import build_segments  # noqa: E402
from src.quality import (  # noqa: E402
    SIGNATURE_SIZE,
    ClipAnalysis,
    QualityConfig,
    WindowPlan,
    analyse_clips,
    check_window,
    plan_windows,
    signature_distance,
    spread_windows,
    window_score,
    find_highlights,
)


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def _analysis(
    path: str = "x.mov",
    *,
    duration: float = 10.0,
    luma: float = 140.0,
    sharpness: float = 12000.0,
    black: float = 0.02,
    white: float = 0.02,
    frames: int = 40,
    signature: np.ndarray | None = None,
) -> ClipAnalysis:
    """Eine gleichförmige Analyse - bewusst synthetisch, keine echten Frames."""
    size = SIGNATURE_SIZE * SIGNATURE_SIZE
    return ClipAnalysis(
        path=Path(path),
        duration=duration,
        sample_fps=4.0,
        sharpness=np.full(frames, sharpness, dtype=np.float32),
        luma=np.full(frames, luma, dtype=np.float32),
        black_ratio=np.full(frames, black, dtype=np.float32),
        white_ratio=np.full(frames, white, dtype=np.float32),
        signatures=(
            np.tile(signature.reshape(1, -1), (frames, 1)).astype(np.float32)
            if signature is not None
            else np.zeros((frames, size), dtype=np.float32)
        ),
    )


def _broken(path: str = "kaputt.mov", duration: float = 10.0) -> ClipAnalysis:
    """Analyse mit gesetztem Fehler - so liefert ffmpeg-Misser/Timeouts aus."""
    size = SIGNATURE_SIZE * SIGNATURE_SIZE
    empty = np.zeros(0, dtype=np.float32)
    return ClipAnalysis(
        path=Path(path),
        duration=duration,
        sample_fps=4.0,
        sharpness=empty,
        luma=empty,
        black_ratio=empty,
        white_ratio=empty,
        signatures=np.zeros((0, size), dtype=np.float32),
        error="ffmpeg-Timeout nach 60s",
    )


def _cfg(**overrides: object) -> QualityConfig:
    base = {"cache_dir": "/tmp/tenor-test-quality", "search_offsets": (0.3, -0.3)}
    base.update(overrides)
    return QualityConfig(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Fensterverteilung: das Material gleichmäßig ausschöpfen
# --------------------------------------------------------------------------- #
class TestSpreadWindows:
    def test_single_window_is_centred(self) -> None:
        # Rückwärtskompatibilität: ein einzelner Cut sitzt in der Mitte.
        assert spread_windows(10.0, [1.5], 0.0) == pytest.approx([4.25])

    def test_edge_trim_is_respected(self) -> None:
        starts = spread_windows(10.0, [1.5], 0.25)
        assert starts[0] >= 0.25
        assert starts[0] + 1.5 <= 10.0 - 0.25

    def test_windows_never_overlap(self) -> None:
        # Der Aufrufer garantiert, dass die Längen in den Clip passen; das ist
        # Aufgabe des Schnittplans, nicht der Verteilung.
        for duration in (5.0, 10.0, 20.0, 45.0, 90.0):
            for count in range(1, 6):
                lengths = [1.5] * count
                if sum(lengths) > duration - 0.5:
                    continue
                starts = spread_windows(duration, lengths, 0.25)
                for index, start in enumerate(starts):
                    end = start + lengths[index]
                    assert end <= duration, (duration, count, index)
                    if index + 1 < count:
                        assert end <= starts[index + 1] + 1e-6, (duration, count, index)

    def test_clip_barely_longer_than_one_cut(self) -> None:
        # Knapp passende Clips: die Verteilung darf nicht über das Ende hinaus
        # planen, der Schnittplan klemmt die Fenster fest.
        for duration in (1.4, 1.5, 1.6, 2.0, 2.6):
            clips = [ClipInfo(path=Path("x.mov"), duration=duration)]
            segments = build_segments(clips, {"target_min": 1.4}, {}, durations=[1.5])
            assert segments
            for segment in segments:
                assert segment.start >= 0.0
                assert segment.end <= duration + 1e-6
                assert segment.duration >= 1.4 - 1e-3

    def test_empty_input(self) -> None:
        assert spread_windows(10.0, [], 0.25) == []
        assert spread_windows(0.0, [1.5], 0.0) == [0.0]


class TestBuildSegmentsPositions:
    def test_no_overlap_and_within_bounds(self) -> None:
        for duration in (1.5, 3.0, 5.0, 10.0, 20.0, 45.0):
            clips = [ClipInfo(path=Path("x.mov"), duration=duration)]
            segments = build_segments(clips, {"target_min": 1.4}, {}, durations=[1.5])
            previous = -1.0
            for segment in segments:
                assert 0.0 <= segment.start
                assert segment.end <= duration + 1e-6
                assert segment.start >= previous - 1e-6, (duration, segment)
                previous = segment.end

    def test_single_cut_matches_legacy_centre(self) -> None:
        # Der wichtigste Rückwärtskompatibilitätsfall: genau ein Cut aus einem
        # 3-s-Clip lag immer bei 0,75-2,25 und muss dort bleiben.
        clips = [ClipInfo(path=Path("x.mov"), duration=3.0)]
        segments = build_segments(clips, {"target_min": 1.4}, {}, durations=[1.5])
        assert len(segments) == 1
        assert segments[0].start == pytest.approx(0.75, abs=0.02)
        assert segments[0].end == pytest.approx(2.25, abs=0.02)

    def test_long_clip_uses_wider_span_than_staircase(self) -> None:
        # Der Treppenlauf klebte am Rest: bei 10 s lagen die zwei Cuts in
        # 4,3-5,8 und 7,1-8,6. Jetzt decken sie mehr vom Clip ab.
        clips = [ClipInfo(path=Path("x.mov"), duration=10.0)]
        segments = build_segments(clips, {"target_min": 1.4}, {}, durations=[1.5])
        assert len(segments) == 2
        span = segments[-1].end - segments[0].start
        assert span > 4.3, f"zu enge Abdeckung: {span:.2f}s"

    def test_beat_grid_still_limits_cut_count(self) -> None:
        # Das Gate verändert *wo* geschnitten wird, nicht *wie viele* Cuts
        # der Beat-Takt vorgibt.
        clips = [ClipInfo(path=Path(f"c{i}.mov"), duration=3.0) for i in range(4)]
        for cut_cfg in ({"target_min": 1.4}, {"target_min": 1.4, "max_cuts": 2}):
            for video_cfg in ({}, {"max_duration": 3.5}):
                segments = build_segments(clips, cut_cfg, video_cfg, durations=[1.5])
                if cut_cfg.get("max_cuts"):
                    assert len(segments) <= 2
                total = sum(s.duration for s in segments)
                if video_cfg:
                    assert total <= 3.5 + 1e-6


# --------------------------------------------------------------------------- #
# Lokale Kriterien
# --------------------------------------------------------------------------- #
class TestLocalGate:
    def test_good_material_passes(self) -> None:
        analysis = _analysis()
        assert check_window(analysis, 1.0, 2.5, _cfg()).ok

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"luma": 5.0}, "zu dunkel"),
            ({"luma": 252.0}, "überbelichtet"),
            ({"sharpness": 10.0}, "unscharf"),
            ({"black": 0.9}, "zu viel Schwarz"),
            ({"white": 0.8}, "zu viel Weiß"),
        ],
    )
    def test_defective_material_is_flagged(self, kwargs: dict, expected: str) -> None:
        score = check_window(_analysis(**kwargs), 1.0, 2.5, _cfg())
        assert not score.ok
        assert any(expected in problem for problem in score.problems)

    def test_thresholds_are_measured_against_real_iphone_range(self) -> None:
        # An 4K-iPhone-Clips ausgemessen: Schärfe 3 300-35 000, Luma 99-187.
        # Die Vorgaben müssen echtes Material durchlassen und Kaputtes fangen.
        cfg = QualityConfig()
        good = _analysis(sharpness=3300.0, luma=99.0, black=0.04, white=0.10)
        assert check_window(good, 1.0, 2.5, cfg).ok
        assert not check_window(_analysis(luma=0.0), 1.0, 2.5, cfg).ok
        assert not check_window(_analysis(sharpness=1.0), 1.0, 2.5, cfg).ok

    def test_disabled_gate_never_judges(self) -> None:
        cfg = _cfg(enabled=False, min_luma=250.0, min_sharpness=1e9)
        score = check_window(_analysis(), 1.0, 2.5, cfg)
        assert score.ok

    def test_window_score_uses_only_frames_inside(self) -> None:
        analysis = _analysis(frames=40)  # 40 Frames à 0,25 s = 10 s
        inside = window_score(analysis, 1.0, 2.0, _cfg())
        assert inside.frames == 4
        assert inside.frames < 8

    def test_window_shorter_than_sample_grid_still_scores(self) -> None:
        # Sonst hätte ein sehr kurzer Cut gar keine Bewertung.
        analysis = _analysis(frames=4)  # 4 Frames à 0,25 s = 1 s Material
        assert window_score(analysis, 0.4, 0.5, _cfg()).frames == 1


# --------------------------------------------------------------------------- #
# Wiederholung
# --------------------------------------------------------------------------- #
class TestDuplicateGate:
    def test_identical_signature_is_a_duplicate(self) -> None:
        analysis = _analysis(signature=np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 100.0))
        first = window_score(analysis, 1.0, 2.5, _cfg())
        assert signature_distance(first.signature, first.signature) == pytest.approx(0.0)
        assert not check_window(analysis, 1.0, 2.5, _cfg(), reference=first.signature).ok

    def test_different_signature_passes(self) -> None:
        low = _analysis(signature=np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 10.0))
        high = _analysis(signature=np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 200.0))
        score = check_window(high, 1.0, 2.5, _cfg(), reference=low.signatures[0])
        assert score.ok

    def test_first_cut_has_no_reference(self) -> None:
        assert check_window(_analysis(), 1.0, 2.5, _cfg(), reference=None).ok

    def test_disabled_duplicate_check(self) -> None:
        analysis = _analysis(signature=np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 100.0))
        cfg = _cfg(duplicate_threshold=0.0)
        assert check_window(analysis, 1.0, 2.5, cfg, reference=analysis.signatures[0]).ok

    def test_burst_moment_is_dropped_once_not_rescued_by_shifting(self) -> None:
        """Der eigentliche Fund aus der Kalibrierung.

        Bei einem iPhone-Burst liegt der nächste Versand derselben Szene
        0,15 s entfernt. Ein Such-Offset von 0,3 s "rettet" das Fenster dann
        nicht wirklich - es steht immer noch derselbe Moment im Reel. Deshalb
        darf eine Wiederholung nicht wegschiebbar sein.
        """
        same = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 90.0)
        # Signatur, die sich nur minimal unterscheidet (Abstand ~7 < 20).
        near = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 97.0)

        plans, _ = plan_windows(
            _analysis(signature=near), [(1.0, 2.5)], _cfg(), source_duration=10.0,
            reference=[same],
        )
        assert len(plans) == 1
        assert not plans[0].kept
        assert "Wiederholung" in plans[0].reason

    def test_duplicate_can_be_kept_when_configured(self) -> None:
        same = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 90.0)
        plans, _ = plan_windows(
            _analysis(signature=same),
            [(1.0, 2.5)],
            _cfg(drop_duplicates=False),
            source_duration=10.0,
            reference=[same],
        )
        assert plans[0].kept

    def test_reference_follows_last_kept_cut(self) -> None:
        # Drei Clips: A, B, dann A nochmal. A darf am Ende nicht allein
        # deshalb durchfallen, weil B dazwischen lag.
        sig_a = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 50.0)
        sig_b = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 200.0)
        cfg = _cfg()
        plans, history = plan_windows(
            _analysis("a.mov", signature=sig_a), [(1.0, 2.5)], cfg, source_duration=10.0
        )
        assert plans[0].kept
        plans, history = plan_windows(
            _analysis("b.mov", signature=sig_b), [(1.0, 2.5)], cfg,
            source_duration=10.0, reference=history,
        )
        assert plans[0].kept
        plans, history = plan_windows(
            _analysis("a2.mov", signature=sig_a), [(1.0, 2.5)], cfg,
            source_duration=10.0, reference=history,
        )
        assert not plans[0].kept, "dieselbe Szene hätte fallen müssen"

    def test_sandwich_pattern_a_b_a_is_caught(self) -> None:
        # Nur der allerletzte Cut zu vergleichen würde ein A-B-A-Muster
        # durchwinken: A steht dann zweimal im Reel.
        sig_a = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 50.0)
        sig_b = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 200.0)
        cfg = _cfg(duplicate_memory=3)
        _, history = plan_windows(
            _analysis("a.mov", signature=sig_a), [(1.0, 2.5)], cfg, source_duration=10.0
        )
        _, history = plan_windows(
            _analysis("b.mov", signature=sig_b), [(1.0, 2.5)], cfg,
            source_duration=10.0, reference=history,
        )
        plans, _ = plan_windows(
            _analysis("a2.mov", signature=sig_a), [(1.0, 2.5)], cfg,
            source_duration=10.0, reference=history,
        )
        assert not plans[0].kept

        # Mit duplicate_memory: 1 darf es nicht auffallen.
        narrow = _cfg(duplicate_memory=1)
        _, history = plan_windows(
            _analysis("a.mov", signature=sig_a), [(1.0, 2.5)], narrow, source_duration=10.0
        )
        _, history = plan_windows(
            _analysis("b.mov", signature=sig_b), [(1.0, 2.5)], narrow,
            source_duration=10.0, reference=history,
        )
        plans, _ = plan_windows(
            _analysis("a2.mov", signature=sig_a), [(1.0, 2.5)], narrow,
            source_duration=10.0, reference=history,
        )
        assert plans[0].kept

    def test_threshold_sits_in_the_measured_gap(self) -> None:
        """Die Vorgabe muss zwischen den gemessenen Gruppen liegen.

        An der echten Schnittplanung der Madeira-Clips gemessen:

        * gleicher Moment aus einem iPhone-Burst: 6,8 - 13,9
        * zwei Cuts aus demselben statischen Clip:   7,4 -  8,0
        * verschiedene Clips / Szenen:              24,7 - 104,6

        Ein Wert unter 14 verwirft echte Szenen - die erste Kalibrierung lag
        auf Whole-Clip-Mitteln statt auf Fenstern und flog genau deshalb durch.
        """
        assert 14.0 < QualityConfig().duplicate_threshold < 24.7

    def test_distance_scales_with_similarity(self) -> None:
        size = SIGNATURE_SIZE * SIGNATURE_SIZE
        threshold = QualityConfig().duplicate_threshold
        base = np.full(size, 100.0)
        near = base.copy()
        near[: size // 4] += 10.0   # ein Viertel der Merkmale leicht anders
        far = base.copy()
        far[: size // 4] += 120.0
        assert signature_distance(base, near) < signature_distance(base, far)
        assert signature_distance(base, near) < threshold
        assert signature_distance(base, far) > threshold

    def test_history_is_capped(self) -> None:
        cfg = _cfg(duplicate_memory=2)
        distinct = [
            np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, float(v)) for v in (40, 90, 150, 210)
        ]
        _, history = plan_windows(
            _analysis(signature=distinct[0]), [(1.0, 2.5)], cfg, source_duration=10.0
        )
        _, history = plan_windows(
            _analysis(signature=distinct[1]), [(1.0, 2.5)], cfg,
            source_duration=10.0, reference=history,
        )
        _, history = plan_windows(
            _analysis(signature=distinct[2]), [(1.0, 2.5)], cfg,
            source_duration=10.0, reference=history,
        )
        assert len(history) == 2

    def test_dropped_cut_does_not_move_the_threshold(self) -> None:
        # Ein verworfener Cut darf die Vergleichsbasis nicht verschieben.
        sig_dup = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 90.0)
        sig_new = np.full(SIGNATURE_SIZE * SIGNATURE_SIZE, 200.0)
        cfg = _cfg(duplicate_memory=1)
        _, history = plan_windows(
            _analysis("a.mov", signature=sig_dup), [(1.0, 2.5)], cfg, source_duration=10.0
        )
        _, history = plan_windows(
            _analysis("b.mov", signature=sig_dup), [(1.0, 2.5)], cfg,
            source_duration=10.0, reference=history,
        )
        assert history
        plans, _ = plan_windows(
            _analysis("c.mov", signature=sig_new), [(1.0, 2.5)], cfg,
            source_duration=10.0, reference=history,
        )
        assert plans[0].kept


# --------------------------------------------------------------------------- #
# Schieben
# --------------------------------------------------------------------------- #
class TestShifting:
    def test_window_is_shifted_into_better_material(self) -> None:
        # Erstes Fenster zu dunkel, das um 0,3 s verschobene ist es nicht.
        times = np.arange(40, dtype=np.float32) / 4.0
        luma = np.full(40, 140.0, dtype=np.float32)
        luma[(times >= 1.0) & (times < 2.0)] = 2.0
        analysis = ClipAnalysis(
            path=Path("x.mov"), duration=10.0, sample_fps=4.0,
            sharpness=np.full(40, 12000.0, dtype=np.float32),
            luma=luma,
            black_ratio=np.full(40, 0.02, dtype=np.float32),
            white_ratio=np.full(40, 0.02, dtype=np.float32),
            signatures=np.zeros((40, SIGNATURE_SIZE * SIGNATURE_SIZE), dtype=np.float32),
        )
        assert not check_window(analysis, 1.0, 2.5, _cfg(min_luma=60.0)).ok
        plans, _ = plan_windows(
            analysis, [(1.0, 2.5)], _cfg(min_luma=60.0), source_duration=10.0
        )
        assert plans[0].kept
        assert plans[0].start == pytest.approx(1.3, abs=0.01)
        assert plans[0].shifted == pytest.approx(0.3, abs=0.01)

    def test_shifting_never_leaves_the_source(self) -> None:
        plans, _ = plan_windows(
            _analysis(luma=2.0), [(1.0, 2.5)], _cfg(), source_duration=2.0
        )
        assert plans[0].end <= 2.0 + 1e-6

    def test_imperfect_material_is_kept_by_default(self) -> None:
        cfg = _cfg(min_sharpness=1e9)
        plans, _ = plan_windows(_analysis(), [(1.0, 2.5)], cfg, source_duration=10.0)
        assert plans[0].kept
        assert "unscharf" in plans[0].reason

    def test_imperfect_material_can_be_dropped(self) -> None:
        cfg = _cfg(min_sharpness=1e9, keep_imperfect=False)
        plans, _ = plan_windows(_analysis(), [(1.0, 2.5)], cfg, source_duration=10.0)
        assert not plans[0].kept


# --------------------------------------------------------------------------- #
# Das Gate darf nie Material verlieren, das es nicht beurteilen kann
# --------------------------------------------------------------------------- #
class TestNeverLosesMaterial:
    def test_broken_analysis_keeps_every_window(self) -> None:
        plans, history = plan_windows(
            _broken(), [(1.0, 2.5), (4.0, 5.5)], _cfg(), source_duration=10.0
        )
        assert [p.kept for p in plans] == [True, True]
        assert [(p.start, p.end) for p in plans] == [(1.0, 2.5), (4.0, 5.5)]
        assert history == []

    def test_missing_analysis_keeps_every_window(self) -> None:
        plans, _ = plan_windows(None, [(1.0, 2.5)], _cfg(), source_duration=10.0)
        assert plans[0].kept and (plans[0].start, plans[0].end) == (1.0, 2.5)

    def test_disabled_gate_keeps_every_window(self) -> None:
        plans, _ = plan_windows(
            _analysis(luma=0.0, sharpness=0.0), [(1.0, 2.5)],
            _cfg(enabled=False), source_duration=10.0,
        )
        assert plans[0].kept and (plans[0].start, plans[0].end) == (1.0, 2.5)

    def test_build_segments_without_analyses_matches_plain_planning(self) -> None:
        # Ohne Analyse-Ergebnis darf der Schnittplan nicht schrumpfen.
        clips = [ClipInfo(path=Path(f"c{i}.mov"), duration=4.0) for i in range(5)]
        plain = build_segments(clips, {"target_min": 1.4}, {}, durations=[1.5])
        gated = build_segments(
            clips, {"target_min": 1.4}, {}, durations=[1.5],
            quality_cfg=_cfg(), analyses={},
        )
        assert len(gated) == len(plain)
        assert sum(s.duration for s in gated) == sum(s.duration for s in plain)

    def test_build_segments_uses_broken_analysis_without_dropping(self) -> None:
        clips = [ClipInfo(path=Path("a.mov"), duration=4.0)]
        gated = build_segments(
            clips, {"target_min": 1.4}, {}, durations=[1.5],
            quality_cfg=_cfg(), analyses={Path("a.mov"): _broken()},
        )
        assert len(gated) == 1


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
class TestConfig:
    def test_defaults_are_loaded_from_deFAULTS(self) -> None:
        from src import DEFAULTS

        cfg = QualityConfig.from_config(DEFAULTS["quality"])
        assert cfg.enabled
        assert cfg.duplicate_threshold == DEFAULTS["quality"]["duplicate_threshold"]
        assert cfg.search_offsets == tuple(DEFAULTS["quality"]["search_offsets"])

    def test_missing_section_uses_dataclass_defaults(self) -> None:
        assert QualityConfig.from_config({}).enabled

    def test_visual_interest_metrics_are_parsed(self) -> None:
        cfg = QualityConfig.from_config({
            "min_motion": 5.5,
            "min_faces": 1.0,
            "min_color_variance": 12.3
        })
        assert cfg.min_motion == 5.5
        assert cfg.min_faces == 1.0
        assert cfg.min_color_variance == 12.3

    def test_garbage_values_do_not_crash(self) -> None:
        cfg = QualityConfig.from_config(
            {"search_offsets": ["x", None, 0.5, {}], "sample_fps": "nonsense",
             "timeout": -1, "cache_dir": None, "min_luma": "dunkel",
             "max_black_ratio": float("inf"), "duplicate_memory": 0}
        )
        assert cfg.search_offsets == (0.5,)
        assert cfg.sample_fps >= 0.5
        assert cfg.timeout >= 5.0
        assert cfg.cache_dir
        assert cfg.min_luma == QualityConfig.min_luma
        assert cfg.max_black_ratio <= 1.0
        assert cfg.duplicate_memory >= 1

    def test_empty_offset_list_falls_back_to_default(self) -> None:
        cfg = QualityConfig.from_config({"search_offsets": []})
        assert cfg.search_offsets == QualityConfig.search_offsets

    def test_offsets_without_usable_value_fall_back(self) -> None:
        cfg = QualityConfig.from_config({"search_offsets": ["a", None]})
        assert cfg.search_offsets == QualityConfig.search_offsets

    def test_ratios_are_clamped(self) -> None:
        cfg = QualityConfig.from_config(
            {"max_black_ratio": 5.0, "max_white_ratio": -1.0, "max_luma": 999.0}
        )
        assert cfg.max_black_ratio == 1.0
        assert cfg.max_white_ratio == 0.0
        assert cfg.max_luma == 255.0

    def test_zero_disables(self) -> None:
        # Untergrenzen mit 0 sind aus; bei Anteilen ist 1,0 der ausgeschaltete
        # Wert (100 % schwarze Pixel kann es nicht geben).
        cfg = QualityConfig.from_config(
            {"min_sharpness": 0, "min_luma": 0, "max_luma": 255,
             "max_black_ratio": 1.0, "max_white_ratio": 1.0, "duplicate_threshold": 0}
        )
        assert check_window(
            _analysis(luma=0.0, sharpness=0.0, black=1.0, white=1.0), 1.0, 2.5, cfg
        ).ok


# --------------------------------------------------------------------------- #
# Analyse-Pipeline
# --------------------------------------------------------------------------- #
class TestAnalyseClips:
    def test_disabled_returns_nothing(self) -> None:
        clips = [ClipInfo(path=Path("/nicht/da.mov"), duration=3.0)]
        assert analyse_clips(clips, _cfg(enabled=False)) == {}

    def test_duplicate_paths_analysed_once(self, tmp_path: Path) -> None:
        clip = ClipInfo(path=tmp_path / "a.mov", duration=3.0)
        analyses = analyse_clips([clip, clip], _cfg(), cache_dir=tmp_path)
        assert list(analyses) == [tmp_path / "a.mov"]

    def test_failed_analysis_is_recorded_not_raised(self) -> None:
        missing = ClipInfo(path=Path("/gibt/es/nicht.mov"), duration=3.0)
        analyses = analyse_clips([missing], _cfg(), cache_dir=Path("/tmp/tenor-test-quality"))
        analysis = next(iter(analyses.values()))
        assert not analysis.usable
        assert analysis.error

    def test_accepts_plain_paths(self) -> None:
        analyses = analyse_clips(
            [Path("/gibt/es/nicht.mov")], _cfg(), cache_dir=Path("/tmp/tenor-test-quality")
        )
        assert Path("/gibt/es/nicht.mov") in analyses


# --------------------------------------------------------------------------- #
# Fensterplan-Dataclass
# --------------------------------------------------------------------------- #
class TestWindowPlan:
    def test_defaults(self) -> None:
        plan = WindowPlan(1.0, 2.5)
        assert plan.kept
        assert plan.shifted == 0.0
        assert plan.reason == ""
        assert plan.length == pytest.approx(1.5)

    def test_length_never_negative(self) -> None:
        assert WindowPlan(2.5, 1.0).length == 0.0

def test_quality_config_validation() -> None:
    with pytest.raises(ValueError, match="min_sharpness darf nicht negativ sein"):
        QualityConfig(min_sharpness=-10.0)

    with pytest.raises(ValueError, match="min_luma darf nicht negativ sein"):
        QualityConfig(min_luma=-5.0)

    with pytest.raises(ValueError, match="max_luma muss zwischen 0 und 255 liegen"):
        QualityConfig(max_luma=300.0)

    with pytest.raises(ValueError, match="max_luma muss zwischen 0 und 255 liegen"):
        QualityConfig(max_luma=-1.0)

    with pytest.raises(ValueError, match="max_black_ratio muss zwischen 0 und 1 liegen"):
        QualityConfig(max_black_ratio=1.5)

    with pytest.raises(ValueError, match="max_black_ratio muss zwischen 0 und 1 liegen"):
        QualityConfig(max_black_ratio=-0.1)

    with pytest.raises(ValueError, match="max_white_ratio muss zwischen 0 und 1 liegen"):
        QualityConfig(max_white_ratio=2.0)

    with pytest.raises(ValueError, match="max_white_ratio muss zwischen 0 und 1 liegen"):
        QualityConfig(max_white_ratio=-0.5)

    with pytest.raises(ValueError, match="min_motion darf nicht negativ sein"):
        QualityConfig(min_motion=-1.0)

    with pytest.raises(ValueError, match="min_faces darf nicht negativ sein"):
        QualityConfig(min_faces=-2.0)

    with pytest.raises(ValueError, match="min_color_variance darf nicht negativ sein"):
        QualityConfig(min_color_variance=-3.0)

    with pytest.raises(ValueError, match="duplicate_threshold darf nicht negativ sein"):
        QualityConfig(duplicate_threshold=-5.0)


def test_find_highlights_mismatched_lengths() -> None:
    analysis = _analysis(duration=10.0, frames=40)
    cfg = QualityConfig(enabled=True)
    lengths = [2.0, 1.0, 1.5]

    # max_length = 2.0
    # edge_trim = 1.0 (safe_edge = 1.0)
    # total_len = 4.5
    # gap = 0.5
    # needed_span = 4.5 + 2 * 0.5 = 5.5
    # usable = 10.0 - 2.0 = 8.0 > 5.5

    # Candidates are tested with avg_length = 1.5
    # Start candidate max: 10.0 - 1.0 - 1.5 = 7.5
    # Since we allocate using max_length = 2.0, chosen cand_end = cand_start + 2.0
    # First cut cand_start = 1.0. (cand_end = 3.0)
    # Next must start at 3.0 + 0.5 = 3.5.
    # Second cand_start = 3.5. (cand_end = 5.5)
    # Next must start at 5.5 + 0.5 = 6.0.
    # Third cand_start = 6.0.

    starts = find_highlights(analysis, 10.0, lengths, cfg, edge_trim=1.0)

    assert len(starts) == 3
    assert starts == [1.0, 3.5, 6.0]
