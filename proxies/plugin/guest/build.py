"""Builds the package virtualenv inside a builder microVM. Module level imports stdlib only."""
import base64
import glob
import io
import os
import shutil
import subprocess
import sys
import tarfile

# A virtualenv finds itself from its own pyvenv.cfg, so it runs from wherever it is mounted.
VENV = "/tmp/proxies-build"


def build(packages):
    """Install packages into a virtualenv and return it as a base64 .tar.gz."""
    shutil.rmtree(VENV, ignore_errors=True)
    for command in (["/usr/bin/python3", "-m", "venv", VENV],
                    [f"{VENV}/bin/python", "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
                     *packages]):
        process = subprocess.run(command, capture_output=True, text=True)
        if process.returncode:
            raise RuntimeError(f"{' '.join(command[:4])} failed:\n{(process.stderr or process.stdout)[-3000:]}")
    # pip and setuptools are not needed to run anything; leaving them out halves the archive.
    for pattern in ("pip", "pip-*", "setuptools", "setuptools-*", "pkg_resources", "_distutils_hack",
                    "distutils-precedence.pth"):
        for path in glob.glob(f"{VENV}/lib/python3*/site-packages/{pattern}"):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(VENV, arcname=".")
    shutil.rmtree(VENV, ignore_errors=True)
    return {"archive": base64.b64encode(buffer.getvalue()).decode(), "python": sys.version.split()[0]}
