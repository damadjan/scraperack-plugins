import time
from concurrent.futures import ThreadPoolExecutor

from scraperack_python import Python


py = Python("http://192.168.2.15:7070")


def identity(value):
    return value


start = time.perf_counter()
with ThreadPoolExecutor(max_workers=5) as pool:
    executions = list(pool.map(
        lambda value: py.wait(
            execution_id=py.run("bench2", identity, (value,))["id"]
        ),
        range(5),
    ))
durations = [(execution["finished_at_ms"] - execution["started_at_ms"]) / 1000
             for execution in executions]
print(f"{time.perf_counter() - start:.3f}s total; "
      f"executions: {', '.join(f'{duration:.3f}s' for duration in durations)}")
py.close()
