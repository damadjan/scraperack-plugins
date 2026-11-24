"""Ship your own modules and files with a call.

working_dir is uploaded once (content-addressed, so unchanged folders are not resent),
is importable during the call, and is its current directory.
"""
import os
from pathlib import Path

from scraperack_python import Python

URL = os.environ["SCRAPERACK_URL"]
API_KEY = os.environ["SCRAPERACK_API_KEY"]
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")
PROJECT = Path(__file__).parent / "project"


def score_words():
    import scoring                        # project/scoring.py

    with open("words.txt") as f:          # project/words.txt, relative to the cwd
        return scoring.rank(f.read().split())


with Python(URL, API_KEY) as py:
    print(py.call(ENVIRONMENT, score_words, working_dir=PROJECT))
