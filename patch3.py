import re

with open("src/pipeline.py", "r") as f:
    content = f.read()

# We need to remove the duplicate `want_audio` definition that was added.
search_str2 = """    want_audio = bool(video_cfg.get("audio", False))
    rendered = render_segments("""

replace_str2 = """    rendered = render_segments("""

if search_str2 in content:
    content = content.replace(search_str2, replace_str2)
else:
    print("search_str2 not found!")

with open("src/pipeline.py", "w") as f:
    f.write(content)

print("done")
