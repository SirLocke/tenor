import re

with open("tests/test_stability.py", "r") as f:
    content = f.read()

# To fix the FileNotFoundError we should mock `os.replace` or `pathlib.Path.replace` since it is not really rendering.
# Alternatively, mock `_attempt_export` to just create a dummy partial file. But the easiest is to patch pathlib.Path.replace

search = """            target = export_reel(
                segments=[seg],
                cfg=cfg,
                base_out=base_out,
                title="EnoughSpaceTest",
                subtitle="Subtitle",
                day="2026-09-11",
                grade=grade,
                when=1600000000.0,
                dry_run=False,
                errors_seen=errors,
            )

            # Not returning None due to disk space, mock_render should be called
            mock_render.assert_called_once()"""

replace = """            with patch("pathlib.Path.replace"):
                target = export_reel(
                    segments=[seg],
                    cfg=cfg,
                    base_out=base_out,
                    title="EnoughSpaceTest",
                    subtitle="Subtitle",
                    day="2026-09-11",
                    grade=grade,
                    when=1600000000.0,
                    dry_run=False,
                    errors_seen=errors,
                )

            # Not returning None due to disk space, mock_render should be called
            mock_render.assert_called_once()"""

content = content.replace(search, replace)

with open("tests/test_stability.py", "w") as f:
    f.write(content)

print("done")
