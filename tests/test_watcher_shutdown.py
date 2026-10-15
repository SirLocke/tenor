import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from src.watcher import IngestProcessor


def test_wait_for_renders_blocks_and_unblocks():
    # Setup mock config and processor
    cfg = {"ingest_dir": "test_ingest", "output_dir": "test_out"}
    processor = IngestProcessor(cfg)

    # Add a mock inflight item
    processor._inflight.add(Path("dummy.mp4"))

    result = []

    def waiter():
        processor.wait_for_renders()
        result.append("done")

    t = threading.Thread(target=waiter, daemon=True)
    t.start()

    # Give it a moment to block
    time.sleep(0.1)

    try:
        # Assert it hasn't unblocked yet because inflight is not empty
        assert not result, "Should block while inflight is not empty"
    finally:
        # Clear inflight to allow unblocking
        with processor._lock:
            processor._inflight.clear()

        # Join with a timeout to prevent hanging test on failure
        t.join(timeout=2)

    # Assert it unblocked successfully
    assert result == ["done"], "Should unblock when inflight is empty"


def test_settle_and_collect_returns_early_on_stop():
    cfg = {"ingest_dir": "test_ingest", "output_dir": "test_out", "watcher.batch_window": 0.5, "watcher.debounce_seconds": 0}
    processor = IngestProcessor(cfg)

    # Mock some methods to trace behavior
    processor.process_batch = MagicMock()
    processor._await_batch_window = MagicMock()

    # Set the state such that wait_until_stable would succeed instantly
    processor._pending.add(Path("dummy.mp4"))

    # Trigger stop immediately
    processor.request_stop()

    # Bypass wait_until_stable inside _settle_and_collect by forcing it to return True
    # or just let it fail naturally if debounce is checked, but we really just want to ensure
    # process_batch isn't called.
    # Actually wait_until_stable checks file existance. We should mock it.
    import unittest.mock
    with unittest.mock.patch("src.watcher.wait_until_stable", return_value=True):
        processor._settle_and_collect(Path("dummy.mp4"))

    # Because stop is set, _await_batch_window or the stop check after it should have returned early.
    processor.process_batch.assert_not_called()
