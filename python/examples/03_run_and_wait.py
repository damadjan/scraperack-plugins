"""Start an execution, do other work, then collect its result, output and timings."""
import os
import platform

from scraperack_python import Python

URL = os.environ.get("SCRAPERACK_URL", "http://127.0.0.1:7070")
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")


def where_am_i():
    import platform
    print("hello from the microVM")          # captured as stdout
    return {"node": platform.node(), "python": platform.python_version()}


with Python(URL) as py:
    execution = py.run(ENVIRONMENT, where_am_i)  # returns at once
    print("started", execution["id"], execution["status"])

    print("local python:", platform.python_version())  # meanwhile, locally

    print(py.result(execution["id"]))                 # waits, then decodes the value
    print(py.logs(execution["id"])["stdout"], end="")

    details = py.execution(execution["id"])            # status, timings, metrics, logs
    print(details["status"], details["duration_ms"], "ms on", details["node"])
