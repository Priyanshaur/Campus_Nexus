"""E5 -- Live event delivery latency (paper Section V, "Event Delivery Latency").

Opens N authenticated WebSocket clients (each a separate user in one group
chat), then a sender posts M messages, M file commits and M alerts. For every
action we measure the time from just before the HTTP request to the moment
each client receives the matching event.

Sender and listeners run in this one process, so they share a clock; the
server can be on another machine (set NEXUS_SERVER=http://<server>:8000).

  python experiments/e5_latency.py --clients 10 50 100 --messages 50
"""

import argparse
import asyncio
import io
import json
import time
import uuid

import httpx
import websockets

from common import PASSWORD, SERVER, fmt, percentile, write_results


async def login(ac: httpx.AsyncClient, name: str) -> str:
    await ac.post("/api/auth/register", json={"username": name, "email": f"{name}@exp.local",
                                              "password": PASSWORD})
    r = await ac.post("/api/auth/login", data={"username": name, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


async def run(n_clients: int, n_actions: int, kinds: list[str]) -> dict:
    ws_base = SERVER.replace("http", "ws", 1)
    async with httpx.AsyncClient(base_url=SERVER, timeout=120) as ac:
        tag = uuid.uuid4().hex[:6]
        sender = f"exp_e5_s_{tag}"
        names = [f"exp_e5_{tag}_{i}" for i in range(n_clients)]
        tok_s = await login(ac, sender)
        # admin: resumes sync before the run and cleans up afterwards (admin deletes are not
        # counted as a mass-delete burst; a regular user's cleanup of many files would be)
        admin = {"Authorization": f"Bearer {await login(ac, 'exp_admin')}"}
        await ac.post("/api/sync/resume-scheduler", headers=admin)
        toks = await asyncio.gather(*(login(ac, n) for n in names))
        hs = {"Authorization": f"Bearer {tok_s}"}
        ch = (await ac.post("/api/messages/channels", json={"name": f"e5_{tag}", "members": names},
                            headers=hs)).json()

        pending: dict[str, float] = {}           # key -> send time
        arrivals: list[tuple[str, str, float]] = []   # (kind, key, arrival time)
        expected = {k: 0 for k in kinds}
        done = asyncio.Event()
        total_expected = n_clients * n_actions * len(kinds)

        def key_of(frame) -> tuple[str, str] | None:
            ev, d = frame.get("event"), frame.get("data") or {}
            p = d.get("payload") or {}
            if ev == "MESSAGE_SENT" and p.get("message_id"):
                return "message", f"m{p['message_id']}"
            if ev in ("FILE_CREATED", "FILE_MODIFIED") and d.get("resource", "").startswith(f"e5_{tag}/"):
                return "file", f"f{d['resource']}"
            if ev == "ALERT_CREATED" and d.get("resource"):
                return "alert", f"a{d['resource']}"
            return None

        async def listen(token: str, ready: asyncio.Event):
            async with websockets.connect(f"{ws_base}/ws/events?token={token}", max_size=None) as ws:
                ready.set()
                while not done.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    now = time.perf_counter()
                    k = key_of(json.loads(raw))
                    # events can arrive before the HTTP response returns, so record
                    # every arrival and match it to its send time afterwards
                    if k and k[0] in kinds:
                        arrivals.append((k[0], k[1], now))
                        if len(arrivals) >= total_expected:
                            done.set()

        readies = [asyncio.Event() for _ in toks]
        tasks = [asyncio.create_task(listen(t, r)) for t, r in zip(toks, readies)]
        await asyncio.wait_for(asyncio.gather(*(r.wait() for r in readies)), timeout=60)
        await asyncio.sleep(0.5)

        for i in range(n_actions):
            if "message" in kinds:
                t0 = time.perf_counter()
                r = await ac.post("/api/messages", json={"channel_id": ch["id"], "content": f"ping {i}"}, headers=hs)
                pending[f"m{r.json()['id']}"] = t0
                expected["message"] += n_clients
            if "file" in kinds:
                rel = f"e5_{tag}/f{i}.txt"
                uid = uuid.uuid4().hex
                data = f"payload {i}".encode()
                (await ac.post("/api/sync/chunk", params={"path": rel, "index": 0, "total": 1, "upload_id": uid},
                               files={"file": ("f", io.BytesIO(data), "text/plain")}, headers=hs)).raise_for_status()
                pending[f"f{rel}"] = time.perf_counter()          # commit starts now
                (await ac.post("/api/sync/complete", params={"path": rel, "upload_id": uid},
                               json={"size": len(data)}, headers=hs)).raise_for_status()
                expected["file"] += n_clients
            if "alert" in kinds:
                t0 = time.perf_counter()
                r = await ac.post("/api/alerts", json={"title": f"e5 {i}", "priority": "NORMAL",
                                                       "target_users": names}, headers=hs)
                pending[f"a{r.json()['id']}"] = t0
                expected["alert"] += n_clients
            await asyncio.sleep(0.05)
        try:
            await asyncio.wait_for(done.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        done.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ac.delete("/api/files/item", params={"path": f"e5_{tag}"}, headers=admin)
        await ac.delete(f"/api/messages/channels/{ch['id']}", headers=hs)

    lat: dict[str, list[float]] = {k: [] for k in kinds}
    for kind, key, t in arrivals:
        if key in pending:
            lat[kind].append(t - pending[key])
    out = {}
    for k in kinds:
        ms = [x * 1000 for x in lat[k]]
        out[k] = {"p50_ms": percentile(ms, 0.5), "p95_ms": percentile(ms, 0.95),
                  "received": len(ms), "expected": expected[k]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, nargs="+", default=[10, 50])
    ap.add_argument("--messages", type=int, default=30, help="actions of each kind")
    ap.add_argument("--kinds", nargs="+", default=["message", "file", "alert"])
    args = ap.parse_args()
    results, rows = {}, []
    for n in args.clients:
        res = asyncio.run(run(n, args.messages, args.kinds))
        results[n] = res
        for k, v in res.items():
            print(f"{n:>4} clients  {k:<8} p50 {v['p50_ms']:.1f} ms  p95 {v['p95_ms']:.1f} ms  "
                  f"({v['received']}/{v['expected']} delivered)")
            rows.append(f"{n} & {k} & {fmt(v['p50_ms'], None, 1)} & {fmt(v['p95_ms'], None, 1)} \\\\")
    write_results("e5_latency", {"params": vars(args), "results": results}, rows,
                  note="Columns: clients & event kind & p50 (ms) & p95 (ms)")


if __name__ == "__main__":
    main()
