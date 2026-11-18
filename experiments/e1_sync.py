"""E1 -- Upload throughput and deduplication savings (paper Table I).

For each file size and repeat:
  1. upload a new random file (full upload)           -> time, throughput
  2. overwrite a small region in the middle, re-upload -> bytes actually sent
"Saved on edit" = 1 - bytes_sent / file_size for step 2.

Run the server first (python experiments/run_server.py), ideally on the campus
server with this script on a client machine, then:
  python experiments/e1_sync.py --sizes 1 10 100 500 --repeats 5
"""

import argparse
import os
import tempfile
import uuid
from pathlib import Path

from common import client, ensure_user, fmt, mean_std, upload_file, write_results


def make_file(path: Path, size_mb: float):
    remaining = int(size_mb * 1024 * 1024)
    with open(path, "wb") as f:
        while remaining > 0:
            n = min(remaining, 4 << 20)
            f.write(os.urandom(n))
            remaining -= n


def edit_middle(path: Path, nbytes: int = 4096):
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.seek(max(0, size // 2 - nbytes // 2))
        f.write(os.urandom(min(nbytes, size)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=float, nargs="+", default=[1, 10, 100, 500], help="file sizes in MB")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--user", default="exp_sync")
    args = ap.parse_args()

    c = client()
    h = ensure_user(c, args.user)
    rows, raw = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        for size in args.sizes:
            full_t, full_tp, saved, edit_t = [], [], [], []
            for r in range(args.repeats):
                p = Path(tmp) / f"f_{size}_{r}.bin"
                make_file(p, size)
                rel = f"e1/{uuid.uuid4().hex[:8]}_{size}MB.bin"
                up = upload_file(c, h, rel, p)
                full_t.append(up["seconds"])
                full_tp.append(up["bytes_total"] / up["seconds"] / 1e6)
                edit_middle(p)
                ed = upload_file(c, h, rel, p)
                edit_t.append(ed["seconds"])
                saved.append(1 - ed["bytes_sent"] / ed["bytes_total"])
                c.delete("/api/files/item", params={"path": "e1"}, headers=h)  # keep disk tidy
                p.unlink()
                print(f"{size:>6} MB  run {r + 1}/{args.repeats}: {up['seconds']:.2f}s "
                      f"({full_tp[-1]:.1f} MB/s), edit re-upload sent {ed['chunks_sent']}/{ed['chunks_total']} chunks")
            raw[str(size)] = {"full_seconds": full_t, "throughput_MBps": full_tp,
                              "edit_seconds": edit_t, "saved_fraction": saved}
            (tm, ts), (pm, ps), (sm, ss) = mean_std(full_t), mean_std(full_tp), mean_std(saved)
            label = f"{size:g} MB"
            rows.append(f"{label} & {fmt(tm, ts)} & {fmt(pm, ps, 1)} & {fmt(sm * 100, ss * 100, 1)}\\% \\\\")
    write_results("e1_sync", {"sizes_mb": args.sizes, "repeats": args.repeats, "raw": raw}, rows,
                  note="Columns: size & upload time (s) & throughput (MB/s) & saved on edit (%)")


if __name__ == "__main__":
    main()
