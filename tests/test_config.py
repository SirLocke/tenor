from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from src import (
    REPO_ROOT,
    ConfigError,
    _apply_env_overrides,
    _coerce,
    _read_yaml_mapping,
    find_config,
    load_config,
    setup_logging,
)


def test_coerce_list():
    assert _coerce("1.0, -1.0, 2.0", [0.3], "TENOR_QUALITY_SEARCH_OFFSETS", "quality.search_offsets") == [1.0, -1.0, 2.0]
    assert _coerce("a, b, c", ["string"]) == ["a", "b", "c"]
    assert _coerce("1, 2, 3", [1]) == [1, 2, 3]
    assert _coerce("true, false, 1", [True]) == [True, False, True]
    assert _coerce("a, b", []) == ["a", "b"]


def test_coerce_invalid_int_and_float():
    with pytest.raises(ConfigError, match="ist kein gueltiger Integer"):
        _coerce("not_an_int", 10, "TEST_INT")
    with pytest.raises(ConfigError, match="ist kein gueltiger Float"):
        _coerce("not_a_float", 10.5, "TEST_FLOAT")


def test_coerce_negative_range():
    with pytest.raises(ConfigError, match="darf nicht negativ sein"):
        _coerce("-5", 10, "TEST_INT", "video.width")
    with pytest.raises(ConfigError, match="darf nicht negativ sein"):
        _coerce("-5.5", 10.5, "TEST_FLOAT", "music.gain")

    # Offsets should allow negative numbers
    assert _coerce("-0.3", 0.0, "TENOR_QUALITY_SEARCH_OFFSETS", "quality.search_offsets") == -0.3


def test_apply_env_overrides_list():
    cfg = {"test_list": [0.1, 0.2]}
    os.environ["TENOR_TEST_LIST"] = "0.5, 0.6"
    try:
        _apply_env_overrides(cfg)
        assert cfg["test_list"] == [0.5, 0.6]
    finally:
        os.environ.pop("TENOR_TEST_LIST", None)


def test_find_config_missing_explicit_path():
    missing_path = Path("/nonexistent/config_file.yaml")
    with pytest.raises(ConfigError, match="Konfiguration nicht gefunden"):
        find_config(missing_path)


def test_find_config_missing_env_path(monkeypatch):
    monkeypatch.setenv("TENOR_CONFIG", "/nonexistent/tenor_config.yaml")
    with pytest.raises(ConfigError, match="TENOR_CONFIG zeigt auf eine fehlende Datei"):
        find_config()


def test_find_config_fallback_to_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv("TENOR_CONFIG", raising=False)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    # When neither cwd/config.yaml nor REPO_ROOT/config.yaml exists in a mock environment,
    # find_config should return REPO_ROOT / "config.yaml"
    with patch.object(Path, "is_file", return_value=False):
        res = find_config()
        assert res == REPO_ROOT / "config.yaml"


def test_read_yaml_mapping_invalid_yaml(tmp_path):
    invalid_yaml_file = tmp_path / "invalid.yaml"
    invalid_yaml_file.write_text("key: [unclosed_list", encoding="utf-8")

    with pytest.raises(ConfigError, match="ist kein gültiges YAML"):
        _read_yaml_mapping(invalid_yaml_file)


def test_read_yaml_mapping_non_dict_yaml(tmp_path):
    non_dict_file = tmp_path / "list.yaml"
    non_dict_file.write_text("- item1\n- item2\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="muss ein YAML-Mapping enthalten"):
        _read_yaml_mapping(non_dict_file)


def test_load_config_with_local_config(tmp_path):
    config_file = tmp_path / "config.yaml"
    local_config_file = tmp_path / "config.local.yaml"

    config_file.write_text("video:\n  fps: 24\n", encoding="utf-8")
    local_config_file.write_text("video:\n  fps: 60\n", encoding="utf-8")

    cfg = load_config(config_file)
    assert cfg["video"]["fps"] == 60
    assert cfg["_config_path"] == str(config_file)
    assert cfg["_local_config_path"] == str(local_config_file)


def test_setup_logging_value_error():
    cfg = {"logging": {"level": "INFO", "file": "invalid_path"}}
    with patch("src.expand_path", side_effect=ValueError("Ungültiger Pfad")):
        logger = setup_logging(cfg)
        assert isinstance(logger, logging.Logger)


def test_setup_logging_oserror():
    cfg = {"logging": {"level": "INFO", "file": "/nonexistent_dir/tenor.log"}}
    with patch("src.expand_path", side_effect=OSError("Permission denied")):
        logger = setup_logging(cfg)
        assert isinstance(logger, logging.Logger)
