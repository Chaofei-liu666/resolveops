"""Frozen entry point for the portable ResolveOps desktop runtime.

One executable is started twice by Electron: once for the local FastAPI server
and once for the durable worker.  It deliberately has no Docker dependency.
"""
from __future__ import annotations

import argparse
import time


def run_api(port: int) -> None:
    import uvicorn
    from production.main import app

    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')


def run_worker() -> None:
    from production import worker

    worker.ensure_schema()
    while True:
        if not worker.once():
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('api', 'worker'))
    parser.add_argument('--port', type=int, default=8090)
    args = parser.parse_args()
    if args.mode == 'api':
        run_api(args.port)
    elif args.mode == 'worker':
        run_worker()
    else:
        run_worker()


if __name__ == '__main__':
    main()
