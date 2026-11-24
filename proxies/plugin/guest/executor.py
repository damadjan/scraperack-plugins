#!/usr/bin/python3
"""Proxies plugin executor, started by the guest agent for the plugin's pools.

The plugin sends its code as `python-source` payloads ({"source": ..., "params": [...]}; the
source defines main(*params)). Values come back as {"encoding": "json", "data": ...}.
"""
import importlib
import json
import socket
import sys
import traceback
from pathlib import Path

SOCKET = Path("/tmp/scraperack-executor.sock")


def execute(payload):
    if payload.get("encoding") == "scraperack-invocation":
        payload = payload["payload"]
    encoding = payload.get("encoding")
    if encoding == "scraperack-preload":
        for module in payload["preload"]:
            importlib.import_module(module)
        return payload["preload"]
    if encoding == "scraperack-describe":
        return f"proxies executor, Python {sys.version.split()[0]}"
    if encoding == "python-source":
        namespace = {"__name__": "proxies_task"}
        exec(compile(payload["source"], "<proxies>", "exec"), namespace)
        return namespace["main"](*payload.get("params", []))
    raise ValueError(f"unknown payload encoding: {encoding}")


def handle(payload):
    try:
        value = {"encoding": "json", "data": execute(payload)}
        return {"result": {"value": value, "stdout": "", "stderr": ""}, "error": None, "timed_out": False}
    except BaseException:
        return {"result": {"value": None, "stdout": "", "stderr": ""},
                "error": traceback.format_exc(), "timed_out": False}


def receive(connection, size):
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def main():
    SOCKET.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET))
    server.listen(1)
    while True:
        connection, _ = server.accept()
        with connection:
            while header := receive(connection, 4):
                body = receive(connection, int.from_bytes(header, "big"))
                if body is None:
                    break
                try:
                    reply = handle(json.loads(body))
                except BaseException:
                    reply = {"result": {"value": None, "stdout": "", "stderr": ""},
                             "error": traceback.format_exc(), "timed_out": False}
                data = json.dumps(reply).encode()
                connection.sendall(len(data).to_bytes(4, "big") + data)


if __name__ == "__main__":
    main()
