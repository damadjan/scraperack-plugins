"""Look back at recent executions: status, duration, node, resource use, errors."""
import os

from scraperack_python import Python

URL = os.environ.get("SCRAPERACK_URL", "http://127.0.0.1:7070")
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")

with Python(URL) as py:
    executions = py.executions(environment=ENVIRONMENT, limit=20)  # newest first
    for e in executions:
        print(f"{e['id']}  {e['function'] or '?':<16} {e['status']:<10} "
              f"{e['duration_ms'] or 0:>6} ms  {e['node'] or ''}")

    failed = [e for e in executions if e["status"] in ("failed", "timed_out")]
    if failed:
        details = py.execution(failed[0]["id"])
        print("\nlast failure:", details["error"])
        print(details["logs"]["stderr"][-500:])
