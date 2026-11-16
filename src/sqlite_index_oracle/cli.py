"""CLI: sqlite-index-oracle advise --db FILE --workload FILE.sql"""
from __future__ import annotations

import argparse
import json

from .oracle import advise, read_workload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sqlite-index-oracle",
        description="Recommend SQLite indexes only when results stay identical "
                    "and the planner actually uses them.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("advise")
    a.add_argument("--db", required=True)
    a.add_argument("--workload", required=True, help="one SELECT per line")
    a.add_argument("--repeats", type=int, default=5)
    a.add_argument("--min-speedup", type=float, default=1.10)
    a.add_argument("--max-candidates", type=int, default=10)
    a.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd != "advise":
        return 2
    queries = read_workload(args.workload)
    results = advise(args.db, queries, repeats=args.repeats,
                     min_speedup=args.min_speedup,
                     max_candidates=args.max_candidates)
    payload = {
        "db": args.db,
        "queries": len(queries),
        "accepted": sum(1 for r in results if r.status == "accepted"),
        "rejected": sum(1 for r in results if r.status == "rejected"),
        "advice": [r.to_dict() for r in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"db: {args.db}  queries: {len(queries)}")
        for r in results:
            d = r.to_dict()
            print(f"{d['status'].upper():8} {d['index']:<45} "
                  f"{d['speedup']:.2f}x  {d['reason']}")
        print(f"accepted: {payload['accepted']}  rejected: {payload['rejected']}")
    return 0 if payload["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
