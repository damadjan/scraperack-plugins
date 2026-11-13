import sys
import time
from concurrent.futures import ThreadPoolExecutor

from scraperack_python import Python


def request():
    import requests

    start = time.perf_counter()
    status = requests.get(url="http://192.168.2.9:8080/").status_code
    return status, time.perf_counter() - start


def peak(executions):
    active = maximum = 0
    events = sorted(
        [(execution["started_at_ms"], 1) for execution in executions]
        + [(execution["finished_at_ms"], -1) for execution in executions],
        key=lambda event: (event[0], -event[1]),
    )
    for _, change in events:
        active += change
        maximum = max(maximum, active)
    return maximum


levels = [int(value) for value in sys.argv[1:]] or [1, 2, 4, 8, 16, 32]
py = Python("http://192.168.2.15:7070")
print("sent  peak  ok  total    req/s  p50     p95")
for concurrency in levels:
    def execute(_):
        execution = py.run(environment="bench2", function=request)
        value = py.result(execution["id"])
        return py.execution(execution["id"]), value

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        completed = list(pool.map(execute, range(concurrency)))
    total = time.perf_counter() - start
    executions = [execution for execution, _ in completed]
    elapsed = sorted(value[1] for _, value in completed)
    p50 = elapsed[len(elapsed) // 2] if elapsed else 0
    p95 = elapsed[int((len(elapsed) - 1) * .95)] if elapsed else 0
    print(f"{concurrency:>4}  {peak(executions):>4}  {len(elapsed):>2}  "
          f"{total:>6.3f}s  {len(elapsed) / total:>5.1f}  {p50:>6.3f}s  {p95:>6.3f}s")
py.close()
