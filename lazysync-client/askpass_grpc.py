#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import sys


def request_password(prompt: str, socket_path: str) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(socket_path)
        file = sock.makefile("rwb")
        payload = {"prompt": prompt, "echo": False}
        file.write((json.dumps(payload) + "\n").encode("utf-8"))
        file.flush()
        line = file.readline()
        if not line:
            return ""
        response = json.loads(line.decode("utf-8"))
        if response.get("cancelled"):
            return ""
        return response.get("password", "")


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Password: "
    socket_path = os.getenv("LAZYSYNC_ASKPASS_SOCKET")
    if not socket_path:
        sys.stdout.write("")
        sys.stdout.flush()
        return
    try:
        password = request_password(prompt, socket_path)
    except (OSError, json.JSONDecodeError):
        password = ""
    sys.stdout.write(password)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
