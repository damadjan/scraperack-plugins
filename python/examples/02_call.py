"""Run a function remotely and get its return value: the simplest pattern."""
import os

from scraperack_python import Python

URL = os.environ["SCRAPERACK_URL"]
API_KEY = os.environ["SCRAPERACK_API_KEY"]
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")


def add(a, b):
    return a + b


with Python(URL, API_KEY) as py:
    # Pickles add by value, runs add(2, 3) in a microVM, waits, returns 5.
    print(py.call(ENVIRONMENT, add, 2, 3))

    # Lambdas and closures work too.
    factor = 10
    print(py.call(ENVIRONMENT, lambda x: x * factor, 4))
