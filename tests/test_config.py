from __future__ import annotations

import os

from src import _apply_env_overrides, _coerce


def test_coerce_list():
    assert _coerce("1.0, -1.0, 2.0", [0.3]) == [1.0, -1.0, 2.0]
    assert _coerce("a, b, c", ["string"]) == ["a", "b", "c"]
    assert _coerce("1, 2, 3", [1]) == [1, 2, 3]
    assert _coerce("true, false, 1", [True]) == [True, False, True]
    assert _coerce("a, b", []) == ["a", "b"]


def test_apply_env_overrides_list():
    cfg = {"test_list": [0.1, 0.2]}
    os.environ["TENOR_TEST_LIST"] = "0.5, 0.6"
    _apply_env_overrides(cfg)
    assert cfg["test_list"] == [0.5, 0.6]
    del os.environ["TENOR_TEST_LIST"]
