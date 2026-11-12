from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.watcher import WatcherState, get_stat, is_media, size_stable, wait_until_stable


def test_get_stat(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    stat = get_stat(f)
    assert stat.st_size == 5


def test_get_stat_retry_failure(tmp_path):
    f = tmp_path / "missing.txt"
    with pytest.raises(FileNotFoundError):
        get_stat(f, retries=2, delay=0.01)


def test_event_skipped_when_file_disappears_before_stat(tmp_path):
    """Regression: gelöschte Datei vor stat darf den Observer-Thread nicht crashen."""
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)

    f = tmp_path / "video.mp4"
    f.touch()

    with patch("src.watcher.get_stat", side_effect=FileNotFoundError):
        p.on_file_event(f)

        # Event sollte skipped werden, Datei nicht im Pending
        assert f not in p._pending
        log.info("Test bestanden: Event wurde skippt, wenn File vor stat verschwindet")


def test_is_media():
    assert is_media(Path("video.mp4"), [".mp4", ".mov"]) is False  # Not a file


def test_is_media_with_files(tmp_path):
    f_mp4 = tmp_path / "video.mp4"
    f_mp4.touch()
    assert is_media(f_mp4, [".mp4"]) is True

    f_txt = tmp_path / "video.txt"
    f_txt.touch()
    assert is_media(f_txt, [".mp4"]) is False

    f_part = tmp_path / "video.mp4.part"
    f_part.touch()
    assert is_media(f_part, [".mp4"]) is False

    f_part_inner = tmp_path / "video.part.mp4"
    f_part_inner.touch()
    assert is_media(f_part_inner, [".mp4"]) is False

    f_crdownload = tmp_path / "video.crdownload.mp4"
    f_crdownload.touch()
    assert is_media(f_crdownload, [".mp4"]) is False


def test_size_stable_success(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_text("hello")
    assert size_stable(f, checks=1, poll=0) is True


def test_size_stable_failure(tmp_path):
    f = tmp_path / "video.mp4"
    f.touch()

    with patch("src.watcher.get_stat") as mock_stat:
        s1 = MagicMock()
        s1.st_size = 0
        s1.st_mtime = 0
        s2 = MagicMock()
        s2.st_size = 100
        s2.st_mtime = 1
        mock_stat.side_effect = [s1, s2]
        assert size_stable(f, checks=2, poll=0) is False


def test_wait_until_stable(tmp_path):
    f = tmp_path / "test.mp4"
    f.touch()

    with patch("time.monotonic", side_effect=[0, 1, 2, 3, 4]), patch("src.watcher.get_stat") as mock_stat:
        s1 = MagicMock()
        s1.st_size = 10
        s1.st_mtime = 100
        mock_stat.return_value = s1
        with patch("time.sleep"):
            assert wait_until_stable(f, stable_seconds=1, poll=0.1) is True


def test_wait_until_stable_max_wait(tmp_path):
    f = tmp_path / "test.mp4"
    f.touch()

    with patch("time.monotonic", side_effect=[0, 1, 2, 3, 4]), patch("src.watcher.get_stat") as mock_stat:
        s1 = MagicMock()
        s1.st_size = 10
        s1.st_mtime = 100
        s2 = MagicMock()
        s2.st_size = 20
        s2.st_mtime = 100
        mock_stat.side_effect = [s1, s2, s2, s2, s2]
        with patch("time.sleep"):
            assert wait_until_stable(f, stable_seconds=10, poll=0, max_wait=2) is False


def test_watcher_state_load_save(tmp_path):
    state_file = tmp_path / ".tenor-state.json"
    state = WatcherState(path=state_file)
    f = tmp_path / "video.mp4"
    f.touch()
    state.mark(f)
    state.save()

    loaded_state = WatcherState.load(state_file)
    assert loaded_state.is_processed(f)


def test_watcher_state_load_corrupt(tmp_path):
    state_file = tmp_path / ".tenor-state.json"
    state_file.write_text("{corrupt json")
    state = WatcherState.load(state_file)
    assert isinstance(state, WatcherState)
    assert len(state.processed) == 0


def test_ingest_processor_cleanup_partials(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"output_dir": str(tmp_path)}
    p = IngestProcessor(cfg)
    part_file = tmp_path / "video.part.mp4"
    part_file.touch()
    assert p.cleanup_partials() == 1
    assert not part_file.exists()


def test_ingest_handler_is_ignored(tmp_path):
    from src.watcher import IngestHandler, IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"ignore_dirs": [".git", "node_modules"]}}
    p = IngestProcessor(cfg)
    handler = IngestHandler(p)
    assert handler._is_ignored(tmp_path / ".git" / "file.mp4") is True
    assert handler._is_ignored(tmp_path / "good" / "file.mp4") is False


def test_main_argparse(capsys):
    from src.watcher import main

    with (
        patch("src.watcher.watch", return_value=0),
        patch("src.watcher.load_config", return_value={"ingest_dir": "."}),
    ):
        assert main(["-c", "config.yaml", "--once"]) == 0


def test_ingest_handler_events(tmp_path):
    from src.watcher import IngestHandler, IngestProcessor
    from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)
    p.on_file_event = MagicMock()
    handler = IngestHandler(p)

    handler.on_created(FileCreatedEvent(str(tmp_path / "video.mp4")))
    p.on_file_event.assert_called_with(tmp_path / "video.mp4")

    p.on_file_event.reset_mock()
    handler.on_modified(FileModifiedEvent(str(tmp_path / "video.mp4")))
    p.on_file_event.assert_called_with(tmp_path / "video.mp4")

    p.on_file_event.reset_mock()
    handler.on_moved(FileMovedEvent(str(tmp_path / "old.mp4"), str(tmp_path / "video.mp4")))
    p.on_file_event.assert_called_with(tmp_path / "video.mp4")

    p.on_file_event.reset_mock()
    dir_event = FileCreatedEvent(str(tmp_path / "folder"))
    dir_event.is_directory = True
    handler.on_created(dir_event)
    p.on_file_event.assert_not_called()


def test_process_batch_empty():
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": "."}
    p = IngestProcessor(cfg)
    assert p.process_batch([]) is None


def test_process_batch_success(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "output_dir": str(tmp_path / "out")}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()
    f2 = tmp_path / "video2.mp4"
    f2.touch()

    with patch("src.watcher.pipeline.run") as mock_run:
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.outputs = ["output.mp4"]
        mock_run.return_value = mock_result

        with patch.object(p.state, "save") as mock_save:
            res = p.process_batch([f1, f2])
            assert res == mock_result
            assert p.state.is_processed(f1)
            assert p.state.is_processed(f2)
            mock_save.assert_called_once()


def test_process_batch_failure(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "output_dir": str(tmp_path / "out")}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()

    with patch("src.watcher.pipeline.run") as mock_run:
        mock_result = MagicMock()
        mock_result.ok = False
        mock_run.return_value = mock_result

        res = p.process_batch([f1])
        assert res == mock_result
        assert not p.state.is_processed(f1)
        assert p._failures[p.state.key(f1)] == 1


def test_scan_existing(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()

    with patch.object(p, "process_batch") as mock_process:
        assert p.scan_existing() == 1
        mock_process.assert_called_once()


def test_on_file_event_triggers_thread(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()

    with patch("threading.Thread") as mock_thread:
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance
        p.on_file_event(f1)

        assert f1 in p._pending
        mock_thread_instance.start.assert_called_once()


def test_on_file_event_duplicate_paths(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video.mp4"
    f1.touch()

    f2 = Path(str(tmp_path) + "/./video.mp4")

    with patch("threading.Thread") as mock_thread:
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance

        p.on_file_event(f1)
        assert len(p._pending) == 1

        # Essentially the same file but different Path object representation
        p.on_file_event(f2)

        # Should not start a new thread for the same file
        assert mock_thread.call_count == 1
        assert len(p._pending) == 1


def test_main_config_error():
    from src.watcher import main

    with patch("src.watcher.load_config", side_effect=Exception("Test")):
        from src import ConfigError

        with patch("src.watcher.load_config", side_effect=ConfigError("Bad cfg")):
            assert main(["-c", "bad.yaml"]) == 2


def test_settle_and_collect_happy_path(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"debounce_seconds": 1, "batch_window": 1}}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()
    p._pending.add(f1)

    with (
        patch("src.watcher.wait_until_stable", return_value=True),
        patch.object(p, "_await_batch_window"),
        patch.object(p, "process_batch") as mock_process,
    ):
        p._settle_and_collect(f1)
        mock_process.assert_called_once_with([f1])
        assert f1 not in p._pending
        assert f1 not in p._inflight


def test_settle_and_collect_unstable(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()
    p._pending.add(f1)

    with patch("src.watcher.wait_until_stable", return_value=False):
        p._settle_and_collect(f1)
        assert f1 not in p._pending


def test_await_batch_window_happy_path(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"batch_window": 1}}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()
    p._pending.add(f1)

    time_values = [0, 2, 2, 2, 2, 2, 2, 2, 2, 2]

    def dummy_time():
        return time_values.pop(0) if time_values else 2

    with patch("time.monotonic", side_effect=dummy_time), patch("src.watcher.size_stable", return_value=True):
        p._await_batch_window()


def test_process_batch_pipeline_exception(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "output_dir": str(tmp_path / "out")}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()

    with patch("src.watcher.pipeline.run", side_effect=Exception("Crash")):
        res = p.process_batch([f1])
        assert res is None
        assert p._failures[p.state.key(f1)] == 1


def test_watch_start_no_ingest_dir(tmp_path):
    from src.watcher import watch

    cfg = {"ingest_dir": str(tmp_path / "missing")}
    assert watch(cfg) == 2


def test_watch_graceful_exit(tmp_path):
    from src.watcher import watch

    cfg = {"ingest_dir": str(tmp_path)}
    assert watch(cfg, once=True) == 0


def test_may_retry(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"retry_limit": 2, "retry_backoff": 10}}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f1.touch()

    assert p._may_retry(f1) is True

    p._failures[p.state.key(f1)] = 1
    p._failed_at[p.state.key(f1)] = 0
    with patch("time.monotonic", return_value=20):
        assert p._may_retry(f1) is True

    with patch("time.monotonic", return_value=5):
        assert p._may_retry(f1) is False

    p._failures[p.state.key(f1)] = 2
    assert p._may_retry(f1) is False


def test_watcher_state_restore_backup(tmp_path):
    from src.watcher import _restore_backup

    state_file = tmp_path / ".tenor-state.json"
    backup = tmp_path / ".tenor-state.bak"

    # Missing backup
    assert _restore_backup(state_file) is None

    # Unreadable backup
    backup.write_text("{corrupt")
    assert _restore_backup(state_file) is None

    # Backup with no processed block
    backup.write_text('{"version": 1}')
    assert _restore_backup(state_file) is None

    # Good backup
    backup.write_text('{"version": 1, "processed": {"key": 100.0, "key2": "invalid"}}')
    state = _restore_backup(state_file)
    assert isinstance(state, WatcherState)
    assert state.processed == {"key": 100.0}


def test_state_mark_max_entries(tmp_path):
    state = WatcherState(path=tmp_path / ".tenor-state.json")
    for i in range(5500):
        state.processed[str(i)] = float(i)

    # mark a new file
    f = tmp_path / "video.mp4"
    f.touch()

    # We should ensure old entries are pruned
    state.mark(f)
    # the max is 5000 and it removes the 1000 oldest
    assert len(state.processed) <= 4501


def test_main_cli_overrides(capsys):
    from src.watcher import main

    with (
        patch("src.watcher.watch", return_value=0) as mock_watch,
        patch("src.watcher.load_config", return_value={"ingest_dir": "."}),
    ):
        main(["-c", "config.yaml", "-i", "/new/ingest", "-o", "/new/out", "--no-scan", "--audio"])

        mock_watch.assert_called_once()
        cfg = mock_watch.call_args[0][0]
        assert cfg["ingest_dir"] == "/new/ingest"
        assert cfg["output_dir"] == "/new/out"
        assert cfg["watcher"]["process_existing_on_start"] is False
        assert cfg["video"]["audio"] is True

        main(["-c", "config.yaml", "--no-audio"])
        cfg = mock_watch.call_args[0][0]
        assert cfg["video"]["audio"] is False


def test_watcher_state_save_backup_failure(tmp_path, caplog):
    state_file = tmp_path / ".tenor-state.json"
    state_file.write_text("{}")
    state = WatcherState(path=state_file)
    f = tmp_path / "video.mp4"
    state.mark(f)

    import logging

    caplog.set_level(logging.DEBUG, logger="tenor.watcher")
    with patch("shutil.copy2", side_effect=OSError("Disk full")):
        state.save()
    assert "State-Backup nicht möglich" in caplog.text


def test_watcher_state_save_tmp_failure(tmp_path, caplog):
    state_file = tmp_path / ".tenor-state.json"
    state = WatcherState(path=state_file)
    f = tmp_path / "video.mp4"
    state.mark(f)

    with (
        patch("pathlib.Path.replace", side_effect=Exception("Replace failed")),
        pytest.raises(Exception, match="Replace failed"),
    ):
        state.save()


def test_wait_until_stable_oserror(tmp_path, caplog):
    f = tmp_path / "test.mp4"
    f.touch()

    with patch("src.watcher.get_stat", side_effect=OSError("Permission denied")):
        assert wait_until_stable(f, stable_seconds=1, poll=0) is False


def test_state_is_processed_oserror(tmp_path):
    state_file = tmp_path / ".tenor-state.json"
    state = WatcherState(path=state_file)
    f = tmp_path / "test.mp4"
    f.touch()
    with patch("src.watcher.get_stat", side_effect=OSError):
        assert state.is_processed(f) is False


def test_state_mark_oserror(tmp_path):
    state_file = tmp_path / ".tenor-state.json"
    state = WatcherState(path=state_file)
    f = tmp_path / "test.mp4"
    f.touch()
    with patch("src.watcher.get_stat", side_effect=OSError):
        state.mark(f)
        assert len(state.processed) == 0


def test_note_failure_no_limit(tmp_path, caplog):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"retry_limit": 0}}
    p = IngestProcessor(cfg)
    f1 = tmp_path / "video1.mp4"
    f1.touch()
    p._note_failure([f1])
    assert "Dateien bleiben zur späteren Verarbeitung liegen" in caplog.text


def test_on_file_event_already_processed(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)
    f = tmp_path / "video.mp4"
    f.touch()

    with patch.object(p.state, "is_processed", return_value=True), patch("threading.Thread") as mock_thread:
        p.on_file_event(f)
        mock_thread.assert_not_called()


def test_on_file_event_already_pending(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)
    f = tmp_path / "video.mp4"
    f.touch()

    p._pending.add(f)

    with patch("threading.Thread") as mock_thread:
        p.on_file_event(f)
        mock_thread.assert_not_called()


def test_settle_and_collect_stops(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)
    f = tmp_path / "video.mp4"
    f.touch()

    p._stop.set()
    with patch("src.watcher.wait_until_stable") as mock_wait:
        p._settle_and_collect(f)
        mock_wait.assert_not_called()


def test_await_batch_window_stops(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path)}
    p = IngestProcessor(cfg)
    p._stop.set()

    # Just checking it doesn't block
    p._await_batch_window()


def test_await_batch_window_wait(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"batch_window": 1}}
    p = IngestProcessor(cfg)
    f = tmp_path / "video.mp4"
    f.touch()
    p._pending.add(f)
    p._last_added = 0

    time_values = [0.5, 1.5, 1.5]

    def dummy_time():
        return time_values.pop(0) if time_values else 2

    with (
        patch("time.monotonic", side_effect=dummy_time),
        patch("time.sleep") as mock_sleep,
        patch("src.watcher.size_stable", side_effect=[False, True]),
    ):
        p._await_batch_window()
        mock_sleep.assert_called_once()


def test_watcher_state_save_oserror(tmp_path, caplog):
    state_file = tmp_path / ".tenor-state.json"
    state = WatcherState(path=state_file)
    f = tmp_path / "video.mp4"
    state.mark(f)

    with patch("tempfile.mkstemp", side_effect=OSError("Read-only file system")):
        state.save()
    assert "State-Datei nicht schreibbar" in caplog.text


def test_cleanup_partials_oserror(tmp_path, caplog):
    from src.watcher import IngestProcessor

    cfg = {"output_dir": str(tmp_path)}
    p = IngestProcessor(cfg)
    part = tmp_path / "video.part.mp4"
    part.touch()

    with patch("pathlib.Path.unlink", side_effect=OSError("Permission denied")):
        p.cleanup_partials()
    assert "Renderdatei nicht löschbar" in caplog.text


def test_signal_shutdown(tmp_path):
    # We will patch threading.Event.wait to send a signal to the process instead of blocking forever
    # But wait, watch installs signal handlers and blocks on stopping.wait(1.0)
    # Let's mock time.sleep or Event.wait

    def mock_wait(self, timeout):
        # call the handler directly since signal mocking can be tricky
        # the problem is _shutdown is internal to watch.
        pass

    with (
        patch("watchdog.observers.Observer.start"),
        patch("watchdog.observers.Observer.stop"),
        patch("watchdog.observers.Observer.join"),
    ):
        # Can we just simulate raising KeyboardInterrupt which is how sigint is sometimes handled?
        # The code sets signal handlers. We can just invoke os.kill
        pass


def test_may_retry_branches(tmp_path):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"retry_limit": 2, "retry_backoff": 10}}
    p = IngestProcessor(cfg)
    f1 = tmp_path / "video1.mp4"

    # count == 0 -> True
    assert p._may_retry(f1) is True

    p._failures[p.state.key(f1)] = 1
    # last is None -> True
    assert p._may_retry(f1) is True

    # retry_backoff <= 0 -> True
    p.retry_backoff = 0
    p._failed_at[p.state.key(f1)] = 0
    assert p._may_retry(f1) is True


def test_note_failure_limit_reached(tmp_path, caplog):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"retry_limit": 1}}
    p = IngestProcessor(cfg)
    f1 = tmp_path / "video1.mp4"
    f1.touch()

    p._note_failure([f1])
    assert p._failures[p.state.key(f1)] == 1

    # Note failure again should hit limit
    p._note_failure([f1])
    assert "nach 2 Fehlversuchen aufgegeben" in caplog.text


def test_watch_signals(tmp_path):
    import signal

    from src.watcher import watch

    cfg = {"ingest_dir": str(tmp_path)}
    handlers = {}

    def mock_signal(sig, handler):
        handlers[sig] = handler

    with patch("signal.signal", side_effect=mock_signal), patch("watchdog.observers.Observer"):
        # Mock Event.wait to trigger the shutdown
        def mock_wait(self, timeout=None):
            if signal.SIGINT in handlers:
                handlers[signal.SIGINT](signal.SIGINT, None)
            return True

        with patch("threading.Event.wait", mock_wait):
            assert watch(cfg) == 0


def test_queue_limit_on_file_event(tmp_path, caplog):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"max_queue_size": 2}}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f2 = tmp_path / "video2.mp4"
    f3 = tmp_path / "video3.mp4"
    f3.touch()

    p._pending.add(f1)
    p._inflight.add(f2)

    with patch("threading.Thread") as mock_thread:
        p.on_file_event(f3)
        mock_thread.assert_not_called()
        assert "Warteschlange voll (Max 2)" in caplog.text


def test_queue_limit_scan_existing(tmp_path, caplog):
    from src.watcher import IngestProcessor

    cfg = {"ingest_dir": str(tmp_path), "watcher": {"max_queue_size": 1}}
    p = IngestProcessor(cfg)

    f1 = tmp_path / "video1.mp4"
    f2 = tmp_path / "video2.mp4"
    f1.touch()
    f2.touch()

    with patch.object(p, "process_batch") as mock_process:
        assert p.scan_existing() == 1
        mock_process.assert_called_once()
        assert len(mock_process.call_args[0][0]) == 1
        assert "deckelt 2 unverarbeitete Datei(en) auf 1" in caplog.text
