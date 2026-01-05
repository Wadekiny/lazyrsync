# LazySync Client

Rust客户端，通过TCP连接服务器获取文件系统快照，并提供HTTP接口供Python调用。

## 功能特性

1. **Cache管理**: 自动将接收到的数据保存到 `cache.json` 文件，格式为 `{"path": [files or dirs in path]}`
2. **HTTP接口**: 提供HTTP API接口，替代stdin输入，方便Python代码调用
3. **自动更新**: 如果路径已存在于cache中，会自动更新

## 构建

```bash
cargo build --release
```

## 运行

```bash
cargo run --release
```

或者运行编译后的二进制文件：

```bash
./target/release/rfb_client
```

程序会：
- 连接到 `127.0.0.1:9000` 的TCP服务器
- 启动HTTP服务器在 `http://127.0.0.1:8080`

## HTTP API

### POST /request

发送路径请求。

**请求体:**
```json
{
  "path": "/your/path"
}
```

**响应:**
```json
{
  "success": true,
  "message": "Request sent for path: /your/path"
}
```

**示例 (使用curl):**
```bash
curl -X POST http://127.0.0.1:8080/request \
  -H "Content-Type: application/json" \
  -d '{"path": "/home/user"}'
```

## Cache文件格式

Cache文件 `cache.json` 的格式如下：

```json
{
  "/path/to/dir": [
    {
      "name": "file1.txt",
      "is_dir": false,
      "size": 1024
    },
    {
      "name": "subdir",
      "is_dir": true,
      "size": 0
    }
  ],
  "/path/to/dir/subdir": [
    {
      "name": "file2.txt",
      "is_dir": false,
      "size": 2048
    }
  ]
}
```

## AsyncSSH 测试脚本

`asyncssh_tunnel_only.py` 用于验证 asyncssh 连接 + 端口转发 + rfb_client 是否可用，且不会被 rfb_client 的阻塞调用影响。

核心流程：
- 主进程通过 IPC 与子进程通信
- 子进程负责 asyncssh 连接与端口转发
- 若需要密码，子进程通知主进程读取并回传
- 主进程继续执行 rfb_client 调用

**运行示例:**
```bash
LAZYSYNC_REMOTE_PATH=/home/ubuntu python asyncssh_tunnel_only.py
```

可选环境变量：
- `LAZYSYNC_SSH_PASSWORD`: SSH 密码（省略则动态提示）
- `LAZYSYNC_SSH_KEY_PATH`: 指定密钥路径

## OpenSSH + gRPC 测试脚本

`openssh_grpc_client.py` 用于验证 OpenSSH 长连接 + 端口转发 + gRPC 调用 Rust server。

依赖：
- `grpcio`
- `grpcio-tools`

运行示例：
```bash
LAZYSYNC_REMOTE_PATH=/home/ubuntu python openssh_grpc_client.py
```

可选环境变量：
- `LAZYSYNC_SSH_HOST`/`LAZYSYNC_SSH_PORT`/`LAZYSYNC_SSH_USER`
- `LAZYSYNC_SSH_KEY_PATH`: 指定密钥路径
- `LAZYSYNC_LOCAL_PORT`/`LAZYSYNC_REMOTE_PORT`: 端口转发配置
- `LAZYSYNC_ASKPASS_ADDR`: askpass gRPC 服务监听地址

## 依赖

- `serde` / `serde_json`: JSON序列化/反序列化
- `tokio`: 异步运行时
- `axum`: HTTP服务器框架
- `tower` / `tower-http`: HTTP中间件
