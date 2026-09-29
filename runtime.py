"""Hard per-market deadline, including hung DNS, HTTP and provider libraries."""
from __future__ import annotations

import logging
import multiprocessing as mp
import time


def _worker(connection, function, argument):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    try:
        connection.send((True, function(argument)))
    except Exception as exc:
        connection.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def execute_market(function, argument, timeout_seconds=None):
    if timeout_seconds is None:
        from config import SETTINGS

        timeout_seconds = SETTINGS.market_deadline_seconds
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, function, argument))
    started = time.monotonic()
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout_seconds):
            raise TimeoutError(f"{function.__name__}: market deadline exceeded ({timeout_seconds}s)")
        try:
            success, value = receiver.recv()
        except EOFError as exc:
            raise RuntimeError("market worker exited without a result") from exc
        if not success:
            raise RuntimeError(value)
        return value
    finally:
        receiver.close()
        process.join(timeout=1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        logging.getLogger(__name__).info("%s elapsed=%.2fs", function.__name__, time.monotonic() - started)
