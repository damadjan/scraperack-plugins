"""Pass configuration and credentials to a call without baking them into the function.

Secrets are encrypted at rest and redacted from the execution's output.
"""
import os

from scraperack_python import Python

URL = os.environ.get("SCRAPERACK_URL", "http://127.0.0.1:7070")
ENVIRONMENT = os.environ.get("SCRAPERACK_ENV", "examples")


def connect():
    from scraperack_runtime import secret, variable  # available inside every execution

    region = variable("REGION", "eu")                 # second argument is the default
    token = secret("API_TOKEN")
    print("token is", token)                          # shows as redacted in the logs
    return f"{region}: token of length {len(token)}"


with Python(URL) as py:
    execution = py.run(ENVIRONMENT, connect,
                       variables={"REGION": "us-east"},
                       secrets={"API_TOKEN": "s3cr3t-value"})
    print(py.result(execution["id"]))
    print(py.logs(execution["id"])["stdout"], end="")
