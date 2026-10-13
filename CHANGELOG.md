# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial open-source release preparation
- LICENSE (MIT)
- pyproject.toml with modern packaging
- GitHub Actions CI workflow (lint, type-check, test, build, publish)
- CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md
- Pre-commit configuration
- GitHub issue and PR templates

### Changed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Fixed
- N/A

### Security
- N/A

---

## [1.0.0] - 2026-09-28

### Added
- **Core Pipeline**: Ingest → metadata extraction → beat-matching → quality gate → Dark-Film grading → 9:16 export
- **Beat Matching**: Three-tier fallback (Spotify Audio Analysis → local audio file via librosa → BPM fallback)
- **Quality Gate**: Sharpness, luma, black/white ratios, duplicate detection with window search
- **Dark-Film Grading**: 256-entry LUT (gamma, S-curve, matte black toe, highlight roll-off) + color stage
- **Typography**: Right-aligned, no-box text overlay with drop shadow, safe-zone positioning
- **Watcher**: watchdog-based ingest with debounce, batch window, retry logic, state persistence
- **Systemd Integration**: Service + path units for user-mode automation
- **Configuration**: YAML + environment variable overrides with deep merge
- **Geocoding**: Reverse geocoding via Nominatim with persistent cache
- **Cover Thumbnail**: Auto-generated at 0.5s mark
- **Multi-day Trips**: Automatic per-day Reel separation
- **Music Handling**: Conscious choice for audio mix vs. beat-only sources
- **Original Audio**: Optional clip audio export with gain control under music
- **Comprehensive Tests**: 4 test modules covering stability, quality, styling, metadata
- **CLI**: Full-featured pipeline, watcher, and metadata inspection commands

### Changed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Fixed
- Watcher "deaf" bug: IngestHandler missing `ingest_dir` attribute
- State file corruption resilience: atomic writes + .bak fallback
- Audio channel mismatch: mono/stereo mixing handled gracefully
- Spotify 403 handling: explicit fallback with clear logging
- Quality gate: never loses unevaluable material

### Security
- No new privileges, private tmp, protected system/home in systemd units
- Secrets via environment files, never in config.yaml

---

## Template for Future Releases

## [X.Y.Z] - YYYY-MM-DD

### Added
- 

### Changed
- 

### Deprecated
- 

### Removed
- 

### Fixed
- 

### Security
-