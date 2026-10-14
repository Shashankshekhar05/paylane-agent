"""Phase 4 evaluation gate: all 12 offline scenarios must pass."""

from eval.evaluate import evaluate_all


def test_all_twelve_scenarios_pass():
    report = evaluate_all(write=False)
    assert len(report["scenarios"]) == 12
    failed = [item["id"] for item in report["scenarios"] if not item["pass"]]
    assert not failed, f"scenarios failed: {failed}"
