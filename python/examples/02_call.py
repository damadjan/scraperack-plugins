"""Run a function remotely and get its return value: the simplest pattern."""
import os

from scraperack_python import Python

URL = os.environ.get("SCRAPERACK_URL", "http://127.0.0.1:7070")
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")


def add(a, b):
    return a + b


with Python(URL) as py:
    # Pickles add by value, runs add(2, 3) in a microVM, waits, returns 5.
    print(py.call(ENVIRONMENT, add, 2, 3))

    # Lambdas and closures work too.
    factor = 10
    print(py.call(ENVIRONMENT, lambda x: x * factor, 4))
