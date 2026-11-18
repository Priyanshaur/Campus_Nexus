"""E6 -- System Usability Scale (paper Section V, "Usability").

Participants answer the 10 SUS statements (see sus_questionnaire.md) on a 1-5
scale. Put one row per participant in a CSV with columns q1..q10, e.g.

  participant,q1,q2,q3,q4,q5,q6,q7,q8,q9,q10
  p01,4,2,5,1,4,2,5,1,4,2

  python experiments/e6_sus.py responses.csv

Scoring (Brooke, 1996): odd items contribute (score - 1), even items
(5 - score); the sum is multiplied by 2.5 to give 0-100 per participant.
"""

import csv
import statistics
import sys

from common import fmt, write_results


def sus(row: dict) -> float:
    total = 0
    for i in range(1, 11):
        v = int(row[f"q{i}"])
        if not 1 <= v <= 5:
            raise ValueError(f"q{i}={v} is outside 1..5")
        total += (v - 1) if i % 2 else (5 - v)
    return total * 2.5


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    scores = [sus(r) for r in rows]
    m = statistics.fmean(scores)
    s = statistics.stdev(scores) if len(scores) > 1 else 0.0
    print(f"n = {len(scores)}, mean SUS = {m:.1f} (sd {s:.1f}), median {statistics.median(scores):.1f}")
    write_results("e6_sus", {"n": len(scores), "scores": scores, "mean": m, "sd": s},
                  [f"% SUS mean {fmt(m, s, 1)} (n = {len(scores)})"])


if __name__ == "__main__":
    main()
