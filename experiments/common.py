"""Shared helpers for the Campus Nexus experiments (paper Section V).

Every experiment writes two files into experiments/results/:
  <name>.json        raw measurements + summary (keep these with the paper)
  <name>_table.tex   LaTeX rows to paste into the matching table

Results produced from synthetic data are marked SYNTHETIC in both files and
must not be reported as findings.
"""

import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"
RESULTS = HERE / "results"
SERVER = os.environ.get("NEXUS_SERVER", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("NEXUS_EXP_PASSWORD", "exp-pass-123")
CHUNK = 1 << 20


# --------------------------------------------------------------------------- backend import
def use_backend(data_dir: str | None = None, database_url: str | None = None):
    """Import the backend package in-process (offline experiments).

    Point DATA_DIR / DATABASE_URL somewhere harmless *before* importing so the
    experiment never touches the real deployment's files.
    """
    if data_dir:
        os.environ["DATA_DIR"] = data_dir
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("SECRET_KEY", "experiments-only-secret-key-0123456789abcdef")
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))


# --------------------------------------------------------------------------- HTTP helpers
def client() -> httpx.Client:
    return httpx.Client(base_url=SERVER, timeout=300)


def ensure_user(c: httpx.Client, username: str, password: str = PASSWORD) -> dict:
    """Register (if needed) and log in; returns auth headers."""
    c.post("/api/auth/register", json={"username": username, "email": f"{username}@exp.local",
                                       "password": password})
    r = c.post("/api/auth/login", data={"username": username, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def upload_file(c: httpx.Client, headers: dict, rel: str, path: Path,
                base_version: int | None = None, dedup: bool = True) -> dict:
    """Upload like the real clients do: manifest, changed chunks, complete.

    Returns timing and byte counts so experiments can measure throughput and
    deduplication. Time covers the whole protocol (manifest .. complete).
    """
    size = path.stat().st_size
    total = max(1, math.ceil(size / CHUNK))
    t0 = time.perf_counter()
    existing: list[str] = []
    if dedup:
        m = c.get("/api/sync/manifest", params={"path": rel}, headers=headers)
        m.raise_for_status()
        man = m.json()
        if man.get("exists"):
            existing = [ch["hash"] for ch in man["chunks"]]
        if base_version is None and man.get("version"):
            base_version = man["version"]
    upload_id = uuid.uuid4().hex
    sent = chunks_sent = 0
    with open(path, "rb") as f:
        for i in range(total):
            piece = f.read(CHUNK)
            if i < len(existing) and existing[i] == hashlib.sha256(piece).hexdigest():
                continue
            r = c.post("/api/sync/chunk", params={"path": rel, "index": i, "total": total,
                                                  "upload_id": upload_id},
                       files={"file": ("chunk", piece, "application/octet-stream")}, headers=headers)
            r.raise_for_status()
            sent += len(piece)
            chunks_sent += 1
    body = {"size": size}
    if base_version is not None:
        body["base_version"] = base_version
    r = c.post("/api/sync/complete", params={"path": rel, "upload_id": upload_id}, json=body,
               headers=headers)
    r.raise_for_status()
    return {"result": r.json(), "seconds": time.perf_counter() - t0, "bytes_total": size,
            "bytes_sent": sent, "chunks_total": total, "chunks_sent": chunks_sent}


# --------------------------------------------------------------------------- stats + output
def mean_std(xs: list[float]) -> tuple[float, float]:
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not xs:
        return float("nan"), float("nan")
    return statistics.fmean(xs), (statistics.stdev(xs) if len(xs) > 1 else 0.0)


def percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt(m: float, s: float | None = None, digits: int = 2) -> str:
    if m is None or (isinstance(m, float) and math.isnan(m)):
        return "--"
    if s is None:
        return f"{m:.{digits}f}"
    return f"${m:.{digits}f} \\pm {s:.{digits}f}$"


def environment() -> dict:
    return {"python": sys.version.split()[0], "platform": platform.platform(),
            "processor": platform.processor(), "server": SERVER,
            "when": datetime.now(timezone.utc).isoformat()}


def write_results(name: str, data: dict, tex_rows: list[str], synthetic: bool = False,
                  note: str = "") -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    data = {"experiment": name, "synthetic": synthetic, "note": note, "environment": environment(), **data}
    out = RESULTS / f"{name}.json"
    out.write_text(json.dumps(data, indent=2, default=str))
    header = [f"% {name} -- generated {data['environment']['when']}"]
    if synthetic:
        header.append("% SYNTHETIC DATA -- may only be reported as a clearly labelled simulation, never as measurements")
    if note:
        header.append(f"% {note}")
    (RESULTS / f"{name}_table.tex").write_text("\n".join(header + tex_rows) + "\n")
    print(f"\nwrote {out}")
    print(f"wrote {RESULTS / (name + '_table.tex')}")
    if synthetic:
        print("NOTE: synthetic data -- report only as a clearly labelled simulation.")
    return out
