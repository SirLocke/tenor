import re

with open("src/pipeline.py", "r") as f:
    content = f.read()

# We need to insert this right before `if not dry_run: partial.unlink(missing_ok=True)`

search_str = """
    target = unique_path(out_dir, stem, suffix)
    # Staging-Datei trägt dieselbe Endung wie das Ziel, sonst räumt
    # cleanup_partials() sie nicht mehr auf.
    partial = target.parent / f"{target.stem}.part{suffix}"
    if not dry_run:
        partial.unlink(missing_ok=True)"""

replace_str = """
    target = unique_path(out_dir, stem, suffix)
    # Staging-Datei trägt dieselbe Endung wie das Ziel, sonst räumt
    # cleanup_partials() sie nicht mehr auf.
    partial = target.parent / f"{target.stem}.part{suffix}"

    want_audio = bool(video_cfg.get("audio", False))
    if not dry_run:
        total_duration = sum(s.duration for s in segments)
        if not check_disk_space(video_cfg, total_duration, out_dir, want_audio, music):
            errors_seen.extend(["Zu wenig Speicherplatz für den Export"])
            return None

    if not dry_run:
        partial.unlink(missing_ok=True)"""

if search_str in content:
    content = content.replace(search_str, replace_str)
else:
    print("search_str not found!")

with open("src/pipeline.py", "w") as f:
    f.write(content)

print("done")
