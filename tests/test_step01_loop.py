"""Step 1: harness loop with one working tool (shell); observe-act-verify end to end."""
from pathlib import Path

from wright.demos import step1


def test_loop_observe_act_verify(tmp_path):
    task = step1(tmp_path, verbose=False)
    assert task.status == "done"
    assert (tmp_path / "greeting.txt").read_text() == "hello from wright\n"


def test_premature_done_claim_is_rejected_by_verifier(tmp_path):
    task = step1(tmp_path, verbose=False)
    # the first "done" claim was checked and found RED; the loop continued instead of accepting it
    assert task.hook_failures == {"file has greeting": 1, "printed it back": 1}
    assert any("STOP HOOKS FAILED" in str(m.get("content")) for m in task.messages)


def test_verification_reads_the_harness_ledger_not_the_model(tmp_path):
    task = step1(tmp_path, verbose=False)
    cmds = [r.get("command") for r in task.ledger]
    assert any("cat greeting.txt" in (c or "") for c in cmds)
