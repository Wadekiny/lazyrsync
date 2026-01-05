from __future__ import annotations

from pathlib import Path
import importlib
import sys


def ensure_grpc_codegen(proto_path: Path, out_dir: Path):
    proto_path = proto_path.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    pb2_file = out_dir / f"{proto_path.stem}_pb2.py"
    pb2_grpc_file = out_dir / f"{proto_path.stem}_pb2_grpc.py"

    if (
        not pb2_file.exists()
        or not pb2_grpc_file.exists()
        or pb2_file.stat().st_mtime < proto_path.stat().st_mtime
        or pb2_grpc_file.stat().st_mtime < proto_path.stat().st_mtime
    ):
        try:
            from grpc_tools import protoc
        except ImportError as exc:
            raise RuntimeError(
                "Missing grpcio-tools. Install with: pip install grpcio grpcio-tools"
            ) from exc

        args = [
            "grpc_tools.protoc",
            f"-I{proto_path.parent}",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            str(proto_path),
        ]
        if protoc.main(args) != 0:
            raise RuntimeError(f"Failed to compile proto: {proto_path}")

    if str(out_dir) not in sys.path:
        sys.path.insert(0, str(out_dir))

    pb2 = importlib.import_module(f"{proto_path.stem}_pb2")
    pb2_grpc = importlib.import_module(f"{proto_path.stem}_pb2_grpc")
    return pb2, pb2_grpc
