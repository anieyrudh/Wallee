"""ControlLease: the multi-writer guard on live runtime execution."""

from __future__ import annotations

import multiprocessing

from wallee.runtime_control import ControlLease


def test_lease_acquire_is_idempotent_and_releases(tmp_path):
    lease = ControlLease(tmp_path / "control.lock")
    assert lease.acquire()
    assert lease.acquire(), "re-acquiring an already-held lease must succeed"
    assert lease.held
    lease.release()
    assert not lease.held
    assert lease.acquire(), "a released lease is acquirable again"
    lease.release()


def test_lease_records_holder_pid(tmp_path):
    import os

    path = tmp_path / "control.lock"
    lease = ControlLease(path)
    assert lease.acquire()
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
    lease.release()


def _try_acquire_in_child(path: str, queue) -> None:
    lease = ControlLease(path)
    queue.put(lease.acquire())


def test_second_process_cannot_take_a_held_lease(tmp_path):
    # flock is per-process; a second *process* must be refused (a second
    # runtime instance is exactly the multi-writer hazard this guards).
    path = tmp_path / "control.lock"
    lease = ControlLease(path)
    assert lease.acquire()

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    child = ctx.Process(target=_try_acquire_in_child, args=(str(path), queue))
    child.start()
    child.join(timeout=30)
    assert not child.is_alive()
    assert queue.get(timeout=5) is False, "a held lease must refuse a second process"

    lease.release()
    child2 = ctx.Process(target=_try_acquire_in_child, args=(str(path), queue))
    child2.start()
    child2.join(timeout=30)
    assert queue.get(timeout=5) is True, "after release the lease is free"
