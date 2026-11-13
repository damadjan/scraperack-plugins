"""Clients for the Scraperack Python plugin: environments and executions.

They talk to the plugin's API at ``/plugins/python/api`` through the core
``scraperack`` client, which stays available as ``.cluster`` for everything
else (images, pools, microVMs).
"""
import base64
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import cloudpickle
from scraperack import AsyncCluster, Cluster
from scraperack.types import FINISHED

from .types import EncodedValue, Environment, Execution, ExecutionDetails, ExecutionLogs

DEFAULT_URL = "http://127.0.0.1:7070"


class RemoteError(RuntimeError):
    """The function raised, timed out, or could not run in its environment."""

    def __init__(self, message: str, execution: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.execution = execution


def function_payload(
    function: Callable[..., Any], params: Sequence[Any] = (), working_dir: str | None = None
) -> dict[str, Any]:
    """Encode a function and its arguments for the Python executor."""
    data = cloudpickle.dumps(function)
    payload = {
        "encoding": "python-function",
        "hash": hashlib.sha256(data).hexdigest(),
        "function": base64.b64encode(data).decode(),
        "params": base64.b64encode(cloudpickle.dumps(tuple(params))).decode(),
    }
    if working_dir:
        payload["working_dir"] = working_dir
        payload["cache_key"] = hashlib.sha256(data + working_dir.encode()).hexdigest()
    return payload


def decode(value: EncodedValue | None) -> Any:
    """Turn an executor result back into a Python value."""
    if value is None:
        return None
    if value["encoding"] == "json":
        return value["data"]
    if value["encoding"] == "cloudpickle":
        return cloudpickle.loads(base64.b64decode(value["data"]))
    raise ValueError(f'unknown result encoding: {value["encoding"]}')


RENAMES = {"warm": "warm_instances", "timeout": "timeout_secs", "idle_timeout": "idle_timeout_secs"}


def _environment_body(name: str | None, image: str | None, packages: Sequence[str] | None,
                      preload: Sequence[str] | None, cpu: float | None,
                      settings: Mapping[str, Any]) -> dict[str, Any]:
    body = {RENAMES.get(key, key): value for key, value in settings.items()}
    if name is not None:
        body["name"] = name
    if image is not None:
        body["image"] = image
    if packages is not None:
        body["packages"] = list(packages)
    if preload is not None:
        body["preload"] = list(preload)
    if cpu is not None:
        body["cpu_millis"] = round(cpu * 1000)
    return body


def _run_body(environment: str, payload: Mapping[str, Any], queue: bool,
              variables: Mapping[str, str] | None,
              secrets: Mapping[str, str] | None) -> dict[str, Any]:
    return {
        "environment": environment,
        "payload": dict(payload),
        "queue": queue,
        "variables": dict(variables or {}),
        "secrets": dict(secrets or {}),
    }


def _raise_for(execution: Mapping[str, Any], outcome: Mapping[str, Any]) -> None:
    if outcome["status"] != "succeeded":
        raise RemoteError(outcome.get("error") or f'execution {outcome["status"]}', execution)


def _key(value: str) -> str:
    return quote(str(value), safe="")


class Python:
    """Synchronous client for Python environments and executions."""

    def __init__(self, cluster: str | Cluster = DEFAULT_URL, *, plugin: str = "python") -> None:
        self._owns_cluster = isinstance(cluster, str)
        self.cluster = Cluster(cluster) if isinstance(cluster, str) else cluster
        self.api = f"/plugins/{_key(plugin)}/api"

    def __enter__(self) -> "Python":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_cluster:
            self.cluster.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.cluster.request(method, self.api + path, **kwargs)

    # Environments
    def environments(self) -> list[Environment]:
        return self._call("GET", "/environments")

    def environment(self, environment: str) -> Environment:
        return self._call("GET", f"/environments/{_key(environment)}")

    def create_environment(self, name: str, image: str, packages: Sequence[str] = (),
                           preload: Sequence[str] = (), cpu: float = 1,
                           **settings: Any) -> Environment:
        """Create an environment; blocks while its packages are installed.

        ``settings``: memory_mib, disk_mib, timeout, warm, max_instances, idle_timeout,
        reuse, network.
        """
        body = _environment_body(name, image, packages, preload, cpu, settings)
        return self._call("POST", "/environments", json=body, timeout=None)

    def update_environment(self, environment: str, *, packages: Sequence[str] | None = None,
                           preload: Sequence[str] | None = None, cpu: float | None = None,
                           **settings: Any) -> Environment:
        """Change an environment; packages are rebuilt only if they changed."""
        body = _environment_body(None, None, packages, preload, cpu, settings)
        return self._call("PUT", f"/environments/{_key(environment)}", json=body, timeout=None)

    def restart_environment(self, environment: str) -> Environment:
        return self._call("POST", f"/environments/{_key(environment)}/restart")

    def delete_environment(self, environment: str) -> None:
        self._call("DELETE", f"/environments/{_key(environment)}")

    # Executions
    def run(self, environment: str, function: Callable[..., Any], params: Sequence[Any] = (),
            queue: bool = True, variables: Mapping[str, str] | None = None,
            secrets: Mapping[str, str] | None = None,
            working_dir: str | Path | None = None) -> Execution:
        """Start ``function(*params)`` in an environment and return the execution."""
        digest = self.cluster.upload_working_dir(working_dir) if working_dir else None
        payload = function_payload(function, params, digest)
        return self._call("POST", "/executions",
                          json=_run_body(environment, payload, queue, variables, secrets))

    def call(self, environment: str, function: Callable[..., Any], *params: Any,
             **options: Any) -> Any:
        """Run ``function(*params)`` and return its value; raises ``RemoteError`` on failure."""
        return self.result(self.run(environment, function, params, **options)["id"])

    def executions(self, environment: str | None = None, limit: int = 200) -> list[Execution]:
        """Newest executions first, optionally of one environment."""
        params: dict[str, Any] = {"limit": limit}
        if environment:
            params["environment"] = environment
        return self._call("GET", "/executions", params=params)

    def execution(self, execution_id: str) -> ExecutionDetails:
        """One execution with its logs and encoded result."""
        return self._call("GET", f"/executions/{_key(execution_id)}")

    def wait(self, execution_id: str) -> Execution:
        """Block until the execution succeeds, fails, or times out."""
        while True:
            execution = self._call("GET", f"/executions/{_key(execution_id)}/wait", timeout=None)
            if execution["status"] in FINISHED:
                return execution

    def result(self, execution_id: str) -> Any:
        """Wait for an execution and return its decoded value."""
        execution = self.wait(execution_id)
        outcome = self._call("GET", f"/executions/{_key(execution_id)}/result")
        _raise_for(execution, outcome)
        return decode(outcome["value"])

    def logs(self, execution_id: str) -> ExecutionLogs:
        return self._call("GET", f"/executions/{_key(execution_id)}/logs")


class AsyncPython:
    """Asynchronous client for Python environments and executions."""

    def __init__(self, cluster: str | AsyncCluster = DEFAULT_URL, *,
                 plugin: str = "python") -> None:
        self._owns_cluster = isinstance(cluster, str)
        self.cluster = AsyncCluster(cluster) if isinstance(cluster, str) else cluster
        self.api = f"/plugins/{_key(plugin)}/api"

    async def __aenter__(self) -> "AsyncPython":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_cluster:
            await self.cluster.close()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self.cluster.request(method, self.api + path, **kwargs)

    # Environments
    async def environments(self) -> list[Environment]:
        return await self._call("GET", "/environments")

    async def environment(self, environment: str) -> Environment:
        return await self._call("GET", f"/environments/{_key(environment)}")

    async def create_environment(self, name: str, image: str, packages: Sequence[str] = (),
                                 preload: Sequence[str] = (), cpu: float = 1,
                                 **settings: Any) -> Environment:
        body = _environment_body(name, image, packages, preload, cpu, settings)
        return await self._call("POST", "/environments", json=body, timeout=None)

    async def update_environment(self, environment: str, *,
                                 packages: Sequence[str] | None = None,
                                 preload: Sequence[str] | None = None,
                                 cpu: float | None = None, **settings: Any) -> Environment:
        body = _environment_body(None, None, packages, preload, cpu, settings)
        return await self._call("PUT", f"/environments/{_key(environment)}", json=body,
                                timeout=None)

    async def restart_environment(self, environment: str) -> Environment:
        return await self._call("POST", f"/environments/{_key(environment)}/restart")

    async def delete_environment(self, environment: str) -> None:
        await self._call("DELETE", f"/environments/{_key(environment)}")

    # Executions
    async def run(self, environment: str, function: Callable[..., Any],
                  params: Sequence[Any] = (), queue: bool = True,
                  variables: Mapping[str, str] | None = None,
                  secrets: Mapping[str, str] | None = None,
                  working_dir: str | Path | None = None) -> Execution:
        digest = await self.cluster.upload_working_dir(working_dir) if working_dir else None
        payload = function_payload(function, params, digest)
        return await self._call("POST", "/executions",
                                json=_run_body(environment, payload, queue, variables, secrets))

    async def call(self, environment: str, function: Callable[..., Any], *params: Any,
                   **options: Any) -> Any:
        execution = await self.run(environment, function, params, **options)
        return await self.result(execution["id"])

    async def executions(self, environment: str | None = None,
                         limit: int = 200) -> list[Execution]:
        params: dict[str, Any] = {"limit": limit}
        if environment:
            params["environment"] = environment
        return await self._call("GET", "/executions", params=params)

    async def execution(self, execution_id: str) -> ExecutionDetails:
        return await self._call("GET", f"/executions/{_key(execution_id)}")

    async def wait(self, execution_id: str) -> Execution:
        while True:
            execution = await self._call("GET", f"/executions/{_key(execution_id)}/wait",
                                         timeout=None)
            if execution["status"] in FINISHED:
                return execution

    async def result(self, execution_id: str) -> Any:
        execution = await self.wait(execution_id)
        outcome = await self._call("GET", f"/executions/{_key(execution_id)}/result")
        _raise_for(execution, outcome)
        return decode(outcome["value"])

    async def logs(self, execution_id: str) -> ExecutionLogs:
        return await self._call("GET", f"/executions/{_key(execution_id)}/logs")
