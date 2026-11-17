# Proxies plugin

Collects free public proxies from the sources you add, tests them in Scraperack microVMs for
protocol, speed, HTTPS support and anonymity, and serves the working ones through an API with
keys. Everything is kept in one SQLite file on the controller. No SDK: the plugin's page and
its HTTP API.

```sh
tar -czf proxies-plugin.tar.gz -C plugin .
```

Install the archive from **Plugins → Install plugin**, click **Open Proxies** on the plugin's
entry, add proxy lists on **Sources**, and create a key on **API keys**.

Needs an image with `/usr/bin/python3` and its `venv` module (`python-generic-v2` by default).

See [docs/proxies-plugin.md](../docs/proxies-plugin.md).
