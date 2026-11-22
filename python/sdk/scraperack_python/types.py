from typing import Any, Literal, TypedDict

from scraperack import TaskMetrics, TaskStatus

ExecutionStatus = TaskStatus


class Environment(TypedDict):
    id: str
    name: str
    image: str
    packages: list[str]
    preload: list[str]
    pool: str
    venv_disk: str | None
    warm_instances: int
    max_instances: int | None
    cpu_millis: int
    memory_mib: int
    disk_mib: int
    timeout_secs: int
    idle_timeout_secs: int
    reuse: bool
    network: bool
    ready: bool
    revision: int | None


class EncodedValue(TypedDict):
    encoding: Literal["json", "cloudpickle"]
    data: Any


class ExecutionLogs(TypedDict):
    stdout: str
    stderr: str


class Execution(TypedDict):
    """Python code run in an environment, as the Python plugin lists it."""

    id: str
    environment: str
    environment_id: str | None
    status: ExecutionStatus
    function: str | None
    module: str | None
    file: str | None
    encoding: str | None
    node: str | None
    error: str | None
    created_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None
    duration_ms: int | None
    metrics: TaskMetrics | None


class ExecutionDetails(Execution):
    logs: ExecutionLogs
    result: EncodedValue | None


JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class QueueCounts(TypedDict):
    queued: int
    running: int
    done: int
    failed: int
    cancelled: int
    finished_last_minute: int


class QueueView(TypedDict):
    """A queue of the queue plugin: its settings and job counts."""

    name: str
    concurrency: int
    rate_per_minute: int | None
    max_attempts: int
    retry_code_errors: bool
    timeout_secs: int | None
    max_queued: int
    paused: bool
    created_ms: int
    counts: QueueCounts


class Job(TypedDict):
    """A queued job's metadata; the queue keeps nothing the job produced."""

    id: str
    queue: str
    runner: str
    status: JobStatus
    reason: str | None
    attempts: int
    run_id: str | None
    created_ms: int
    started_ms: int | None
    finished_ms: int | None


class Attempt(TypedDict):
    number: int
    run_id: str | None
    started_ms: int
    finished_ms: int | None
    outcome: Literal["done", "failed", "cancelled"] | None
    reason: str | None


class JobDetails(Job):
    attempts_detail: list[Attempt]
