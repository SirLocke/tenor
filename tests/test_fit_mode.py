from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import src.pipeline as pipeline_module
from moviepy import VideoClip
from src import DEFAULTS, load_config
from src.metadata import ClipInfo
from src.pipeline import Segment, _compute_fingerprint, build_parser, build_segment_clip
from src.styling import GradeConfig


def test_default_fit_mode_and_env_override(tmp_path: Path, monkeypatch: Any) -> None:
    assert DEFAULTS["video"]["fit_mode"] == "crop"

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("ingest_dir: .\n", encoding="utf-8")

    monkeypatch.setenv("TENOR_VIDEO_FIT_MODE", "blur")
    cfg = load_config(cfg_path)
    assert cfg["video"]["fit_mode"] == "blur"


def test_cli_fit_flag_parsing_and_run_wiring(tmp_path: Path, monkeypatch: Any) -> None:
    parser = build_parser()
    assert parser.parse_args(["-i", "clip.mov"]).fit is None
    assert parser.parse_args(["-i", "clip.mov", "--fit", "blur"]).fit == "blur"
    assert parser.parse_args(["-i", "clip.mov", "--fit", "pad"]).fit == "pad"
    assert parser.parse_args(["-i", "clip.mov", "--fit", "crop"]).fit == "crop"

    captured: dict[str, Any] = {}

    def _fake_run(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(output=None, outputs=[], covers=[], segments=[], errors=[])

    monkeypatch.setattr(pipeline_module, "run", _fake_run)
    pipeline_module.main(["-i", str(tmp_path), "--fit", "pad"])
    assert captured["fit_mode"] == "pad"


def test_run_applies_fit_mode_override_to_cfg(tmp_path: Path, monkeypatch: Any) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("ingest_dir: .\nvideo:\n  fit_mode: crop\n", encoding="utf-8")
    clip_file = tmp_path / "clip.mov"
    clip_file.touch()

    seen_cfg: dict[str, Any] = {}

    def _fake_collect(*_args: Any, **_kwargs: Any) -> list[ClipInfo]:
        seen_cfg["cfg_loaded"] = True
        return []

    original_load = pipeline_module.load_config

    def _spy_load(path: str | None = None) -> dict[str, Any]:
        loaded = original_load(path)
        seen_cfg["cfg"] = loaded
        return loaded

    monkeypatch.setattr(pipeline_module, "load_config", _spy_load)
    monkeypatch.setattr(pipeline_module, "collect_clips", _fake_collect)

    pipeline_module.run([clip_file], config_path=str(cfg_path), fit_mode="blur")
    assert seen_cfg["cfg"]["video"]["fit_mode"] == "blur"


def test_build_segment_clip_dispatches_pad_and_blur(monkeypatch: Any) -> None:
    frame = np.full((180, 320, 3), 180, dtype=np.uint8)
    source = VideoClip(lambda _t: frame, duration=2.0)

    monkeypatch.setattr(pipeline_module, "_open_source", lambda _p, **_kw: source)
    segment = Segment(
        clip=ClipInfo(path=Path("/tmp/landscape.mov"), duration=2.0, width=320, height=180),
        start=0.0,
        end=1.0,
    )

    pad_clip = build_segment_clip(
        segment,
        {"video": {"fit_mode": "pad"}},
        GradeConfig(),
        None,
        fps=30,
        width=108,
        height=192,
    )
    assert pad_clip.size == (108, 192)
    pad_frame = pad_clip.get_frame(0.0)
    assert pad_frame.shape == (192, 108, 3)
    assert pad_frame[0, 0].tolist() == [0, 0, 0]
    assert pad_frame[96, 54].tolist() == [180, 180, 180]

    blur_clip = build_segment_clip(
        segment,
        {"video": {"fit_mode": "blur"}},
        GradeConfig(),
        None,
        fps=30,
        width=108,
        height=192,
    )
    assert blur_clip.size == (108, 192)
    blur_frame = blur_clip.get_frame(0.0)
    assert blur_frame.shape == (192, 108, 3)
    # Darkened background around 180 * (1 - 0.45) = 99
    assert blur_frame[0, 0].tolist() == [99, 99, 99]
    assert blur_frame[96, 54].tolist() == [180, 180, 180]


def test_compute_fingerprint_differentiates_fit_modes() -> None:
    seg = Segment(clip=ClipInfo(path=Path("dummy.mov"), duration=2.0), start=0.0, end=2.0)
    grade = GradeConfig()

    fp_default = _compute_fingerprint([seg], None, grade, "Title", "Sub")
    fp_crop = _compute_fingerprint([seg], None, grade, "Title", "Sub", fit_mode="crop")
    fp_blur = _compute_fingerprint([seg], None, grade, "Title", "Sub", fit_mode="blur")
    fp_pad = _compute_fingerprint([seg], None, grade, "Title", "Sub", fit_mode="pad")

    assert fp_default == fp_crop
    assert len({fp_crop, fp_blur, fp_pad}) == 3
