"""E2 -- Conflict detection on scripted edit scenarios (paper Table II).

Simulates U users, each with D devices, editing one shared file through the
real server API. Before each edit a device either syncs (its base becomes the
latest version) with probability --p-sync, or edits from the stale copy it has.

Ground truth: an upload truly conflicts iff the device did not have the latest
version when it started editing (someone committed after its base).

Compared methods:
  * last-writer baseline: flags a conflict whenever the uploader differs from
    the author of the previous version (what the old code did);
  * base version (ours): the server's own `conflict` flag from /sync/complete.

  python experiments/e2_conflicts.py --users 4 --devices 2 --steps 200 --repeats 5
"""

import argparse
import random
import tempfile
import time
import uuid

import httpx
from pathlib import Path

from common import client, ensure_user, fmt, mean_std, upload_file, write_results


def run_once(c, headers_by_user, users, devices, steps, p_sync, seed, admin=None, pace=0.0):
    rnd = random.Random(seed)
    rel = f"e2/{uuid.uuid4().hex[:8]}.txt"
    counts = {"uploads": 0, "true_conflicts": 0,
              "baseline_false": 0, "baseline_missed": 0, "ours_false": 0, "ours_missed": 0,
              "sync_pauses": 0}
    latest = 0
    prev_author = None
    base = {}                     # (user, device) -> version its copy is based on
    with tempfile.TemporaryDirectory() as tmp:
        for step in range(steps):
            u = rnd.choice(users)
            d = rnd.randrange(devices)
            key = (u, d)
            if key not in base or rnd.random() < p_sync:
                base[key] = latest                      # device pulls the latest copy
            p = Path(tmp) / "doc.txt"
            p.write_text(f"{u}/{d} step {step} {uuid.uuid4().hex}\n")
            truth = latest > 0 and base[key] != latest
            baseline = prev_author is not None and prev_author != u
            for attempt in range(3):
                try:
                    res = upload_file(c, headers_by_user[u], rel, p,
                                      base_version=base[key], dedup=False)["result"]
                    break
                except httpx.HTTPStatusError as e:
                    # rapid scripted uploads can trip the burst detector (it pauses sync);
                    # an admin resumes and the upload is retried -- counted in sync_pauses
                    if e.response.status_code != 423 or admin is None:
                        raise
                    counts["sync_pauses"] += 1
                    c.post("/api/sync/resume-scheduler", headers=admin)
            if pace:
                time.sleep(pace)
            ours = bool(res.get("conflict"))
            latest = res["version"]
            base[key] = latest                          # the device now holds its own new version
            prev_author = u
            counts["uploads"] += 1
            counts["true_conflicts"] += int(truth)
            counts["baseline_false"] += int(baseline and not truth)
            counts["baseline_missed"] += int(truth and not baseline)
            counts["ours_false"] += int(ours and not truth)
            counts["ours_missed"] += int(truth and not ours)
    c.delete("/api/files/item", params={"path": rel}, headers=headers_by_user[users[0]])
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=4)
    ap.add_argument("--devices", type=int, default=2, help="devices per user")
    ap.add_argument("--steps", type=int, default=200, help="uploads per run")
    ap.add_argument("--p-sync", type=float, default=0.7, help="chance a device syncs before editing")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--pace", type=float, default=0.1, help="seconds between uploads")
    args = ap.parse_args()

    c = client()
    users = [f"exp_e2_u{i}" for i in range(args.users)]
    headers = {u: ensure_user(c, u) for u in users}
    admin = ensure_user(c, "exp_admin")      # run_server.py makes exp_admin an admin
    c.post("/api/sync/resume-scheduler", headers=admin)
    runs = []
    for r in range(args.repeats):
        cnt = run_once(c, headers, users, args.devices, args.steps, args.p_sync, seed=1000 + r,
                       admin=admin, pace=args.pace)
        runs.append(cnt)
        print(f"run {r + 1}: {cnt}")
    def rate(key):
        return mean_std([x[key] / x["uploads"] * 100 for x in runs])
    rows = [
        f"Last-writer baseline & {fmt(*rate('baseline_false'), 1)}\\% & {fmt(*rate('baseline_missed'), 1)}\\% \\\\",
        f"Base version (ours) & {fmt(*rate('ours_false'), 1)}\\% & {fmt(*rate('ours_missed'), 1)}\\% \\\\",
    ]
    true_rate = mean_std([x["true_conflicts"] / x["uploads"] * 100 for x in runs])
    write_results("e2_conflicts", {"params": vars(args), "runs": runs,
                                   "true_conflict_rate_pct": true_rate}, rows,
                  note=(f"Rates are % of uploads; {args.users} users x {args.devices} devices, "
                        f"{args.steps} uploads/run, p_sync={args.p_sync}; true conflict rate "
                        f"{true_rate[0]:.1f}%"))


if __name__ == "__main__":
    main()
