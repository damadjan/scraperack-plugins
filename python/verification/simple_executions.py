import os

from scraperack_python import Python


py = Python(os.getenv("SCRAPERACK_URL", "http://192.168.2.15:7070"))
environment = os.getenv("SCRAPERACK_ENVIRONMENT", "python")


def hello():
    return "hello from a microVM"


def math(a, b):
    return {"sum": a + b, "product": a * b}


def squares(values):
    return [value**2 for value in values]


jobs = [
    ("hello", hello, ()),
    ("math", math, (6, 7)),
    ("python", squares, ([1, 2, 3, 4, 5],)),
]

try:
    for name, function, params in jobs:
        execution = py.run(environment, function, params)
        print(name, py.result(execution["id"]))
finally:
    py.close()
