#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

import grpc

from grpc_utils import ensure_grpc_codegen


def build_grpc_stub():
    repo_root = Path(__file__).resolve().parents[1]
    proto_path = repo_root / "proto" / "lazysync.proto"
    out_dir = repo_root / "lazysync-client" / ".generated"
    lazysync_pb2, lazysync_pb2_grpc = ensure_grpc_codegen(proto_path, out_dir)
    return lazysync_pb2, lazysync_pb2_grpc


def get_path_entries(path: str, server_addr: str | None = None):
    """Return GetPath response from gRPC server."""
    addr = server_addr or os.getenv("LAZYSYNC_GRPC_ADDR", "127.0.0.1:9000")
    lazysync_pb2, lazysync_pb2_grpc = build_grpc_stub()
    with grpc.insecure_channel(addr) as channel:
        stub = lazysync_pb2_grpc.LazySyncStub(channel)
        response = stub.GetPath(lazysync_pb2.GetPathRequest(path=path))
    return response


def main():
    target_path = os.getenv("LAZYSYNC_REMOTE_PATH", "/Users/wadekiny")

    response = get_path_entries(target_path)
    for group in response.entries:
        print(f"[{group.absolute_path}]")
        for entry in group.entries:
            print(f"  {entry.permissions} {entry.size:>8} {entry.modified} {entry.name}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, grpc.RpcError) as exc:
        print(f"执行失败: {exc}")
        sys.exit(1)
