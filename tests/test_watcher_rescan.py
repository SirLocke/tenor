import threading
from unittest.mock import patch

from src.watcher import IngestProcessor, watch


def test_rescan_interval_config():
    # Defaults to 300
    cfg1 = {"ingest_dir": "test_ingest"}
    processor1 = IngestProcessor(cfg1)
    assert processor1.rescan_interval == 300.0

    # Overridden to 60
    cfg2 = {"ingest_dir": "test_ingest", "watcher": {"rescan_interval": 60}}
    processor2 = IngestProcessor(cfg2)
    assert processor2.rescan_interval == 60.0

    # Disabled
    cfg3 = {"ingest_dir": "test_ingest", "watcher": {"rescan_interval": 0}}
    processor3 = IngestProcessor(cfg3)
    assert processor3.rescan_interval == 0.0


@patch("src.watcher.IngestProcessor.scan_existing")
@patch("src.watcher.IngestProcessor.cleanup_partials")
@patch("src.watcher.IngestProcessor.wait_for_renders")
@patch("watchdog.observers.Observer.start")
@patch("watchdog.observers.Observer.stop")
@patch("watchdog.observers.Observer.join")
@patch("watchdog.observers.Observer.schedule")
@patch("time.monotonic")
def test_watch_loop_rescan(
    mock_monotonic,
    mock_schedule,
    mock_join,
    mock_stop,
    mock_start,
    mock_wait_for_renders,
    mock_cleanup_partials,
    mock_scan_existing,
    tmp_path,
):
    # Pass a valid dir so watch() does not abort
    ingest_dir = tmp_path / "test_ingest"
    ingest_dir.mkdir()
    cfg = {
        "ingest_dir": str(ingest_dir),
        "watcher": {"rescan_interval": 60, "process_existing_on_start": False},
    }

    # We want to simulate the passage of time without actually sleeping.
    # time.monotonic() calls:
    # 1. Before loop (last_scan)
    # 2. Inside loop check
    # 3. After scan_existing() if it ran

    time_values = [0.0, 30.0, 61.0, 61.0, 122.0, 122.0]

    def monotonic_side_effect():
        if time_values:
            return time_values.pop(0)
        return 150.0

    mock_monotonic.side_effect = monotonic_side_effect

    # We need to stop the loop after a few iterations.
    real_event = threading.Event()
    real_wait = real_event.wait
    wait_calls = [0]

    def wait_side_effect(timeout):
        wait_calls[0] += 1
        if wait_calls[0] >= 3:
            # Tell the loop to stop on the 3rd iteration
            real_event.set()
        return real_wait(0.01)

    with (
        patch.object(threading.Event, "wait", side_effect=wait_side_effect),
        patch.object(threading.Event, "is_set", side_effect=real_event.is_set),
    ):
        watch(cfg)

    # 1st loop iter: monotonic is 30, last_scan is 0. 30 < 60, no scan.
    # 2nd loop iter: monotonic is 61, last_scan is 0. 61 >= 60, scan happens!
    # 3rd loop iter: monotonic is 122, last_scan is 61. 122-61 >= 60, scan happens! loop stops.
    assert mock_scan_existing.call_count == 2
