"""Failures: the function raised, the call was rejected, or the environment is busy."""
import os

from scraperack import APIError
from scraperack_python import Python, RemoteError

URL = os.environ.get("SCRAPERACK_URL", "http://127.0.0.1:7070")
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")


def divide(a, b):
    return a / b


with Python(URL) as py:
    try:
        py.call(ENVIRONMENT, divide, 1, 0)
    except RemoteError as error:
        # The function raised, timed out, or could not run; the message is the remote error.
        print("remote failure:", str(error).strip().splitlines()[-1])
        print("execution:", error.execution["id"], error.execution["status"])

    try:
        py.call("no-such-environment", divide, 1, 1)
    except APIError as error:
        print("rejected:", error.status, error.message)

    try:
        # queue=False fails at once instead of waiting when max_instances are busy.
        print(py.call(ENVIRONMENT, divide, 6, 3, queue=False))
    except APIError as error:
        print("busy:", error.status, error.message)
