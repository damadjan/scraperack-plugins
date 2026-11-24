import os
import time

from scraperack_python import Python


def resize():
    from PIL import Image

    start = time.perf_counter()
    result = Image.effect_noise((4096, 4096), 100).resize(
        (1024, 1024), Image.Resampling.LANCZOS,
    ).getextrema()
    return time.perf_counter() - start, result


py = Python(os.environ["SCRAPERACK_URL"], os.environ["SCRAPERACK_API_KEY"])
start = time.perf_counter()
execution = py.run(environment="pillow", function=resize)
elapsed, result = py.result(execution["id"])
execution = py.execution(execution["id"])
py.close()
duration = (execution["finished_at_ms"] - execution["started_at_ms"]) / 1000
print(f"{elapsed:.3f}s Pillow; {duration:.3f}s execution; "
      f"{time.perf_counter() - start:.3f}s total; extrema: {result}")
