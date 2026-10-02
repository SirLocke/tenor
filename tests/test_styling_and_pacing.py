"""Tests für die erweiterten Styling-Presets (natural, original, cinematic)
sowie flexible Reel-Dauer und Schnitt-Pacing.
"""

from pathlib import Path

from src.metadata import ClipInfo
from src.pipeline import build_parser, build_segments
from src.styling import GradeConfig, make_grader


class TestGradePresets:
    def test_default_is_original_and_identity(self) -> None:
        grade = GradeConfig()
        assert grade.preset == "original"
        assert grade.black_lift == 0.0
        assert grade.contrast == 0.0
        assert grade.saturation == 1.0
        assert grade.highlight_ceiling == 1.0
        assert grade.is_identity

    def test_original_preset_is_identity(self) -> None:
        grade = GradeConfig.from_config({}, preset="original")
        assert grade.preset == "original"
        assert grade.is_identity
        # make_grader gibt None zurück, damit frame transforms übersprungen werden
        assert make_grader(grade) is None

    def test_cinematic_preset_loads_dark_film(self) -> None:
        grade = GradeConfig.from_config({}, preset="cinematic")
        assert grade.preset == "cinematic"
        assert grade.contrast == 0.18
        assert grade.black_lift == 0.015
        assert grade.saturation == 0.92
        assert not grade.is_identity
        assert make_grader(grade) is not None

    def test_preset_from_dict_style(self) -> None:
        cfg = {"style": {"preset": "original"}}
        grade = GradeConfig.from_config(cfg)
        assert grade.preset == "original"
        assert grade.is_identity


class TestGradeConfigToneCurve:
    def test_tone_curve_defaults_and_invariants(self) -> None:
        grade = GradeConfig()
        curve = grade.tone_curve(samples=256)
        import numpy as np

        assert isinstance(curve, np.ndarray)
        assert curve.dtype == np.float32
        assert curve.shape == (256,)
        assert np.all(curve >= 0.0) and np.all(curve <= 1.0)
        # Default GradeConfig is identity -> curve should match linear ramp closely
        ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        np.testing.assert_allclose(curve, ramp, atol=1e-5)

    def test_tone_curve_gamma_edge_cases(self) -> None:
        import numpy as np

        ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)

        # Gamma <= 1e-6 (zero or negative) defaults to gamma = 1.0
        g_zero = GradeConfig(gamma=0.0)
        g_neg = GradeConfig(gamma=-0.5)
        g_tiny = GradeConfig(gamma=1e-7)
        np.testing.assert_allclose(g_zero.tone_curve(), ramp, atol=1e-5)
        np.testing.assert_allclose(g_neg.tone_curve(), ramp, atol=1e-5)
        np.testing.assert_allclose(g_tiny.tone_curve(), ramp, atol=1e-5)

        # Gamma < 1.0 darkens midtones (x ** (1/gamma) where 1/gamma > 1)
        g_dark = GradeConfig(gamma=0.5)
        c_dark = g_dark.tone_curve()
        assert c_dark[128] < ramp[128]

        # Gamma > 1.0 brightens midtones
        g_bright = GradeConfig(gamma=2.0)
        c_bright = g_bright.tone_curve()
        assert c_bright[128] > ramp[128]

    def test_tone_curve_contrast_edge_cases(self) -> None:
        import numpy as np

        # Contrast < 0 clipped to 0
        g_neg_c = GradeConfig(contrast=-0.5)
        g_zero_c = GradeConfig(contrast=0.0)
        np.testing.assert_allclose(g_neg_c.tone_curve(), g_zero_c.tone_curve(), atol=1e-5)

        # Contrast > 1 clipped to 1
        g_over_c = GradeConfig(contrast=1.5)
        g_max_c = GradeConfig(contrast=1.0)
        np.testing.assert_allclose(g_over_c.tone_curve(), g_max_c.tone_curve(), atol=1e-5)

    def test_tone_curve_black_lift_and_toe_edge_cases(self) -> None:
        import numpy as np

        # black_lift <= 0 produces no lift
        g_no_lift = GradeConfig(black_lift=0.0)
        g_neg_lift = GradeConfig(black_lift=-0.1)
        ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        np.testing.assert_allclose(g_no_lift.tone_curve(), ramp, atol=1e-5)
        np.testing.assert_allclose(g_neg_lift.tone_curve(), ramp, atol=1e-5)

        # black_lift > 0 lifts black point
        g_lifted = GradeConfig(black_lift=0.05, black_toe=1.0)
        c_lifted = g_lifted.tone_curve()
        assert c_lifted[0] > 0.0

        # black_toe < 1.0 is clamped to 1.0 in calculation (toe = max(black_toe, 1.0))
        g_toe_sub1 = GradeConfig(black_lift=0.05, black_toe=0.5)
        g_toe_one = GradeConfig(black_lift=0.05, black_toe=1.0)
        np.testing.assert_allclose(g_toe_sub1.tone_curve(), g_toe_one.tone_curve(), atol=1e-5)

    def test_tone_curve_highlight_ceiling_edge_cases(self) -> None:
        import numpy as np

        # highlight_ceiling < 0.5 is clamped to 0.5
        g_sub_ceil = GradeConfig(highlight_ceiling=0.2)
        g_half_ceil = GradeConfig(highlight_ceiling=0.5)
        np.testing.assert_allclose(g_sub_ceil.tone_curve(), g_half_ceil.tone_curve(), atol=1e-5)

        # Active ceiling < 1.0 applies soft roll-off
        g_ceil = GradeConfig(highlight_ceiling=0.9)
        curve = g_ceil.tone_curve()
        assert curve[-1] < 1.0
        assert np.all(curve >= 0.0) and np.all(curve <= 1.0)

    def test_tone_curve_sample_counts(self) -> None:
        import numpy as np

        grade = GradeConfig(gamma=0.9, contrast=0.1)
        for num_samples in (1, 2, 10, 256, 512):
            c = grade.tone_curve(samples=num_samples)
            assert c.shape == (num_samples,)
            assert c.dtype == np.float32
            assert np.all(c >= 0.0) and np.all(c <= 1.0)

        # samples=0 returns empty float32 array
        c_zero = grade.tone_curve(samples=0)
        assert c_zero.shape == (0,)
        assert c_zero.dtype == np.float32


class TestPacingAndDuration:
    def test_cli_parser_accepts_duration_grade_pace(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["-i", "clip.mov", "--duration", "30", "--grade", "natural", "--pace", "medium"])
        assert args.duration == 30.0
        assert args.grade == "natural"
        assert args.pace == "medium"

    def test_pacing_adjusts_cut_targets(self, tmp_path: Path) -> None:
        clips = []
        for i in range(3):
            p = tmp_path / f"c{i}.mov"
            p.touch()
            clips.append(ClipInfo(path=p, duration=6.0))

        # Test build_segments with fast pace vs slow pace cut_cfg
        fast_cut_cfg = {"target": 1.6, "target_min": 1.3, "target_max": 2.0}
        slow_cut_cfg = {"target": 3.2, "target_min": 2.4, "target_max": 4.0}

        segs_fast = build_segments(clips, fast_cut_cfg, {"max_duration": 30.0}, durations=[1.6])
        segs_slow = build_segments(clips, slow_cut_cfg, {"max_duration": 30.0}, durations=[3.2])

        assert segs_fast
        assert segs_slow
        avg_fast = sum(s.duration for s in segs_fast) / len(segs_fast)
        avg_slow = sum(s.duration for s in segs_slow) / len(segs_slow)
        assert avg_fast < avg_slow

    def test_fair_budget_distribution_across_clips(self) -> None:
        # Bei 5 Clips und 30s Gesamtdauer sollen alle Clips vertreten sein
        clips = [ClipInfo(path=Path(f"c{i}.mov"), duration=8.0) for i in range(5)]
        segments = build_segments(
            clips,
            {"target_min": 1.7, "target_max": 2.9, "target": 2.3},
            {"max_duration": 30.0},
            durations=[2.3],
        )
        seen_paths = {s.clip.path for s in segments}
        assert len(seen_paths) == 5  # Jeder Clip ist im Schnittplan vertreten!
        total_dur = sum(s.duration for s in segments)
        assert 20.0 <= total_dur <= 30.0
