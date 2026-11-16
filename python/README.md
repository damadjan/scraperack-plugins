# Python plugin

Runs Python functions in Scraperack microVMs: environments with pinned pip packages and
warm microVMs, and an Executions view of every call with its output.

```text
plugin/         the installable plugin (pack this folder)
sdk/            scraperack-python, the SDK for this plugin
examples/       SDK usage, one concept per file
verification/   end-to-end checks and benchmarks
tests/          plugin unit tests
```

Documentation: [the plugin](../docs/python-plugin.md), [the SDK](../docs/python-sdk.md) and
[examples](examples/).

## Develop

```sh
python -m unittest discover -s python/tests          # plugin, Linux only
pip install ./python/sdk                              # installs scraperack too
(cd python/sdk && python -m unittest discover -s tests)
```
