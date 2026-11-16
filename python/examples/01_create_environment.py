"""Create the environment the other examples run in.

An environment is an image plus pip packages and VM limits. Creating it blocks while
the packages are installed; after that, calls start from a warm snapshot.
"""
import os

from scraperack_python import Python

URL = os.environ.get("SCRAPERACK_URL", "http://127.0.0.1:7070")
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")
IMAGE_NAME = os.environ.get("IMAGE_NAME", "python-runtime")  # an imported image with python3

with Python(URL) as py:
    if any(e["name"] == ENVIRONMENT for e in py.environments()):
        print(f"{ENVIRONMENT} already exists")
    else:
        image = next(i for i in py.cluster.images() if i["name"] == IMAGE_NAME)
        env = py.create_environment(
            ENVIRONMENT, image["id"],
            packages=["httpx==0.28.1"],  # pinned pip requirements
            preload=["httpx"],           # imported before the snapshot, so calls start warm
            cpu=0.5, memory_mib=512,
            warm=1,                      # microVMs kept ready
            max_instances=10,            # extra executions queue
            timeout=120,                 # seconds per execution
        )
        print(f"created {env['name']} (pool {env['pool']}), ready={env['ready']}")
