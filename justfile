# Justfile - common development commands for tenor
# Install: https://github.com/casey/just

# Default recipe
default: help

# Help
help:
	@just --list

# Setup
setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	.venv/bin/pre-commit install

# Linting & Formatting
lint:
	.venv/bin/ruff check src tests

fmt:
	.venv/bin/ruff format src tests

fmt-check:
	.venv/bin/ruff format --check src tests

# Type checking
typecheck:
	.venv/bin/mypy src

# Testing
test:
	.venv/bin/pytest tests -v

test-cov:
	.venv/bin/pytest tests --cov=src --cov-report=term-missing --cov-report=html

test-quick:
	.venv/bin/pytest tests -q

test-watch:
	.venv/bin/pytest-watch tests

# Quality gate (all checks)
check: lint fmt-check typecheck test
	@echo "✅ All checks passed!"

# Pipeline commands
run-pipeline:
	.venv/bin/python -m src.pipeline -i ~/video-cut/ingest --dry-run

run-pipeline-real:
	.venv/bin/python -m src.pipeline -i ~/video-cut/ingest

run-watcher:
	.venv/bin/python -m src.watcher --config config.yaml

run-watcher-once:
	.venv/bin/python -m src.watcher --config config.yaml --once

inspect-metadata:
	.venv/bin/python -m src.metadata -i ~/video-cut/ingest

# Build & Release
build:
	.venv/bin/python -m build

build-check:
	.venv/bin/twine check dist/*

# Clean
clean:
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Docker (if Dockerfile exists)
docker-build:
	docker build -t tenor:latest .

docker-run:
	docker run --rm -it \
	  -v ~/video-cut:~/video-cut \
	  -v ~/.config/tenor:/root/.config/tenor \
	  tenor:latest \
	  python -m src.pipeline -i ~/video-cut/ingest --dry-run

# Systemd (local user install)
systemd-install:
	mkdir -p ~/.config/systemd/user
	cp systemd/tenor-watcher.service ~/.config/systemd/user/
	cp systemd/tenor-watcher.path ~/.config/systemd/user/
	systemctl --user daemon-reload

systemd-enable:
	systemctl --user enable --now tenor-watcher.service

systemd-disable:
	systemctl --user disable --now tenor-watcher.service

systemd-logs:
	journalctl --user -u tenor-watcher.service -f

systemd-status:
	systemctl --user status tenor-watcher.service