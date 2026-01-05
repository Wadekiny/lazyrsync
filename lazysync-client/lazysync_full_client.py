#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from multiprocessing import Pipe, Process
from pathlib import Path
import subprocess
import shlex
from typing import AsyncIterator, Iterable, Optional

import grpc

from askpass_ipc import AskPassIPCServer, create_askpass_socket_path
from grpc_utils import ensure_grpc_codegen


@dataclass(frozen=True)
class SSHConfig:
    host: str
    port: int
    user: str
    key_path: Optional[str]
    local_host: str
    local_port: int
    remote_host: str
    remote_port: int
    askpass_socket: Optional[str]


def _load_lazysync_proto():
    repo_root = Path(__file__).resolve().parents[1]
    proto_path = repo_root / "proto" / "lazysync.proto"
    out_dir = repo_root / "lazysync-client" / ".generated"
    return ensure_grpc_codegen(proto_path, out_dir)


def _create_askpass_wrapper(askpass_script: Path) -> Path:
    wrapper_dir = Path(tempfile.mkdtemp(prefix="lazysync-askpass-"))
    wrapper_path = wrapper_dir / "askpass.sh"
    python_exec = shlex.quote(sys.executable)
    script_path = shlex.quote(str(askpass_script))
    wrapper_path.write_text(f"#!/bin/sh\nexec {python_exec} {script_path} \"$@\"\n")
    wrapper_path.chmod(0o755)
    return wrapper_path


class SSHForwardWorker:
    def __init__(self, conn, askpass_path: str, config: SSHConfig):
        self._conn = conn
        self._askpass_path = askpass_path
        self._config = config

    def _build_ssh_cmd(self):
        cmd = [
            "ssh",
            "-N",
            "-L",
            f"{self._config.local_host}:{self._config.local_port}:"
            f"{self._config.remote_host}:{self._config.remote_port}",
            "-p",
            str(self._config.port),
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self._config.key_path:
            cmd.extend(["-i", self._config.key_path])
        cmd.append(f"{self._config.user}@{self._config.host}")
        return cmd

    def run(self):
        env = os.environ.copy()
        env["SSH_ASKPASS"] = self._askpass_path
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", "1")
        if self._config.askpass_socket:
            env["LAZYSYNC_ASKPASS_SOCKET"] = self._config.askpass_socket

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
    def process_main(cls, conn, askpass_path: str, config: SSHConfig):
        cls(conn, askpass_path, config).run()


class SSHForwardManager:
    def __init__(self, config: SSHConfig):
        self._config = config
        self._conn = None
        self._proc = None
        self._askpass_wrapper = None

    def start(self):
        if self._proc and self._proc.is_alive():
            return
        parent_conn, child_conn = Pipe()
        askpass_script = Path(__file__).resolve().parent / "askpass_grpc.py"
        try:
            askpass_script.chmod(0o755)
        except OSError:
            pass
        self._askpass_wrapper = _create_askpass_wrapper(askpass_script)
        self._conn = parent_conn
        self._proc = Process(
            target=SSHForwardWorker.process_main,
            args=(child_conn, str(self._askpass_wrapper), self._config),
        )
        self._proc.daemon = True
        self._proc.start()

    def wait_started(self, timeout_s: Optional[float] = 5.0):
        if not self._conn:
            raise RuntimeError("SSH worker not started.")
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
        if self._proc and self._proc.is_alive():
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


class LazySyncGrpcClient:
    def __init__(self, server_addr: str):
        lazysync_pb2, lazysync_pb2_grpc = _load_lazysync_proto()
        self._pb2 = lazysync_pb2
        self._pb2_grpc = lazysync_pb2_grpc
        self._server_addr = server_addr
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub = None

    async def connect(self):
        self._channel = grpc.aio.insecure_channel(self._server_addr)
        self._stub = self._pb2_grpc.LazySyncStub(self._channel)

    async def close(self):
        if self._channel:
            await self._channel.close()

    async def health(self) -> str:
        response = await self._stub.Health(self._pb2.HealthRequest())
        return response.status

    async def get_path(self, path: str):
        result = await self._stub.GetPath(self._pb2.GetPathRequest(path=path))
        return result

    async def stat(self, path: str):
        return await self._stub.Stat(self._pb2.StatRequest(path=path))

    async def read_file(
        self, path: str, offset: int = 0, length: int = 0
    ) -> AsyncIterator[bytes]:
        request = self._pb2.ReadFileRequest(path=path, offset=offset, length=length)
        async for chunk in self._stub.ReadFile(request):
            if chunk.data:
                yield bytes(chunk.data)
            if chunk.eof:
                break

    async def write_file(
        self, path: str, chunks: Iterable[bytes], offset: int = 0
    ) -> int:
        async def request_iter():
            current_offset = offset
            for data in chunks:
                yield self._pb2.WriteFileChunk(
                    path=path,
                    offset=current_offset,
                    data=data,
                    eof=False,
                )
                current_offset += len(data)
            yield self._pb2.WriteFileChunk(
                path=path,
                offset=current_offset,
                data=b"",
                eof=True,
            )

        response = await self._stub.WriteFile(request_iter())
        return response.bytes_written


class LazySyncController:
    def __init__(self, config: SSHConfig):
        self._config = config
        self._askpass_server = None
        self._askpass_socket = None
        self._ssh_manager: Optional[SSHForwardManager] = None
        self._grpc_client: Optional[LazySyncGrpcClient] = None

    async def start(self):
        if not self._config.askpass_socket:
            socket_path = create_askpass_socket_path()
            self._config = replace(self._config, askpass_socket=str(socket_path))
        self._askpass_socket = self._config.askpass_socket
        self._askpass_server = AskPassIPCServer(Path(self._askpass_socket))
        self._askpass_server.start()
        self._ssh_manager = SSHForwardManager(self._config)
        self._ssh_manager.start()
        self._ssh_manager.wait_started()
        if not wait_for_local_port(
            self._config.local_host, self._config.local_port, timeout_s=15
        ):
            raise RuntimeError("Port forward not ready.")
        self._grpc_client = LazySyncGrpcClient(
            f"{self._config.local_host}:{self._config.local_port}"
        )
        await self._grpc_client.connect()

    async def stop(self):
        if self._grpc_client:
            await self._grpc_client.close()
        if self._ssh_manager:
            self._ssh_manager.close()
        if self._askpass_server:
            self._askpass_server.stop()

    @property
    def grpc(self) -> LazySyncGrpcClient:
        if not self._grpc_client:
            raise RuntimeError("Client not started.")
        return self._grpc_client


def load_config_from_env() -> SSHConfig:
    return SSHConfig(
        host=os.getenv("LAZYSYNC_SSH_HOST", "116.172.93.227"),
        port=int(os.getenv("LAZYSYNC_SSH_PORT", "29293")),
        user=os.getenv("LAZYSYNC_SSH_USER", "ubuntu"),
        key_path=os.getenv("LAZYSYNC_SSH_KEY_PATH"),
        local_host=os.getenv("LAZYSYNC_LOCAL_HOST", "127.0.0.1"),
        local_port=int(os.getenv("LAZYSYNC_LOCAL_PORT", "9000")),
        remote_host=os.getenv("LAZYSYNC_REMOTE_HOST", "127.0.0.1"),
        remote_port=int(os.getenv("LAZYSYNC_REMOTE_PORT", "9000")),
        askpass_socket=os.getenv("LAZYSYNC_ASKPASS_SOCKET"),
    )


async def main():
    config = load_config_from_env()
    target_path = os.getenv("LAZYSYNC_REMOTE_PATH", "/Users/wadekiny")

    controller = LazySyncController(config)
    await controller.start()
    try:
        status = await controller.grpc.health()
        print(f"health: {status}")
        response = await controller.grpc.get_path(target_path)
        for group in response.entries:
            print(f"[{group.absolute_path}]")
            for entry in group.entries:
                print(
                    f"  {entry.permissions} {entry.size:>8} {entry.modified} {entry.name}"
                )
    finally:
        await controller.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (OSError, RuntimeError, grpc.RpcError) as exc:
        print(f"执行失败: {exc}")
        sys.exit(1)
