# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

---

## [1.0.0] - 2026-11-22

### Added
- **Core Pipeline**: Ingest -> metadata extraction -> beat-matching -> quality gate -> grading -> 9:16 export
- **Framing Modes**: `crop` (center-crop), `blur` (blurred/darkened background), and `pad` (letterbox/pillarbox) for non-9:16 clips (`--fit` / `video.fit_mode`)
- **Grading Presets**: `natural` (default), `cinematic` (Dark-Film LUT + color stage), and `original` (`--grade` / `style.preset`)
- **Pacing & Duration Controls**: `--pace` (`fast`/`medium`/`slow`), `--bpm`, `--duration`, and `--single-reel` / `--merge-days`
- **Beat Matching**: Three-tier fallback (Spotify Audio Analysis -> local audio file via librosa -> BPM fallback)
- **Quality Gate**: Sharpness, luma, black/white ratios, duplicate detection with window search, and undecodable/empty frame skipping
- **Typography**: Right-aligned, no-box text overlay with drop shadow, safe-zone positioning
- **Watcher**: watchdog-based ingest with debounce, batch window, `max_queue_size` backpressure, retry logic, PermissionError tolerance, and atomic `.bak` state persistence
- **Systemd & Docker**: User-mode systemd units (`tenor-watcher.service`, `tenor-watcher.path`), `Dockerfile`, and `justfile`
- **Configuration**: `config.yaml`, `config.local.yaml` local override overlay (`config.local.yaml.example`), and `TENOR_*` environment variables
- **Geocoding**: Reverse geocoding via Nominatim with persistent cache
- **Cover Thumbnail & Audio**: Auto-generated cover at 0.5s mark, optional original clip audio (`--audio`) mixed cleanly under music (`-m`)
- **CLI**: `tenor`, `tenor-watcher`, and `tenor-metadata` entrypoints with `--json` feedback, validation, and `--version`
- **Open-Source Infrastructure**: MIT `LICENSE`, `pyproject.toml`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, pre-commit hooks, GitHub issue/PR templates, and GitHub Actions CI (`ubuntu-latest`)
- **Tests**: 23 test modules covering pipeline, watcher, quality gate, metadata, styling, pacing, Spotify beats, config, and Nextcloud integration

### Changed
- Deduplicated 10-bit transcode, `ffprobe`, and process-cleanup calls for faster batch exports
- Decomposed pipeline run/export internals and hardened cleanup & state handling

### Fixed
- Watcher "deaf" bug and full-queue crash protection
- State file corruption resilience via atomic writes and `.bak` fallback
- Audio channel mismatch when mixing mono/stereo sources
- Spotify HTTP 403 handling with explicit fallback logging and JSON `beat_note`
- Quality gate safeguards against zero/missing FPS, NaN/infinite clip durations, and undecodable frames
- Package discovery in `pyproject.toml` so wheels include all `src` modules

### Security
- Prevented `ffprobe`/`ffmpeg` option injection via `--` separator
- Prevented Nextcloud scan command injection via `shlex.split` without shell execution
- Systemd hardening (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, `ProtectHome`)
- Secrets isolated in environment files (`spotify.env.example`), never stored in `config.yaml`
