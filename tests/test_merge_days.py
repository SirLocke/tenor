"""Ein Reel pro Tag (Default) vs. bewusst alles in einem Reel (--single-reel).

Der Homelab-Selector erlaubt Mehrfachauswahl mehrerer Tage. Diese Auswahl soll
genau ein Video ergeben; der Album-Automatismus (ein Reel pro Aufnahmetag) darf
aber nicht verloren gehen. Deshalb wird die Gruppierung als eigene, testbare
Funktion geführt.
"""

from datetime import UTC, datetime
from pathlib import Path

from src.metadata import ClipInfo
from src.pipeline import build_parser, group_clips_by_day


def _clip(day: str) -> ClipInfo:
    """ClipInfo mit festem Aufnahmedatum (date_label leitet sich davon ab)."""
    year, month, dom = (int(part) for part in day.split("-"))
    return ClipInfo(path=Path(f"{day}.mov"), created=datetime(year, month, dom, tzinfo=UTC), duration=5.0)


def test_default_groups_by_recording_day() -> None:
    clips = [_clip("2026-09-27"), _clip("2026-09-27"), _clip("2026-09-28")]
    groups = group_clips_by_day(clips)
    assert set(groups) == {"2026-09-27", "2026-09-28"}
    assert len(groups["2026-09-27"]) == 2


def test_merge_days_produces_single_group_with_range_label() -> None:
    clips = [_clip("2026-09-27"), _clip("2026-09-30"), _clip("2026-09-28")]
    groups = group_clips_by_day(clips, merge_days=True)
    assert list(groups) == ["2026-09-27_2026-09-30"]
    assert len(groups["2026-09-27_2026-09-30"]) == 3


def test_merge_days_single_day_keeps_label() -> None:
    groups = group_clips_by_day([_clip("2026-09-27"), _clip("2026-09-27")], merge_days=True)
    assert list(groups) == ["2026-09-27"]


def test_merge_days_without_any_date() -> None:
    clips = [ClipInfo(path=Path("ohne-datum.mov"), duration=5.0)]
    groups = group_clips_by_day(clips, merge_days=True)
    assert list(groups) == ["_ohne-datum"]


def test_default_groups_without_any_date() -> None:
    clips = [ClipInfo(path=Path("ohne-datum.mov"), duration=5.0)]
    groups = group_clips_by_day(clips, merge_days=False)
    assert list(groups) == ["_ohne-datum"]
    assert len(groups["_ohne-datum"]) == 1


def test_group_clips_empty_list() -> None:
    assert group_clips_by_day([], merge_days=False) == {}
    assert group_clips_by_day([], merge_days=True) == {"_ohne-datum": []}


def test_parser_exposes_single_reel_flag() -> None:
    parser = build_parser()
    assert parser.parse_args(["-i", "x.mov", "--single-reel"]).merge_days is True
    # Alias und Default
    assert parser.parse_args(["-i", "x.mov", "--merge-days"]).merge_days is True
    assert parser.parse_args(["-i", "x.mov"]).merge_days is False
