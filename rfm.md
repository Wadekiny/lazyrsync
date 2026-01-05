下面是一份**工程级、可执行、对齐 VS Code Remote-SSH 思路的「框架说明」**。
它不是代码清单，而是**架构说明 + 职责边界 + 生命周期**，可以直接作为你项目的 `ARCHITECTURE.md` 初稿。

---

# 远程文件服务框架说明

## 1. 目标与设计原则

### 目标

* 使用 **Rust** 编写远程服务（agent），通过 **gRPC** 提供文件相关能力
* 使用 **Python** 编写客户端，负责：

  * 调用 gRPC 函数
  * 处理首次连接的 bootstrap
  * 管理 SSH 长连接与端口转发
* 支持 **免密 SSH key 优先**，在需要交互时通过 **askpass + gRPC** 向用户请求输入
* 保证 **事件循环不被阻塞**，SSH 端口转发稳定可靠

### 设计原则

1. **职责分离**

   * SSH ≠ gRPC
   * bootstrap ≠ 业务调用
2. **系统级组件不进入事件循环**

   * OpenSSH 必须运行在子进程
3. **最小信任边界**

   * Rust server 不提供任意命令执行
4. **与 VS Code Remote-SSH 对齐**

   * 使用系统 OpenSSH
   * 使用 SSH_ASKPASS
   * 使用端口转发而非暴露端口

---

## 2. 总体架构

```
┌─────────────────────────────────────┐
│             Python Client           │
│                                     │
│  ┌──────────────┐   ┌────────────┐ │
│  │  gRPC API    │   │ Bootstrap  │ │
│  │  (async)     │   │ (SSH mgr)  │ │
│  └──────▲───────┘   └──────▲─────┘ │
│         │                    │     │
│         │ gRPC                │ ssh │
│         │                    │     │
│  ┌──────┴────────┐    ┌──────┴────┐│
│  │  ask_pass     │◀───│ OpenSSH   ││
│  │ (Python gRPC) │    │ (process) ││
│  └───────────────┘    └───────────┘│
└──────────────▲─────────────────────┘
               │ localhost:forward
┌──────────────┴─────────────────────┐
│           Remote Machine            │
│                                     │
│     ┌────────────────────────┐     │
│     │      Rust Server       │     │
│     │   (gRPC, async)        │     │
│     │   listen 127.0.0.1     │     │
│     └────────────────────────┘     │
└─────────────────────────────────────┘
```

---

## 3. 组件职责划分

### 3.1 Rust Server（远程）

**职责**

* 监听本地端口（如 `127.0.0.1:9000`）
* 提供 gRPC 接口
* 处理文件系统相关请求

**不负责**

* SSH
* 用户交互
* 自我安装
* 任意命令执行

**示例接口**

* `Health()`
* `GetPath(path)`
* `Stat(path)`
* `ReadFile(path, offset, length) -> stream`
* `WriteFile(stream)`

**GetPath 返回路径的逻辑（当前实现）**

* 输入 `path`
* 计算并返回以下目录的条目列表（按需可裁剪）：
  * `path` 的父目录（若存在）
  * `path` 本身（若是目录或指向目录的 symlink）
  * `path` 的子目录（仅当前目录的一层子目录，包含 symlink 目标为目录的项）
* 每个目录的返回结构为：
  * `absolute_path`（规范化后的绝对路径, 如果包含链接，保持连接名在路径中，比如/home/ubuntu/.config, 其中.condig指向dotfiles, 返回路径应该是.config而不是dotfiles
  * `entries[]`（文件信息：name/type/permissions/absolute_path/modified/size）

---

### 3.2 Python Client（本地）

Python 客户端包含 **三个逻辑子模块**：

---

#### A. gRPC 调用模块（async）

**职责**

* 调用 Rust server 的 gRPC 接口
* 处理返回结果
* 不关心 SSH / 端口转发细节

**特性**

* 全异步
* 每个请求有 timeout
* 失败可重试

---

#### B. Bootstrap & SSH 管理模块（blocking）

**职责**

* 判断是否需要 SSH 连接
* 判断远程 server 是否存在
* 建立 SSH 长连接 + 端口转发
* 维持 SSH 生命周期

**关键约束**

* **OpenSSH 必须运行在子进程**
* 绝不进入 Python async event loop

**实现方式**

* `subprocess.Popen(["ssh", ...])`
* stdout / stderr 重定向或 drain
* 生命周期由父进程管理

---

#### C. ask_pass 模块（Python gRPC）

**职责**

* 接收 SSH askpass 请求
* 向用户展示交互提示
* 收集输入（密码 / passphrase）
* 返回给 askpass 调用方

**说明**

* ask_pass 本身是一个小程序
* 被 OpenSSH 通过 `SSH_ASKPASS` 调用
* 通过 gRPC 与 Python Client 主进程通信

---

## 4. SSH 相关设计

### 4.1 SSH 长连接与端口转发

* 使用系统 OpenSSH
* 示例命令（逻辑）：

```bash
ssh -N -T \
  -L local_port:127.0.0.1:remote_port \
  user@host
```

* Python 中通过 `subprocess.Popen`
* **必须在独立子进程**

---

### 4.2 SSH_ASKPASS 交互流程

```
OpenSSH
  └─ 需要认证
       └─ 调用 ask_pass 程序
            └─ ask_pass → gRPC → Python Client
                 └─ 用户输入
            └─ stdout 返回密码
```

**要点**

* 仅在 key 不可用时触发
* 适用于 password / 单步 keyboard-interactive
* 不支持多轮 MFA

---

## 5. 生命周期与控制流

### 5.1 启动流程

```
Python Client start
  ↓
用户选择：是否使用 SSH
  ↓
[如果是]
  ├─ 启动 SSH 子进程（端口转发）
  ├─ 等待本地端口就绪
  ↓
建立 gRPC Channel
  ↓
调用 Rust Server
```

---

### 5.2 错误处理

* SSH 子进程退出
  → 通知 gRPC 层连接失效
* gRPC 超时 / 失败
  → 不直接操作 SSH
* ask_pass 失败
  → 终止 SSH 建立

---

## 6. 并发与事件循环策略（关键）

| 组件          | 执行模型              |
| ----------- | ----------------- |
| OpenSSH     | **子进程（blocking）** |
| gRPC Client | async             |
| Rust Server | async             |
| ask_pass    | 短生命周期进程           |

**原则**

> **系统级连接永远不进入事件循环**

---

## 7. 用户可选行为

* 是否使用 SSH 长连接 + 端口转发
* 是否只在已建立连接时调用 gRPC
* 是否允许 ask_pass 交互

---

## 8. 安全边界

* Rust server 不暴露 shell
* 所有端口默认仅监听 localhost
* SSH 是唯一的远程执行通道
* 密码只存在于 ask_pass 生命周期内

---

## 9. 总结（核心结论）

> * gRPC 负责 **远程能力调用**
> * OpenSSH 负责 **安全连接与转发**
> * ask_pass 负责 **交互兜底**
> * 三者 **物理隔离、逻辑协作**

这套设计：

* 工程上可行
* 安全边界清晰
* 已被 VS Code Remote-SSH 证明
* 非常适合你当前的目标
