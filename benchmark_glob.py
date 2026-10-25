import time
from pathlib import Path
import tempfile

def benchmark():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        for i in range(10000):
            (p / f"file_{i}.txt").touch()
        for i in range(100):
            (p / f"cache_{i}.beat-cache").touch()

        # Baseline: multiple globs
        start = time.perf_counter()
        for _ in range(3):
            for bc in p.glob("*.beat-cache"):
                pass
        t_baseline = time.perf_counter() - start

        # Optimized: list once
        start = time.perf_counter()
        beat_caches = list(p.glob("*.beat-cache"))
        for _ in range(3):
            for bc in beat_caches:
                pass
        t_optimized = time.perf_counter() - start

        print(f"Baseline: {t_baseline:.6f}s")
        print(f"Optimized: {t_optimized:.6f}s")
        print(f"Improvement: {(t_baseline - t_optimized) / t_baseline * 100:.2f}%")

if __name__ == "__main__":
    benchmark()
