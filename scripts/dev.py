"""Start the local ResolveOps API and Worker without Docker.

The launcher intentionally uses the same Python interpreter that invoked it,
so a developer can work inside a virtual environment. Press Ctrl+C once to
stop both child processes cleanly.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def start(command: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(command, cwd=PROJECT_ROOT, env=env)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    env = os.environ.copy()
    env.setdefault('DATABASE_URL', 'sqlite:///./data/resolveops.db')
    env.setdefault('RESOLVEOPS_RUNTIME_CONFIG_PATH', str(PROJECT_ROOT / '.resolveops-runtime' / 'connections.json'))
    env.setdefault('PYTHONPATH', str(PROJECT_ROOT))

    print('Starting ResolveOps local development runtime...')
    print('Workbench: http://127.0.0.1:8090/')
    print('API docs:  http://127.0.0.1:8090/docs')
    print('Press Ctrl+C to stop API and Worker.')

    api = start([sys.executable, '-m', 'uvicorn', 'production.main:app', '--host', '127.0.0.1', '--port', '8090'], env)
    worker = start([sys.executable, '-m', 'production.worker'], env)
    processes = (api, worker)
    try:
        while True:
            exited = next((process for process in processes if process.poll() is not None), None)
            if exited is not None:
                print(f'A local runtime process exited with code {exited.returncode}.', file=sys.stderr)
                return exited.returncode or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print('\nStopping ResolveOps local development runtime...')
        return 0
    finally:
        for process in processes:
            stop(process)


if __name__ == '__main__':
    raise SystemExit(main())
