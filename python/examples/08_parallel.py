"""Fan out many calls at once and gather the results.

Calls beyond the environment's max_instances queue until a microVM frees up.
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from scraperack_python import AsyncPython, Python

URL = os.environ["SCRAPERACK_URL"]
API_KEY = os.environ["SCRAPERACK_API_KEY"]
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")


def square(n):
    return n * n


# Async: one client, many concurrent calls.
async def main() -> None:
    async with AsyncPython(URL, API_KEY) as py:
        print(await asyncio.gather(*(py.call(ENVIRONMENT, square, n) for n in range(10))))

asyncio.run(main())

# Sync: start everything first, then collect.
with Python(URL, API_KEY) as py:
    executions = [py.run(ENVIRONMENT, square, (n,)) for n in range(10)]
    with ThreadPoolExecutor(10) as pool:
        print(list(pool.map(lambda e: py.result(e["id"]), executions)))
