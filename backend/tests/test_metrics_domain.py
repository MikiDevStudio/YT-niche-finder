"""Pure tests for the breakout formulas in domain/metrics.py. No DB, no network.

Run: python3 tests/test_metrics_domain.py (or pytest)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from domain import metrics as M  # noqa: E402


def test_effective_outlier_prefers_age_adjusted():
    row = {"outlierScoreAgeAdjusted": 5.0, "outlierScore": 4.0, "outlierScoreNexlev": 900.0}
    assert M.effective_outlier(row) == 5.0
    assert M.effective_outlier(row, age_adjusted=False) == 4.0


def test_effective_outlier_never_falls_back_to_nexlev():
    row = {"outlierScoreAgeAdjusted": None, "outlierScore": None, "outlierScoreNexlev": 2394.9}
    assert M.effective_outlier(row) is None
    assert M.effective_outlier(row, age_adjusted=False) is None


def test_effective_outlier_keeps_a_real_zero():
    # 0.0 is a measured flop, not "unknown": the old `a or b or c` chains lost it
    assert M.effective_outlier({"outlierScoreAgeAdjusted": 0.0, "outlierScore": 0.5}) == 0.0


def test_youth_factor_steps():
    assert M.youth_factor(None) == 1.0
    assert M.youth_factor(30) == 2.0
    assert M.youth_factor(200) == 1.5
    assert M.youth_factor(800) == 1.0
    assert M.youth_factor(5000) == 0.6


def test_breakout_score_series_equals_one_mega_hit():
    assert M.breakout_score([4, 4, 4], 1.0) == M.breakout_score([64], 1.0) == 6.0


def test_breakout_score_ignores_non_hits_and_unknowns():
    assert M.breakout_score([1.9, None, 0.3], 2.0) == 0.0
    assert M.breakout_score([8, 1.0], 2.0) == 6.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
