"""Regression: a whole class registering and logging in at once.

The latency experiment found that 100 simultaneous logins exhausted the
database connection pool (connections were held during password hashing and
while queueing for SQLite's write lock), returning HTTP 500 after 30 s.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_register_and_login(client):
    tag = uuid.uuid4().hex[:6]
    names = [f"c_{tag}_{i}" for i in range(60)]

    def go(n):
        r = client.post("/api/auth/register", json={"username": n, "email": f"{n}@x.com", "password": "pass123"})
        t = client.post("/api/auth/login", data={"username": n, "password": "pass123"})
        return r.status_code, t.status_code

    with ThreadPoolExecutor(max_workers=60) as pool:
        results = list(pool.map(go, names))
    assert all(r == (200, 200) for r in results), results
