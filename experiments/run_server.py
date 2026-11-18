"""Start a throwaway Campus Nexus server for experiments.

Uses a fresh database, shared root and data directory under experiments/.runs/<id>
so experiments never touch real data. Prints the root path (needed by
e3_burst.py --live) and serves on 0.0.0.0:<port>.

  python experiments/run_server.py            # port 8000
  python experiments/run_server.py --port 8100
  python experiments/run_server.py --keep-dir experiments/.runs/my-run   # reuse a run dir
"""

import argparse
import os
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--keep-dir", help="run directory to (re)use instead of a new one")
    args = ap.parse_args()

    run = Path(args.keep_dir) if args.keep_dir else HERE / ".runs" / uuid.uuid4().hex[:8]
    run = run.resolve()
    (run / "root").mkdir(parents=True, exist_ok=True)
    os.environ.update(
        DATABASE_URL=f"sqlite:///{(run / 'exp.db').as_posix()}",
        SHARED_FOLDER=str(run / "root"),
        DATA_DIR=str(run / "data"),
        SECRET_KEY=os.environ.get("SECRET_KEY", "experiments-only-secret-key-0123456789abcdef"),
        ADMIN_USERNAMES="exp_admin",
    )
    backend = HERE.parent / "backend"
    sys.path.insert(0, str(backend))
    os.chdir(backend)
    print(f"[run_server] run dir:     {run}")
    print(f"[run_server] shared root: {run / 'root'}")
    print(f"[run_server] url:         http://127.0.0.1:{args.port}")
    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
