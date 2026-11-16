# Examples

One concept per file. Set `SCRAPERACK_URL` to your controller (default
`http://127.0.0.1:7070`), run `01_create_environment.py` once, then any other file.
They use the environment `examples` (override with `SCRAPERACK_ENV`).

```sh
pip install scraperack-python
```

Your local Python must have the same minor version as the environment's image, because
functions are pickled by value.

| File | Shows |
|---|---|
| [01_create_environment.py](01_create_environment.py) | Create an environment: image, packages, preload, VM size, warm VMs |
| [02_call.py](02_call.py) | Run a function and get its value in one step |
| [03_run_and_wait.py](03_run_and_wait.py) | Start now, collect the result, stdout and timings later |
| [04_variables_and_secrets.py](04_variables_and_secrets.py) | Pass settings and credentials to a call |
| [05_packages.py](05_packages.py) | Use packages installed in the environment |
| [06_working_dir.py](06_working_dir.py) | Ship your own modules and files ([project/](project/)) |
| [07_errors.py](07_errors.py) | `RemoteError`, `APIError`, `queue=False` |
| [08_parallel.py](08_parallel.py) | Many calls at once, async and with threads |
| [09_history.py](09_history.py) | Recent executions, durations, failures |
| [10_manage_environments.py](10_manage_environments.py) | Update, restart and delete environments |
