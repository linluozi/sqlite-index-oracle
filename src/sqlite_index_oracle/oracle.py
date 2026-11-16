"""Index advice for SQLite that must prove two things before it recommends.

1. The query results are byte-identical with and without the candidate index.
2. The query planner actually uses the index, and the workload gets faster.

Anything else is reported as a rejection with the measured reason.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Candidate", "Advice", "advise", "extract_columns", "read_workload"]

_IDENT = r"[A-Za-z_][\w$]*"
_FROM_RE = re.compile(rf"\bFROM\s+({_IDENT})", re.I)
_EQ_RE = re.compile(rf"({_IDENT})\s*=\s*(?:'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|-?\d+(?:\.\d+)?|\?)")
_RANGE_RE = re.compile(rf"({_IDENT})\s*(?:>=|<=|>|<|\bBETWEEN\b)", re.I)
_ORDER_RE = re.compile(r"\bORDER\s+BY\s+(.+?)(?:\bLIMIT\b|;|$)", re.I | re.S)
_STOP_RE = re.compile(r"\b(?:GROUP\s+BY|HAVING|LIMIT|UNION|EXCEPT|INTERSECT)\b", re.I)


@dataclass(frozen=True)
class Candidate:
    table: str
    columns: tuple[str, ...]

    @property
    def name(self) -> str:
        return "idx_oracle_" + self.table + "_" + "_".join(self.columns)

    def ddl(self) -> str:
        cols = ", ".join(self.columns)
        return f'CREATE INDEX "{self.name}" ON "{self.table}" ({cols})'


@dataclass
class Advice:
    candidate: Candidate
    status: str                    # accepted | rejected
    reason: str
    baseline_ms: float = 0.0
    optimized_ms: float = 0.0
    speedup: float = 0.0
    queries_verified: int = 0
    queries_using_index: int = 0
    result_mismatches: int = 0

    def to_dict(self) -> dict:
        return {
            "table": self.candidate.table,
            "columns": list(self.candidate.columns),
            "index": self.candidate.name,
            "ddl": self.candidate.ddl(),
            "status": self.status,
            "reason": self.reason,
            "baseline_ms": round(self.baseline_ms, 3),
            "optimized_ms": round(self.optimized_ms, 3),
            "speedup": round(self.speedup, 4),
            "queries_verified": self.queries_verified,
            "queries_using_index": self.queries_using_index,
            "result_mismatches": self.result_mismatches,
        }


def read_workload(path: str | Path) -> list[str]:
    """One self-contained SQL statement per line; blank lines and `--` ignored."""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        if not line.endswith(";"):
            line += ";"
        out.append(line)
    return out


def extract_columns(sql: str) -> tuple[str, list[str], list[str], list[str]]:
    """Return (table, equality_columns, range_columns, order_columns)."""
    m = _FROM_RE.search(sql)
    if not m:
        return "", [], [], []
    table = m.group(1)
    where = ""
    wm = re.search(r"\bWHERE\b(.+?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|;|$)",
                   sql, re.I | re.S)
    if wm:
        where = wm.group(1)
    eq = []
    for c in _EQ_RE.findall(where):
        if c.lower() not in (x.lower() for x in eq):
            eq.append(c)
    rng = []
    for c in _RANGE_RE.findall(where):
        if c.lower() not in (x.lower() for x in eq + rng):
            rng.append(c)
    order = []
    om = _ORDER_RE.search(sql)
    if om:
        for part in om.group(1).split(","):
            part = part.strip()
            part = re.sub(r"\s+(ASC|DESC)$", "", part, flags=re.I)
            if re.fullmatch(_IDENT, part):
                order.append(part)
    return table, eq, rng, order


def candidates_for(queries: list[str], max_candidates: int = 10) -> list[Candidate]:
    seen: dict[tuple[str, tuple[str, ...]], Candidate] = {}
    order: list[tuple[str, tuple[str, ...]]] = []

    def add(table: str, cols: list[str]) -> None:
        cols = [c for c in cols if c]
        if not table or not cols:
            return
        key = (table, tuple(cols))
        if key not in seen:
            seen[key] = Candidate(table, tuple(cols))
            order.append(key)

    for sql in queries:
        table, eq, rng, order_cols = extract_columns(sql)
        if not table:
            continue
        for c in eq:
            add(table, [c])
        if len(eq) >= 2:
            add(table, eq[:3])
        if eq and (rng or order_cols):
            add(table, eq[:3] + [(rng or order_cols)[0]])
        if not eq and order_cols:
            add(table, [order_cols[0]])
    cands = [seen[k] for k in order]
    # Prefer composite (more selective) candidates first, then by column count.
    cands.sort(key=lambda c: (-len(c.columns), c.table, c.columns))
    return cands[:max_candidates]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _hashes(conn: sqlite3.Connection, queries: list[str]) -> list[str]:
    out = []
    for sql in queries:
        rows = conn.execute(sql).fetchall()
        payload = repr(rows).encode("utf-8", "surrogateescape")
        out.append(hashlib.sha256(payload).hexdigest())
    return out


def _timed(conn: sqlite3.Connection, queries: list[str], repeats: int) -> float:
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        for sql in queries:
            conn.execute(sql).fetchall()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


def _uses_index(conn: sqlite3.Connection, sql: str, name: str) -> bool:
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    except sqlite3.Error:
        return False
    text = " ".join(str(row[-1]) for row in plan)
    return name in text or f"USING INDEX {name}".lower() in text.lower() or (
        "USING INDEX" in text.upper() and name.lower() in text.lower()
    ) or ("USING COVERING INDEX" in text.upper() and name.lower() in text.lower())


def advise(
    db_path: str | Path,
    queries: list[str],
    repeats: int = 5,
    min_speedup: float = 1.10,
    max_candidates: int = 10,
) -> list[Advice]:
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    if not queries:
        return []
    base_conn = _connect(db_path)
    try:
        baseline_hashes = _hashes(base_conn, queries)
        baseline_ms = _timed(base_conn, queries, repeats)
    finally:
        base_conn.close()

    results: list[Advice] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="sqlite-index-oracle-"))
    try:
        for cand in candidates_for(queries, max_candidates=max_candidates):
            copy = tmpdir / (cand.name + ".db")
            shutil.copyfile(db_path, copy)
            conn = sqlite3.connect(copy)
            adv = Advice(candidate=cand, status="rejected", reason="")
            try:
                conn.execute(cand.ddl())
                conn.commit()
                conn.execute("ANALYZE")
                conn.commit()
                got = _hashes(conn, queries)
                mism = [i for i, (a, b) in enumerate(zip(baseline_hashes, got)) if a != b]
                adv.queries_verified = len(queries)
                adv.result_mismatches = len(mism)
                used = sum(1 for q in queries if _uses_index(conn, q, cand.name))
                adv.queries_using_index = used
                if mism:
                    adv.reason = f"result mismatch on {len(mism)}/{len(queries)} queries"
                    results.append(adv)
                    continue
                opt_ms = _timed(conn, queries, repeats)
                adv.baseline_ms = baseline_ms
                adv.optimized_ms = opt_ms
                adv.speedup = baseline_ms / opt_ms if opt_ms else 0.0
                if used == 0:
                    adv.reason = "planner never chose the index"
                elif adv.speedup < min_speedup:
                    adv.reason = f"speedup {adv.speedup:.3f}x below {min_speedup:.2f}x"
                elif adv.speedup < 1.0:
                    adv.reason = f"slower than baseline ({adv.speedup:.3f}x)"
                else:
                    adv.status = "accepted"
                    adv.reason = (
                        f"{adv.speedup:.2f}x on {len(queries)} queries, "
                        f"results identical, used by {used}/{len(queries)} plans"
                    )
            except sqlite3.Error as exc:
                adv.reason = f"sqlite error: {exc}"
            finally:
                conn.close()
                copy.unlink(missing_ok=True)
            results.append(adv)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    accepted = [r for r in results if r.status == "accepted"]
    rejected = [r for r in results if r.status == "rejected"]
    return accepted + rejected
