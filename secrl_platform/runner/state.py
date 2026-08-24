from __future__ import annotations


ALLOWED_TRANSITIONS = {
    "DRAFT": frozenset({"QUEUED", "CANCELED"}),
    "QUEUED": frozenset({"RUNNING", "CANCELED"}),
    "RUNNING": frozenset(
        {
            "PAUSE_REQUESTED",
            "SUCCEEDED",
            "FAILED",
            "BUDGET_EXHAUSTED",
            "CANCELED",
        }
    ),
    "PAUSE_REQUESTED": frozenset(
        {"PAUSED", "FAILED", "BUDGET_EXHAUSTED", "CANCELED"}
    ),
    "PAUSED": frozenset({"QUEUED", "CANCELED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset({"QUEUED"}),
    "BUDGET_EXHAUSTED": frozenset(),
    "CANCELED": frozenset(),
}


class InvalidTransition(ValueError):
    pass


class RunStateMachine:
    def __init__(self, state: str) -> None:
        if state not in ALLOWED_TRANSITIONS:
            raise ValueError(f"unknown runner state: {state}")
        self.state = state

    def transition(self, target: str) -> None:
        if target not in ALLOWED_TRANSITIONS.get(self.state, frozenset()):
            raise InvalidTransition(f"cannot transition {self.state} to {target}")
        self.state = target

    def request_pause(self) -> None:
        self.transition("PAUSE_REQUESTED")

    def case_committed(self) -> None:
        if self.state == "PAUSE_REQUESTED":
            self.transition("PAUSED")
