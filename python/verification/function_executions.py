import os

from scraperack_python import Python


def hello(name):
    return f"hello {name} from a microVM"


def add(a, b):
    return a + b


def squares(values):
    return [value**2 for value in values]


def python_version():
    import sys
    return sys.version


py = Python(os.environ["SCRAPERACK_URL"], os.environ["SCRAPERACK_API_KEY"])
environment = os.getenv("SCRAPERACK_ENVIRONMENT", "python")
jobs = [
    (python_version, ()),
    (hello, ("Todor",)),
    (add, (6, 7)),
    (squares, ([1, 2, 3, 4, 5],)),
]

try:
    for function, params in jobs:
        execution = py.run(environment, function, params)
        print(function.__name__, py.result(execution["id"]))
finally:
    py.close()
