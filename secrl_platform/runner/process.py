from __future__ import annotations

from secrl_platform.runner.engine import RunnerEngine


class RunnerProcess:
    """Small process boundary used by CLI/background worker orchestration."""

    def __init__(self, engine: RunnerEngine) -> None:
        self._engine = engine

    async def run_once(self, task_id: str, run_id: str) -> str:
        return await self._engine.run(task_id, run_id)
