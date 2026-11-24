"""Change, restart and delete environments."""
import os

from scraperack_python import Python

URL = os.environ["SCRAPERACK_URL"]
API_KEY = os.environ["SCRAPERACK_API_KEY"]
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")
DELETE = os.environ.get("DELETE") == "1"  # DELETE=1 removes the examples environment

with Python(URL, API_KEY) as py:
    for e in py.environments():
        print(f"{e['name']}: {len(e['packages'])} packages, warm {e['warm_instances']}, "
              f"max {e['max_instances']}, ready={e['ready']}")

    # Scaling and limits apply right away; packages or image changes rebuild the disk.
    env = py.update_environment(ENVIRONMENT, warm=2, timeout=300)
    print("warm now", env["warm_instances"])

    py.restart_environment(ENVIRONMENT)  # replace its microVMs, e.g. after a bad state

    if DELETE:
        py.delete_environment(ENVIRONMENT)
        print("deleted", ENVIRONMENT)
