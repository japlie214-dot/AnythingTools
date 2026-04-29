# utils/startup/core.py

import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Callable, Awaitable, Tuple
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)


@dataclass
class StartupContext:
    ok: bool = True
    failures: List[str] = field(default_factory=list)
    phases_completed: List[str] = field(default_factory=list)

StartupStep = Callable[[], Awaitable[None]]

class StartupOrchestrator:
    def __init__(self):
        self._tiers: List[List[Tuple[str, StartupStep]]] = []

    def add_sequential(self, phase_name: str, step: StartupStep) -> None:
        self._tiers.append([(phase_name, step)])

    def add_concurrent_tier(self, steps: List[Tuple[str, StartupStep]]) -> None:
        self._tiers.append(steps)

    async def run(self, ctx: StartupContext) -> None:
        for tier in self._tiers:
            if not ctx.ok:
                log.dual_log(tag="Startup:Orchestrator", message="Skipping remaining phases due to prior failure", level="WARNING", payload={"reason": "prior_failure"})
                break

            if len(tier) == 1:
                await self._run_step(ctx, tier[0][0], tier[0][1])
            else:
                tasks = [self._run_step(ctx, name, step) for name, step in tier]
                await asyncio.gather(*tasks)

    async def _run_step(self, ctx: StartupContext, name: str, step: StartupStep) -> None:
        try:
            log.dual_log(
                tag="Startup:Phase",
                message=f"Starting: {name}",
                level="INFO",
                payload={"phase": name, "status": "STARTED"},
            )
            start_t = time.monotonic()
            await step()
            dur = round(time.monotonic() - start_t, 3)
            ctx.phases_completed.append(name)
            log.dual_log(
                tag="Startup:Phase",
                message=f"Completed: {name}",
                level="INFO",
                payload={"phase": name, "status": "SUCCESS", "duration_s": dur},
            )
        except Exception as e:
            ctx.ok = False
            ctx.failures.append(name)
            log.dual_log(
                tag="Startup:Phase",
                message=f"Failed: {name}",
                level="CRITICAL",
                exc_info=e,
                payload={"phase": name, "status": "CRITICAL_FAILURE", "error": str(e)},
            )
            raise RuntimeError(f"Startup phase '{name}' failed: {e}") from e
