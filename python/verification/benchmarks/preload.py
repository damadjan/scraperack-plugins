import time
from concurrent.futures import ThreadPoolExecutor

from scraperack_python import Python


def get():
    import requests

    return requests.__version__


py = Python("http://192.168.2.15:7070")

start = time.perf_counter()
with ThreadPoolExecutor(max_workers=5) as pool:
    executions = list(pool.map(
        lambda _: py.wait(execution_id=py.run(environment="bench2", function=get)["id"]),
        range(5),
    ))
errors = [execution["error"] for execution in executions if execution["error"]]
if errors:
    raise RuntimeError(errors[0])
durations = [(execution["finished_at_ms"] - execution["started_at_ms"]) / 1000
             for execution in executions]
print(f"{time.perf_counter() - start:.3f}s total; "
      f"executions: {', '.join(f'{duration:.3f}s' for duration in durations)}")

py.close()
