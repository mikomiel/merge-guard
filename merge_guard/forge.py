"""The forge port: everything the engine reads from or writes to a forge.

The engine reads only this port. Each adapter (``github.GitHub``,
``forgejo.Forgejo``) answers it with the records described below, so the same
verdict rules and the same refusal codes run on both forges. A record carries
the fields the engine reads and nothing it does not; an adapter maps its own
forge's response onto these fields. A field that the forge does not return is
derived from a response that proves it, or left absent so that the rule which
reads it refuses. A value an adapter cannot map is passed through unchanged, so
the engine's own validation refuses it with the shared code.

Records:

- pull listing entry: ``number``, ``head.sha``.
- pull: ``number``, ``state`` (``open`` or ``closed``), ``draft``,
  ``mergeable``, ``mergeable_state`` (``clean`` is the only state that can
  merge), ``changed_files``, ``merged``, ``merge_commit_sha``, ``base.ref``,
  ``base.sha``, ``base.repo.full_name``, ``head.sha``, ``user.login``.
- review: ``id`` (ordered by time), ``user.id``, ``user.login``, ``user.type``
  (``User`` for a person), ``state`` (``APPROVED``, ``CHANGES_REQUESTED``,
  ``DISMISSED``, ``COMMENTED`` or ``PENDING``), ``commit_id``,
  ``submitted_at``. When ``policy.require_reviewed_head`` is on, the engine
  also attaches ``edit_state`` from ``review_edit_state``: the raw Markdown
  ``body`` and ``last_edited_at``, which is None only for a review that was
  never edited. An adapter never fills ``edit_state`` in ``reviews``.
- check run: CI evidence for one commit. ``id``, ``name``, ``head_sha``,
  ``status`` (``completed`` when finished), ``conclusion``, ``app.id``. The
  engine counts a required check only when ``app.id`` is
  ``policy.ACTIONS_APP_ID``, which means the forge's own CI service wrote it.
- status: any other commit status, the gate's own included. ``id``,
  ``context``, ``state``, ``description``, ``creator.login``, ``creator.type``.
  The gate's own status counts only when its creator is
  ``policy.GATE_PUBLISHER``.
- comparison of ``base...head``: ``status`` (``ahead`` when the head contains
  the base), ``base_commit.sha``, ``merge_base_commit.sha``, ``commits``
  (oldest first, so the last one is the head, each with
  ``commit.committer.date``) and ``files`` (``filename``,
  ``previous_filename``, which a ``renamed`` file must have, ``status``, one
  of ``policy.CHANGED_FILE_STATUSES``, and ``patch`` only where the forge
  returns one).
- permission: ``request("collaborators/{login}/permission")`` answers
  ``permission`` and ``user.id``. Both forges serve that path in that shape.
  The engine also reads ``request("issues/{number}")`` before closing an issue,
  to confirm it is open and is not itself a pull request, and asks
  ``closing_issues(number)`` which issues the merge closes. The test double the
  engine is tested against implements all three calls.

Writes (``publish``, ``merge``, ``comment``, ``close_issue``) need an adapter
built with ``apply=True``. ``merge`` is the expected-head merge: it merges
``sha`` or refuses, and it proves the result by re-reading the pull request.
``close_issue`` proves the issue reports ``state: closed`` or refuses.
"""

from __future__ import annotations

from typing import Any, Protocol


class ApiError(RuntimeError):
    """A forge operation is unavailable or its postcondition is unproven."""


class Forge(Protocol):
    """The port. ``name`` prefixes the engine's forge-specific refusal codes."""

    name: str

    def pulls(self) -> list[dict[str, Any]]:
        """Open pull requests against the policy's base branch."""
        ...

    def pull(self, number: int) -> dict[str, Any]:
        """One pull request, read now."""
        ...

    def reviews(self, number: int) -> list[dict[str, Any]]:
        """Every review of the pull request; review requests are not reviews."""
        ...

    def review_edit_state(self, number: int) -> dict[int, dict[str, Any]]:
        """The body and the edit state of the reviews, read together, by review id.

        The key is the id that ``reviews`` gives the review. Each value holds
        ``body``, the raw Markdown, and ``last_edited_at``, None for a review
        that was never edited. Only a run with ``policy.require_reviewed_head``
        calls it. An unreadable or malformed answer, or one that cannot be
        read completely, raises ``ApiError("review_edit_state_unusable")``,
        and so does a forge that records no edit time.
        """
        ...

    def request(self, path: str) -> Any:
        """A repository-relative read: collaborator permission, and the issue a close names."""
        ...

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        """CI evidence written for exactly this commit."""
        ...

    def statuses(self, sha: str) -> list[dict[str, Any]]:
        """Commit statuses for exactly this commit that are not CI evidence."""
        ...

    def compare(self, base: str, head: str) -> dict[str, Any]:
        """The pull request's own change: ``base...head``."""
        ...

    def commit_time(self, sha: str) -> str:
        """The committer date that the commit itself carries."""
        ...

    def branch_head(self, branch: str) -> str:
        """The commit the branch points at now, or ``""`` when unreadable."""
        ...

    def publish(self, sha: str, state: str, description: str) -> None:
        """Write the gate status unless the newest one already says this."""
        ...

    def merge(self, number: int, sha: str) -> None:
        """Merge exactly ``sha`` or raise; a merge is proven by a re-read."""
        ...

    def comment(self, number: int, body: str) -> None:
        """Add a comment to the pull request."""
        ...

    def close_issue(self, number: int) -> None:
        """Close one issue; the adapters refuse rather than assume it worked."""
        ...

    def closing_issues(self, number: int) -> list[int]:
        """Issue numbers the forge itself says this pull request closes.

        GitHub answers from ``closingIssuesReferences``, its own parse of the body;
        a forge without such a list answers empty, and the engine then closes
        nothing rather than modelling Markdown itself.
        """
        ...
