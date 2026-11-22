"""Calls built ahead of time, and the queue plugin's queues.

A ``Call`` is a function call that has not started: ``run()`` starts it now as an
execution, ``put(queue)`` hands it to the queue plugin, which starts it when the queue
has room. Queues are not tied to Python: a job names its runner (``"python"`` here) and
carries that runner's input, so the same queue can hold work of other runtimes.
"""
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .types import Execution, Job, JobDetails, JobStatus, QueueView

if TYPE_CHECKING:
    from .client import AsyncPython, Python

BATCH = 500
SETTINGS = ("concurrency", "rate_per_minute", "max_attempts", "retry_code_errors", "timeout_secs",
            "max_queued", "paused")


def _key(value: str) -> str:
    return quote(str(value), safe="")


class _CallData:
    runner = "python"

    def __init__(self, environment: str, payload: Mapping[str, Any],
                 variables: Mapping[str, str] | None, secrets: Mapping[str, str] | None) -> None:
        self.environment = environment
        self.payload = dict(payload)
        self.variables = dict(variables or {})
        self.secrets = dict(secrets or {})

    @property
    def input(self) -> dict[str, Any]:
        """What the Python plugin needs to start the call (the runner contract's input)."""
        return {"environment": self.environment, "payload": self.payload,
                "variables": self.variables, "secrets": self.secrets}

    def job(self) -> dict[str, Any]:
        """The call as a queue job: ``{runner, input}``."""
        return {"runner": self.runner, "input": self.input}


class Call(_CallData):
    """A Python call that has not started; built with ``Python.bind``."""

    def __init__(self, client: "Python", environment: str, payload: Mapping[str, Any],
                 variables: Mapping[str, str] | None = None,
                 secrets: Mapping[str, str] | None = None) -> None:
        super().__init__(environment, payload, variables, secrets)
        self._client = client

    def run(self) -> Execution:
        """Start the call now, as an execution."""
        return self._client._call("POST", "/executions", json={**self.input, "queue": True})

    def put(self, queue: str) -> str:
        """Put the call in a queue; returns the job id."""
        return self._client.queue(queue).put(self)


class AsyncCall(_CallData):
    """A Python call that has not started; built with ``AsyncPython.bind``."""

    def __init__(self, client: "AsyncPython", environment: str, payload: Mapping[str, Any],
                 variables: Mapping[str, str] | None = None,
                 secrets: Mapping[str, str] | None = None) -> None:
        super().__init__(environment, payload, variables, secrets)
        self._client = client

    async def run(self) -> Execution:
        return await self._client._call("POST", "/executions", json={**self.input, "queue": True})

    async def put(self, queue: str) -> str:
        return await self._client.queue(queue).put(self)


JobLike = _CallData | Mapping[str, Any]


def _job(item: JobLike) -> dict[str, Any]:
    """A call, or a ``{runner, input}`` job of any runtime."""
    if isinstance(item, _CallData):
        return item.job()
    if not isinstance(item, Mapping) or set(item) != {"runner", "input"}:
        raise TypeError("put a Call, or a job {'runner': ..., 'input': {...}}")
    return {"runner": item["runner"], "input": dict(item["input"])}


def _settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(settings) - set(SETTINGS)
    if unknown:
        raise TypeError(f"unknown queue settings: {', '.join(sorted(unknown))}")
    return dict(settings)


def _jobs_params(status: JobStatus | None, limit: int) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    return params


class Queue:
    """One queue of the queue plugin. Get it with ``Python.queue(name)``."""

    def __init__(self, client: "Python", name: str, api: str) -> None:
        self.name = name
        self._client = client
        self._api = api
        self._path = f"{api}/queues/{_key(name)}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._client.cluster.request(method, path, **kwargs)

    def configure(self, **settings: Any) -> QueueView:
        """Create the queue, or change some of its settings.

        ``concurrency``, ``rate_per_minute``, ``max_attempts``, ``retry_code_errors``,
        ``timeout_secs``, ``max_queued``, ``paused``.
        """
        return self._request("PUT", self._path, json=_settings(settings))

    def info(self) -> QueueView:
        """Settings and job counts."""
        return self._request("GET", self._path)

    def put(self, item: JobLike) -> str:
        """Add one job at the back of the queue; returns its id.

        Raises ``scraperack.APIError`` with status 429 when the queue is full.
        """
        return self._request("POST", f"{self._path}/jobs", json=_job(item))["id"]

    def put_many(self, items: Iterable[JobLike]) -> list[str]:
        """Add jobs in batches of 500, in order; returns their ids."""
        ids: list[str] = []
        batch: list[dict[str, Any]] = []
        for item in items:
            batch.append(_job(item))
            if len(batch) == BATCH:
                ids += self._request("POST", f"{self._path}/jobs", json={"jobs": batch})["ids"]
                batch = []
        if batch:
            ids += self._request("POST", f"{self._path}/jobs", json={"jobs": batch})["ids"]
        return ids

    def jobs(self, status: JobStatus | None = None, limit: int = 100) -> list[Job]:
        """The newest jobs, optionally with one status."""
        return self._request("GET", f"{self._path}/jobs", params=_jobs_params(status, limit))

    def job(self, job_id: str) -> JobDetails:
        """One job with its attempts and their run ids."""
        return self._request("GET", f"{self._api}/jobs/{_key(job_id)}")

    def cancel(self, job_id: str) -> JobDetails:
        """Cancel a job; a running one is stopped."""
        return self._request("POST", f"{self._api}/jobs/{_key(job_id)}/cancel", json={})

    def cancel_waiting(self, running: bool = False) -> int:
        """Cancel every waiting job (and with ``running=True`` stop the running ones too)."""
        return self._request("POST", f"{self._path}/cancel", json={"running": running})["cancelled"]

    def delete(self) -> None:
        """Delete the queue: waiting jobs are dropped, running ones stopped."""
        self._request("DELETE", self._path)


class AsyncQueue:
    """One queue of the queue plugin. Get it with ``AsyncPython.queue(name)``."""

    def __init__(self, client: "AsyncPython", name: str, api: str) -> None:
        self.name = name
        self._client = client
        self._api = api
        self._path = f"{api}/queues/{_key(name)}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self._client.cluster.request(method, path, **kwargs)

    async def configure(self, **settings: Any) -> QueueView:
        return await self._request("PUT", self._path, json=_settings(settings))

    async def info(self) -> QueueView:
        return await self._request("GET", self._path)

    async def put(self, item: JobLike) -> str:
        return (await self._request("POST", f"{self._path}/jobs", json=_job(item)))["id"]

    async def put_many(self, items: Iterable[JobLike]) -> list[str]:
        ids: list[str] = []
        batch: list[dict[str, Any]] = []
        for item in items:
            batch.append(_job(item))
            if len(batch) == BATCH:
                ids += (await self._request("POST", f"{self._path}/jobs",
                                            json={"jobs": batch}))["ids"]
                batch = []
        if batch:
            ids += (await self._request("POST", f"{self._path}/jobs", json={"jobs": batch}))["ids"]
        return ids

    async def jobs(self, status: JobStatus | None = None, limit: int = 100) -> list[Job]:
        return await self._request("GET", f"{self._path}/jobs",
                                   params=_jobs_params(status, limit))

    async def job(self, job_id: str) -> JobDetails:
        return await self._request("GET", f"{self._api}/jobs/{_key(job_id)}")

    async def cancel(self, job_id: str) -> JobDetails:
        return await self._request("POST", f"{self._api}/jobs/{_key(job_id)}/cancel", json={})

    async def cancel_waiting(self, running: bool = False) -> int:
        return (await self._request("POST", f"{self._path}/cancel",
                                    json={"running": running}))["cancelled"]

    async def delete(self) -> None:
        await self._request("DELETE", self._path)
