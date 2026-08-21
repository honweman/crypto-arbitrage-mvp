from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any


async def _complete_market_maker_cycle_on_shutdown(
    cycle: Awaitable[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Finish an in-flight cancel/replace cycle before honoring shutdown."""
    cycle_task = asyncio.ensure_future(cycle)
    try:
        return await asyncio.shield(cycle_task), False
    except asyncio.CancelledError:
        return await cycle_task, True
