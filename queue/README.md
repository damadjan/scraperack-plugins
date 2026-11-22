# Queue plugin

Queues of jobs fed to runner plugins (the Python plugin today) as long as each queue has
room: concurrency, start rate, attempts. Not tied to a language: a job names its runner and
carries that runner's input. Jobs keep only their status and reason; data in SQLite on the
controller. No scheduling: jobs start when there is room.

```sh
tar -czf queue-plugin.tar.gz -C plugin .
```

Install the archive from **Plugins → Install plugin**, create a queue on its page, and put
jobs in it from the [Python SDK](../docs/python-sdk.md#queues).

See [docs/queue-plugin.md](../docs/queue-plugin.md).

## Develop

```sh
python -m unittest discover -s queue/tests
```
