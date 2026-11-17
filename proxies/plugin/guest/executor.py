#!/usr/bin/python3
"""Proxies plugin executor: runs scrape and test batches inside the microVMs.

Started by the guest agent with the package disk's Python. Requests are JSON:
{"op": "scrape", "urls": [...], "timeout": 30}
{"op": "test", "items": [[id, ip, port, hint, known], ...], "judges": [...], "https_url": "...",
 "timeout": 10, "concurrency": 120, "rate": 4.0}
"""
import importlib
import json
import os
import socket
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check
import scrape

SOCKET = Path("/tmp/scraperack-executor.sock")


def execute(payload):
    if payload.get("encoding") == "scraperack-invocation":
        payload = payload["payload"]
    if payload.get("encoding") == "scraperack-preload":
        for module in payload.get("modules", []):
            importlib.import_module(module)
        return payload.get("modules", [])
    if payload.get("encoding") == "scraperack-describe":
        return f"proxies executor, Python {sys.version.split()[0]}"
    op = payload.get("op")
    if op == "scrape":
        return scrape.scrape_batch(payload["urls"], payload.get("timeout", 30))
    if op == "test":
        return check.test_batch(payload["items"], payload["judges"], payload["https_url"],
                                payload.get("timeout", 10), payload.get("concurrency", 60),
                                payload.get("rate", 4))
    raise ValueError(f"unknown op: {op!r}")


def handle(payload):
    try:
        return {"result": {"value": execute(payload), "stdout": "", "stderr": ""},
                "error": None, "timed_out": False}
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
