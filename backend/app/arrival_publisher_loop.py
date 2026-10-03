"""Serial, fair publisher queue draining with a quiet period between activations."""
from __future__ import annotations
import json
import time


def drain_queues(queues, reject, *, quiet_seconds=3, clock=None, sleep=None):
    if quiet_seconds < 3:
        raise ValueError("Quiet period must be at least three seconds")
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + quiet_seconds
    failures = 0
    while True:
        found = False
        for queue, handler in queues:
            request = next(iter(sorted(queue.glob("*.json"))), None)
            if request is None:
                continue
            found = True
            try:
                handler(request)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
                reject(request, str(error))
                failures += 1
            if request.exists():
                raise RuntimeError("Publisher did not retire the selected request")
        if found:
            deadline = clock() + quiet_seconds
            continue
        remaining = deadline - clock()
        if remaining <= 0:
            return failures
        sleep(min(0.25, remaining))
