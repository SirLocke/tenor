import time
import timeit

setup_code = """
import os
from pathlib import Path
import tempfile

temp_dir = tempfile.mkdtemp()
temp_path = Path(temp_dir)
for i in range(10):
    (temp_path / f"test_{i}.beat-cache").touch()
for i in range(50000):
    (temp_path / f"test_{i}.txt").touch()

partial_parent = temp_path
"""

test_code_glob = """
try:
    raise ValueError("test")
except Exception:
    for bc in partial_parent.glob("*.beat-cache"):
        pass
    try:
        raise ValueError("test2")
    except Exception:
        for bc in partial_parent.glob("*.beat-cache"):
            pass
"""

test_code_list = """
beat_caches = list(partial_parent.glob("*.beat-cache"))
try:
    raise ValueError("test")
except Exception:
    for bc in beat_caches:
        pass
    try:
        raise ValueError("test2")
    except Exception:
        for bc in beat_caches:
            pass
"""

print("Benchmarking glob in exception block vs list beforehand")
glob_time = timeit.timeit(test_code_glob, setup=setup_code, number=10)
list_time = timeit.timeit(test_code_list, setup=setup_code, number=10)

print(f"Glob directly: {glob_time:.5f}s")
print(f"List beforehand: {list_time:.5f}s")
