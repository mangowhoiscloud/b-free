from bravo_rehearsal.verdict import Verdict, classify, next_action


def test_classify():
    assert classify(1.0, False) is Verdict.PASS
    assert classify(0.0, False) is Verdict.FAIL
    assert classify(None, False) is Verdict.INFRA_ERROR
    assert classify(1.0, True) is Verdict.INFRA_ERROR


def test_next_action():
    assert next_action(Verdict.PASS, 1) == "REQUEST_APPROVAL"
    assert next_action(Verdict.INFRA_ERROR, 3) == "RETRY_SAME_INPUT"
    assert next_action(Verdict.FAIL, 2) == "RETRY"
    assert next_action(Verdict.FAIL, 3) == "NEEDS_HUMAN"
