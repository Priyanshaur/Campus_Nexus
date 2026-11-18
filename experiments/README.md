# Campus Nexus experiments (paper Section V)

Each script fills one results table in `paper/campus_nexus.tex`. Every run writes
`results/<name>.json` (raw data, keep it) and `results/<name>_table.tex` (rows to
paste over the `\tbd` cells). Anything run on synthetic data is labelled
**SYNTHETIC — DO NOT REPORT** in both files.

| Script | Paper table | Needs |
|--------|-------------|-------|
| `e1_sync.py` | Table I — throughput, dedup savings | a running server |
| `e2_conflicts.py` | Table II — conflict detection | a running server |
| `e3_burst.py` | Table III — burst detection | pilot database (`--db`); optional live run |
| `e4_ranking.py` | Table IV — alert ranking | pilot database (`--db`) |
| `e5_latency.py` | delivery latency (text) | a running server |
| `e6_sus.py` | usability (text) | questionnaire answers |

## 1. Record the setup

Write down the server's CPU, RAM and disk, the client machines, and the network
(campus LAN / Wi-Fi). The paper's "Experimental Setup" needs these. Run the
clients on a **different machine** from the server for E1 and E5; localhost
numbers only show the software's own overhead.

## 2. Server for E1, E2, E5 (throwaway data)

```bash
python experiments/run_server.py --port 8000
```

It prints the shared-root path. On the client machine:

```bash
export NEXUS_SERVER=http://<server-ip>:8000     # PowerShell: $env:NEXUS_SERVER="http://<server-ip>:8000"
python experiments/e1_sync.py --sizes 1 10 100 500 --repeats 5
python experiments/e2_conflicts.py --users 4 --devices 2 --steps 200 --repeats 5
python experiments/e5_latency.py --clients 10 50 100 --messages 50
```

E2 note: with honest base versions the base-version method is correct by
construction; the experiment's point is how often the last-writer approach
raises false conflicts. Say this in the paper.

## 3. Pilot deployment (for E3, E4, E6)

Run the real app (not `run_server.py`) for your pilot group for 2–4 weeks so
there is real file activity and alert history. Then, with the server stopped
or from a copy of its database:

```bash
python experiments/e3_burst.py --db sqlite:///backend/campus_nexus.db --trials 20
python experiments/e4_ranking.py --db sqlite:///backend/campus_nexus.db
# PostgreSQL: --db postgresql://nexus:nexus@<host>:5432/campus_nexus
```

E4 needs enough alerts that each user has at least 20 labelled ones for the
learned model; users with fewer fall back to the heuristic (the script reports
how many had a learned model).

Optional end-to-end burst timing against a server started with `run_server.py`
(writes real files into its shared root):

```bash
python experiments/e3_burst.py --live --root <shared root> --rate 30 --trials 3
```

## 4. Usability

Give `sus_questionnaire.md` to pilot users, enter answers in
`responses_template.csv` (one row per person, q1..q10 from 1 to 5), then:

```bash
python experiments/e6_sus.py experiments/responses_template.csv
```

## Reproducing the paper's simulation results

Tables III and IV in the current paper are **labelled simulations** (no pilot data yet):

```bash
python experiments/e3_burst.py --synthetic-days 28 --synthetic-seed 11 --train-frac 0.6 --trials 30 --attack-seconds 120 --seed 99
for s in 1 2 3 4 5; do python experiments/e4_ranking.py --synthetic --seed $s; done   # averaged over seeds
```

Seed 7 of the burst simulator was used during development (it revealed that
folder uploads caused all false alarms); the reported numbers use the unseen
seed 11. Replace both tables with pilot results when available.

## Smoke tests (no pilot data yet)

```bash
python experiments/e3_burst.py --synthetic-days 14
python experiments/e4_ranking.py --synthetic
```

These only check the scripts work; their outputs are marked synthetic.
