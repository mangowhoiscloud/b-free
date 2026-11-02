"""Transition table: full spec path (§8.2), FR-5 retry cap, failback (S4)."""

from __future__ import annotations

import pytest

from bravo_api.errors import BusinessError
from bravo_api.statemachine import TRANSITIONS, State, Trigger, next_state


def test_pilot_happy_path() -> None:
    s = State.COLLECT
    for trigger in (
        Trigger.COLLECTED,
        Trigger.ANALYZED_NO_GAPS,
        Trigger.GATE_PASSED,
        Trigger.SHIFT_STARTED,
        Trigger.SHIFT_DONE,
        Trigger.RUN_DONE,
        Trigger.CONTROLS_VERIFIED,
    ):
        s = next_state(s, trigger)
    assert s is State.DONE


def test_spec_path_through_rehearsal() -> None:
    s = State.COLLECT
    for trigger in (
        Trigger.COLLECTED,
        Trigger.ANALYZED_GAPS,
        Trigger.PROPOSAL_ACCEPTED,
        Trigger.REHEARSAL_PASSED,  # r2: stays in REHEARSE, opens a change (T31)
        Trigger.CHANGE_APPLIED,  # change approved + applied with the approved digest -> GATE re-check
        Trigger.GATE_PASSED,
        Trigger.SHIFT_STARTED,
        Trigger.SHIFT_DONE,
        Trigger.RUN_DONE,
        Trigger.FAILBACK_STARTED,
        Trigger.RUN_DONE,
    ):
        s = next_state(s, trigger)
    assert s is State.DONE_ROLLED_BACK


def test_r2_change_path_stays_in_rehearse_until_applied() -> None:
    assert next_state(State.REHEARSE, Trigger.REHEARSAL_PASSED) is State.REHEARSE
    assert next_state(State.REHEARSE, Trigger.CHANGE_APPLIED) is State.GATE
    # Only REHEARSE can take an applied change, and a passing rehearsal never jumps straight to GATE.
    for s in State:
        if s is not State.REHEARSE:
            with pytest.raises(BusinessError):
                next_state(s, Trigger.CHANGE_APPLIED)


def test_rehearsal_failure_paths() -> None:
    assert next_state(State.REHEARSE, Trigger.REHEARSAL_FAILED) is State.PROPOSE
    assert next_state(State.REHEARSE, Trigger.ATTEMPTS_EXHAUSTED) is State.NEEDS_HUMAN
    assert next_state(State.PROPOSE, Trigger.REHEARSAL_INFRA_ERROR) is State.PROPOSE
    assert next_state(State.GATE, Trigger.REPROPOSAL_REQUESTED) is State.PROPOSE
    assert next_state(State.FAILBACK, Trigger.RUN_ABORTED_PRE_WRITE) is State.WATCH
    assert next_state(State.FAILBACK, Trigger.RUN_SAFE_HOLD) is State.SAFE_HOLD


def test_failure_paths() -> None:
    assert next_state(State.CUTOVER, Trigger.RUN_ABORTED_PRE_WRITE) is State.ABORT_PRE_WRITE
    assert next_state(State.ABORT_PRE_WRITE, Trigger.ABORT_SETTLED) is State.AWAIT_APPROVAL
    assert next_state(State.CUTOVER, Trigger.RUN_SAFE_HOLD) is State.SAFE_HOLD
    assert next_state(State.AWAIT_APPROVAL, Trigger.GATE_BLOCKED) is State.GATE


def test_rehearsal_is_never_entered_after_cutover() -> None:
    # S2-AC5: no transition leads from a cutover-or-later state into PROPOSE or REHEARSE.
    after = {State.CUTOVER, State.WATCH, State.DONE, State.FAILBACK, State.DONE_ROLLED_BACK, State.SAFE_HOLD}
    assert not [(s, t) for (s, t), d in TRANSITIONS.items() if s in after and d in (State.PROPOSE, State.REHEARSE)]


@pytest.mark.parametrize(
    ("state", "trigger"),
    [
        (State.COLLECT, Trigger.GATE_PASSED),
        (State.GATE, Trigger.SHIFT_STARTED),  # no move without approval state
        (State.WATCH, Trigger.SHIFT_STARTED),
        (State.AWAIT_APPROVAL, Trigger.SHIFT_DONE),  # CUTOVER only through SHIFT
        (State.WATCH, Trigger.PROPOSAL_ACCEPTED),  # no rehearsal after cutover (S2-AC5)
        (State.PROPOSE, Trigger.GATE_PASSED),  # a proposal must pass the rehearsal first
        (State.REHEARSE, Trigger.SHIFT_STARTED),
        (State.SAFE_HOLD, Trigger.ABORT_SETTLED),  # SAFE_HOLD is left only by a human
        (State.SAFE_HOLD, Trigger.RUN_DONE),
        (State.GATE, Trigger.GATE_BLOCKED),  # BLOCK in GATE is not a transition
    ],
)
def test_invalid_transitions_rejected(state: State, trigger: Trigger) -> None:
    with pytest.raises(BusinessError, match="state_mismatch"):
        next_state(state, trigger)


def test_safe_hold_is_terminal_for_code() -> None:
    assert not [t for (s, t) in TRANSITIONS if s is State.SAFE_HOLD]


def test_shift_sits_between_approval_and_cutover() -> None:
    """Contract 15 §10.1: no AWAIT_APPROVAL -> CUTOVER shortcut; SHIFT done -> CUTOVER, 1a -> AWAIT_APPROVAL."""
    assert next_state(State.AWAIT_APPROVAL, Trigger.SHIFT_STARTED) is State.SHIFT
    assert next_state(State.SHIFT, Trigger.SHIFT_DONE) is State.CUTOVER
    assert next_state(State.SHIFT, Trigger.SHIFT_REVERTED) is State.AWAIT_APPROVAL
    assert next_state(State.SHIFT, Trigger.RUN_SAFE_HOLD) is State.SAFE_HOLD
    into_cutover = {current for (current, _t), target in TRANSITIONS.items() if target is State.CUTOVER}
    assert into_cutover == {State.SHIFT}
