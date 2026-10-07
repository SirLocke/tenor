# Contributing to tenor

Thank you for your interest in contributing! This document outlines the process and standards for contributing to tenor.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Testing](#testing)
- [Code Style](#code-style)
- [Commit Messages](#commit-messages)
- [Pull Request Process](#pull-request-process)
- [Reporting Issues](#reporting-issues)

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold this code.

---

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR-USERNAME/tenor.git
   cd tenor
   ```
3. **Add upstream remote**:
   ```bash
   git remote add upstream https://github.com/SirLocke/tenor.git
   ```

---

## Development Setup

### Prerequisites

- Python 3.11+
- ffmpeg (system package)
- Git

### Install

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install

# Verify setup
pytest tests -q
ruff check src tests
mypy src
```

---

## Making Changes

### Branch Naming

Use descriptive branch names:
- `feat/short-description` — new feature
- `fix/short-description` — bug fix
- `docs/short-description` — documentation only
- `refactor/short-description` — code restructuring
- `test/short-description` — adding tests
- `chore/short-description` — maintenance

### Workflow

1. **Sync with upstream**:
   ```bash
   git fetch upstream
   git checkout main
   git merge upstream/main
   ```

2. **Create feature branch**:
   ```bash
   git checkout -b feat/your-feature-name
   ```

3. **Make changes** following the guidelines below

4. **Run quality checks locally**:
   ```bash
   ruff check src tests
   ruff format src tests
   mypy src
   pytest tests
   ```

5. **Commit** with conventional commits (see below)

6. **Push and open PR**:
   ```bash
   git push origin feat/your-feature-name
   ```

---

## Testing

- **Run all tests**: `pytest tests -v`
- **Run with coverage**: `pytest tests --cov=src --cov-report=term-missing`
- **Run specific test file**: `pytest tests/test_quality.py -v`
- **Add tests** for new functionality in `tests/`
- **Regression tests** are especially valuable — if you fix a bug, add a test that would have caught it

### Test Guidelines

- Tests should be fast and isolated
- Mock external dependencies (Spotify API, Nominatim, filesystem)
- Use `tmp_path` fixture for filesystem tests
- Name tests descriptively: `test_what_it_does_when_condition`

---

## Code Style

We use **Ruff** for linting and formatting, **MyPy** for type checking.

### Configuration

All settings are in `pyproject.toml`:
- Line length: 110
- Target: Python 3.11+
- Double quotes
- Import sorting (isort via Ruff)

### Type Hints

- All public functions must have type hints
- Use `from __future__ import annotations` for forward references
- Prefer `Mapping`/`Sequence` over `dict`/`list` in signatures
- Add `# type: ignore[code]` only with explanation comment

### Imports

```python
# stdlib
import os
from pathlib import Path
from typing import Any

# third-party
import numpy as np
import cv2

# local
from . import load_config
from .metadata import ClipInfo
```

---

## Commit Messages

We follow **Conventional Commits** (caveman-compress style):

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

| Type | Purpose |
|------|---------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `style` | Formatting, no code change |
| `refactor` | Code restructuring |
| `test` | Adding/modifying tests |
| `chore` | Maintenance, deps, config |
| `perf` | Performance improvement |

### Examples

```
feat(pipeline): add max_duration option for Reels

Clips longer than max_duration are now trimmed during export.
Config key: video.max_duration (default 0 = unlimited).

Closes #42
```

```
fix(watcher): handle missing ingest_dir attribute on handler

The IngestHandler didn't store ingest_dir, causing AttributeError
on first file event and making the watcher deaf.
```

```
docs(readme): clarify Spotify 403 behavior for new apps
```

---

## Pull Request Process

1. **PR Title** should follow conventional commits format
2. **Description** must include:
   - What changed and why
   - Link to related issue (if any)
   - Screenshots/logs for visual/behavioral changes
   - Test plan (how you verified it works)
3. **CI must pass** — all checks green
4. **Review** — at least one approval required
5. **Squash merge** — maintainers will squash on merge

### PR Checklist

- [ ] Tests added/updated
- [ ] Documentation updated (README, config comments, docstrings)
- [ ] CHANGELOG.md updated (under `## [Unreleased]`)
- [ ] No new lint/type errors
- [ ] Commit history is clean (squash fixup commits)

---

## Reporting Issues

### Bug Reports

Use the bug report template (`.github/ISSUE_TEMPLATE/bug_report.yml`). Include:

- **Environment**: OS, Python version, tenor version
- **Steps to reproduce**: Minimal, complete steps
- **Expected vs actual behavior**
- **Logs**: Relevant output (redact paths/secrets)
- **Config**: Relevant `config.yaml` sections (redact secrets)

### Feature Requests

Use the feature request template. Describe:

- **Problem**: What use case isn't solved?
- **Proposed solution**: How should it work?
- **Alternatives considered**
- **Config/API impact**: Breaking changes?

### Security Issues

**Do not open public issues** for security vulnerabilities. See [SECURITY.md](SECURITY.md) for responsible disclosure.

---

## Architecture Notes

For larger changes, understand the module structure:

| Module | Responsibility |
|--------|----------------|
| `src/__init__.py` | Config loading, defaults, env overrides, utilities |
| `src/metadata.py` | ffprobe, EXIF, geocoding, clip sorting |
| `src/pipeline.py` | Beat matching, cut planning, rendering, export, CLI |
| `src/quality.py` | Quality gate: sharpness, luma, duplicate detection |
| `src/styling.py` | Center-crop, LUT grading, text overlay, fonts |
| `src/watcher.py` | watchdog ingest, debounce, batch, systemd |
| `src/spotify_beats.py` | Spotify Audio Analysis (optional) |

---

## Questions?

Open a discussion on GitHub or check existing issues/PRs. We're happy to help!

---

*This document is a living guide. Suggestions for improvement welcome via PR.*