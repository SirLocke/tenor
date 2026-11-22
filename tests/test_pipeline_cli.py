"""CLI-Validierung, JSON-Fehlerausgabe, Dry-Run-Exitcodes und Fehlerprotokollierung."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from src import ConfigError
from src.metadata import ClipInfo
from src.pipeline import RenderResult, Segment, export_reel, main, run
from src.styling import GradeConfig


def _base_cfg(tmp_path: Path) -> dict[str, object]:
    return {
        "ingest_dir": str(tmp_path / "ingest"),
        "output_dir": str(tmp_path / "output"),
        "logging": {"level": "INFO", "file": ""},
        "naming": {"folder_template": ""},
        "quality": {"enabled": False, "cache_dir": str(tmp_path / "qcache")},
        "geocoding": {"enabled": False},
        "spotify": {"enabled": False},
        "video": {"audio": False, "fps": 30},
    }


@pytest.mark.parametrize(
    "flag_args",
    [
        ["--bpm", "0"],
        ["--bpm", "-10"],
        ["--bpm", "nan"],
        ["--bpm", "inf"],
        ["--duration", "-5"],
        ["--duration", "nan"],
        ["--limit", "-1"],
    ],
)
def test_invalid_numeric_cli_flags_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag_args: list[str],
) -> None:
    with patch("src.pipeline.run") as mock_run:
        rc = main(["-i", str(tmp_path), *flag_args])

    assert rc == 2
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()


@pytest.mark.parametrize(
    "flag_args",
    [
        ["--bpm", "0"],
        ["--bpm", "-10"],
        ["--bpm", "nan"],
        ["--duration", "-5"],
        ["--limit", "-1"],
    ],
)
def test_invalid_numeric_cli_flags_json_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag_args: list[str],
) -> None:
    with patch("src.pipeline.run") as mock_run:
        rc = main(["-i", str(tmp_path), *flag_args, "--json"])

    assert rc == 2
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["errors"]
    assert payload["output"] is None
    assert payload["cuts"] == 0
    assert captured.err.strip()


def test_json_output_on_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("src.pipeline.load_config", side_effect=ConfigError("kaputte yaml")):
        rc = main(["-i", str(tmp_path), "--json"])

    assert rc == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["errors"]
    assert "kaputte yaml" in payload["errors"][0]
    assert "kaputte yaml" in captured.err


def test_json_output_on_missing_default_ingest_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _base_cfg(tmp_path)
    with patch("src.pipeline.load_config", return_value=cfg):
        rc = main(["--json"])

    assert rc == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["errors"]
    assert "ingest_dir" in payload["errors"][0]


def test_json_payload_handles_minimal_test_double(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _base_cfg(tmp_path)
    minimal = SimpleNamespace(output=None, outputs=[], covers=[], segments=[], errors=["boom"])
    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.run", return_value=minimal),
    ):
        rc = main(["-i", str(tmp_path), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == ["boom"]
    assert payload["cuts"] == 0
    assert payload["output"] is None


def test_run_early_exit_not_clips_populates_errors_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _base_cfg(tmp_path)
    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.collect_clips", return_value=[]),
        patch("src.pipeline.Geocoder"),
    ):
        rc = main(["-i", str(tmp_path), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["cuts"] == 0
    assert payload["errors"]
    assert "Keine verwertbaren Eingabedateien" in payload["errors"][0]


def test_run_early_exit_not_usable_populates_errors_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _base_cfg(tmp_path)
    clip = ClipInfo(path=tmp_path / "bad.mp4", duration=5.0)
    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.collect_clips", return_value=[clip]),
        patch("src.pipeline.is_valid_video", return_value=False),
        patch("src.pipeline.Geocoder"),
    ):
        rc = main(["-i", str(tmp_path), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"]
    assert "ungültig" in payload["errors"][0]


def test_run_early_exit_not_segments_populates_errors_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _base_cfg(tmp_path)
    clip = ClipInfo(path=tmp_path / "ok.mp4", duration=5.0, created=datetime(2026, 9, 11, tzinfo=UTC))
    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.collect_clips", return_value=[clip]),
        patch("src.pipeline.is_valid_video", return_value=True),
        patch("src.pipeline.analyse_clips", return_value={}),
        patch("src.pipeline.build_segments", return_value=[]),
        patch("src.pipeline.Geocoder"),
    ):
        rc = main(["-i", str(tmp_path), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"]
    assert "Kein einziger Cut zustande gekommen" in payload["errors"][0]


def test_run_early_exit_not_outputs_populates_errors_only_when_empty(tmp_path: Path) -> None:
    cfg = _base_cfg(tmp_path)
    clip = ClipInfo(path=tmp_path / "ok.mp4", duration=5.0, created=datetime(2026, 9, 11, tzinfo=UTC))
    seg = Segment(clip=clip, start=0.0, end=2.0)

    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.collect_clips", return_value=[clip]),
        patch("src.pipeline.is_valid_video", return_value=True),
        patch("src.pipeline.analyse_clips", return_value={}),
        patch("src.pipeline.build_segments", return_value=[seg]),
        patch("src.pipeline.export_reel", return_value=None),
        patch("src.pipeline.Geocoder"),
    ):
        res_empty_errors = run([str(tmp_path)])

    assert res_empty_errors.errors == ["Kein einziges Reel zustande gekommen"]

    def _fail_with_error(*_args: object, errors_seen: list[str] | None = None, **_kwargs: object) -> None:
        if errors_seen is not None:
            errors_seen.append("Spezifischer Export-Fehler")
        return None

    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.collect_clips", return_value=[clip]),
        patch("src.pipeline.is_valid_video", return_value=True),
        patch("src.pipeline.analyse_clips", return_value={}),
        patch("src.pipeline.build_segments", return_value=[seg]),
        patch("src.pipeline.export_reel", side_effect=_fail_with_error),
        patch("src.pipeline.Geocoder"),
    ):
        res_with_error = run([str(tmp_path)])

    assert res_with_error.errors == ["Spezifischer Export-Fehler"]


def test_dry_run_exit_codes_and_summary_logging(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _base_cfg(tmp_path)
    clip = ClipInfo(path=tmp_path / "ok.mp4", duration=5.0)
    seg = Segment(clip=clip, start=0.0, end=2.0)

    empty_res = RenderResult(segments=[], errors=["Kein Cut"])
    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.run", return_value=empty_res),
    ):
        rc_empty = main(["-i", str(tmp_path), "--dry-run"])
    assert rc_empty == 1

    planned_res = RenderResult(
        segments=[seg],
        duration=2.0,
        title="Madeira 2026",
        beat_source="fallback:120bpm",
    )
    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.run", return_value=planned_res),
    ):
        rc_ok = main(["-i", str(tmp_path), "--dry-run"])
    assert rc_ok == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "1 Cuts" in captured.err
    assert "2.0s" in captured.err
    assert "fallback:120bpm" in captured.err
    assert "Madeira 2026" in captured.err

    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.run", return_value=planned_res),
    ):
        rc_json = main(["-i", str(tmp_path), "--dry-run", "--json"])
    assert rc_json == 0
    json_captured = capsys.readouterr()
    payload = json.loads(json_captured.out)
    assert payload["cuts"] == 1
    assert "Dry-Run Zusammenfassung" not in json_captured.err


def test_export_reel_errors_seen_none_on_low_disk_space_and_render_failure(tmp_path: Path) -> None:
    cfg = {"naming": {"folder_template": ""}, "video": {"audio": False, "crf": 19}}
    clip_path = tmp_path / "fake.mp4"
    clip_path.write_bytes(b"dummy")
    seg = Segment(clip=ClipInfo(path=clip_path, duration=5.0), start=0.0, end=2.0)
    grade = GradeConfig()

    with (
        patch("src.pipeline.check_disk_space", return_value=False),
        patch("src.pipeline.render_segments") as mock_render,
    ):
        target = export_reel(
            segments=[seg],
            cfg=cfg,
            base_out=tmp_path / "out",
            title="Test",
            subtitle="Sub",
            day="2026-09-11",
            grade=grade,
            when=1600000000.0,
            dry_run=False,
            errors_seen=None,
        )
    assert target is None
    mock_render.assert_not_called()

    def _empty_render(*_args: object, on_error: list[str] | None = None, **_kwargs: object) -> list[object]:
        if on_error is not None:
            on_error.append("Segment-Render fehlgeschlagen")
        return []

    with (
        patch("src.pipeline.check_disk_space", return_value=True),
        patch("src.pipeline.make_overlay_array", return_value=None),
        patch("src.pipeline.render_segments", side_effect=_empty_render),
    ):
        target_fail = export_reel(
            segments=[seg],
            cfg=cfg,
            base_out=tmp_path / "out",
            title="Test",
            subtitle="Sub",
            day="2026-09-11",
            grade=grade,
            when=1600000000.0,
            dry_run=False,
            errors_seen=None,
        )
    assert target_fail is None

    mock_final = MagicMock()
    mock_final.duration = 2.0
    mock_final.audio = None
    with (
        patch("src.pipeline.check_disk_space", return_value=True),
        patch("src.pipeline.make_overlay_array", return_value=None),
        patch("src.pipeline.render_segments", return_value=[mock_final]),
    ):
        target_dry = export_reel(
            segments=[seg],
            cfg=cfg,
            base_out=tmp_path / "out",
            title="Test",
            subtitle="Sub",
            day="2026-09-11",
            grade=grade,
            when=1600000000.0,
            dry_run=True,
            errors_seen=None,
        )
    assert target_dry is not None


@pytest.mark.parametrize("music_arg", ["nonexistent.mp3", "auto"])
def test_missing_music_logs_warning_instead_of_schnitttakt(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    music_arg: str,
) -> None:
    cfg = _base_cfg(tmp_path)
    clip = ClipInfo(path=tmp_path / "ok.mp4", duration=5.0, created=datetime(2026, 9, 11, tzinfo=UTC))
    seg = Segment(clip=clip, start=0.0, end=2.0)

    caplog.set_level(logging.INFO, logger="tenor.pipeline")
    with (
        patch("src.pipeline.load_config", return_value=cfg),
        patch("src.pipeline.collect_clips", return_value=[clip]),
        patch("src.pipeline.is_valid_video", return_value=True),
        patch("src.pipeline.analyse_clips", return_value={}),
        patch("src.pipeline.build_segments", return_value=[seg]),
        patch("src.pipeline.export_reel", return_value=tmp_path / "out.mp4"),
        patch("src.pipeline.Geocoder"),
    ):
        res = run([str(tmp_path)], music=music_arg, dry_run=True)

    assert res.beat_source.startswith("fallback:")
    assert "liefert nur den Schnitttakt" not in caplog.text
    assert any(
        record.levelno == logging.WARNING and "nicht nutzbar - Export ohne Musikton" in record.message
        for record in caplog.records
    )


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from src import __version__
    from src.metadata import main as metadata_main
    from src.watcher import main as watcher_main

    for entrypoint in (main, watcher_main, metadata_main):
        with pytest.raises(SystemExit) as exc_info:
            entrypoint(["--version"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert f"tenor {__version__}" in captured.out
