import re

with open("src/pipeline.py", "r") as f:
    content = f.read()

check_disk_space_func = """
def check_disk_space(
    video_cfg: Mapping[str, Any],
    duration: float,
    out_dir: Path,
    want_audio: bool,
    music: str | Path | None,
) -> bool:
    import shutil

    if not out_dir.exists():
        return True

    try:
        usage = shutil.disk_usage(out_dir)
    except OSError as exc:
        log.warning("Konnte freien Speicherplatz für %s nicht ermitteln: %s", out_dir, exc)
        return True

    is_lossless = bool(video_cfg.get("lossless"))
    width = int(video_cfg.get("width", 1080))
    height = int(video_cfg.get("height", 1920))
    fps = int(video_cfg.get("fps", 30))

    if is_lossless:
        # Grobe Schätzung für unkomprimiert / CRF 0: ~1.5 Bytes pro Pixel (YUV420) geteilt durch 3 als x264-Lossless Floor
        video_bps = width * height * fps * 1.5 * 8 / 3
    else:
        crf = float(video_cfg.get("crf", 19) or 19)
        bpp = 0.20 * 2.0 ** ((19.0 - crf) / 6.0)
        video_bps = width * height * fps * bpp

    audio_bps = 0.0
    if want_audio or music is not None:
        audio_bitrate_str = str(video_cfg.get("audio_bitrate", "192k"))
        if audio_bitrate_str.endswith("k"):
            try:
                audio_bps = float(audio_bitrate_str.replace("k", "")) * 1000
            except ValueError:
                audio_bps = 192000.0
        else:
            try:
                audio_bps = float(audio_bitrate_str)
            except ValueError:
                audio_bps = 192000.0

    bytes_per_second = (video_bps + audio_bps) / 8
    estimated_bytes = bytes_per_second * duration

    # Sicherheitsmarge: 20% oder mindestens 200 MB
    margin = max(estimated_bytes * 0.2, 200 * 1024 * 1024)
    required_bytes = estimated_bytes + margin

    if usage.free < required_bytes:
        log.error(
            "Nicht genug freier Speicher auf %s: geschätzt %.1f MB (inkl. Reserve), verfügbar %.1f MB",
            out_dir,
            required_bytes / 1024 / 1024,
            usage.free / 1024 / 1024,
        )
        return False

    return True
"""

content = content.replace("def _compute_fingerprint(", check_disk_space_func + "\n\ndef _compute_fingerprint(")

with open("src/pipeline.py", "w") as f:
    f.write(content)

print("done")
