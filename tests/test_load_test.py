"""Step 7 — pure-logic tests for the load-test harness (scripts/load_test.py).

The load test itself is an integration script (seed 12k -> run -> verify); these
just lock down the two bits with real logic: the per-page gap computation and the
check accumulator.
"""

import scripts.load_test as lt


def test_progress_monitor_gaps_are_deltas_between_page_completions():
    m = lt._ProgressMonitor()
    m.page_completed_at = {1: 100.0, 2: 100.5, 3: 118.0, 4: 118.2}
    gaps = m.gaps()
    assert [p for p, _ in gaps] == [2, 3, 4]
    assert gaps[0][1] == 0.5
    assert round(gaps[1][1], 1) == 17.5
    assert round(gaps[2][1], 1) == 0.2


def test_progress_monitor_gaps_empty_when_fewer_than_two_pages():
    m = lt._ProgressMonitor()
    assert m.gaps() == []
    m.page_completed_at = {1: 10.0}
    assert m.gaps() == []


def test_check_records_pass_and_fail(monkeypatch):
    monkeypatch.setattr(lt, "CHECKS", [])
    lt.check("a", True)
    lt.check("b", False, "boom")
    assert lt.CHECKS == [("a", True, ""), ("b", False, "boom")]
