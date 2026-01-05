from __future__ import annotations

import json
import getpass
import socket
import tempfile
import threading
from pathlib import Path


def create_askpass_socket_path() -> Path:
    socket_dir = Path(tempfile.mkdtemp(prefix="lazysync-askpass-"))
    return socket_dir / "askpass.sock"


class AskPassIPCServer:
    def __init__(self, socket_path: Path):
        self._socket_path = Path(socket_path)
        self._server = None
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._socket_path.exists():
            self._socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._socket_path))
        server.listen(1)
        server.settimeout(0.2)
        self._server = server
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop_event.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                file = conn.makefile("rwb")
                line = file.readline()
                if not line:
                    continue
                try:
                    request = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                prompt = request.get("prompt") or "Password: "
                echo = bool(request.get("echo", False))
                try:
                    if echo:
                        password = input(prompt)
                    else:
                        password = getpass.getpass(prompt)
                    response = {"password": password, "cancelled": False}
                except (KeyboardInterrupt, EOFError):
                    response = {"password": "", "cancelled": True}
                file.write((json.dumps(response) + "\n").encode("utf-8"))
                file.flush()

    def stop(self):
        self._stop_event.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=1)
        try:
            self._socket_path.unlink()
            self._socket_path.parent.rmdir()
        except OSError:
            pass
