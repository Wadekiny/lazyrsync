#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from multiprocessing import Pipe, Process
from pathlib import Path
import subprocess
import shlex

import grpc

from askpass_ipc import AskPassIPCServer, create_askpass_socket_path
from grpc_utils import ensure_grpc_codegen


SSH_HOST = os.getenv("LAZYSYNC_SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.getenv("LAZYSYNC_SSH_PORT", "22"))
SSH_USER = os.getenv("LAZYSYNC_SSH_USER", "user")
SSH_KEY_PATH = os.getenv("LAZYSYNC_SSH_KEY_PATH")

LOCAL_FORWARD_HOST = os.getenv("LAZYSYNC_LOCAL_HOST", "127.0.0.1")
LOCAL_FORWARD_PORT = int(os.getenv("LAZYSYNC_LOCAL_PORT", "9000"))
REMOTE_HOST = os.getenv("LAZYSYNC_REMOTE_HOST", "127.0.0.1")
REMOTE_PORT = int(os.getenv("LAZYSYNC_REMOTE_PORT", "9000"))

ASKPASS_SOCKET = os.getenv("LAZYSYNC_ASKPASS_SOCKET")


def _create_askpass_wrapper(askpass_script: Path) -> Path:
    wrapper_dir = Path(tempfile.mkdtemp(prefix="lazysync-askpass-"))
    wrapper_path = wrapper_dir / "askpass.sh"
    python_exec = shlex.quote(sys.executable)
    script_path = shlex.quote(str(askpass_script))
    wrapper_path.write_text(f"#!/bin/sh\nexec {python_exec} {script_path} \"$@\"\n")
    wrapper_path.chmod(0o755)
    return wrapper_path


class SSHForwardWorker:
    def __init__(self, conn, askpass_path: str):
        self._conn = conn
        self._askpass_path = askpass_path

    def _build_ssh_cmd(self):
        cmd = [
            "ssh",
            "-N",
            "-L",
            f"{LOCAL_FORWARD_HOST}:{LOCAL_FORWARD_PORT}:{REMOTE_HOST}:{REMOTE_PORT}",
            "-p",
            str(SSH_PORT),
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if SSH_KEY_PATH:
            cmd.extend(["-i", SSH_KEY_PATH])
        cmd.append(f"{SSH_USER}@{SSH_HOST}")
        return cmd

    def run(self):
        env = os.environ.copy()
        env["SSH_ASKPASS"] = self._askpass_path
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", "1")
        if ASKPASS_SOCKET:
            env["LAZYSYNC_ASKPASS_SOCKET"] = ASKPASS_SOCKET

        cmd = self._build_ssh_cmd()
        proc = subprocess.Popen(cmd, env=env)
        self._conn.send({"type": "started", "pid": proc.pid})

        while True:
            if self._conn.poll(0.2):
                message = self._conn.recv()
                if message.get("type") == "shutdown":
                    proc.terminate()
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.1)

        returncode = proc.wait()
        self._conn.send({"type": "exited", "returncode": returncode})

    @classmethod
    def process_main(cls, conn, askpass_path: str):
        cls(conn, askpass_path).run()


class SSHForwardClient:
    def __init__(self):
        parent_conn, child_conn = Pipe()
        askpass_script = Path(__file__).resolve().parent / "askpass_grpc.py"
        try:
            askpass_script.chmod(0o755)
        except OSError:
            pass
        self._askpass_wrapper = _create_askpass_wrapper(askpass_script)
        askpass_path = str(self._askpass_wrapper)
        self._conn = parent_conn
        self._proc = Process(
            target=SSHForwardWorker.process_main,
            args=(child_conn, askpass_path),
        )
        self._proc.daemon = True
        self._proc.start()

    def wait_started(self, timeout_s: Optional[float] = 5.0):
        if timeout_s is None:
            while True:
                if self._conn.poll(0.2):
                    message = self._conn.recv()
                    if message.get("type") == "started":
                        return message
                    if message.get("type") == "exited":
                        raise RuntimeError(
                            f"SSH exited early: {message.get('returncode')}"
                        )
                time.sleep(0.1)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._conn.poll(0.2):
                message = self._conn.recv()
                if message.get("type") == "started":
                    return message
                if message.get("type") == "exited":
                    raise RuntimeError(
                        f"SSH exited early: {message.get('returncode')}"
                    )
            time.sleep(0.1)
        raise RuntimeError("SSH worker did not start in time")

    def close(self):
        if self._proc.is_alive():
            self._conn.send({"type": "shutdown"})
            self._proc.join(timeout=3)
        if getattr(self, "_askpass_wrapper", None):
            try:
                self._askpass_wrapper.unlink()
                self._askpass_wrapper.parent.rmdir()
            except OSError:
                pass


def wait_for_local_port(host, port, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def build_grpc_stub():
    repo_root = Path(__file__).resolve().parents[1]
    proto_path = repo_root / "proto" / "lazysync.proto"
    out_dir = repo_root / "lazysync-client" / ".generated"
    lazysync_pb2, lazysync_pb2_grpc = ensure_grpc_codegen(proto_path, out_dir)
    return lazysync_pb2, lazysync_pb2_grpc


def main():
    global ASKPASS_SOCKET
    if not ASKPASS_SOCKET:
        ASKPASS_SOCKET = str(create_askpass_socket_path())
    askpass_server = AskPassIPCServer(Path(ASKPASS_SOCKET))
    askpass_server.start()
    ssh_client = SSHForwardClient()
    try:
        ssh_client.wait_started()
        if not wait_for_local_port(LOCAL_FORWARD_HOST, LOCAL_FORWARD_PORT, timeout_s=15):
            raise RuntimeError("Port forward not ready, check SSH connection.")

        lazysync_pb2, lazysync_pb2_grpc = build_grpc_stub()
        with grpc.insecure_channel(
            f"{LOCAL_FORWARD_HOST}:{LOCAL_FORWARD_PORT}"
        ) as channel:
            stub = lazysync_pb2_grpc.LazySyncStub(channel)
            target_path = os.getenv("LAZYSYNC_REMOTE_PATH", "/")
            response = stub.GetPath(lazysync_pb2.GetPathRequest(path=target_path))
            for group in response.entries:
                print(f"[{group.absolute_path}]")
                for entry in group.entries:
                    print(
                        f"  {entry.permissions} {entry.size:>8} {entry.modified} {entry.name}"
                    )
    finally:
        ssh_client.close()
        askpass_server.stop()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, grpc.RpcError) as exc:
        print(f"执行失败: {exc}")
        sys.exit(1)
