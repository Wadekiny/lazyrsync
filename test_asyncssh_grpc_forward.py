#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import asyncssh
import grpc


SSH_HOST = "116.172.93.227"
SSH_PORT = 48600
SSH_USER = "ubuntu"

LOCAL_FORWARD_HOST = "127.0.0.1"
LOCAL_FORWARD_PORT = 43800
REMOTE_FORWARD_HOST = "127.0.0.1"
REMOTE_FORWARD_PORT = 43800

REMOTE_PATH = os.getenv("LAZYSYNC_REMOTE_PATH", "/")


def _load_grpc_stub():
    repo_root = Path(__file__).resolve().parent
    client_dir = repo_root / "lazysync-client"
    sys.path.insert(0, str(client_dir))
    from grpc_utils import ensure_grpc_codegen  # noqa: E402

    proto_path = repo_root / "proto" / "lazysync.proto"
    out_dir = client_dir / ".generated"
    lazysync_pb2, lazysync_pb2_grpc = ensure_grpc_codegen(proto_path, out_dir)
    return lazysync_pb2, lazysync_pb2_grpc


async def _wait_for_local_port(host: str, port: int, timeout_s: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            if hasattr(writer, "wait_closed"):
                await writer.wait_closed()
            return True
        except OSError:
            await asyncio.sleep(0.2)
    return False


async def _call_grpc(addr: str) -> None:
    lazysync_pb2, lazysync_pb2_grpc = _load_grpc_stub()
    channel = grpc.aio.insecure_channel(addr)
    try:
        stub = lazysync_pb2_grpc.LazySyncStub(channel)
        health = await stub.Health(lazysync_pb2.HealthRequest())
        print(f"Health: {health.status}")
        response = await stub.GetPath(lazysync_pb2.GetPathRequest(path=REMOTE_PATH))
        for group in response.entries:
            print(f"[{group.absolute_path}]")
            for entry in group.entries:
                print(
                    f"  {entry.permissions} {entry.size:>8} {entry.modified} {entry.name}"
                )
    finally:
        await channel.close()


async def main() -> int:
    conn: Optional[asyncssh.SSHClientConnection] = None
    try:
        conn = await asyncssh.connect(
            SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
        )
        listener = await conn.forward_local_port(
            LOCAL_FORWARD_HOST,
            LOCAL_FORWARD_PORT,
            REMOTE_FORWARD_HOST,
            REMOTE_FORWARD_PORT,
        )
        if not await _wait_for_local_port(LOCAL_FORWARD_HOST, LOCAL_FORWARD_PORT, 15.0):
            print("端口转发未就绪，请检查 SSH 连接。")
            listener.close()
            return 1

        await _call_grpc(f"{LOCAL_FORWARD_HOST}:{LOCAL_FORWARD_PORT}")
        listener.close()
        return 0
    except (OSError, asyncssh.Error, grpc.RpcError) as exc:
        print(f"执行失败: {exc}")
        return 1
    finally:
        # Keep the connection object alive; let process exit handle cleanup.
        _ = conn


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
