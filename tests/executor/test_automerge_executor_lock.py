"""The automerge executor lock as a stand-alone owner."""

from __future__ import annotations

import os

import pytest
from git_gate import automerge_queue
from git_gate.automerge_queue import QueueStateError, executor_lock


@pytest.fixture
def lock_path(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(0o700)
    return state_dir / "automerge.lock"


class _Clock:
    """A clock the injected sleep advances, so no test waits for real time."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps: list[float] = []
        self._on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(len(self.sleeps))


def test_busy_lock_without_wait_raises_at_once(lock_path):
    clock = _Clock()
    with executor_lock(lock_path):
        with pytest.raises(QueueStateError, match="AUTOMERGE_PASS_BUSY"):
            with executor_lock(lock_path, clock=clock, sleep=clock.sleep):
                pass
    assert clock.sleeps == []


def test_wait_acquires_once_the_holder_releases_within_the_deadline(lock_path):
    holder = executor_lock(lock_path)
    holder.__enter__()
    waiting_notices = []

    def release_on_second_sleep(count):
        if count == 2:
            holder.__exit__(None, None, None)

    clock = _Clock(release_on_second_sleep)
    with executor_lock(
        lock_path, wait_seconds=10, poll_seconds=1, clock=clock, sleep=clock.sleep,
        on_wait=lambda: waiting_notices.append(clock.now),
    ):
        pass
    assert clock.sleeps == [1, 1]
    assert waiting_notices == [0.0]


def test_wait_past_the_deadline_raises_busy_without_overshooting(lock_path):
    clock = _Clock()
    with executor_lock(lock_path):
        with pytest.raises(QueueStateError, match="AUTOMERGE_PASS_BUSY"):
            with executor_lock(
                lock_path, wait_seconds=2.5, poll_seconds=1, clock=clock, sleep=clock.sleep,
            ):
                pass
    assert clock.sleeps == [1, 1, 0.5]


def test_lock_is_released_when_the_body_raises(lock_path):
    with pytest.raises(RuntimeError):
        with executor_lock(lock_path):
            raise RuntimeError("boom")
    with executor_lock(lock_path):
        pass


def test_descriptor_is_not_inheritable_by_children(lock_path, monkeypatch):
    opened: list[int] = []
    real_open = os.open

    def spy(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(automerge_queue.os, "open", spy)
    with executor_lock(lock_path):
        assert len(opened) == 1
        assert os.get_inheritable(opened[0]) is False


def test_missing_directory_is_unavailable(tmp_path):
    with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_DIRECTORY_UNAVAILABLE"):
        with executor_lock(tmp_path / "absent" / "automerge.lock"):
            pass


def test_group_readable_directory_is_unsafe(lock_path):
    lock_path.parent.chmod(0o750)
    with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_DIRECTORY_UNSAFE"):
        with executor_lock(lock_path):
            pass


def test_symlinked_lock_file_is_unavailable(lock_path):
    target = lock_path.parent / "elsewhere"
    target.write_text("")
    lock_path.symlink_to(target)
    with pytest.raises(QueueStateError, match="AUTOMERGE_LOCK_UNAVAILABLE"):
        with executor_lock(lock_path):
            pass


def test_hard_linked_lock_file_is_unsafe(lock_path):
    lock_path.write_text("")
    lock_path.chmod(0o600)
    os.link(lock_path, lock_path.parent / "second-name")
    with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_FILE_UNSAFE"):
        with executor_lock(lock_path):
            pass


@pytest.fixture
def executor_cgroup(monkeypatch, tmp_path):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpu memory pids\n")
    mounts = tmp_path / "mountinfo"
    mounts.write_text(f"30 1 0:25 / {root} rw - cgroup2 cgroup rw\n")
    membership = tmp_path / "membership"
    membership.write_text("0::/system.slice/merge-guard-automerge.service\n")
    monkeypatch.setattr(automerge_queue, "CGROUP_ROOT", root, raising=False)
    monkeypatch.setattr(automerge_queue, "CGROUP_MOUNTINFO", mounts, raising=False)
    monkeypatch.setattr(automerge_queue, "CGROUP_MEMBERSHIP", membership, raising=False)
    group = root / "system.slice" / "merge-guard-automerge.service"
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    return root, group, mounts, membership


@pytest.mark.parametrize("populated", [0, 1])
def test_executor_cgroup_observes_recursive_population(executor_cgroup, populated):
    _root, group, _mounts, _membership = executor_cgroup
    (group / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
    assert automerge_queue.executor_cgroup_quiescent() is (populated == 0)


def test_executor_cgroup_absence_requires_a_mounted_readable_v2_root(executor_cgroup):
    root, group, mounts, _membership = executor_cgroup
    (group / "cgroup.events").unlink()
    group.rmdir()
    assert automerge_queue.executor_cgroup_quiescent() is True
    mounts.write_text(f"30 1 0:25 / {root} rw - tmpfs tmpfs rw\n")
    with pytest.raises(QueueStateError, match="AUTOMERGE_CGROUP_UNAVAILABLE"):
        automerge_queue.executor_cgroup_quiescent()


@pytest.mark.parametrize("broken", ["root", "missing_events", "malformed", "unreadable"])
def test_executor_cgroup_missing_evidence_never_means_empty(executor_cgroup, monkeypatch, broken):
    root, group, _mounts, _membership = executor_cgroup
    events = group / "cgroup.events"
    if broken == "root":
        (root / "cgroup.controllers").unlink()
    elif broken == "missing_events":
        events.unlink()
    elif broken == "malformed":
        events.write_text("frozen 0\n")
    else:
        original = type(events).read_text
        def unreadable(path, *args, **kwargs):
            if path == events:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(type(events), "read_text", unreadable)
    with pytest.raises(QueueStateError, match="AUTOMERGE_CGROUP_UNAVAILABLE"):
        automerge_queue.executor_cgroup_quiescent()


@pytest.mark.parametrize("membership", [
    "0::/system.slice/merge-guard-automerge.service\n",
    "0::/system.slice/merge-guard-automerge.service/worker\n",
])
def test_paid_review_admission_uses_actual_unit_membership(executor_cgroup, membership):
    _root, _group, _mounts, current = executor_cgroup
    current.write_text(membership)
    automerge_queue.require_review_executor()


@pytest.mark.parametrize("membership", [
    "0::/user.slice/manual.scope\n",
    "0::/system.slice/merge-guard-automerge.service-other\n",
    "1:name=systemd:/system.slice/merge-guard-automerge.service\n",
])
def test_paid_review_admission_rejects_manual_or_wrong_hierarchy(executor_cgroup, membership):
    _root, _group, _mounts, current = executor_cgroup
    current.write_text(membership)
    with pytest.raises(QueueStateError, match="EXECUTOR_SERVICE_REQUIRED"):
        automerge_queue.require_review_executor()
