"""Step 5: regression baseline. A change that improves one metric and breaks another is rejected,
even though the average improves; the sandbox returns to its last green checkpoint."""
import textwrap
from pathlib import Path

import pytest

from wright.baseline import Baseline, Dimension
from wright.sandbox import DirSandbox

SLOW = '''
import time
def total(xs):
    time.sleep(0.2)
    return sum(xs)
def mean(xs):
    return total(xs) / len(xs)
'''
FAST_BUT_BROKEN = '''
def total(xs):
    return sum(xs)
def mean(xs):
    return total(xs) // len(xs)   # "optimised" — integer division breaks mean([1, 2])
'''
FAST_AND_RIGHT = '''
def total(xs):
    return sum(xs)
def mean(xs):
    return total(xs) / len(xs)
'''
TESTS = '''
from stats import total, mean
assert total([1, 2, 3]) == 6
assert mean([1, 2]) == 1.5
print("ok")
'''
BENCH = '''
import time, stats
t = time.perf_counter(); stats.total(list(range(1000))); print(round((time.perf_counter() - t) * 1000, 2))
'''


def project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "stats.py").write_text(SLOW)
    (root / "check_stats.py").write_text(TESTS)
    (root / "bench.py").write_text(BENCH)
    return root


def baseline(target: Path) -> Baseline:
    return Baseline(id="bl", target=str(target), dimensions=[
        Dimension("tests", {"kind": "command", "run": "python3 check_stats.py"}, "pass"),
        Dimension("total_ms", {"kind": "metric", "run": "python3 bench.py"}, "lower", tolerance=0.5,
                  abs_tolerance=5),
        Dimension("module_lines", {"kind": "metric", "run": "wc -l < stats.py"}, "lower",
                  abs_tolerance=20),
    ])


def test_change_improving_one_metric_but_breaking_another_is_rejected(tmp_path):
    real = project(tmp_path / "real")
    sb = DirSandbox(real, tmp_path / "task")
    root = sb.create()
    bl = baseline(real)
    frozen = bl.freeze(root)
    assert all(v["ok"] for v in frozen.values()) and frozen["total_ms"]["value"] >= 200

    (root / "stats.py").write_text(FAST_BUT_BROKEN)                  # 200ms → ~0ms, but wrong
    verdict = bl.compare(root)
    assert "total_ms" in verdict["improvements"]                     # the average got much better…
    assert verdict["regressions"] == ["tests"]                       # …and one dimension broke
    assert verdict["accept"] is False                                # fail closed
    sb.reset_to_green()                                              # rejected → last green state
    assert (root / "stats.py").read_text() == SLOW
    assert (real / "stats.py").read_text() == SLOW                   # real target never touched

    (root / "stats.py").write_text(FAST_AND_RIGHT)
    verdict = bl.compare(root)
    assert verdict["accept"] and verdict["regressions"] == []
    raised = bl.absorb(root, fixed_cases=[{"name": "mean_of_pair", "kind": "command",
                                           "run": "python3 -c 'import stats; assert stats.mean([1,2])==1.5'"}])
    assert "mean_of_pair" in raised and "total_ms" in raised and bl.version == 2
    (root / "stats.py").write_text(FAST_BUT_BROKEN)
    v = bl.compare(root)
    assert set(v["regressions"]) == {"tests", "mean_of_pair"}        # the fixed case is now permanent


def test_unmeasurable_metric_counts_as_regression(tmp_path):
    real = project(tmp_path / "real")
    bl = baseline(real)
    bl.freeze(real)
    (real / "bench.py").write_text("raise SystemExit(1)\n")
    v = bl.compare(real)
    assert "total_ms" in v["regressions"] and not v["accept"]


def test_flaky_dimension_decided_by_majority_and_recorded(tmp_path):
    flag = tmp_path / "count"
    flag.write_text("0")
    # fails only on its first run after freezing, then passes: a flake, not a regression
    script = tmp_path / "flaky.sh"
    script.write_text(textwrap.dedent(f"""\
        n=$(cat {flag}); echo $((n+1)) > {flag}
        [ "$n" = "1" ] && exit 1 || exit 0
    """))
    bl = Baseline(id="f", target=str(tmp_path), dimensions=[
        Dimension("svc", {"kind": "command", "run": f"sh {script}"}, "pass")])
    bl.freeze(tmp_path)
    v = bl.compare(tmp_path)
    assert v["accept"] and v["flakes"] and v["flakes"][0]["dimension"] == "svc"


def test_manifest_round_trip_and_validation(tmp_path):
    bl = baseline(tmp_path)
    bl.save(tmp_path / "bl.json")
    assert Baseline.load(tmp_path / "bl.json").dimensions[1].direction == "lower"
    bad = Baseline(id="x", target="t", dimensions=[Dimension("a", {"kind": "command", "run": "true"}, "lower")])
    with pytest.raises(ValueError):
        bad.freeze(tmp_path)
