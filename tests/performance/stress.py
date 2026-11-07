"""
Stress by steps: how many concurrent visitors the current infrastructure holds within the SLO (README.md).

Same Visitor as the baseline, so both are comparable. Each step measures on its own: the stats are reset once the
step has settled, so the ramp-up does not count, and are read when the step ends. The capacity is the last step that
meets the SLO before the first one that fails.
"""

from __future__ import annotations

import os
import time

from locust import LoadTestShape, events

from locustfile import Visitor  # noqa: F401  (Locust finds the user here; importing also registers the listeners)
from slo import measure, stress_report

STEP_USERS = [int(u) for u in os.getenv("STRESS_STEPS", "5,10,20,40,80,160").split(",")]
STEP_SECONDS = float(os.getenv("STRESS_STEP_MINUTES", "3")) * 60
SETTLE_SECONDS = float(os.getenv("STRESS_SETTLE_SECONDS", "20"))
SPAWN_RATE = float(os.getenv("STRESS_SPAWN_RATE", "20"))

steps = []


class SteppedLoad(LoadTestShape):
    def __init__(self):
        super().__init__()
        self.step = -1
        self.measuring_since = None

    def _close_step(self) -> None:
        if self.measuring_since is not None:
            elapsed = time.monotonic() - self.measuring_since
            steps.append(measure(self.runner.stats, STEP_USERS[self.step], elapsed))
            self.measuring_since = None

    def tick(self):
        run_time = self.get_run_time()
        step = int(run_time // STEP_SECONDS)
        if step != self.step:
            self._close_step()
            self.step = step
        if step >= len(STEP_USERS):
            return None
        if self.measuring_since is None and run_time - step * STEP_SECONDS >= SETTLE_SECONDS:
            self.runner.stats.reset_all()
            self.measuring_since = time.monotonic()
        return STEP_USERS[step], SPAWN_RATE


@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    report, capacity = stress_report(steps)
    print(report)
    environment.process_exit_code = 0 if capacity is not None else 1
