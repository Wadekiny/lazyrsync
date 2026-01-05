# LazySync Full Client (Python)

本文档说明 `lazysync-client/lazysync_full_client.py` 的类、函数与整体工作流程。
该模块提供完整客户端栈：

- OpenSSH 子进程长连接（端口转发）
- askpass IPC 服务用于密码输入
- LazySync gRPC API 客户端

## 入口

- `main()`：从环境变量加载配置，启动控制器，调用健康检查与 `GetPath`，最后关闭。
- `__main__`：运行 `asyncio.run(main())` 并输出错误信息。

## 配置

### `SSHConfig`
用于存放 SSH 与端口转发相关配置的 dataclass：

- `host`, `port`, `user`, `key_path`：OpenSSH 连接信息
- `local_host`, `local_port`：本地转发绑定
- `remote_host`, `remote_port`：远端目标（Rust gRPC server）
- `askpass_socket`：askpass IPC socket 路径（Unix domain socket）

### `load_config_from_env() -> SSHConfig`
从环境变量加载配置（带默认值）：

- `LAZYSYNC_SSH_HOST`, `LAZYSYNC_SSH_PORT`, `LAZYSYNC_SSH_USER`
- `LAZYSYNC_SSH_KEY_PATH`
- `LAZYSYNC_LOCAL_HOST`, `LAZYSYNC_LOCAL_PORT`
- `LAZYSYNC_REMOTE_HOST`, `LAZYSYNC_REMOTE_PORT`
- `LAZYSYNC_ASKPASS_SOCKET`

## SSH 转发层（子进程阻塞模型）

### `SSHForwardWorker`
在子进程中运行，负责 OpenSSH 生命周期。

- `_build_ssh_cmd()`：构造 `ssh -N -L ...` 命令
- `run()`：设置 `SSH_ASKPASS` 等环境变量，启动 OpenSSH，并通过 Pipe 汇报状态
- `process_main(...)`：`multiprocessing.Process` 的入口

### `SSHForwardManager`
父进程控制器，用于管理子进程。

- `__init__(config)`：保存配置
- `start()`：启动子进程
- `wait_started(timeout_s=5.0)`：等待子进程启动或失败
- `close()`：发送关闭请求并等待退出

### `wait_for_local_port(host, port, timeout_s=10.0) -> bool`
轮询检查本地端口转发是否就绪。

## gRPC 客户端层

### `_load_lazysync_proto()`
通过 `grpc_utils.ensure_grpc_codegen` 生成/加载 gRPC 代码。

### `LazySyncGrpcClient`
基于 `grpc.aio` 的异步 gRPC 客户端。

- `connect()`：建立 gRPC 连接
- `close()`：关闭连接
- `health() -> str`
- `get_path(path)`
- `stat(path)`
- `read_file(path, offset=0, length=0) -> AsyncIterator[bytes]`
- `write_file(path, chunks, offset=0) -> int`

## 高层控制器

### `LazySyncController`
协调 askpass、SSH 转发、gRPC 客户端。

- `start()`：启动 askpass IPC 服务 → 启动 SSH 子进程 → 等待端口就绪 → 连接 gRPC
- `stop()`：关闭 gRPC → 关闭 SSH → 关闭 askpass 服务
- `grpc` 属性：返回已连接的 `LazySyncGrpcClient`

## 工作流程概览

1. `load_config_from_env()` 构建 `SSHConfig`
2. `LazySyncController.start()`：
   - 启动 askpass gRPC 服务
   - 启动 OpenSSH 子进程
   - 等待本地端口转发就绪
   - 连接 gRPC
3. 通过 `controller.grpc` 调用 gRPC 接口
4. `LazySyncController.stop()` 反向清理

## 最小使用示例

```python
import asyncio
from lazysync_full_client import LazySyncController, load_config_from_env


async def demo():
    controller = LazySyncController(load_config_from_env())
    await controller.start()
    try:
        status = await controller.grpc.health()
        print("health:", status)
    finally:
        await controller.stop()


asyncio.run(demo())
```
