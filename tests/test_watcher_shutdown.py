import threading
import time
from pathlib import Path

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

    t = threading.Thread(target=waiter)
    t.start()

    # Give it a moment to block
    time.sleep(0.1)

    # Assert it hasn't unblocked yet because inflight is not empty
    assert not result, "Should block while inflight is not empty"

    # Clear inflight to allow unblocking
    with processor._lock:
        processor._inflight.clear()

    # Join with a timeout to prevent hanging test on failure
    t.join(timeout=2)

    # Assert it unblocked successfully
    assert result == ["done"], "Should unblock when inflight is empty"
