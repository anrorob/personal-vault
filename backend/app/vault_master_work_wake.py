"""Coalesced in-process wake-up for the existing Vault Master worker."""

from __future__ import annotations

import asyncio
import logging
from threading import Lock


logger = logging.getLogger("pv.vault-master-worker")


class VaultMasterWorkWake:
    """Wake one worker promptly while retaining timeout-based polling.

    The generation counter makes a signal durable until the worker observes it,
    so signals that arrive before a wait (or while work is running) are not
    lost.  The Event only wakes the current idle wait and naturally coalesces
    repeated publications.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def generation(self) -> int:
        with self._lock:
            return self._generation

    def signal_work_available(self) -> None:
        with self._lock:
            self._generation += 1
            loop = self._loop
            event = self._event
        if loop is not None and event is not None and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

    async def wait_for_work(self, seen_generation: int, timeout: float) -> int:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._event is None or self._loop is not loop:
                self._loop = loop
                self._event = asyncio.Event()
            event = self._event
            current_generation = self._generation
            if current_generation != seen_generation:
                return current_generation
            # Clear before awaiting. A concurrent signal is scheduled after
            # this synchronous section and either sets the Event or advances
            # the generation observed on the next wait.
            event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        with self._lock:
            return self._generation

    def detach_worker_loop(self) -> None:
        with self._lock:
            self._loop = None
            self._event = None


_work_wake = VaultMasterWorkWake()


def get_vault_master_work_wake() -> VaultMasterWorkWake:
    return _work_wake


def signal_arrival_hall_work_available() -> None:
    """Best-effort notification after a completed Arrival Hall publication."""
    try:
        _work_wake.signal_work_available()
    except Exception:
        # Publishing is already complete; periodic polling remains a safe
        # fallback if an in-process notification ever fails unexpectedly.
        logger.warning("Arrival Hall work-available signal failed", exc_info=True)
