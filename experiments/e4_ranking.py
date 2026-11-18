"""E4 -- Alert ranking quality (paper Table IV).

Reads alerts, receipts and chat history from a real deployment's database and
evaluates four orderings with a temporal split (train on the first part of the
timeline, test on the rest -- no future information leaks into training):

  newest     order by creation time
  priority   order by priority weight
  heuristic  the cold-start formula used by app.ml.ranker
  learned    per-user logistic regression on the point-in-time features of
             app.ml.ranker (falls back to the heuristic for users without
             enough labelled training rows, exactly as deployed)

Metrics on the test period:
  ROC-AUC        pooled over users, predicting "the user acknowledged it"
  Precision@3    for each (user, day) with >=3 alerts: share of the top 3 that
                 the user acknowledged

  python experiments/e4_ranking.py --db sqlite:///path/to/campus_nexus.db
  python experiments/e4_ranking.py --synthetic           # smoke test only (marked SYNTHETIC)
"""

import argparse
import math
import random
import sys
import tempfile
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from common import fmt, use_backend, write_results


def make_synthetic_db(path: Path, users: int = 30, days: int = 45, seed: int = 3):
    """Users with hidden per-priority / per-sender acknowledgement habits."""
    from app.core.timeutil import utcnow
    from app.db.init_db import init_db
    from app.db.models.models import Alert, AlertReceipt, Channel, Message, User
    from app.db.session import SessionLocal
    init_db()
    rnd = random.Random(seed)
    senders = [f"staff{i}" for i in range(5)]
    names = [f"student{i}" for i in range(users)]
    prio_w = {"CRITICAL": 1.0, "URGENT": 0.8, "IMPORTANT": 0.6, "NORMAL": 0.4, "INFO": 0.1}
    with SessionLocal() as db:
        for n in senders + names:
            db.add(User(username=n, email=f"{n}@syn.local", hashed_password="x", role="student"))
        ch = Channel(name="class", type="group", members=senders + names, created_by=senders[0])
        db.add(ch)
        db.flush()
        start = utcnow() - timedelta(days=days)
        like = {u: {s: rnd.random() for s in senders} for u in names}
        for day in range(days):
            for _ in range(rnd.randint(3, 8)):
                s = rnd.choice(senders)
                p = rnd.choices(list(prio_w), [1, 2, 3, 5, 4])[0]
                created = start + timedelta(days=day, hours=rnd.uniform(8, 20))
                a = Alert(title=f"{p} from {s}", body="", priority=p, sender=s, created_at=created)
                db.add(a)
                db.flush()
                for u in names:
                    prob = 1 / (1 + math.exp(-(3 * prio_w[p] + 2.5 * like[u][s] - 3)))
                    acked = created + timedelta(minutes=rnd.uniform(1, 600)) if rnd.random() < prob else None
                    db.add(AlertReceipt(alert_id=a.id, user=u, delivered_at=created, acked_at=acked))
            for _ in range(rnd.randint(5, 15)):
                db.add(Message(channel_id=ch.id, sender=rnd.choice(senders), content="hi",
                               created_at=start + timedelta(days=day, hours=rnd.uniform(8, 20))))
        db.commit()


def auc(scores: list[float], labels: list[int]) -> float:
    """Rank-based ROC-AUC (ties count half)."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    order = sorted(scores)
    rank = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and order[j + 1] == order[i]:
            j += 1
        for k in range(i, j + 1):
            rank.setdefault(order[k], (i + j) / 2 + 1)
        i = j + 1
    r_pos = sum(rank[s] for s in pos)
    return (r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="SQLAlchemy URL of a real deployment's database (pilot data)")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--seed", type=int, default=3, help="synthetic generator seed")
    args = ap.parse_args()
    if not args.db and not args.synthetic:
        sys.exit("give --db <pilot database URL> (or --synthetic for a smoke test)")

    tmp = Path(tempfile.mkdtemp(prefix="nexus_e4_"))
    db_url = args.db or f"sqlite:///{(tmp / 'syn.db').as_posix()}"
    use_backend(data_dir=str(tmp / "data"), database_url=db_url)
    if args.synthetic:
        make_synthetic_db(tmp / "syn.db", seed=args.seed)

    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from app.db.models.models import Alert, AlertReceipt
    from app.db.session import SessionLocal
    from app.ml import ranker

    with SessionLocal() as db:
        times = [t for (t,) in db.query(Alert.created_at).all()]
        if len(times) < 20:
            sys.exit(f"only {len(times)} alerts in the database -- collect more pilot data first")
        times.sort()
        t_lo, t_hi = times[0], times[-1]
        cutoff = t_lo + (t_hi - t_lo) * args.train_frac
        horizon = timedelta(hours=ranker.LABEL_HORIZON_H)
        users = [u for (u,) in db.query(AlertReceipt.user).distinct().all()]

        pooled = defaultdict(lambda: ([], []))          # method -> (scores, labels)
        p_at_3 = defaultdict(list)
        n_learned = n_heur = 0
        for u in users:
            hist = ranker._History(db, u)
            X, y = [], []
            for rec, a in hist.rows:                      # labels as known at the cutoff
                if a.created_at >= cutoff:
                    continue
                if rec.acked_at is not None and rec.acked_at < cutoff:
                    lab = 1
                elif a.created_at <= cutoff - horizon:
                    lab = 0
                else:
                    continue
                X.append(hist.feats(a.priority, a.sender, a.created_at))
                y.append(lab)
            model = None
            if len(y) >= ranker.MIN_TRAIN_ROWS and len(set(y)) == 2:
                model = LogisticRegression(class_weight="balanced", C=1.0, max_iter=500).fit(
                    np.array(X, dtype=float), np.array(y))
                n_learned += 1
            else:
                n_heur += 1
            test = []
            for rec, a in hist.rows:                      # final labels for the test period
                if a.created_at < cutoff:
                    continue
                if rec.acked_at is None and a.created_at > t_hi - horizon:
                    continue                              # not seen yet at the end of the data
                f = hist.feats(a.priority, a.sender, a.created_at)
                heur = ranker.heuristic_relevance(a.priority, f[1], f[2])
                learned = float(model.predict_proba(np.array([f]))[0, 1]) if model is not None else heur
                test.append((a, int(rec.acked_at is not None), {
                    "newest": a.created_at.timestamp(), "priority": f[0],
                    "heuristic": heur, "learned": learned}))
            for _, lab, sc in test:
                for m, s in sc.items():
                    pooled[m][0].append(s)
                    pooled[m][1].append(lab)
            by_day = defaultdict(list)
            for a, lab, sc in test:
                by_day[a.created_at.date()].append((lab, sc))
            for items in by_day.values():
                if len(items) < 3:
                    continue
                for m in ("newest", "priority", "heuristic", "learned"):
                    top = sorted(items, key=lambda it: -it[1][m])[:3]
                    p_at_3[m].append(sum(l for l, _ in top) / 3)

    label = {"newest": "Newest first", "priority": "Priority only", "heuristic": "Heuristic",
             "learned": "Learned (ours)"}
    rows, summary = [], {}
    for m in ("newest", "priority", "heuristic", "learned"):
        a = auc(*pooled[m])
        p3 = sum(p_at_3[m]) / len(p_at_3[m]) if p_at_3[m] else float("nan")
        summary[m] = {"roc_auc": a, "precision_at_3": p3, "n_test": len(pooled[m][1])}
        rows.append(f"{label[m]} & {fmt(a, None, 3)} & {fmt(p3, None, 3)} \\\\")
        print(f"{label[m]:<16} AUC {a:.3f}  P@3 {p3:.3f}")
    write_results("e4_ranking", {"summary": summary, "users": len(users), "users_learned": n_learned,
                                 "users_heuristic": n_heur, "cutoff": cutoff.isoformat()}, rows,
                  synthetic=bool(args.synthetic),
                  note=f"{len(users)} users ({n_learned} with a learned model); temporal split at {cutoff:%Y-%m-%d}")


if __name__ == "__main__":
    main()
