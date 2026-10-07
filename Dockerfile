# Dockerfile for tenor
# Build: docker build -t tenor .
# Run: docker run --rm -v /host/ingest:/data/ingest -v /host/output:/data/output tenor

FROM python:3.12-slim-bookworm

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-roboto \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash tenor
USER tenor
WORKDIR /home/tenor

# Install Python dependencies
COPY --chown=tenor:tenor pyproject.toml README.md ./
COPY --chown=tenor:tenor src/ ./src/
RUN pip install --no-cache-dir -e .

# Default directories (override with -v mounts)
ENV TENOR_INGEST_DIR=/data/ingest \
    TENOR_OUTPUT_DIR=/data/output \
    TENOR_MUSIC_DIR=/data/music \
    TENOR_CONFIG=/home/tenor/config.yaml

# Config file (user mounts their own)
# COPY --chown=tenor:tenor config.yaml ./config.yaml

# Entrypoint
ENTRYPOINT ["python", "-m", "src.pipeline"]
CMD ["--help"]