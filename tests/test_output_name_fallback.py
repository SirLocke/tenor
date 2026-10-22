import pytest
from src.pipeline import build_output_name

def test_build_output_name_fallback_empty_template():
    cfg = {"naming": {"template": ""}}
    result = build_output_name(cfg, "Test Title", 1600000000)
    assert result == "reel"

def test_build_output_name_fallback_strips_separators():
    cfg = {"naming": {"template": "_{slug}_"}}
    result = build_output_name(cfg, "", 1600000000)
    # The slug for "" is "reel", so "_{slug}_" -> "_reel_" -> "reel"
    assert result == "reel"

def test_build_output_name_fallback_only_spaces():
    cfg = {"naming": {"template": "   "}}
    result = build_output_name(cfg, "Test Title", 1600000000)
    assert result == "reel"

def test_build_output_name_normal():
    cfg = {"naming": {"template": "{date}_{slug}"}}
    result = build_output_name(cfg, "My Video", 1600000000)
    # 1600000000 is 2020-09-13
    assert result == "2020-09-13_My-Video"

def test_build_output_name_invalid_chars_stripped():
    cfg = {"naming": {"template": "{slug}?!*"}}
    result = build_output_name(cfg, "Test Title", 1600000000)
    assert result == "Test-Title"

def test_build_output_name_collapse_underscores():
    cfg = {"naming": {"template": "{date}_____{slug}"}}
    result = build_output_name(cfg, "My Video", 1600000000)
    assert result == "2020-09-13_My-Video"
