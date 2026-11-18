"""Run merge-guard for one repository: publish the review status, or merge.

Ported from mikomiel/ci-burst `scripts/github_pr_automation.py` at main
31a0fefe: the same trusted-context guard, listing/deep-collection split,
revocation ordering, unchanged-status dedup, merge postcondition and
one-merge-per-run, with the repository constants replaced by the consumer's
policy file.

The engine reads the forge only through the port in ``forge.py``; the GitHub
and Forgejo adapters answer it.

Local execution is a dry run. Writes need `--apply` *and* a trusted workflow
context bound to the default branch, so a pull-request run cannot publish or
merge.
"""

from __future__ import annotations

import os
import time
from typing import Any

from . import policy as mg
from .forge import ApiError, Forge
from .github import GitHub  # noqa: F401 -- importable from here, as before the port

SAFE_LOGIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
MAINTAINER_PERMISSIONS = {"write", "maintain", "admin"}
#: A Forgejo login can also hold "_" and ".", and Forgejo reports the
#: permission of a repository owner as "owner". These apply on Forgejo only.
FORGEJO_SAFE_LOGIN = SAFE_LOGIN | {"_", "."}
FORGEJO_MAINTAINER_PERMISSIONS = MAINTAINER_PERMISSIONS | {"owner"}


def _forge_name(api: Forge) -> str:
    """The adapter's forge. An object without a name is read as GitHub, as before the port."""
    return getattr(api, "name", "github")


def maintainer_ids(api: Forge, reviews: Any, author: str) -> set[int]:
    """Numeric ids the API confirms have write, maintain or admin permission.

    Reviews are pre-filtered before the API call: human identity, not the
    author, a decisive state and a login usable as an API path segment. The
    permission payload must name the same user id, and each user is asked once.
    On Forgejo a login can also hold "_" and ".", and "owner" also counts.
    """
    if not isinstance(reviews, list):
        raise ApiError("reviews_unusable")
    forgejo = _forge_name(api) == "forgejo"
    alphabet = FORGEJO_SAFE_LOGIN if forgejo else SAFE_LOGIN
    granted = FORGEJO_MAINTAINER_PERMISSIONS if forgejo else MAINTAINER_PERMISSIONS
    ids: set[int] = set()
    seen: set[Any] = set()
    for item in reviews:
        if not isinstance(item, dict):
            raise ApiError("review_unusable")
        user = item.get("user")
        if not isinstance(user, dict):
            raise mg.Unusable("review_user_unusable")
        login = user.get("login")
        if (user.get("type") != "User" or login == author
                or item.get("state") not in mg.DECISIVE):
            continue
        if (not isinstance(login, str) or not login
                or any(char not in alphabet for char in login)
                # On Forgejo, a leading "." or a ".." could name another path.
                or (forgejo and (login.startswith(".") or ".." in login))):
            raise ApiError("maintainer_identity_unusable")
        if user.get("id") in seen:
            continue
        seen.add(user.get("id"))
        permission = api.request(f"collaborators/{login}/permission")
        if (not isinstance(permission, dict)
                or (permission.get("user") or {}).get("id") != user.get("id")):
            raise ApiError("maintainer_permission_unproven")
        if permission.get("permission") in granted:
            ids.add(user["id"])
    return ids


def with_edit_state(api: Forge, pr: dict[str, Any], reviews: list[Any],
                    maintainers: set[int], policy: mg.Policy) -> list[Any]:
    """The reviews, with ``edit_state`` on each approval that the verdict counts.

    Only a policy with ``require_reviewed_head`` reads it: any other gets
    ``reviews`` back, and the forge is not asked. The line is never read from the
    REST body. ``edit_state`` is built here from one answer of the forge, and a
    key of that name that a review already carried is dropped, so nothing else
    can supply it.

    The approvals are those of ``mg.approvals_at_head`` for these maintainers,
    joined to the answer by review id. One that the answer lacks, and a record
    that is not usable, are refused: a review is never read as unedited because
    the forge was silent. A pull request with no such approval has nothing to
    read, so the forge is not asked.
    """
    if not policy.require_reviewed_head:
        return reviews
    counted = {item["id"] for item in mg.approvals_at_head(
        reviews, pr["head"]["sha"], pr["user"]["login"], maintainers, policy)}
    states = api.review_edit_state(pr["number"]) if counted else {}
    attached: list[Any] = []
    for item in reviews:
        review = {key: value for key, value in item.items() if key != "edit_state"}
        if item["id"] in counted:
            state = states.get(item["id"]) if isinstance(states, dict) else None
            if not mg.edit_state_usable(state):
                raise ApiError("review_edit_state_unusable")
            review["edit_state"] = {"body": state["body"],
                                    "last_edited_at": state["last_edited_at"]}
        attached.append(review)
    return attached


def _carried_review(reviews: list[Any], head: str, policy: mg.Policy) -> str | None:
    """The configured reviewer's latest decisive review, when it approved an older head."""
    latest: dict[str, Any] | None = None
    for item in reviews:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        if not isinstance(user, dict) or not mg.is_configured_reviewer(user, policy):
            continue
        if item.get("state") not in mg.DECISIVE or type(item.get("id")) is not int:
            continue
        if latest is None or item["id"] > latest["id"]:
            latest = item
    if latest is None or latest["state"] != "APPROVED":
        return None
    commit = latest.get("commit_id")
    if not mg.valid_sha(commit) or commit == head:
        return None
    return commit


def _patch_snapshot(comparison: Any) -> list[tuple[Any, Any, Any, Any]] | None:
    """Every file's own patch, or None when any patch is missing (binary, too large)."""
    if not isinstance(comparison, dict):
        return None
    items = comparison.get("files")
    if not isinstance(items, list):
        return None
    snapshot = []
    for item in items:
        if not isinstance(item, dict) or "patch" not in item:
            return None
        snapshot.append((item.get("filename"), item.get("previous_filename"),
                         item.get("status"), item.get("patch")))
    return sorted(snapshot, key=lambda row: (str(row[0]), str(row[1])))


def carried_heads(api: Forge, pr: dict[str, Any], reviews: list[Any],
                  policy: mg.Policy) -> set[str]:
    """Heads the reviewer approved whose own change is identical to the current one.

    Both sides are the same comparison shape: the current base against the head,
    and the current base against the older head. That is the pull request's own
    change in each case, including renames and conflict-resolution merges.

    A policy that turns ``carry_unchanged_diff`` off gets no carry at all: an
    unchanged patch is not an approval of the commit that carries it, and that
    repository binds every approval to its own head. So does one that requires
    the reviewed head, whose attestation names exactly one commit.
    """
    if not policy.carry_unchanged_diff or policy.require_reviewed_head:
        return set()
    old = _carried_review(reviews, pr["head"]["sha"], policy)
    if old is None:
        return set()
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    current = _patch_snapshot(api.compare(base, head))
    previous = _patch_snapshot(api.compare(base, old))
    if current is None or previous is None or not current:
        return set()
    changed = pr.get("changed_files")
    # The compare endpoint returns at most 300 files without paging. A truncated
    # list would compare only the visible files, so a difference past the cap
    # could carry an approval it should not.
    if type(changed) is not int or changed < 1 or len(current) != changed:
        return set()
    if len(current) >= 300 or len(previous) >= 300:
        return set()
    return {old} if current == previous else set()


def collect_reviews(api: Forge, known_heads: set[str], policy: mg.Policy) -> list[dict[str, Any]]:
    """List first (recording every head), then deep-read each open pull.

    The four refusal codes below name the forge: ``github_...`` is unchanged on
    GitHub, and Forgejo gets ``forgejo_...``.
    """
    forge = _forge_name(api)
    listing = api.pulls()
    if not isinstance(listing, list):
        raise ApiError(f"{forge}_pull_listing_unusable")
    for entry in listing:
        head = (entry.get("head") or {}).get("sha") if isinstance(entry, dict) else None
        if not mg.valid_sha(head):
            raise ApiError(f"{forge}_pull_listing_head_unusable")
        known_heads.add(head)
    snapshot: list[dict[str, Any]] = []
    for entry in listing:
        pr = api.pull(entry.get("number"))
        if not isinstance(pr, dict):
            raise ApiError(f"{forge}_pull_unreadable")
        if pr.get("state") == "closed":
            continue
        if not mg.pull_scope(pr, policy):
            raise ApiError(f"{forge}_pull_scope_unusable")
        pr["reviews"] = api.reviews(pr["number"])
        pr["head_committed_at"] = api.commit_time(pr["head"]["sha"])
        pr["maintainers"] = sorted(maintainer_ids(api, pr["reviews"], pr["user"]["login"]))
        pr["reviews"] = with_edit_state(api, pr, pr["reviews"], set(pr["maintainers"]), policy)
        pr["carried_heads"] = sorted(carried_heads(api, pr, pr["reviews"], policy))
        snapshot.append(pr)
    return snapshot


def run_review(api: Forge, policy: mg.Policy, apply: bool, event_head: str | None) -> dict[str, Any]:
    known = {event_head} if mg.valid_sha(event_head) else set()
    try:
        snapshot = collect_reviews(api, known, policy)
        plan = mg.review_plan(snapshot, policy)
        for pr in snapshot:
            if not mg.same_refs(pr, api.pull(pr["number"]), policy):
                raise ApiError("review_snapshot_refs_changed")
        # A head whose review verdict is a success can still be blocked by a
        # required check that never ran. The roll-up then simply lacks the
        # context, so the status has to name it (#14); a present-but-red run
        # stays the checks context's signal, not this one.
        approved = sorted({pr["head"]["sha"] for pr in snapshot
                           if plan[pr["head"]["sha"]][0] == "success"})
        for head in approved:
            try:
                missing = mg.missing_required_check(api.check_runs(head), head, policy)
            except (mg.Unusable, ApiError) as exc:
                # A check snapshot that cannot be read for this head is local to
                # it: the other heads still get their status, and this one names
                # the reason instead of aborting the whole run. ApiError covers
                # transport failures and incomplete pagination as well.
                plan[head] = ("pending", f"required_check_unknown: {exc}")
                continue
            if missing is not None:
                plan[head] = ("pending", f"required_check_missing: {missing}")
    except (ApiError, mg.Unusable):
        if apply:
            for head in sorted(known):
                try:
                    api.publish(head, "pending", "review_snapshot_unavailable")
                except ApiError:
                    pass
        raise
    for head in known - plan.keys():
        plan[head] = ("pending", "no_open_pull_carries_head")
    if apply:
        # Every non-success verdict first. A failed revocation aborts before any
        # success is published, so a broken context cannot leave an old green.
        failed = False
        for head, (state, reason) in sorted(plan.items()):
            if state == "success":
                continue
            try:
                api.publish(head, state, reason)
            except ApiError:
                failed = True
        if failed:
            raise ApiError("review_publication_incomplete")
        for head, (state, reason) in sorted(plan.items()):
            if state == "success":
                api.publish(head, state, reason)
    return {"heads": {head: verdict for head, verdict in plan.items()}}


#: Cleanup is best effort and must never threaten the report of a proven merge:
#: the job that merges has ten minutes, so the close attempt is bounded in both
#: requests and wall-clock time, and whatever is left is reported as unclosed.
MAX_CLOSES = 3
CLOSING_DEADLINE_SECONDS = 20.0


def close_referenced_issues(api: Forge, pull_number: int) -> tuple[list[int], list[int], bool]:
    """Close the issues the forge itself says this merge closes, if still open.

    The forge parses the body; the engine never does. GitHub skips its own
    auto-close when the identity that performs the merge lacks `issues: write`,
    so this repeats the same authoritative list (`closingIssuesReferences`)
    instead of reimplementing the parser.
    """
    closed: list[int] = []
    refused: list[int] = []
    unread = False
    # The budget starts before the list read: the read is cleanup traffic too.
    deadline = time.monotonic() + CLOSING_DEADLINE_SECONDS
    try:
        numbers = api.closing_issues(pull_number)
    except (ApiError, mg.Unusable):
        # The merge is proven; a cleanup that could not be evaluated is reported
        # as unread rather than as "this merge names no issues".
        return closed, refused, True
    for index, number in enumerate(numbers):
        if index >= MAX_CLOSES or time.monotonic() >= deadline:
            # Named but not attempted: reported, never silently dropped.
            refused.extend(numbers[index:])
            break
        if number == pull_number:
            continue
        try:
            issue = api.request(f"issues/{number}")
            if (not isinstance(issue, dict) or issue.get("pull_request")
                    or issue.get("state") != "open"):
                continue
            api.close_issue(number)
            closed.append(number)
        except (ApiError, mg.Unusable):
            refused.append(number)
    return closed, refused, unread


def run_merge(api: Forge, policy: mg.Policy, apply: bool) -> list[dict[str, Any]]:
    snapshot = collect_reviews(api, set(), policy)
    plan = mg.review_plan(snapshot, policy)
    counts: dict[str, int] = {}
    for pr in snapshot:
        counts[pr["head"]["sha"]] = counts.get(pr["head"]["sha"], 0) + 1
    decisions: list[dict[str, Any]] = []
    for pr in snapshot:
        head, base, number = pr["head"]["sha"], pr["base"]["sha"], pr["number"]
        decision = {"number": number, "head": head, "eligible": False, "merged": False}
        decisions.append(decision)
        if counts[head] > 1:
            decision["reason"] = "shared_head"
            continue
        if plan[head][0] != "success":
            decision["reason"] = plan[head][1]
            continue
        # The list view carries no mergeable/changed_files: re-read the PR.
        current = api.pull(number)
        if not mg.same_refs(pr, current, policy) or api.branch_head(policy.base_branch) != base:
            decision["reason"] = "refs_changed"
            continue
        reviews = api.reviews(number)
        maintainers = maintainer_ids(api, reviews, current["user"]["login"])
        reviews = with_edit_state(api, current, reviews, maintainers, policy)
        comparison = api.compare(current["base"]["sha"], head)
        carried = carried_heads(api, current, reviews, policy)
        eligible, reason = mg.merge_decision(
            current, reviews, api.check_runs(head), api.statuses(head), comparison,
            maintainers, policy, carried)
        decision.update(eligible=eligible, reason=reason)
        if eligible and apply:
            api.merge(number, head)
            decision["merged"] = True
            closed, refused, unread = close_referenced_issues(api, number)
            # Present only when there is something to report, so an ordinary merge
            # keeps the envelope the fixture cases and consumers already know.
            if closed:
                decision["issues_closed"] = closed
            if refused:
                decision["issues_unclosed"] = refused
            if unread:
                decision["issues_unread"] = True
        if eligible:
            # One admitted merge per run; the base moved, so the next run decides.
            break
    return decisions


def trusted_context(policy: mg.Policy, mode: str) -> bool:
    """Writes only from the pinned default-branch workflow file."""
    workflow = policy.review_workflow if mode == "review" else policy.merge_workflow
    events = ({"workflow_run", "schedule", "pull_request_target"} if mode == "review"
              else {"workflow_run", "schedule"})
    return (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == policy.repository
        and os.environ.get("GITHUB_EVENT_NAME") in events
        and os.environ.get("GITHUB_REF") == f"refs/heads/{policy.base_branch}"
        and os.environ.get("GITHUB_REF_PROTECTED") == "true"
        and os.environ.get("GITHUB_WORKFLOW_REF") ==
        f"{policy.repository}/.github/workflows/{workflow}@refs/heads/{policy.base_branch}"
    )
