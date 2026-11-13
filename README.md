# ScrapeRack plugins

Plugins for [ScrapeRack](https://github.com/damadjan/scraperack). The core only runs
microVMs; a plugin adds a workload type: what runs in the microVMs, its own page in the
GUI, its own API, and an SDK.

| Plugin | What it adds | SDK |
|---|---|---|
| [`python`](python/) | Python environments (pip packages built once, warm microVMs) and executions (every function call with its output) | `pip install scraperack-python` |

## Quick start

```sh
tar -czf python-plugin.tar.gz -C python/plugin .
```

Install the archive from **Plugins → Install plugin** in the ScrapeRack GUI, create an
environment on the plugin's page, then:

```python
from scraperack_python import Python


def status(url):
    import httpx
    return httpx.get(url).status_code


with Python("http://controller:7070") as py:
    print(py.call("scrapers", status, "https://example.com"))
```

## Documentation

- [Installing plugins](docs/installing-plugins.md)
- [Writing a plugin](docs/writing-plugins.md)
- [Python plugin](docs/python-plugin.md)
- [Python SDK](docs/python-sdk.md)

## License

[Apache License 2.0](LICENSE).
