"""Zero-dependency test runner (pytest not required).

Usage:  python run_tests.py
Runs every test in tests/*.py (functions listed in their ALL_TESTS lists),
prints PASS/FAIL with the assertion message, and exits non-zero on any failure
so it can gate a CI pipeline.
"""
from __future__ import annotations
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests import (test_feature_functions, test_parity, test_logreg, test_step3,
                   test_calibration, test_income)

MODULES = [test_feature_functions, test_parity, test_logreg, test_step3,
           test_calibration, test_income]


def main() -> int:
    total = passed = 0
    failures = []
    for mod in MODULES:
        print(f"\n=== {mod.__name__} ===")
        for fn in getattr(mod, "ALL_TESTS", []):
            total += 1
            try:
                fn()
                passed += 1
                print(f"  PASS  {fn.__name__}")
            except Exception as e:
                failures.append((mod.__name__, fn.__name__, e))
                print(f"  FAIL  {fn.__name__}: {e}")
                if os.environ.get("OBCREDIT_TRACE"):
                    traceback.print_exc()
    print(f"\n{passed}/{total} tests passed")
    if failures:
        print("\nFAILURES:")
        for m, f, e in failures:
            print(f"  {m}.{f}: {str(e)[:300]}")
        return 1
    print("ALL GREEN \u2705  Kaggle and TrueLayer construct features identically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
