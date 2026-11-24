"""Use third-party packages installed in the environment (httpx, from 01).

Import them inside the function: they need to exist in the environment, not locally.
"""
import os

from scraperack_python import Python

URL = os.environ["SCRAPERACK_URL"]
API_KEY = os.environ["SCRAPERACK_API_KEY"]
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")


def status_codes(urls):
    import httpx

    with httpx.Client(timeout=10, follow_redirects=True) as client:
        return {url: client.get(url).status_code for url in urls}


with Python(URL, API_KEY) as py:
    print(py.call(ENVIRONMENT, status_codes, ["https://example.com", "https://example.org"]))

    # Need another package? Add it to the environment; the disk is rebuilt once.
    # py.update_environment(ENVIRONMENT, packages=["httpx==0.28.1", "selectolax==0.3.27"])
