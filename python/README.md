# Python plugin

Warm Python environments on Scraperack. An environment's pip packages are built once and
its modules are imported before the microVM snapshot is taken, so every microVM restored
from it starts with the interpreter running and the imports done. Call a function from
the [SDK](sdk/) and it lands in a ready interpreter; each call is kept as an execution
with its result and output.

```text
plugin/         the installable plugin (pack this folder)
images/         Docker build contexts for Python images
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
