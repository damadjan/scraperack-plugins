import base64
import json
import tempfile
import unittest
from pathlib import Path

import cloudpickle
import httpx
from scraperack import AsyncCluster, Cluster

from scraperack_python import AsyncPython, Python, RemoteError, decode


def task(value):
    return value


def mock(handler):
    return Python(Cluster("http://controller", client=httpx.Client(
        base_url="http://controller", transport=httpx.MockTransport(handler))))


class PythonTests(unittest.TestCase):
    def test_run_submits_to_the_plugin_and_reuses_working_directories(self):
        uploaded, requests = set(), []

        def handler(request):
            requests.append(request)
            if request.method == "HEAD":
                return httpx.Response(204 if request.url.path.rsplit("/", 1)[-1] in uploaded else 404)
            if request.method == "PUT":
                uploaded.add(request.url.path.rsplit("/", 1)[-1])
                return httpx.Response(201)
            return httpx.Response(202, json={"id": "execution"})

        with tempfile.TemporaryDirectory() as directory, mock(handler) as py:
            source = Path(directory, "helper.py")
            source.write_text("VALUE = 1", encoding="utf-8")
            py.run("jobs", task, (1,), working_dir=directory, secrets={"TOKEN": "x"})
            py.run("jobs", task, (2,), working_dir=directory)
            source.write_text("VALUE = 2", encoding="utf-8")
            py.run("jobs", task, (3,), working_dir=directory)

        posts = [request for request in requests if request.method == "POST"]
        self.assertEqual({request.url.path for request in posts}, {"/plugins/python/api/executions"})
        bodies = [json.loads(request.content) for request in posts]
        self.assertEqual(bodies[0]["environment"], "jobs")
        self.assertEqual(bodies[0]["secrets"], {"TOKEN": "x"})
        payloads = [body["payload"] for body in bodies]
        self.assertEqual(payloads[0]["encoding"], "python-function")
        self.assertEqual(payloads[0]["working_dir"], payloads[1]["working_dir"])
        self.assertNotEqual(payloads[1]["working_dir"], payloads[2]["working_dir"])
        self.assertEqual(sum(request.method == "PUT" for request in requests), 2)
        self.assertEqual(cloudpickle.loads(base64.b64decode(payloads[2]["params"])), (3,))

    def test_call_waits_decodes_and_raises(self):
        outcomes = {
            "ok": {"status": "succeeded", "value": {"encoding": "cloudpickle",
                   "data": base64.b64encode(cloudpickle.dumps({1, 2})).decode()}, "error": None},
            "bad": {"status": "failed", "value": None, "error": "ValueError: boom"},
        }
        submitted = iter(["ok", "bad"])

        def handler(request):
            path = request.url.path
            if request.method == "POST":
                return httpx.Response(202, json={"id": next(submitted)})
            identifier = path.split("/")[5]
            if path.endswith("/wait"):
                return httpx.Response(200, json={"id": identifier,
                                                 "status": outcomes[identifier]["status"]})
            return httpx.Response(200, json=outcomes[identifier])

        with mock(handler) as py:
            self.assertEqual(py.call("jobs", task, 1), {1, 2})
            with self.assertRaises(RemoteError) as raised:
                py.call("jobs", task, 2)
        self.assertIn("boom", str(raised.exception))
        self.assertEqual(raised.exception.execution["id"], "bad")

    def test_environments_and_listing_use_the_plugin_api(self):
        calls = []

        def handler(request):
            body = json.loads(request.content) if request.content else None
            calls.append((request.method, request.url.path, request.url.query.decode(), body))
            return httpx.Response(200, json=[] if request.method == "GET" else {"id": "env"})

        with mock(handler) as py:
            py.create_environment("jobs", "image", packages=["httpx==0.28.1"], cpu=0.5, warm=2)
            py.update_environment("jobs", warm=4)
            py.environments()
            py.executions(environment="jobs", limit=5)
            py.delete_environment("jobs")
        self.assertEqual([(method, path) for method, path, _, _ in calls], [
            ("POST", "/plugins/python/api/environments"),
            ("PUT", "/plugins/python/api/environments/jobs"),
            ("GET", "/plugins/python/api/environments"),
            ("GET", "/plugins/python/api/executions"),
            ("DELETE", "/plugins/python/api/environments/jobs"),
        ])
        self.assertEqual(calls[0][3]["cpu_millis"], 500)
        self.assertEqual(calls[0][3]["warm_instances"], 2)
        self.assertEqual(calls[1][3], {"warm_instances": 4})
        self.assertEqual(calls[3][2], "limit=5&environment=jobs")

    def test_decode_json(self):
        self.assertEqual(decode({"encoding": "json", "data": [1]}), [1])
        self.assertIsNone(decode(None))


class AsyncPythonTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_uploads_working_directory(self):
        methods = []

        def handler(request):
            methods.append((request.method, request.url.path.split("/")[1]))
            if request.method == "HEAD":
                return httpx.Response(404)
            if request.method == "PUT":
                return httpx.Response(201)
            return httpx.Response(202, json={"id": "execution"})

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "helper.py").write_text("VALUE = 1", encoding="utf-8")
            client = httpx.AsyncClient(base_url="http://controller",
                                       transport=httpx.MockTransport(handler))
            async with AsyncPython(AsyncCluster("http://controller", client=client)) as py:
                execution = await py.run("jobs", task, working_dir=directory)
            await client.aclose()

        self.assertEqual(execution, {"id": "execution"})
        self.assertEqual(methods, [("HEAD", "v1"), ("PUT", "v1"), ("POST", "plugins")])


if __name__ == "__main__":
    unittest.main()
