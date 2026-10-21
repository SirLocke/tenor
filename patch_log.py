import re

with open("src/pipeline.py", "r") as f:
    content = f.read()

search = """
    if usage.free < required_bytes:
        log.error(
            "Nicht genug freier Speicher auf %s: geschätzt %.1f MB (inkl. Reserve), verfügbar %.1f MB",
            out_dir,
            required_bytes / 1024 / 1024,
            usage.free / 1024 / 1024,
        )
        return False

    return True"""

replace = """
    log.info(
        "Speicherplatz-Check (%s): geschätzt %.1f MB (inkl. Reserve), verfügbar %.1f MB",
        out_dir,
        required_bytes / 1024 / 1024,
        usage.free / 1024 / 1024,
    )

    if usage.free < required_bytes:
        log.error(
            "Nicht genug freier Speicher auf %s: geschätzt %.1f MB (inkl. Reserve), verfügbar %.1f MB",
            out_dir,
            required_bytes / 1024 / 1024,
            usage.free / 1024 / 1024,
        )
        return False

    return True"""

content = content.replace(search, replace)

search2 = """    want_audio = bool(video_cfg.get("audio", False))
    if not dry_run:
        total_duration = sum(s.duration for s in segments)
        if not check_disk_space(video_cfg, total_duration, out_dir, want_audio, music):
            errors_seen.extend(["Zu wenig Speicherplatz für den Export"])
            return None

    if not dry_run:
        partial.unlink(missing_ok=True)"""

replace2 = """    want_audio = bool(video_cfg.get("audio", False))
    total_duration = sum(s.duration for s in segments)

    has_space = check_disk_space(video_cfg, total_duration, out_dir, want_audio, music)
    if not dry_run and not has_space:
        errors_seen.extend(["Zu wenig Speicherplatz für den Export"])
        return None

    if not dry_run:
        partial.unlink(missing_ok=True)"""

content = content.replace(search2, replace2)

with open("src/pipeline.py", "w") as f:
    f.write(content)

print("done")
