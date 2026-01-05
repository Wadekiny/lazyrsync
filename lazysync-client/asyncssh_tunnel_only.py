#!/usr/bin/env python3
"""
Use asyncssh to create a local port forward and keep rfb_client traffic unblocked.
"""

import asyncio
import getpass
import os
import socket
import sys
import time
from multiprocessing import Pipe, Process

import rfb_client

try:
    import asyncssh
except ImportError:
    print("Error: asyncssh module not found.")
    print("Please install it first:")
    print("  pip install asyncssh")
    sys.exit(1)

# SSH server info
SSH_HOST = "116.172.93.227"
SSH_PORT = 29293
SSH_USER = "ubuntu"

# Optional auth
SSH_PASSWORD = None  # can also set LAZYSYNC_SSH_PASSWORD
SSH_KEY_PATH = None  # can also set LAZYSYNC_SSH_KEY_PATH

# Port forwarding config (local -> remote)
LOCAL_FORWARD_HOST = "127.0.0.1"
LOCAL_FORWARD_PORT = 9000
REMOTE_HOST = "127.0.0.1"
REMOTE_PORT = 9000


class IPCChannel:
    """IPC 通道封装，提供基础的连接与收发接口。"""

    def __init__(self):
        self._parent_conn, self._child_conn = Pipe()
        print("IPC: 已创建 Pipe 通道")

    def parent(self):
        return self._parent_conn

    def child(self):
        return self._child_conn

    def send(self, conn, payload):
        print(f"IPC: 发送消息 {payload.get('type')}")
        conn.send(payload)

    def recv(self, conn):
        message = conn.recv()
        print(f"IPC: 接收消息 {message.get('type')}")
        return message


class SSHWorker:
    """子进程内部运行的 asyncssh 工作器。"""

    def __init__(self, pipe):
        self._pipe = pipe
        self._conn = None
        self._listener = None
        self._loop = None
        self._overrides = {}
        print("子进程: SSHWorker 初始化完成")

    @staticmethod
    def _build_connect_kwargs(overrides=None, allow_prompt=True):
        overrides = overrides or {}
        connect_kwargs = {
            "host": SSH_HOST,
            "port": SSH_PORT,
            "username": SSH_USER,
        }
        env_password = os.getenv("LAZYSYNC_SSH_PASSWORD")
        env_key_path = os.getenv("LAZYSYNC_SSH_KEY_PATH")

        if overrides.get("password"):
            connect_kwargs["password"] = overrides["password"]
        elif env_password:
            connect_kwargs["password"] = env_password
        elif SSH_PASSWORD:
            connect_kwargs["password"] = SSH_PASSWORD

        if overrides.get("key_path"):
            connect_kwargs["client_keys"] = [overrides["key_path"]]
        elif env_key_path:
            connect_kwargs["client_keys"] = [env_key_path]
        elif SSH_KEY_PATH:
            connect_kwargs["client_keys"] = [SSH_KEY_PATH]

        # 仅在允许提示时询问密码，否则依赖密钥/agent/默认配置。
        if "password" not in connect_kwargs and "client_keys" not in connect_kwargs:
            if allow_prompt and sys.stdin.isatty():
                typed = getpass.getpass(
                    "未设置SSH_PASSWORD或SSH_KEY_PATH，如需密码登录请输入（留空则使用默认密钥/agent）: "
                )
                if typed:
                    connect_kwargs["password"] = typed
            elif allow_prompt:
                print("未设置SSH_PASSWORD或SSH_KEY_PATH，且当前无交互输入；将尝试默认密钥/agent。")

        connect_kwargs.setdefault("keepalive_interval", 30)
        connect_kwargs.setdefault("keepalive_count_max", 3)

        return connect_kwargs

    @staticmethod
    def _is_auth_error(exc):
        # 兼容不同 asyncssh 版本的鉴权错误类型/消息。
        auth_error_types = tuple(
            err
            for err in (
                getattr(asyncssh, "PermissionDenied", None),
                getattr(asyncssh, "AuthenticationFailed", None),
                getattr(asyncssh, "AuthError", None),
            )
            if err is not None
        )
        if auth_error_types and isinstance(exc, auth_error_types):
            return True
        message = str(exc).lower()
        return "permission denied" in message or "authentication" in message

    async def _recv(self):
        return await self._loop.run_in_executor(None, self._pipe.recv)

    async def _connect(self):
        # 子进程侧：先不提示尝试连接，若需要密码则向父进程请求。
        print("子进程: 开始建立 SSH 连接")
        attempts = 0
        while True:
            attempts += 1
            try:
                connect_kwargs = self._build_connect_kwargs(
                    overrides=self._overrides,
                    allow_prompt=False,
                )
                self._conn = await asyncssh.connect(**connect_kwargs)
                print("子进程: SSH 连接成功")
                return
            except asyncssh.Error as exc:
                if not self._is_auth_error(exc):
                    raise
                self._pipe.send({"type": "auth_required"})
                print("子进程: 请求主进程输入密码")
                response = await self._recv()
                password = None if response is None else response.get("password")
                if not password:
                    raise
                self._overrides["password"] = password
                if attempts >= 3:
                    raise

    async def _forward(self, message):
        if not self._conn:
            self._pipe.send({"type": "error", "error": "SSH not connected"})
            return
        try:
            # 已有监听器时先关闭再重建。
            if self._listener:
                self._listener.close()
                await self._listener.wait_closed()
            self._listener = await self._conn.forward_local_port(
                message["local_host"],
                message["local_port"],
                message["remote_host"],
                message["remote_port"],
            )
            print(
                "子进程: 端口转发建立 "
                f"{message['local_host']}:{message['local_port']} -> "
                f"{message['remote_host']}:{message['remote_port']}"
            )
            self._pipe.send({"type": "ok"})
        except Exception as exc:
            self._pipe.send({"type": "error", "error": str(exc)})

    async def _run(self, command):
        if not self._conn:
            self._pipe.send({"type": "error", "error": "SSH not connected"})
            return
        try:
            result = await self._conn.run(command)
            print(f"子进程: 执行远程命令 {command}")
            self._pipe.send(
                {
                    "type": "ok",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_status": result.exit_status,
                }
            )
        except Exception as exc:
            self._pipe.send({"type": "error", "error": str(exc)})

    async def _get_home(self):
        if not self._conn:
            self._pipe.send({"type": "error", "error": "SSH not connected"})
            return
        try:
            result = await self._conn.run("echo $HOME")
            print("子进程: 获取远程 HOME")
            self._pipe.send({"type": "ok", "stdout": result.stdout.strip()})
        except Exception as exc:
            self._pipe.send({"type": "error", "error": str(exc)})

    async def _shutdown(self):
        print("子进程: 收到 shutdown，准备退出")
        if self._listener:
            self._listener.close()
            await self._listener.wait_closed()
        if self._conn:
            self._conn.close()
            await self._conn.wait_closed()
        self._pipe.send({"type": "ok"})

    async def serve(self):
        self._loop = asyncio.get_running_loop()
        print("子进程: 进入事件循环，等待 IPC 消息")
        while True:
            try:
                message = await self._recv()
            except EOFError:
                break

            msg_type = message.get("type")
            if msg_type == "connect":
                try:
                    await self._connect()
                    self._pipe.send({"type": "ok"})
                except Exception as exc:
                    self._pipe.send({"type": "error", "error": str(exc)})
            elif msg_type == "forward":
                await self._forward(message)
            elif msg_type == "run":
                await self._run(message["command"])
            elif msg_type == "get_home":
                await self._get_home()
            elif msg_type == "shutdown":
                await self._shutdown()
                break
            else:
                self._pipe.send({"type": "error", "error": f"Unknown message: {msg_type}"})

    @classmethod
    def process_main(cls, pipe):
        asyncio.run(cls(pipe).serve())


class SSHClient:
    """主进程同步控制器：通过 IPC 驱动子进程中的 asyncssh。"""

    def __init__(self, ipc=None):
        self._ipc = ipc or IPCChannel()
        self._parent_conn = self._ipc.parent()
        self._proc = Process(target=SSHWorker.process_main, args=(self._ipc.child(),))
        self._proc.daemon = True
        self._proc.start()
        print(f"主进程: SSH 子进程已启动 pid={self._proc.pid}")

    @staticmethod
    def wait_for_local_port(host, port, timeout_s=10.0):
        # 轮询检查本地端口转发是否就绪。
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

    @staticmethod
    def run_rfb_client(remote_path):
        # rfb_client 阻塞调用；端口转发在子进程中，不会影响主流程。
        client = rfb_client.PyClient(
            f"{LOCAL_FORWARD_HOST}:{LOCAL_FORWARD_PORT}",
            is_hash=True,
        )
        print(client.get_path(remote_path))

    def _request(self, payload):
        # 发送请求并等待最终响应（ok/error）。
        self._ipc.send(self._parent_conn, payload)
        return self._wait_response()

    def _wait_response(self):
        # 处理鉴权请求：由主进程提示用户输入密码。
        while True:
            message = self._ipc.recv(self._parent_conn)
            msg_type = message.get("type")
            if msg_type == "auth_required":
                if not sys.stdin.isatty():
                    raise RuntimeError("需要密码但当前无交互输入。")
                print("主进程: 收到鉴权请求，等待用户输入密码")
                typed = getpass.getpass(
                    "未设置SSH_PASSWORD或SSH_KEY_PATH，如需密码登录请输入（留空则使用默认密钥/agent）: "
                )
                self._parent_conn.send({"type": "auth_response", "password": typed})
                continue
            if msg_type == "ok":
                return message
            if msg_type == "error":
                raise RuntimeError(message.get("error", "unknown error"))
            raise RuntimeError(f"未知响应: {message}")

    def connect(self):
        # 在子进程中建立 SSH 连接。
        print("主进程: 请求子进程建立 SSH 连接")
        self._request({"type": "connect"})

    def forward_port(self, local_host, local_port, remote_host, remote_port):
        # 在子进程中启动/刷新本地端口转发。
        print("主进程: 请求子进程建立端口转发")
        self._request(
            {
                "type": "forward",
                "local_host": local_host,
                "local_port": local_port,
                "remote_host": remote_host,
                "remote_port": remote_port,
            }
        )

    def run(self, command):
        # 在子进程中执行远程命令。
        print(f"主进程: 请求子进程执行命令 {command}")
        response = self._request({"type": "run", "command": command})
        return response.get("stdout", ""), response.get("stderr", ""), response.get("exit_status", 0)

    def get_home(self):
        # 获取远程 HOME 目录。
        print("主进程: 请求子进程获取 HOME")
        response = self._request({"type": "get_home"})
        return response.get("stdout", "")

    def close(self):
        # 关闭子进程并清理 SSH/端口转发。
        if self._proc.is_alive():
            print("主进程: 请求子进程退出")
            self._request({"type": "shutdown"})
            self._proc.join(timeout=3)


if __name__ == "__main__":
    try:
        remote_path = os.getenv("LAZYSYNC_REMOTE_PATH", "/")
        print(f"主进程: 远程路径 {remote_path}")
        ssh = SSHClient()
        try:
            ssh.connect()
            ssh.forward_port(
                LOCAL_FORWARD_HOST,
                LOCAL_FORWARD_PORT,
                REMOTE_HOST,
                REMOTE_PORT,
            )
            if not SSHClient.wait_for_local_port(
                LOCAL_FORWARD_HOST,
                LOCAL_FORWARD_PORT,
                timeout_s=10.0,
            ):
                raise RuntimeError("本地端口转发未就绪，请检查SSH连接与端口配置。")
            print("主进程: 端口转发已就绪，开始运行 rfb_client")
            SSHClient.run_rfb_client(remote_path)
        finally:
            ssh.close()
    except (OSError, RuntimeError, asyncssh.Error) as exc:
        print(f"执行失败: {exc}")
        sys.exit(1)
