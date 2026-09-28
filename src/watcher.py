"""Ingest-Watcher: überwacht den Eingangsordner und löst Pipeline-Läufe aus.

Debounce-Schutz: Eine Datei gilt erst als "vollständig geschrieben", wenn ihre
Größe über mindestens ``watcher.debounce_seconds`` unverändert geblieben ist.
Danach wird noch ``watcher.batch_window`` Sekunden weitergesammelt, damit ein
iPhone-Burst (IMG_0001..IMG_0012) in einem einzigen Reel landet.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import (
    ConfigError,
    expand_path,
    get,
    iter_media_files,
    load_config,
    pipeline,
    setup_logging,
)

log = logging.getLogger("tenor.watcher")

STATE_FILENAME = ".tenor-state.json"


def is_media(path: Path, extensions: Sequence[str]) -> bool:
    """Endungs- und Temp-Dateicheck (SMB/CIFS schreibt oft ``.part``-Zwischenstände)."""
    if not path.is_file():
        return False
    wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    if path.suffix.lower() not in wanted:
        return False
    name = path.name
    return not (
        name.startswith(".")
        or name.endswith((".part", ".tmp", ".crdownload", "~", ".swp"))
        or "AppleDouble" in name
        or name.startswith("._")
    )


def size_stable(path: Path, checks: int = 2, poll: float = 1.0) -> bool:
    """True, wenn die Dateigröße über ``checks`` Messungen konstant bleibt."""
    last: int | None = None
    for _ in range(max(checks, 1)):
        try:
            current = path.stat().st_size
        except OSError:
            return False
        if last is not None and current != last:
            return False
        last = current
        if poll:
            time.sleep(poll)
    return True


def wait_until_stable(path: Path, stable_seconds: float, poll: float = 1.0) -> bool:
    """Blockiert, bis die Dateigröße ``stable_seconds`` lang stabil ist."""
    deadline = time.monotonic() + max(stable_seconds, 0.0)
    last_size: int | None = None
    stable_since: float | None = None

    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("Stat() für %s fehlgeschlagen: %s", path.name, exc)
            return False

        now = time.monotonic()
        if size != last_size:
            last_size = size
            stable_since = now
        elif stable_since is not None and now - stable_since >= stable_seconds:
            return True

        if now > deadline + stable_seconds:
            # Datei wächst noch - weiter warten (harte Obergrenze vermeiden).
            deadline = now
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class WatcherState:
    """Merkt sich bereits verarbeitete Dateien (überlebt Neustarts)."""

    path: Path
    processed: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> WatcherState:
        state = cls(path=path)
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("processed"), dict):
                state.processed = {str(k): float(v) for k, v in data["processed"].items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            log.warning("State-Datei nicht lesbar (%s): %s", path, exc)
        return state

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"processed": self.processed}, fh, indent=1, sort_keys=True)
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("State-Datei nicht schreibbar: %s", exc)

    def key(self, path: Path) -> str:
        return str(path.resolve())

    def is_processed(self, path: Path) -> bool:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        known = self.processed.get(self.key(path))
        return known is not None and abs(known - mtime) < 1e-6

    def mark(self, path: Path) -> None:
        try:
            self.processed[self.key(path)] = path.stat().st_mtime
        except OSError:
            return
        # State nicht unbegrenzt wachsen lassen
        if len(self.processed) > 5000:
            for stale in sorted(self.processed.items(), key=lambda item: item[1])[:1000]:
                self.processed.pop(stale[0], None)


# --------------------------------------------------------------------------- #
# Verarbeitung
# --------------------------------------------------------------------------- #
class IngestProcessor:
    """Sammelt stabile Dateien ein und startet die Pipeline im Batch."""

    def __init__(self, cfg: Mapping[str, Any], *, config_path: str | None = None) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.extensions: list[str] = list(get(cfg, "extensions", [".mov", ".mp4"]))
        self.ingest_dir = expand_path(get(cfg, "ingest_dir", "."))
        self.debounce = float(get(cfg, "watcher.debounce_seconds", 5) or 5)
        self.batch_window = float(get(cfg, "watcher.batch_window", 12) or 0)
        self.delete_after = bool(get(cfg, "watcher.delete_after", False))
        self.recursive = bool(get(cfg, "watcher.recursive", False))
        self.ignore_dirs = list(get(cfg, "watcher.ignore_dirs", []))
        self.output_dir = expand_path(get(cfg, "output_dir", "output"))
        state_dir = self.output_dir.parent if self.output_dir.name else self.output_dir
        self.state = WatcherState.load(state_dir / STATE_FILENAME)
        self._pending: set[Path] = set()
        self._last_added: float = 0.0
        self._lock = threading.Lock()
        self._collect_lock = threading.Lock()
        self._stop = threading.Event()

    # -- Ereignisse ------------------------------------------------------- #
    def on_file_event(self, path: Path) -> None:
        """Wird vom Handler bei create/move/modify aufgerufen."""
        if not is_media(path, self.extensions):
            return
        try:
            if self.state.is_processed(path):
                log.debug("Bereits verarbeitet, ignoriere: %s", path.name)
                return
        except Exception as exc:  # pragma: no cover
            log.debug("State-Prüfung fehlgeschlagen (%s): %s", path, exc)
        with self._lock:
            if path in self._pending:
                return
            self._pending.add(path)
            self._last_added = time.monotonic()
        log.info("Neue Eingang erkannt: %s", path.name)
        threading.Thread(target=self._settle_and_collect, args=(path,), daemon=True).start()

    def _settle_and_collect(self, path: Path) -> None:
        """Wartet auf Stabilität + Batch-Fenster und triggert danach den Lauf.

        Nur ein Thread sammelt gleichzeitig ein, damit ein Burst nicht in
        mehreren konkurrierenden Exporten landet.
        """
        if self._stop.is_set():
            return
        if not wait_until_stable(path, self.debounce):
            log.warning("Datei unvollständig oder entfernt, wird übersprungen: %s", path.name)
            with self._lock:
                self._pending.discard(path)
            return

        with self._collect_lock:
            if self._stop.is_set():
                return
            self._await_batch_window()
            with self._lock:
                batch = sorted(self._pending)
                self._pending.clear()
                self._last_added = 0.0
        if batch:
            self.process_batch(batch)

    def _await_batch_window(self) -> None:
        """Wartet, bis sich im Batch-Fenster nichts Neues mehr tut."""
        window = max(self.batch_window, 1.0)
        hard_limit = window * 3 + self.debounce
        deadline = time.monotonic() + hard_limit
        while not self._stop.is_set() and time.monotonic() < deadline:
            with self._lock:
                pending = set(self._pending)
                last_added = self._last_added
            if not pending:
                return
            quiet_for = time.monotonic() - last_added
            if quiet_for >= window and all(size_stable(p, checks=1, poll=0) for p in pending):
                log.info("Batch-Fenster erreicht: %d Datei(en)", len(pending))
                return
            time.sleep(1.0)

    def process_batch(self, batch: Sequence[Path]) -> Any:
        """Übergibt einen Batch an die Pipeline und merkt sich den Erfolg."""
        files = [p for p in batch if is_media(p, self.extensions) and not self.state.is_processed(p)]
        if not files:
            log.info("Keine neuen Dateien im Batch - nichts zu tun")
            return None

        log.info("Verarbeite %d Datei(en): %s", len(files), ", ".join(p.name for p in files))
        try:
            result = pipeline.run(
                [str(p) for p in files],
                config_path=self.config_path,
                output_dir=self.output_dir,
                delete_after=self.delete_after,
            )
        except Exception as exc:
            log.exception("Pipeline-Lauf fehlgeschlagen: %s", exc)
            return None

        if result.ok:
            for path in files:
                if not self.delete_after:
                    self.state.mark(path)
            self.state.save()
            for reel in result.outputs or ([result.output] if result.output else []):
                log.info("Reel geschrieben: %s", reel)
        else:
            log.error("Kein Export - Dateien bleiben zur späteren Verarbeitung liegen")
        return result

    def scan_existing(self) -> int:
        """Verarbeitet beim Start bereits vorhandene, unbekannte Clips."""
        files = iter_media_files(
            self.ingest_dir, self.extensions, recursive=self.recursive, ignore_dirs=self.ignore_dirs
        )
        fresh = [p for p in files if not self.state.is_processed(p)]
        if not fresh:
            log.info("Keine unverarbeiteten Clips im Ingest-Ordner")
            return 0
        log.info("Ingest-Scan findet %d unverarbeitete Datei(en)", len(fresh))
        self.process_batch(fresh)
        return len(fresh)

    def request_stop(self) -> None:
        self._stop.set()

    def cleanup_partials(self) -> int:
        """Entfernt ``*.part.mp4`` aus dem Output - Reste abgebrochener Läufe.

        Wird beim Start aufgerufen: nach einem SIGTERM oder einem Stromausfall
        können unfertige Renderdateien liegen bleiben. Sie sind nie gültig
        und werden nie von der Pipeline weiterverwendet.
        """
        if not self.output_dir.is_dir():
            return 0
        removed = 0
        for part in self.output_dir.rglob("*.part.mp4"):
            try:
                part.unlink()
                removed += 1
                log.info("Verwaiste Renderdatei entfernt: %s", part.name)
            except OSError as exc:
                log.warning("Renderdatei nicht löschbar (%s): %s", part.name, exc)
        return removed


class IngestHandler(FileSystemEventHandler):
    """Leitet watchdog-Ereignisse an den :class:`IngestProcessor` weiter."""

    def __init__(self, processor: IngestProcessor) -> None:
        self.processor = processor

    def _maybe(self, raw_path: str | bytes) -> None:
        try:
            path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        except (UnicodeDecodeError, ValueError) as exc:  # pragma: no cover
            log.debug("Pfad nicht dekodierbar: %r (%s)", raw_path, exc)
            return
        try:
            self.processor.on_file_event(path)
        except Exception as exc:  # Handler darf den Observer nie killen
            log.exception("Fehler im Event-Handler: %s", exc)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe(event.dest_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        # nützlich bei Netzwerk-Shares, die kein 'created' liefern
        if not event.is_directory:
            self._maybe(event.src_path)


# --------------------------------------------------------------------------- #
# Watchdog-Loop
# --------------------------------------------------------------------------- #
def watch(cfg: Mapping[str, Any], *, config_path: str | None = None, once: bool = False) -> int:
    """Startet den Watcher (blockierend) und läuft bis SIGTERM/SIGINT."""
    processor = IngestProcessor(cfg, config_path=config_path)
    ingest_dir = processor.ingest_dir
    if not ingest_dir.is_dir():
        log.error("Ingest-Ordner existiert nicht: %s", ingest_dir)
        return 2
    ingest_dir.mkdir(parents=True, exist_ok=True)

    try:
        processor.cleanup_partials()
    except Exception as exc:  # pragma: no cover
        log.warning("Aufräumen verwaister Renderdateien fehlgeschlagen: %s", exc)

    if bool(get(cfg, "watcher.process_existing_on_start", True)):
        try:
            processor.scan_existing()
        except Exception as exc:  # pragma: no cover
            log.exception("Ingest-Scan fehlgeschlagen: %s", exc)

    if once:
        return 0

    handler = IngestHandler(processor)
    observer = Observer()
    observer.schedule(handler, str(ingest_dir), recursive=processor.recursive)
    observer.start()
    log.info(
        "Überwache %s (Debounce %.0fs, Batch-Fenster %.0fs)",
        ingest_dir,
        processor.debounce,
        processor.batch_window,
    )

    stopping = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info("Signal %s empfangen - Watcher wird beendet", signum)
        stopping.set()
        processor.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except ValueError:  # pragma: no cover - nicht im Hauptthread
            pass

    try:
        while not stopping.is_set():
            stopping.wait(1.0)
    finally:
        observer.stop()
        observer.join(timeout=10)
        log.info("Watcher beendet")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.watcher",
        description="Ingest-Ordner überwachen und neue Clips automatisch zu Reels verarbeiten.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-c", "--config", help="Pfad zur config.yaml")
    parser.add_argument("-i", "--ingest", help="Ingest-Ordner (Override)")
    parser.add_argument("-o", "--output", help="Ausgabeordner (Override)")
    parser.add_argument("--once", action="store_true", help="nur einmal scannen und beenden")
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="beim Start keine vorhandenen Dateien einlesen",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="nur Warnungen und Fehler loggen")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    if args.ingest:
        cfg["ingest_dir"] = args.ingest
    if args.output:
        cfg["output_dir"] = args.output
    if args.no_scan:
        cfg["watcher"] = {**cfg.get("watcher", {}), "process_existing_on_start": False}

    setup_logging(cfg, quiet=args.quiet)
    return watch(cfg, config_path=args.config, once=args.once)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
