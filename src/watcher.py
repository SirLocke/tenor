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
import math
import os
import shutil
import signal
import sys
import tempfile
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


def get_stat(path: Path, retries: int = 3, delay: float = 0.5) -> os.stat_result:
    """Versucht path.stat() mit Retries für flüchtige Dateisystemfehler."""
    for attempt in range(retries):
        try:
            return path.stat()
        except FileNotFoundError:
            raise
        except OSError as exc:
            if attempt < retries - 1:
                log.debug("stat() fehlgeschlagen, %d. Retry für %s: %s", attempt + 1, path.name, exc)
                time.sleep(delay)
            else:
                raise
    raise AssertionError("Unreachable")


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
            current = get_stat(path).st_size
        except OSError:
            return False
        if last is not None and current != last:
            return False
        last = current
        if poll:
            time.sleep(poll)
    return True


def wait_until_stable(
    path: Path, stable_seconds: float, poll: float = 1.0, max_wait: float = 0.0
) -> bool:
    """Blockiert, bis die Dateigröße ``stable_seconds`` lang stabil ist.

    ``max_wait`` (Sekunden) setzt eine harte Obergrenze. Ohne sie blieb der
    Thread bei einer endlos wachsenden Datei für immer hängen - ein Thread
    pro solcher Datei. ``0`` heißt: unbegrenzt warten (Standard für CLI-Läufe).
    """
    start = time.monotonic()
    hard_deadline = start + max_wait if max_wait > 0 else None
    last_size: int | None = None
    stable_since: float | None = None

    while True:
        try:
            size = get_stat(path).st_size
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

        if hard_deadline is not None and now >= hard_deadline:
            log.warning(
                "Datei %s wurde nach %.0fs nicht ruhig - wird als unvollständig übersprungen",
                path.name,
                max_wait,
            )
            return False
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class WatcherState:
    """Merkt sich bereits verarbeitete Dateien (überlebt Neustarts)."""

    path: Path
    processed: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: Path) -> WatcherState:
        state = cls(path=path)
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return state
        except (OSError, ValueError) as exc:
            # Beschädigte State-Datei darf NICHT die ganze Historie kosten -
            # sonst rendert der Watcher beim nächsten Start alles erneut.
            log.warning("State-Datei nicht lesbar (%s): %s", path, exc)
            backup = _restore_backup(path)
            if backup:
                return backup
            return state

        raw = data.get("processed") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            log.warning("State-Datei ohne gültigen 'processed'-Block: %s", path)
            return state

        # Eintrag für Eintrag prüfen: ein einzelner kaputter Wert darf nicht
        # den ganzen Zustand verwerfen.
        dropped = 0
        for key, value in raw.items():
            try:
                stamp = float(value)
            except (TypeError, ValueError):
                dropped += 1
                continue
            if math.isnan(stamp) or math.isinf(stamp):
                dropped += 1
                continue
            state.processed[str(key)] = stamp
        if dropped:
            log.warning("%d unbrauchbare State-Einträge übersprungen", dropped)
        return state

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Vorherige Fassung sichern, damit eine beim Schreiben
                # zerstörte Datei nicht still alles verliert.
                if self.path.is_file():
                    try:
                        shutil.copy2(self.path, self.path.with_suffix(".bak"))
                    except OSError as exc:
                        log.debug("State-Backup nicht möglich: %s", exc)
                # Snapshot unter dem Lock, damit json.dump nicht über ein
                # gleichzeitig mutiertes dict iteriert. Wichtig: KEIN zweites
                # `with self._lock` hier - threading.Lock ist nicht reentrant
                # und das würde den Aufrufer blockieren.
                payload = {"version": 1, "processed": dict(self.processed)}
                # Eindeutiger tmp-Name: parallele Saves dürfen sich nicht
                # gegenseitig in dieselbe Datei schreiben.
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, indent=1, sort_keys=True)
                        fh.flush()
                        os.fsync(fh.fileno())
                    Path(tmp_name).replace(self.path)
                except BaseException:
                    Path(tmp_name).unlink(missing_ok=True)
                    raise
            except OSError as exc:
                log.warning("State-Datei nicht schreibbar: %s", exc)

    def key(self, path: Path) -> str:
        return str(path.resolve())

    def is_processed(self, path: Path) -> bool:
        try:
            mtime = get_stat(path).st_mtime
        except OSError:
            return False
        with self._lock:
            known = self.processed.get(self.key(path))
        return known is not None and abs(known - mtime) < 1e-6

    def mark(self, path: Path) -> None:
        try:
            stamp = get_stat(path).st_mtime
        except OSError:
            return
        with self._lock:
            self.processed[self.key(path)] = stamp
            # State nicht unbegrenzt wachsen lassen
            if len(self.processed) > 5000:
                for stale in sorted(self.processed.items(), key=lambda item: item[1])[:1000]:
                    self.processed.pop(stale[0], None)


def _restore_backup(path: Path) -> "WatcherState | None":
    """Lädt die ``.bak``-Fassung, falls die Hauptdatei unlesbar ist."""
    backup = path.with_suffix(".bak")
    if not backup.is_file():
        return None
    try:
        with backup.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Auch das State-Backup ist unbrauchbar: %s", exc)
        return None
    raw = data.get("processed") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    state = WatcherState(path=path)
    state.processed = {
        str(k): float(v)
        for k, v in raw.items()
        if isinstance(v, (int, float)) and math.isfinite(float(v))
    }
    log.warning("State aus Backup wiederhergestellt (%d Einträge)", len(state.processed))
    return state


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
        # Neufehlgeschlagene Batches: nach N Fehlversuchen beim nächsten
        # Rescan erneut versuchen, statt sie endgültig zu verlieren.
        self.retry_limit = max(int(get(cfg, "watcher.retry_limit", 3) or 0), 0)
        self.retry_backoff = max(float(get(cfg, "watcher.retry_backoff", 300) or 0), 0.0)
        self.output_dir = expand_path(get(cfg, "output_dir", "output"))
        # Originalton-Opt-in: der Watcher setzt bei ``--audio``/``--no-audio``
        # ``video.audio`` bereits in der Config; hier nur durchreichen, damit
        # die Pipeline nicht die Datei auf der Platte erneut lesen muss.
        self.audio = get(cfg, "video.audio", None)
        state_dir = self.output_dir.parent if self.output_dir.name else self.output_dir
        self.state = WatcherState.load(state_dir / STATE_FILENAME)
        self._pending: set[Path] = set()
        self._inflight: set[Path] = set()
        self._failures: dict[str, int] = {}
        self._failed_at: dict[str, float] = {}
        self._last_added: float = 0.0
        self._lock = threading.Lock()
        self._collect_lock = threading.Lock()
        # Deckte den RENDER ab, nicht nur das Einsammeln. Ohne das rendern
        # zwei eingetroffene Bursts parallel, was State-Datei und
        # .part-Dateien gegeneinander arbeiten ließ.
        self._render_lock = threading.Lock()
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
            if path in self._pending or path in self._inflight:
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
        # Harte Obergrenze: debounce * 6 reicht für iPhone-Bursts und
        # verhindert hängende Threads bei endlos wachsenden Dateien.
        if not wait_until_stable(path, self.debounce, max_wait=self.debounce * 6):
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
                self._inflight.update(batch)
                self._last_added = 0.0
        if batch:
            try:
                self.process_batch(batch)
            finally:
                with self._lock:
                    self._inflight.difference_update(batch)

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
        files = [
            p
            for p in batch
            if is_media(p, self.extensions)
            and not self.state.is_processed(p)
            and self._may_retry(p)
        ]
        if not files:
            log.info("Keine neuen Dateien im Batch - nichts zu tun")
            return None

        log.info("Verarbeite %d Datei(en): %s", len(files), ", ".join(p.name for p in files))
        # Render serialisieren: parallele Läufe korumpierten sonst die
        # State-Datei und löschten sich gegenseitig die .part-Dateien.
        with self._render_lock:
            try:
                result = pipeline.run(
                    [str(p) for p in files],
                    config_path=self.config_path,
                    output_dir=self.output_dir,
                    delete_after=self.delete_after,
                    audio=self.audio,
                )
            except Exception as exc:
                log.exception("Pipeline-Lauf fehlgeschlagen: %s", exc)
                self._note_failure(files)
                return None

        if result.ok:
            with self._lock:
                for path in files:
                    self._failures.pop(self.state.key(path), None)
                    self._failed_at.pop(self.state.key(path), None)
            for path in files:
                if not self.delete_after:
                    self.state.mark(path)
            self.state.save()
            for reel in result.outputs or ([result.output] if result.output else []):
                log.info("Reel geschrieben: %s", reel)
        else:
            self._note_failure(files)
        return result

    def _note_failure(self, files: Sequence[Path]) -> None:
        """Zählt Fehlversuche, damit ein Batch irgendwann aufgegeben wird."""
        with self._lock:
            for path in files:
                key = self.state.key(path)
                count = self._failures.get(key, 0) + 1
                self._failures[key] = count
                self._failed_at[key] = time.monotonic()
        if not self.retry_limit:
            log.error("Kein Export - Dateien bleiben zur späteren Verarbeitung liegen")
            return
        # Datei von den weiteren Versuchen ausnehmen, aber weiter aufbewahren.
        with self._lock:
            retryable = []
            for path in files:
                key = self.state.key(path)
                count = self._failures.get(key, 0)
                if count >= self.retry_limit:
                    log.error(
                        "%s nach %d Fehlversuchen aufgegeben: %s", path.name, count, self._failures.get(key)
                    )
                    self._failed_at[key] = 0.0
                else:
                    retryable.append(path)
        log.error(
            "Kein Export - %d Datei(en) bleiben liegen, %d werden erneut versucht",
            len(files),
            len(retryable),
        )

    def _may_retry(self, path: Path) -> bool:
        """True, wenn ein gescheiterter Batch frühestens wieder versucht wird."""
        if not self.retry_limit:
            return True
        key = self.state.key(path)
        with self._lock:
            count = self._failures.get(key, 0)
            last = self._failed_at.get(key)
        if count == 0:
            return True
        if count >= self.retry_limit:
            return False
        if last is None or self.retry_backoff <= 0:
            return True
        return time.monotonic() - last >= self.retry_backoff

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
        """Entfernt ``*.part.<ext>`` aus dem Output - Reste abgebrochener Läufe.

        Wird beim Start aufgerufen: nach einem SIGTERM oder einem Stromausfall
        können unfertige Renderdateien liegen bleiben. Sie sind nie gültig
        und werden nie von der Pipeline weiterverwendet.
        """
        if not self.output_dir.is_dir():
            return 0
        removed = 0
        # Alle Endungen, nicht nur .mp4 - naming.extension ist konfigurierbar.
        for part in self.output_dir.rglob("*.part.*"):
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

    def _is_ignored(self, path: Path) -> bool:
        """True, wenn ``path`` in einem der ``ignore_dirs`` liegt."""
        ignore = {name.lower() for name in self.processor.ignore_dirs}
        if not ignore:
            return False
        try:
            parts = {p.lower() for p in path.relative_to(self.processor.ingest_dir).parts}
        except ValueError:
            parts = {p.lower() for p in path.parts}
        return bool(parts & ignore)

    def _maybe(self, raw_path: str | bytes) -> None:
        # WICHTIG: Der ganze Block muss exceptions-sicher sein. Watchdogs
        # EventDispatcher hat keinen Exception-Guard - eine Exception hier
        # killt den inotify-Thread und der Watcher taubt für den Rest der
        # Laufzeit (Prozess lebt weiter, systemd restartet nichts).
        try:
            path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        except (UnicodeDecodeError, ValueError) as exc:  # pragma: no cover
            log.debug("Pfad nicht dekodierbar: %r (%s)", raw_path, exc)
            return
        try:
            if self._is_ignored(path):
                return
            self.processor.on_file_event(path)
        except Exception as exc:  # Handler darf den Observer NIE killen
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

    handler = IngestHandler(processor)
    observer = Observer()
    observer.schedule(handler, str(ingest_dir), recursive=processor.recursive)

    # Observer ZUERST starten, dann scannen. Sonst liegen Dateien, die
    # während des (minutenlangen) Start-Scans landen, ungehört bis zum
    # nächsten Neustart.
    observer.start()
    log.info(
        "Überwache %s (Debounce %.0fs, Batch-Fenster %.0fs)",
        ingest_dir,
        processor.debounce,
        processor.batch_window,
    )

    try:
        if bool(get(cfg, "watcher.process_existing_on_start", True)):
            try:
                processor.scan_existing()
            except Exception as exc:  # pragma: no cover
                log.exception("Ingest-Scan fehlgeschlagen: %s", exc)

        if once:
            return 0

        while not stopping.is_set():
            stopping.wait(1.0)
    finally:
        observer.stop()
        observer.join(timeout=30)
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
    audio_group = parser.add_mutually_exclusive_group()
    audio_group.add_argument(
        "--audio",
        dest="audio",
        action="store_true",
        default=None,
        help="Originalton der Rohclips ins Reel übernehmen (überschreibt video.audio)",
    )
    audio_group.add_argument(
        "--no-audio",
        dest="audio",
        action="store_false",
        default=None,
        help="stumm exportieren, auch wenn video.audio: true gesetzt ist",
    )
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
    if args.audio is not None:
        cfg["video"] = {**cfg.get("video", {}), "audio": bool(args.audio)}

    setup_logging(cfg, quiet=args.quiet)
    return watch(cfg, config_path=args.config, once=args.once)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
