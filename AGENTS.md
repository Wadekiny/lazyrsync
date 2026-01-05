# Repository Guidelines

## Project Structure & Module Organization
- `lazysync-server/`: Rust TCP server that returns filesystem snapshots.
- `lazysync-client/`: Rust client + optional Python bindings (PyO3) and HTTP API.
- `lazysync-python/`: Python TUI app, backend helpers, and frontend widgets.
- Root scripts: `test_server_response.py` for quick server/client checks.

## Build, Test, and Development Commands
- `cd lazysync-server && cargo build --release`: build the TCP server.
- `cd lazysync-server && cargo run --release`: run the server on `127.0.0.1:9000`.
- `cd lazysync-client && cargo build --release`: build the Rust client.
- `cd lazysync-client && cargo run --release`: run the client + HTTP API on `127.0.0.1:8080`.
- `cd lazysync-python && python main.py`: run the Python TUI.
- `python test_server_response.py`: basic server response smoke test (run with server up).

## Coding Style & Naming Conventions
- Rust: keep idiomatic Rust style (rustfmt defaults, 4-space indents).
- Python: 4-space indents, snake_case for functions/modules, CamelCase for classes.
- Paths and modules follow feature folders: `backend/`, `frontend/`, `models/`, `utils/`.

## Testing Guidelines
- Tests are currently script-style, not a framework.
- `lazysync-python/test_ssh_file_manager.py` exercises SSH flows; run with args or env:
  `SSH_HOST=... SSH_PORT=22 SSH_USER=... python test_ssh_file_manager.py`.
- Name new tests with `test_*.py` and keep them runnable as scripts.

## Commit & Pull Request Guidelines
- History is minimal; no formal convention. Use short, scoped messages like `server: handle symlinks`.
- PRs should include: summary, testing performed (commands + results), and any config changes.
- If UI behavior changes in `lazysync-python/frontend/`, include a short note or screenshot.

## Security & Configuration Tips
- Default network endpoints are `127.0.0.1:9000` (server) and `127.0.0.1:8080` (client).
- Keep host/user/port data out of source; prefer environment variables for local testing.


# NOTE
- 工作完毕后，使用中文回答
