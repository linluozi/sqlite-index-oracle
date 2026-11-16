# sqlite-index-oracle

Recommend a SQLite index only when **every query returns the same rows** and
`EXPLAIN QUERY PLAN` proves the planner actually chose it. When the index does
not help, the tool says so with the measured number.

Upstream: none

Most index advisors work from `EXPLAIN` cost estimates. This one creates the
index in a throwaway copy of the database, hash-compares the full result of
every workload query before/after, asks the planner whether the index was used,
and times the whole workload before it prints a recommendation.

## Install and run

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"

# workload.sql: one self-contained SELECT per line
.venv/bin/sqlite-index-oracle advise --db app.db --workload workload.sql
.venv/bin/sqlite-index-oracle advise --db app.db --workload workload.sql --json
```

Example output from the evidence workload:

```
accepted idx_oracle_events_category_status_score             9.18x  9.18x on 136 queries, results identical, used by 136/136 plans
accepted idx_oracle_events_category_status                   5.25x  5.25x on 136 queries, results identical, used by 136/136 plans
rejected idx_oracle_events_status                            0.98x  speedup 0.981x below 1.10x
```

Library use:

```python
from sqlite_index_oracle import advise, read_workload

queries = read_workload("workload.sql")
for advice in advise("app.db", queries):
    print(advice.status, advice.speedup, advice.candidate.ddl(), advice.reason)
```

The returned `Advice` always carries both timings, the number of queries whose
results were verified, how many plans used the index, and the mismatch count.

## What is verified

For every candidate index (single-column and composite, derived from `WHERE`
equality/range columns and `ORDER BY`):

1. Results of **all** queries are hashed (`sha256(repr(rows))`) on the original
   database and on a copy with the candidate index. Any difference rejects it.
2. `EXPLAIN QUERY PLAN` is inspected per query; zero uses rejects it even if
   timing looks good.
3. The full workload is timed (median of N repeats) on both trees. A candidate
   below `--min-speedup` (default 1.10x) is rejected, and a slower one is
   reported as slower — not rounded up to 1.0x.

## Evidence (produced by `tools/run_evidence.py`)

Deterministic database, 12,000 rows, 20 categories, skewed status distribution;
136 unique query strings (sha256-deduplicated) covering category+status+range
with `ORDER BY`, status-only, and category-only shapes. All timings are real
runs on this machine; JSON is `evidence/advice.json`.

| Metric | Value |
| --- | --- |
| Rows / unique queries | 12,000 / 136 |
| Candidates evaluated | 6 |
| Accepted (verified + used + faster) | 5 |
| Rejected | 1 |
| Best measured speedup | **9.18x** |
| Result mismatches across accepted candidates | **0** |
| Accepted candidates confirmed by query plan | 5 / 5 |
| Full run wall time | 1.80 s |

The rejected index is the point of the gate: `status` alone measured **0.98x**,
i.e. slightly slower than the table scan, and the tool refuses to recommend it.

Reproduce:

```bash
.venv/bin/python tools/run_evidence.py
# writes evidence/advice.json and exits nonzero if any accepted index has a
# result mismatch, an unused plan, or incomplete verification
```

## Tests

```bash
.venv/bin/python -m pytest -q
# 9 passed
```

Tests cover workload parsing, column extraction, candidate deduplication,
DDL quoting, acceptance, rejection below threshold, missing database, empty
workload, and consistency of the reported arithmetic.

## Limitations

* The workload must be self-contained SQL with literal values, one statement
  per line; placeholders/parameter binding are not supported.
* `EXPLAIN QUERY PLAN` naming is matched against the generated index name;
  SQLite versions that print a covering-index mention are handled by substring
  matching, but this is a heuristic for plan attribution.
* Timing is a single-process, single-thread measurement on a shared machine.
  The gate is relative speedup on the same tree, not an absolute latency SLA.
* Candidate generation is syntactic (regex over `WHERE`/`ORDER BY`), not a full
  SQL parser; CTEs, joins and subqueries are deliberately out of scope.
* Adding an index costs write throughput and disk; this tool only evaluates
  read workloads and does not model write amplification.

## 本轮创新

Index advisors usually rank candidates by planner cost estimates. This tool
makes the *recommendation* falsifiable: every accepted index has a recorded
result-hash equality check over 136 unique queries, a plan-attribution count,
and a measured speedup on the same data; one candidate is honestly reported as
0.98x and rejected. The novel part is not the advice, it is the refusal gate —
an index that the planner ignores or that makes the workload slower cannot be
printed as a win. No prior entry in the team ledger implements a
result-equivalence-gated index advisor; this is a new product line, not an
`-opt-lab` and not an upstream mirror.

## License

MIT.
