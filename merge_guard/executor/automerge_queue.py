"""Scheduling state for the merge-guard executor.

The forge, CI evidence and review receipts remain authoritative. This file only
remembers which PR owns the next turn and why a previous PR was parked.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator, Iterable

from .repository import _validate_lock_fd

STATE_PATH = Path("/var/lib/merge-guard/automerge.json")
EXECUTOR_LOCK_PATH = STATE_PATH.with_name("automerge.lock")
LOCK_POLL_SECONDS = 1.0
# A deploy waits for at most the current bounded review before taking admission.
DEPLOY_EXECUTOR_WAIT_SECONDS = 1500.0
# Both paid-review admission and deploy use this same native unit boundary.
CGROUP_ROOT = Path("/sys/fs/cgroup")
CGROUP_MOUNTINFO = Path("/proc/self/mountinfo")
CGROUP_MEMBERSHIP = Path("/proc/self/cgroup")
EXECUTOR_CGROUP = "system.slice/merge-guard-automerge.service"
VERSION = 1
MAX_BYTES = 256 * 1024
SHA = re.compile(r"[0-9a-f]{40}\Z")
# A retained or parked reason code, as the executor journals it.
QUEUE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
# The train operation owns the train shape; the row repeats only what it
# must check without importing an operation module.
TRAIN_BRANCH = re.compile(
    r"train/([0-9a-f]{12})-[1-9][0-9]{0,8}(?:-g([0-9a-f]{32}))?\Z"
)
TRAIN_PHASES = frozenset({"owner_wait", "owner_parked", "merging", "cleanup"})
TRAIN_MAX_MEMBERS = 3
TRAIN_CLOSE_REASON = re.compile(r"[A-Za-z0-9_#:,. -]{1,200}\Z")
# The deploy hold a row last published `queue=held` for: the deploy marker's
# plan id, or "-" when the marker names none.
HOLD_ID = re.compile(r"[A-Za-z0-9._:+-]{1,128}\Z")
# Who must act before a parked PR can move. The executor publishes this as the
# `owner=` word of the `gate/queue` commit status.
PARK_OWNERS = {
    "changes_requested": "author", "conflict": "author", "ci_failed": "author",
    "claim_unavailable": "author",
    "review_execution_failed": "operator", "gate_blocked": "operator",
    "ci_stalled": "queue", "evidence_unavailable": "queue", "mergeability_unknown": "queue",
    # The head-of-line guard: retained with one code outside the CI-wait and
    # review families on too many passes in a row. The park keeps the code.
    "stuck": "operator",
    # No CI run was ever created for the active head (a forge branch update
    # that skipped its hooks, so no pull_request event fired). A new push
    # starts CI; the park ends once CI for that head appears or the head
    # changes.
    "ci_run_missing": "operator",
    # Like `ci_run_missing`, but the gate read `refs/pull/<n>/head` on another
    # commit than the branch tip: the forge lost the post-receive step of a
    # push, so it never rewrote the ref nor started CI. The queue has no push
    # that repairs it (main did not move past the PR's merge base, or the gate
    # refused the refresh). The park ends as `ci_run_missing` does, or when
    # main moves, so the queue's own recovery refresh pushes the repair.
    "pull_ref_stale": "operator",
    # The PR head adds nothing to `main` (merged by hand while the forge still
    # lists the PR as open). Nothing the queue does can land it, so it spends
    # no refresh, CI run or review on it. A new head releases the park through
    # `_parked_changed`; the operator closes the PR or unparks it.
    "already_in_base": "operator",
    # A confirmed failed Renovate head needs operator recovery; unrelated
    # main movement cannot spend another CI run on the same failure.
    "renovate_ci_failed": "operator",
    # A rejected Renovate PR: its author is a bot that never answers a review.
    # Corrected content needs fresh review; unpark does not dismiss rejection.
    "renovate_changes_requested": "operator",
    # A major Renovate update, or one whose note asks for a step before merge:
    # parked before any review until the operator removes its `major` or
    # `premerge` label. Each waits by design, so `park_wait_s` skips both.
    "renovate_major": "operator",
    "renovate_premerge": "operator",
    # A Renovate PR whose listed labels could not be read: held, because it
    # cannot be told from one labelled `major` or `premerge`. It counts toward
    # `park_wait_s`, and ends once the labels read again.
    "renovate_labels_unknown": "operator",
    # A Renovate PR whose forge label history shows `major` or `premerge`
    # but which has no consent record: the label went before any pass parked
    # it. Adding the label back and removing it again consents. It counts
    # toward `park_wait_s`.
    "renovate_consent_missing": "operator",
    # The author put the PR on hold with the `gate/hold` label (the hold command).
    # Parked before any refresh, review, train membership or merge, on every
    # head, until the label is gone (the release command); no consent to record.
    # The author chose to wait, so `park_wait_s` skips it.
    "author_hold": "author",
    # A PR whose listed labels could not be read: a hold may sit behind them,
    # so it is not admitted. Like `renovate_labels_unknown` it counts toward
    # `park_wait_s` and ends once the labels read again.
    "hold_labels_unknown": "operator",
}
PARK_REASONS = frozenset(PARK_OWNERS)
# The hold a consented Renovate PR returns to on an unproven head change.
# A row's `consented` entry maps the PR number to
# `[consented head, reason]`.
CONSENT_REASONS = frozenset({"renovate_major", "renovate_premerge"})


def parked_details(parked: dict[str, Any], consent: list[str] | None) -> dict[str, Any]:
    """One advisory projection for local status and the forge status writer.

    The persisted reason remains the hold to renew. A consent on another
    head explains that park; it is not evidence of a currently attached label.
    """
    reason = parked["reason"]
    details = {"reason": reason, "owner": PARK_OWNERS[reason]}
    if consent is not None and reason == consent[1] and parked["head"] != consent[0]:
        details.update(reason="renovate_consent_stale", consented_head=consent[0],
                       hold_label=reason.removeprefix("renovate_"))
    return details


class QueueStateError(RuntimeError):
    """The executor cannot safely use its scheduling state."""


def _require_cgroup_v2_root() -> None:
    try:
        mounts = CGROUP_MOUNTINFO.read_text(encoding="utf-8").splitlines()
        mounted = False
        for line in mounts:
            fields, separator, filesystem = line.partition(" - ")
            columns = fields.split()
            if (separator and len(columns) >= 5 and columns[4] == str(CGROUP_ROOT)
                    and filesystem.split()[0] == "cgroup2"):
                mounted = True
                break
        if not mounted:
            raise QueueStateError("AUTOMERGE_CGROUP_UNAVAILABLE")
        (CGROUP_ROOT / "cgroup.controllers").read_text(encoding="utf-8")
    except (OSError, UnicodeError, IndexError) as exc:
        raise QueueStateError("AUTOMERGE_CGROUP_UNAVAILABLE") from exc


def executor_cgroup_quiescent() -> bool:
    """Prove recursive unit emptiness after deploy owns the executor flock.

    Process-group cleanup cannot cover workers in separate sessions. Kernel
    population includes descendants; absence proves emptiness only underneath
    a mounted, readable unified hierarchy. Never infer it from a unit status.
    """
    _require_cgroup_v2_root()
    group = CGROUP_ROOT / EXECUTOR_CGROUP
    try:
        events = (group / "cgroup.events").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        try:
            group.stat()
        except FileNotFoundError:
            return True
        except OSError as absent:
            raise QueueStateError("AUTOMERGE_CGROUP_UNAVAILABLE") from absent
        raise QueueStateError("AUTOMERGE_CGROUP_UNAVAILABLE") from exc
    except (OSError, UnicodeError) as exc:
        raise QueueStateError("AUTOMERGE_CGROUP_UNAVAILABLE") from exc
    populated = [line.split() for line in events.splitlines() if line.startswith("populated ")]
    if populated == [["populated", "0"]]:
        return True
    if populated == [["populated", "1"]]:
        return False
    raise QueueStateError("AUTOMERGE_CGROUP_UNAVAILABLE")


def require_review_executor() -> None:
    """A foreground paid review has no native containment owner for deploy."""
    _require_cgroup_v2_root()
    try:
        memberships = CGROUP_MEMBERSHIP.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise QueueStateError("AUTOMERGE_CGROUP_UNAVAILABLE") from exc
    unified = [line.removeprefix("0::") for line in memberships if line.startswith("0::")]
    unit = "/" + EXECUTOR_CGROUP
    if len(unified) != 1 or not (unified[0] == unit or unified[0].startswith(unit + "/")):
        raise QueueStateError("EXECUTOR_SERVICE_REQUIRED")


def empty_state() -> dict[str, Any]:
    return {"version": VERSION, "repositories": {}, "cursor": 0}


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise QueueStateError("AUTOMERGE_STATE_DIRECTORY_UNSAFE")


def _regular_private(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise QueueStateError("AUTOMERGE_STATE_FILE_UNSAFE")


def _validate(state: Any, repositories: frozenset[str]) -> dict[str, Any]:
    def run_valid(run: Any) -> bool:
        return (isinstance(run, list) and len(run) == 2
                and all(item is None or type(item) is int and item > 0 for item in run)
                and (run[0] is not None or run[1] is None))

    def time_valid(value: Any) -> bool:
        return type(value) in {int, float} and math.isfinite(value) and value >= 0

    def number_key(key: Any) -> bool:
        return isinstance(key, str) and key.isdecimal() and int(key) > 0 and str(int(key)) == key

    def pr_valid(value: Any) -> bool:
        return type(value) is int and value > 0

    def sha_valid(value: Any) -> bool:
        return isinstance(value, str) and SHA.fullmatch(value) is not None

    def code_valid(value: Any) -> bool:
        return isinstance(value, str) and QUEUE_CODE.fullmatch(value) is not None

    def train_valid(train: Any, active_pr: Any) -> bool:
        # The batched merge train holding the active slot; its first member
        # is the active PR. The gate re-derives everything it acts on, so a
        # malformed row is corrupt state, never a train to guess at.
        if not isinstance(train, dict) or not (
                {"pr", "branch", "base", "started", "members"} <= set(train)
                <= {"pr", "branch", "base", "started", "members", "merged", "prefixes", "cleanup",
                    "mode", "generation", "phase"}):
            return False
        members = train["members"]
        # The members this executor merged itself, in merge order.
        merged = train.get("merged", [])
        branch = TRAIN_BRANCH.fullmatch(train["branch"]) if isinstance(train["branch"], str) else None
        if branch is not None and branch.group(2) is not None:
            if (train.get("mode") != "full" or train.get("generation") != branch.group(2)
                    or not isinstance(train.get("phase"), str)
                    or train["phase"] not in TRAIN_PHASES):
                return False
        elif {"mode", "generation", "phase"} & set(train):
            return False
        return (pr_valid(train["pr"]) and sha_valid(train["base"])
                and branch is not None and train["base"].startswith(branch.group(1))
                and time_valid(train["started"])
                and isinstance(members, list) and 2 <= len(members) <= TRAIN_MAX_MEMBERS
                and all(isinstance(member, dict) and set(member) == {"pr", "head", "candidate"}
                        and pr_valid(member["pr"]) and sha_valid(member["head"])
                        and sha_valid(member["candidate"]) for member in members)
                and len({member["pr"] for member in members}) == len(members)
                and members[0]["pr"] == active_pr
                and train["pr"] not in {member["pr"] for member in members}
                and isinstance(merged, list)
                and all(type(pr) is int and pr in {member["pr"] for member in members}
                        for pr in merged)
                and len(set(merged)) == len(merged)
                and ("prefixes" not in train or prefixes_valid(train, members))
                and ("cleanup" not in train or (
                    isinstance(train["cleanup"], dict)
                    and set(train["cleanup"]) == {"reason", "outcome"}
                    and isinstance(train["cleanup"]["reason"], str)
                    and TRAIN_CLOSE_REASON.fullmatch(train["cleanup"]["reason"]) is not None
                    and isinstance(train["cleanup"]["outcome"], str)
                    and train["cleanup"]["outcome"] in {"closed", "merged"})))

    def prefixes_valid(train: dict[str, Any], members: list[dict[str, Any]]) -> bool:
        prefixes = train["prefixes"]
        if not isinstance(prefixes, list) or len(prefixes) != len(members):
            return False
        member_prs = {member["pr"] for member in members}
        full = train.get("mode") == "full"
        for position, (prefix, member) in enumerate(zip(prefixes, members), 1):
            branch = (train["branch"] if full or position == len(members)
                      else f"{train['branch']}-prefix-{position}")
            if (not isinstance(prefix, dict)
                    or set(prefix) != {"position", "pr", "branch", "candidate"}
                    or type(prefix["position"]) is not int or prefix["position"] != position
                    or not pr_valid(prefix["pr"]) or prefix["pr"] in member_prs
                    or prefix["branch"] != branch or prefix["candidate"] != member["candidate"]):
                return False
        if full:
            return all(prefix["pr"] == train["pr"] for prefix in prefixes)
        return (prefixes[-1]["pr"] == train["pr"]
                and len({prefix["pr"] for prefix in prefixes}) == len(prefixes))

    # `last_pass` is the host clock when the previous pass ended; it is
    # optional, so an older state file still loads.
    if (not isinstance(state, dict)
            or not {"version", "repositories", "cursor"} <= set(state)
            or not set(state) <= {"version", "repositories", "cursor", "last_pass"}
            or type(state["version"]) is not int or state["version"] != VERSION
            or not isinstance(state["repositories"], dict)
            or type(state["cursor"]) is not int or state["cursor"] < 0
            or "last_pass" in state and not time_valid(state["last_pass"])):
        raise QueueStateError("AUTOMERGE_STATE_INVALID")
    # `started`, `base`, `since`, `published`, `review_ahead`, `train`,
    # `retained`, `unmergeable`, `ci_missing`, `refreshed`, `code`, `held`,
    # `consented`, `agent_streak` and `refreshed_head` were added later and are optional, so
    # older state files still load. `refreshed_head` names the executor's last
    # refresh of an active Renovate PR: a red run on it is not refreshed again.
    # `progress` is the active run's last CI state and when it was first seen
    #: the stall budget counts from its change.
    for name, row in state["repositories"].items():
        if (name not in repositories or not isinstance(row, dict)
                or not {"active", "parked"} <= set(row)
                or not set(row) <= {"active", "parked", "published", "review_ahead",
                                    "consented", "held", "agent_streak"}
                or not isinstance(row["parked"], dict)
                or not isinstance(row.get("published", {}), dict)
                or not isinstance(row.get("consented", {}), dict)
                or "held" in row and not (
                    isinstance(row["held"], str) and HOLD_ID.fullmatch(row["held"]))
                or "review_ahead" in row and (
                    type(row["review_ahead"]) is not int or row["review_ahead"] <= 0)
                or "agent_streak" in row and (
                    type(row["agent_streak"]) is not int or row["agent_streak"] < 0)):
            raise QueueStateError("AUTOMERGE_STATE_INVALID")
        for key, consent in row.get("consented", {}).items():
            if (not number_key(key) or not isinstance(consent, list) or len(consent) != 2
                    or not sha_valid(consent[0]) or not isinstance(consent[1], str)
                    or consent[1] not in CONSENT_REASONS):
                raise QueueStateError("AUTOMERGE_STATE_INVALID")
        for key, published in row.get("published", {}).items():
            if (not number_key(key) or not isinstance(published, list) or len(published) != 2
                    or not isinstance(published[0], str) or not SHA.fullmatch(published[0])
                    or not isinstance(published[1], str)):
                raise QueueStateError("AUTOMERGE_STATE_INVALID")
        active = row["active"]
        if active is not None and (not isinstance(active, dict)
                or not {"pr", "head", "base", "run", "wait_since"} <= set(active)
                or not set(active) <= {"pr", "head", "base", "run", "wait_since", "started",
                                       "train", "retained", "unmergeable", "ci_missing",
                                       "refreshed", "refreshed_head", "progress"}
                or type(active.get("started", False)) is not bool
                or "ci_missing" in active and not (
                    isinstance(active["ci_missing"], list) and len(active["ci_missing"]) == 2
                    and sha_valid(active["ci_missing"][0])
                    and time_valid(active["ci_missing"][1]))
                or "refreshed" in active and not sha_valid(active["refreshed"])
                or "refreshed_head" in active and not sha_valid(active["refreshed_head"])
                or "progress" in active and not (
                    isinstance(active["progress"], list) and len(active["progress"]) == 2
                    and code_valid(active["progress"][0])
                    and time_valid(active["progress"][1]))
                or "unmergeable" in active and not (
                    isinstance(active["unmergeable"], list) and len(active["unmergeable"]) == 3
                    and sha_valid(active["unmergeable"][0])
                    and sha_valid(active["unmergeable"][1])
                    and time_valid(active["unmergeable"][2]))
                or "retained" in active and not (
                    isinstance(active["retained"], list) and len(active["retained"]) == 2
                    and code_valid(active["retained"][0])
                    and pr_valid(active["retained"][1]))
                or "train" in active and not train_valid(active["train"], active["pr"])
                or type(active["pr"]) is not int or active["pr"] <= 0
                or not isinstance(active["head"], str) or not SHA.fullmatch(active["head"])
                or not isinstance(active["base"], str) or not SHA.fullmatch(active["base"])
                or not run_valid(active["run"])
                or not time_valid(active["wait_since"])):
            raise QueueStateError("AUTOMERGE_STATE_INVALID")
        for key, parked in row["parked"].items():
            if (not number_key(key)
                    or not isinstance(parked, dict)
                    or not {"reason", "head", "run", "review"} <= set(parked)
                    or not set(parked) <= {"reason", "head", "run", "review", "base", "since", "claim_sha",
                                           "code"}
                    or "code" in parked and not code_valid(parked["code"])
                    or "claim_sha" in parked and (
                        parked["claim_sha"] is not None and (
                            not isinstance(parked["claim_sha"], str)
                            or len(parked["claim_sha"]) != 64
                            or any(ch not in "0123456789abcdef" for ch in parked["claim_sha"])))
                    or "base" in parked and (
                        not isinstance(parked["base"], str) or not SHA.fullmatch(parked["base"]))
                    or "since" in parked and not time_valid(parked["since"])
                    or not isinstance(parked["reason"], str)
                    or parked["reason"] not in PARK_REASONS
                    or not isinstance(parked["head"], str) or not SHA.fullmatch(parked["head"])
                    or not run_valid(parked["run"])
                    or not isinstance(parked["review"], list) or len(parked["review"]) != 2
                    or (parked["review"][0] is not None
                        and (type(parked["review"][0]) is not int or parked["review"][0] <= 0))
                    or (parked["review"][1] is not None
                        and (not isinstance(parked["review"][1], str)
                        or parked["review"][1] not in {
                            "REQUEST_CHANGES", "human_changes_requested", "none"}))):
                raise QueueStateError("AUTOMERGE_STATE_INVALID")
    return state


@contextmanager
def executor_lock(
    lock_path: Path = EXECUTOR_LOCK_PATH,
    *,
    wait_seconds: float = 0.0,
    poll_seconds: float = LOCK_POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_wait: Callable[[], None] | None = None,
) -> Generator[None, None, None]:
    """Hold the exclusive automerge executor lock for the whole `with` body.

    The one owner of this lock: `QueueStore.locked()` takes it without
    waiting for each pass. A local deploy holds it from pinning through
    source apply and environment reconciliation. A remote deploy holds it
    through the same source boundary, with exact pin revalidation after
    acquisition.
    With `wait_seconds > 0` a busy lock is polled until the deadline.
    `on_wait` is called once when the first attempt found it busy. A lock
    still busy raises `AUTOMERGE_PASS_BUSY`. The descriptor is close-on-exec,
    and ordinary executor and CLI commands receive no lock descriptor.
    A retained command has a scoped helper that keeps the native descriptor
    open until the command's descendants have settled.
    """
    try:
        _private_directory(lock_path.parent)
    except (FileNotFoundError, PermissionError) as exc:
        raise QueueStateError("AUTOMERGE_STATE_DIRECTORY_UNAVAILABLE") from exc
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise QueueStateError("AUTOMERGE_LOCK_UNAVAILABLE") from exc
    try:
        if not _validate_lock_fd(fd, os.geteuid()) or os.fstat(fd).st_nlink != 1:
            raise QueueStateError("AUTOMERGE_STATE_FILE_UNSAFE")
        deadline = clock() + wait_seconds
        waited = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise QueueStateError("AUTOMERGE_PASS_BUSY") from exc
            if not waited and on_wait is not None:
                on_wait()
            waited = True
            sleep(min(poll_seconds, remaining))
        yield
    finally:
        os.close(fd)


class QueueStore:
    """The scheduling state of exactly the repositories one executor manages.

    `repositories` is the executor's own selection. A row naming any other
    repository makes the whole state invalid rather than silently ignored.
    """

    def __init__(self, path: Path = STATE_PATH, *, repositories: Iterable[str]) -> None:
        self.path = path
        self.repositories = frozenset(repositories)

    @contextmanager
    def locked(self) -> Generator[dict[str, Any], None, None]:
        """Hold the distinct, nonblocking executor lock for the whole pass."""
        with executor_lock(self.path.with_name("automerge.lock")):
            self._sweep_temporaries()
            yield self.read()

    def _sweep_temporaries(self) -> None:
        """Remove temporaries a SIGKILL left between mkstemp and os.replace.

        Only the lock holder writes, so no live writer owns one.
        """
        for stale in self.path.parent.glob(".automerge-*"):
            try:
                stale.unlink()
            except OSError:
                pass

    def read(self) -> dict[str, Any]:
        try:
            _private_directory(self.path.parent)
        except FileNotFoundError as exc:
            raise QueueStateError("AUTOMERGE_STATE_DIRECTORY_UNAVAILABLE") from exc
        try:
            _regular_private(self.path)
            if self.path.stat().st_size > MAX_BYTES:
                raise QueueStateError("AUTOMERGE_STATE_INVALID")
            with self.path.open("r", encoding="utf-8") as handle:
                return _validate(json.load(handle), self.repositories)
        except FileNotFoundError:
            return empty_state()
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise QueueStateError("AUTOMERGE_STATE_INVALID") from exc

    def save(self, state: dict[str, Any]) -> None:
        _validate(state, self.repositories)
        payload = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
        if len(payload) > MAX_BYTES:
            raise QueueStateError("AUTOMERGE_STATE_FULL")
        _private_directory(self.path.parent)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".automerge-", dir=self.path.parent)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists() or self.path.is_symlink():
                _regular_private(self.path)
            os.replace(temporary, self.path)
            temporary = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise QueueStateError("AUTOMERGE_STATE_WRITE_FAILED") from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
