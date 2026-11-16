"""Produce evidence/advice.json from a deterministic SQLite workload.

Run: python3 tools/run_evidence.py
"""
from __future__ import annotations

import hashlib
import json
import platform
import random
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlite_index_oracle import advise  # noqa: E402

ROWS = 12_000
QUERIES = 120
CATEGORIES = [f"c{i:02d}" for i in range(20)]
STATUSES = ["active", "closed", "pending"]


def build_db(path: Path) -> None:
    rng = random.Random(20261117)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            status TEXT NOT NULL,
            score INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        """
    )
    rows = []
    for i in range(ROWS):
        cat = CATEGORIES[rng.randrange(len(CATEGORIES))]
        st = STATUSES[rng.choices([0, 1, 2], weights=[30, 50, 20])[0]]
        rows.append((i, cat, st, rng.randrange(1000), 1_700_000_000 + i, f"payload-{i}-" + "x" * 24))
    conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def workload() -> list[str]:
    rng = random.Random(777)
    out = []
    for _ in range(QUERIES):
        cat = CATEGORIES[rng.randrange(len(CATEGORIES))]
        st = STATUSES[rng.randrange(len(STATUSES))]
        lo = rng.randrange(0, 900)
        out.append(
            "SELECT id, category, status, score FROM events "
            f"WHERE category = '{cat}' AND status = '{st}' AND score >= {lo} "
            "ORDER BY score DESC, id LIMIT 20;"
        )
    for _ in range(QUERIES // 3):
        st = STATUSES[rng.randrange(len(STATUSES))]
        out.append(
            "SELECT id, payload FROM events "
            f"WHERE status = '{st}' ORDER BY score DESC, id LIMIT 20;"
        )
    for _ in range(QUERIES // 6):
        cat = CATEGORIES[rng.randrange(len(CATEGORIES))]
        out.append(
            "SELECT id, score FROM events "
            f"WHERE category = '{cat}' ORDER BY score DESC, id LIMIT 20;"
        )
    seen, uniq = set(), []
    for q in out:
        h = hashlib.sha256(q.encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            uniq.append(q)
    return uniq


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sio-evidence-"))
    db = tmp / "events.db"
    try:
        build_db(db)
        queries = workload()
        t0 = time.perf_counter()
        results = advise(db, queries, repeats=3, min_speedup=1.10, max_candidates=8)
        elapsed = time.perf_counter() - t0
        accepted = [r for r in results if r.status == "accepted"]
        rejected = [r for r in results if r.status == "rejected"]
        best = max((r.speedup for r in accepted), default=0.0)
        ok = bool(accepted) and all(
            r.result_mismatches == 0 and r.queries_using_index > 0
            and r.queries_verified == len(queries) for r in accepted
        )
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "rows": ROWS,
            "queries": len(queries),
            "unique_query_sha256": len({hashlib.sha256(q.encode()).hexdigest() for q in queries}),
            "workload_sha256": hashlib.sha256("\n".join(queries).encode()).hexdigest(),
            "repeats": 3,
            "min_speedup": 1.10,
            "elapsed_seconds": round(elapsed, 3),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "best_speedup": round(best, 4),
            "results": [r.to_dict() for r in results],
            "ok": ok,
        }
        out = ROOT / "evidence" / "advice.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({k: report[k] for k in (
            "rows", "queries", "unique_query_sha256", "accepted", "rejected",
            "best_speedup", "elapsed_seconds", "ok")}, indent=2))
        for r in results:
            d = r.to_dict()
            print(f"{d['status']:8} {d['index']:<50} {d['speedup']:.2f}x  {d['reason']}")
        return 0 if ok else 1
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())