#!/usr/bin/env python3
from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT_DIR / "frontend" / "dashboard"


def _check_python_dependencies() -> None:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ModuleNotFoundError as exc:
        missing = str(exc).split("'")[1] if "'" in str(exc) else str(exc)
        raise RuntimeError(
            f"Missing Python dependency: {missing}. "
            "Install Python dependencies first (e.g. `pip install -r requirements.txt`)."
        ) from exc


def _ensure_node_modules() -> None:
    if not shutil.which("npm"):
        raise RuntimeError("`npm` not found in PATH. Please install Node.js and npm first.")
    if (FRONTEND_DIR / "node_modules").exists():
        return
    print("[setup] frontend dependencies not found, running `npm install` ...", flush=True)
    result = subprocess.run(
        ["npm", "install"],
        cwd=str(FRONTEND_DIR),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("`npm install` failed. Please fix frontend dependencies and retry.")


def _stream_output(process: subprocess.Popen[str], prefix: str) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        print(f"[{prefix}] {line.rstrip()}", flush=True)


def _terminate_process(process: subprocess.Popen[str], name: str) -> None:
    if process.poll() is not None:
        return
    print(f"[shutdown] stopping {name} ...", flush=True)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print(f"[shutdown] force kill {name}", flush=True)
        process.kill()
        process.wait(timeout=2)


def main() -> int:
    try:
        _check_python_dependencies()
        _ensure_node_modules()
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    backend_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "common.webapi.dashboard_api:app",
        "--reload",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    frontend_cmd = ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"]

    print("[dev] starting backend + frontend ...", flush=True)
    print("[dev] dashboard: http://127.0.0.1:5173", flush=True)
    print("[dev] api:       http://127.0.0.1:8000", flush=True)

    backend = subprocess.Popen(
        backend_cmd,
        cwd=str(ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    frontend = subprocess.Popen(
        frontend_cmd,
        cwd=str(FRONTEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    backend_thread = threading.Thread(target=_stream_output, args=(backend, "backend"), daemon=True)
    frontend_thread = threading.Thread(target=_stream_output, args=(frontend, "frontend"), daemon=True)
    backend_thread.start()
    frontend_thread.start()

    exit_code = 0
    try:
        while True:
            backend_code = backend.poll()
            frontend_code = frontend.poll()
            if backend_code is not None or frontend_code is not None:
                if backend_code not in (None, 0):
                    print(f"[error] backend exited with code {backend_code}", file=sys.stderr)
                    exit_code = backend_code
                if frontend_code not in (None, 0):
                    print(f"[error] frontend exited with code {frontend_code}", file=sys.stderr)
                    exit_code = frontend_code
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[dev] Ctrl+C received, shutting down ...", flush=True)
    finally:
        _terminate_process(frontend, "frontend")
        _terminate_process(backend, "backend")

    return int(exit_code or 0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
