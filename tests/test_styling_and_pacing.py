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
