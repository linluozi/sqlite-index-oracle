import sqlite3
from pathlib import Path

import pytest

from sqlite_index_oracle import advise, extract_columns, read_workload
from sqlite_index_oracle.oracle import Candidate, candidates_for


def make_db(path: Path, rows: int = 800) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, category TEXT, status TEXT,"
        " score INTEGER, payload TEXT)"
    )
    conn.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?)",
        [(i, f"c{i % 7}", "active" if i % 3 else "closed", (i * 37) % 1000, f"p{i}")
         for i in range(rows)],
    )
    conn.commit()
    conn.close()
    return path


QLOG = "SELECT id, payload FROM events WHERE category = 'c1' AND status = 'active' ORDER BY score DESC, id LIMIT 20;"


def test_read_workload(tmp_path):
    p = tmp_path / "w.sql"
    p.write_text("-- comment\n\n" + QLOG.rstrip(";") + "\nSELECT 1;\n", encoding="utf-8")
    qs = read_workload(p)
    assert len(qs) == 2
    assert qs[0].endswith(";")
    assert qs[1] == "SELECT 1;"


def test_extract_columns():
    table, eq, rng, order = extract_columns(
        "SELECT * FROM events WHERE category='c1' AND status='active' "
        "AND score >= 50 ORDER BY score DESC, id LIMIT 10"
    )
    assert table == "events"
    assert eq == ["category", "status"]
    assert rng == ["score"]
    assert order == ["score", "id"]


def test_candidates_dedupe_and_priority():
    q1 = "SELECT * FROM t WHERE a=1 AND b=2 ORDER BY c LIMIT 5;"
    q2 = "SELECT * FROM t WHERE a=1 AND b=2 ORDER BY c LIMIT 9;"
    q3 = "SELECT * FROM t WHERE b=3 ORDER BY c LIMIT 3;"
    cands = candidates_for([q1, q2, q3])
    keys = [(c.table, c.columns) for c in cands]
    assert len(keys) == len(set(keys))
    assert keys[0] == ("t", ("a", "b", "c"))     # composite first
    assert ("t", ("a",)) in keys and ("t", ("b",)) in keys


def test_ddl_quotes_identifiers():
    c = Candidate("events", ("category", "status"))
    assert c.ddl() == 'CREATE INDEX "idx_oracle_events_category_status" ON "events" (category, status)'


def test_advise_accepts_used_verified_index(tmp_path):
    db = make_db(tmp_path / "a.db")
    results = advise(db, [QLOG], repeats=2, min_speedup=0.0, max_candidates=4)
    accepted = [r for r in results if r.status == "accepted"]
    assert accepted
    a = accepted[0]
    assert a.result_mismatches == 0
    assert a.queries_verified == 1
    assert a.queries_using_index >= 1
    assert a.baseline_ms > 0 and a.optimized_ms > 0


def test_advise_rejects_below_threshold(tmp_path):
    db = make_db(tmp_path / "b.db")
    results = advise(db, [QLOG], repeats=2, min_speedup=10_000.0, max_candidates=4)
    assert results and all(r.status == "rejected" for r in results)
    assert all("below" in r.reason for r in results)


def test_advise_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        advise(tmp_path / "nope.db", [QLOG])


def test_advise_empty_workload(tmp_path):
    db = make_db(tmp_path / "c.db")
    assert advise(db, []) == []


def test_all_reported_numbers_are_real(tmp_path):
    db = make_db(tmp_path / "d.db")
    results = advise(db, [QLOG], repeats=2, min_speedup=0.0, max_candidates=3)
    for r in results:
        assert r.queries_verified == 1
        assert r.result_mismatches == 0
        if r.status == "accepted":
            assert r.speedup > 0
            assert round(r.baseline_ms / r.optimized_ms, 4) == round(r.speedup, 4)