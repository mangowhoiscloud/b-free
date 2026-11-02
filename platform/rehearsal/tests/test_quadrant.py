from bravo_rehearsal.quadrant import POLICY, Quadrant, classify


def test_cells():
    assert classify(True, True) is Quadrant.CODE_ONLY
    assert classify(True, False) is Quadrant.SUMMARIZE
    assert classify(False, True) is Quadrant.FIX
    assert classify(False, False) is Quadrant.ANALYZE


def test_code_only_has_no_trial_and_fix_has_retry_cap():
    assert POLICY[Quadrant.CODE_ONLY].trial_kind is None
    assert POLICY[Quadrant.FIX].max_attempts == 3
