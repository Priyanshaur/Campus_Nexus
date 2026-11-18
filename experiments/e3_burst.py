"""E3 -- Burst (ransomware-like mass change) detection (paper Table III).

OFFLINE MODE (default) replays file activity through three detectors:
  rule      hard rule only (>500 changes or >100 deletes per window)
  forest    Isolation Forest only (decision score < tau, no gate, no minimum size)
  ours      the deployed detector: rule OR (>=20 events AND forest score < tau
            AND robust magnitude z > gamma)  -- app.ml.anomaly, unchanged

Normal activity comes from a real deployment's event log (--db), split in time:
the first --train-frac is used for training, the rest for testing. Attacks are
injected into the test period by a simulator that encrypts/renames/deletes
files at a configurable rate, and we measure
  TPR                fraction of attack runs flagged within --attack-seconds
  false alarms/day   flagged windows in normal test traffic per day
  time to pause      seconds from the first malicious write to the first flag

  python experiments/e3_burst.py --db sqlite:///path/to/campus_nexus.db
  python experiments/e3_burst.py --synthetic-days 14     # smoke test only (marked SYNTHETIC)

LIVE MODE writes a real burst into a running server's shared root and measures
the time until the server reports sync paused (end-to-end, via the watcher):
  python experiments/e3_burst.py --live --root <shared root printed by run_server.py> --rate 30
"""

import argparse
import json
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

from common import client, ensure_user, fmt, mean_std, use_backend, write_results

WINDOW_S = 30


# --------------------------------------------------------------------------- data
def load_db_events(url: str) -> list[tuple[float, str, float]]:
    from sqlalchemy import create_engine, text
    eng = create_engine(url)
    q = text("SELECT type, timestamp, payload FROM events WHERE type IN "
             "('FILE_CREATED','FILE_MODIFIED','FILE_DELETED') ORDER BY timestamp")
    out = []
    with eng.connect() as conn:
        for etype, ts, payload in conn.execute(q):
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            payload = payload or {}
            if isinstance(ts, str):
                from datetime import datetime
                ts = datetime.fromisoformat(ts)
            kind = {"FILE_CREATED": "created", "FILE_DELETED": "deleted"}.get(etype, "modified")
            out.append((ts.timestamp(), kind, float(payload.get("size", 0) or 0) / 1e6))
    return out


def synthetic_events(days: int, seed: int = 7) -> list[tuple[float, str, float]]:
    """Plausible campus activity: busy daytime, quiet nights, occasional folder uploads."""
    rnd = random.Random(seed)
    out, t0 = [], 1_700_000_000.0
    for day in range(days):
        for hour in range(24):
            lam = 40 if 9 <= hour < 18 else 10 if 18 <= hour < 23 else 1     # events/hour
            n = sum(1 for _ in range(int(lam * 3)) if rnd.random() < 1 / 3)
            for _ in range(n):
                t = t0 + day * 86400 + hour * 3600 + rnd.random() * 3600
                kind = rnd.choices(["modified", "created", "deleted"], [0.6, 0.35, 0.05])[0]
                out.append((t, kind, rnd.lognormvariate(math.log(0.5), 1.2)))
        for _ in range(rnd.randint(1, 3)):                                     # legit folder upload
            t = t0 + day * 86400 + rnd.uniform(9, 18) * 3600
            for k in range(rnd.randint(20, 60)):
                out.append((t + k * rnd.uniform(0.5, 2.0), "created", rnd.lognormvariate(math.log(0.3), 1.0)))
    return sorted(out)


def attack_events(start: float, rate: float, seconds: float, op: str, sizes: list[float],
                  rnd: random.Random) -> list[tuple[float, str, float]]:
    out, t, i = [], start, 0
    while t < start + seconds:
        mb = rnd.choice(sizes) if sizes else 0.5
        if op == "encrypt":
            out.append((t, "modified", mb))
        elif op == "encrypt_rename":
            out += [(t, "created", mb), (t, "deleted", 0.0)]
        else:  # wipe
            out.append((t, "deleted", 0.0))
        i += 1
        t = start + i / rate
    return out


def windows(events) -> list[list[float]]:
    """Closed windows as [modified, deleted, created, mb] (same bucketing as the server)."""
    out, cur, start = [], None, None
    for t, kind, mb in events:
        if start is None or t - start >= WINDOW_S:
            if cur and sum(cur[:3]):
                out.append(cur)
            cur, start = [0, 0, 0, 0.0], t
        cur[{"modified": 0, "deleted": 1, "created": 2}[kind]] += 1
        cur[3] += mb
    if cur and sum(cur[:3]):
        out.append(cur)
    return out


# --------------------------------------------------------------------------- detectors
class Detectors:
    def __init__(self, train_windows):
        from sklearn.ensemble import IsolationForest
        import numpy as np
        from app.ml import anomaly
        self.anomaly = anomaly
        anomaly.reset()
        trusted = [w for w in train_windows if not self.rule(*w)]
        for w in trusted:
            anomaly.record_window(*w, persist=False)
        anomaly.fit_now()
        X = np.array([anomaly.features(*w) for w in trusted], dtype=float)
        self.forest = IsolationForest(n_estimators=200, contamination="auto", random_state=42).fit(X)
        self.np = np
        self.n_train = len(trusted)

    def rule(self, m, d, c, mb):
        return (m + d + c) > 500 or d > 100

    def check(self, name, m, d, c, mb) -> bool:
        if name == "rule":
            return self.rule(m, d, c, mb)
        if name == "forest":
            x = self.np.array([self.anomaly.features(m, d, c, mb)], dtype=float)
            return float(self.forest.decision_function(x)[0]) < self.anomaly.SCORE_THRESHOLD
        return self.anomaly.is_anomalous(m, d, c, mb)[0]


def replay(events, det: Detectors, name: str) -> list[float]:
    """Return the times at which each window first became flagged (live, per event)."""
    flags, cur, start, flagged = [], None, None, False
    for t, kind, mb in events:
        if start is None or t - start >= WINDOW_S:
            cur, start, flagged = [0, 0, 0, 0.0], t, False
        cur[{"modified": 0, "deleted": 1, "created": 2}[kind]] += 1
        cur[3] += mb
        if not flagged and det.check(name, *cur):
            flagged = True
            flags.append(t)
    return flags


# --------------------------------------------------------------------------- offline experiment
def offline(args):
    tmp = tempfile.mkdtemp(prefix="nexus_e3_")
    use_backend(data_dir=tmp)
    synthetic = args.db is None
    events = synthetic_events(args.synthetic_days, args.synthetic_seed) if synthetic else load_db_events(args.db)
    if len(events) < 100:
        sys.exit(f"only {len(events)} file events found -- collect more pilot data first")
    t_lo, t_hi = events[0][0], events[-1][0]
    cut = t_lo + (t_hi - t_lo) * args.train_frac
    train = [e for e in events if e[0] < cut]
    test = [e for e in events if e[0] >= cut]
    test_days = max((t_hi - cut) / 86400, 1e-9)
    det = Detectors(windows(train))
    print(f"events: {len(events)} ({len(train)} train / {len(test)} test), "
          f"train windows: {det.n_train}, test days: {test_days:.2f}")
    sizes = [mb for _, k, mb in train if k != "deleted" and mb > 0] or [0.5]

    names = ["rule", "forest", "ours"]
    fa = {n: len(replay(test, det, n)) / test_days for n in names}
    rnd = random.Random(args.seed)
    detail = {n: {} for n in names}
    for rate in args.rates:
        for n in names:
            detail[n][rate] = {"detected": 0, "trials": 0, "ttd": []}
        for trial in range(args.trials):
            start = rnd.uniform(cut + 600, max(cut + 601, t_hi - args.attack_seconds - 600))
            op = rnd.choice(args.ops)
            atk = attack_events(start, rate, args.attack_seconds, op, sizes, rnd)
            background = [e for e in test if start - 300 <= e[0] <= start + args.attack_seconds + 300]
            stream = sorted(background + atk)
            for n in names:
                hits = [t for t in replay(stream, det, n) if start <= t <= start + args.attack_seconds]
                d = detail[n][rate]
                d["trials"] += 1
                if hits:
                    d["detected"] += 1
                    d["ttd"].append(hits[0] - start)
        print(f"rate {rate:>5} files/s: " + ", ".join(
            f"{n} {detail[n][rate]['detected']}/{args.trials}" for n in names))

    label = {"rule": "Hard rule only", "forest": "Isolation Forest only", "ours": "Rule + Forest + gate (ours)"}
    rows, summary = [], {}
    for n in names:
        tot = sum(d["trials"] for d in detail[n].values())
        hit = sum(d["detected"] for d in detail[n].values())
        ttd = [x for d in detail[n].values() for x in d["ttd"]]
        m, s = mean_std(ttd)
        summary[n] = {"tpr": hit / tot if tot else float("nan"), "false_alarms_per_day": fa[n],
                      "time_to_pause_mean": m, "time_to_pause_std": s}
        rows.append(f"{label[n]} & {fmt(summary[n]['tpr'] * 100, None, 1)}\\% & "
                    f"{fmt(fa[n], None, 2)} & {fmt(m, s, 1)} \\\\")
    rows.append("% per-rate TPR (files/s: rule / forest / ours):")
    for rate in args.rates:
        rows.append("% " + f"{rate}: " + " / ".join(
            f"{detail[n][rate]['detected']}/{detail[n][rate]['trials']}" for n in names))
    write_results("e3_burst", {"params": {k: v for k, v in vars(args).items()},
                               "events": len(events), "test_days": test_days, "summary": summary,
                               "detail": detail}, rows, synthetic=synthetic,
                  note="Columns: detector & TPR & false alarms/day & time to pause (s)")


# --------------------------------------------------------------------------- live experiment
def live(args):
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"--root {root} is not a directory (use the path printed by run_server.py)")
    c = client()
    admin = ensure_user(c, "exp_admin")
    folder = root / "e3_live"
    folder.mkdir(exist_ok=True)
    runs = []
    for trial in range(args.trials):
        # wait until the previous trial's cleanup (a mass delete!) has left the
        # detector's window, then resume sync and start a clean trial
        time.sleep(WINDOW_S + 5)
        c.post("/api/sync/resume-scheduler", headers=admin)
        time.sleep(2)
        if c.get("/api/ml/anomalies", headers=admin).json().get("scheduler_paused"):
            sys.exit("sync is still paused before the trial -- check the server")
        t0 = time.perf_counter()
        paused_at, i = None, 0
        while time.perf_counter() - t0 < args.attack_seconds:
            target = time.perf_counter() - t0
            while i < target * args.rate:
                (folder / f"f{trial}_{i}.locked").write_bytes(os.urandom(4096))
                i += 1
            st = c.get("/api/ml/anomalies", headers=admin).json()
            if st.get("scheduler_paused"):
                paused_at = time.perf_counter() - t0
                break
            time.sleep(0.05)
        runs.append({"rate": args.rate, "files_written": i, "paused_after_s": paused_at})
        print(f"trial {trial + 1}: wrote {i} files, paused after "
              f"{'%.2f s' % paused_at if paused_at else 'NOT PAUSED'}")
        for p in folder.glob(f"f{trial}_*"):
            p.unlink()
    c.post("/api/sync/resume-scheduler", headers=admin)
    ttp = [r["paused_after_s"] for r in runs if r["paused_after_s"] is not None]
    m, s = mean_std(ttp)
    write_results("e3_burst_live", {"runs": runs, "rate": args.rate}, [
        f"% live end-to-end at {args.rate} files/s: detected {len(ttp)}/{len(runs)}, time to pause {fmt(m, s, 2)} s"],
        note="End-to-end through the file watcher; a fresh server only has the hard rule until it has >=20 windows of history")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="SQLAlchemy URL of a real deployment's database (pilot data)")
    ap.add_argument("--synthetic-days", type=int, default=14)
    ap.add_argument("--synthetic-seed", type=int, default=7)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--rates", type=float, nargs="+", default=[0.5, 1, 2, 5, 10, 20])
    ap.add_argument("--ops", nargs="+", default=["encrypt", "encrypt_rename", "wipe"])
    ap.add_argument("--attack-seconds", type=float, default=120)
    ap.add_argument("--trials", type=int, default=20, help="attack runs per rate")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--root", help="server shared root (live mode)")
    ap.add_argument("--rate", type=float, default=30, help="files/s (live mode)")
    args = ap.parse_args()
    live(args) if args.live else offline(args)


if __name__ == "__main__":
    main()
