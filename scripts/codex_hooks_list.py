#!/usr/bin/python3
"""Read hook metadata through the installed app-server protocol, without a model."""

import argparse
import json
import queue
import shutil
import subprocess
import threading
import time


def list_hooks(cwds):
    process = subprocess.Popen(
        [
            shutil.which("codex"),
            "--strict-config",
            "app-server",
            "--listen",
            "stdio://",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    messages = queue.Queue()

    def reader():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass

    threading.Thread(target=reader, daemon=True).start()

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    def receive(identifier):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            item = messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if item.get("id") == identifier:
                if "error" in item:
                    raise RuntimeError(item["error"])
                return item["result"]
        raise TimeoutError("App-server did not respond")

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "rtk-hook-verifier", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "hooks/list", "params": {"cwds": cwds}})
        return receive(2)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cwd", nargs="*")
    print(json.dumps(list_hooks(parser.parse_args().cwd), indent=2))
