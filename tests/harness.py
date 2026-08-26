"""Assertion-style test harness, shared by the test modules (no pytest).

One failure list, so every module's cases land in the same tally and the exit
code covers all of them.
"""
failures = []


def case(name, passed, detail=""):
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not passed:
        failures.append(name)


def skip(name, why):
    print(f"SKIP  {name}  ({why})")
