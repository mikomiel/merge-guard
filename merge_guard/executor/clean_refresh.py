_CLEAN_REFRESH_MAX_MERGES = 8


class CleanRefreshMixin:
    def _clean_refresh_preserves_review(
        self,
        context: WorktreeContext,
        *,
        reviewed_head: str,
        reviewed_base: str,
        head_sha: str,
        base_sha: str,
    ) -> bool:
        """Whether (head_sha, base_sha) is a clean refresh of (H1, B1).

        "Clean refresh" means the three tests below, and the pair is
        deliberately conservative: a false negative costs one extra review
        round, a false positive merges a diff nobody read.

        *Structure.* Every commit reachable from ``head_sha`` but from
        neither ``reviewed_head`` nor ``base_sha`` must be a two-parent
        merge whose first parent is ``reviewed_head`` or another commit in
        that same set, and whose second parent is already contained in
        ``base_sha``. That is exactly the shape ``git merge origin/main``
        leaves behind (the lane refresh operation, never a rebase),
        and it admits a chain of several such refreshes while rejecting any
        added lane commit -- a plain commit has one parent and fails at the
        first test. ``reviewed_base`` must be contained in ``base_sha``, so
        the base only ever moved forward.

        *Content.* Whitespace-exact content identity of the two-dot diff the
        reviewer saw, ``reviewed_base..reviewed_head``, with the diff of
        ``base_sha..head_sha`` -- only hunk headers and blob ids normalized
        away (``_range_diff_digest``). The structural test above cannot
        carry this on its own: git does not verify a merge commit's tree
        against its parents, so a two-parent commit of exactly the permitted
        shape can still hold a tampered tree. A hand-resolved conflict
        likewise produces a merge commit with the same
        subject and the same structure as a clean one. An unreadable or
        empty diff yields no digest, and no digest compares equal to
        anything, so a degenerate range refuses rather than approves.

        *Reviewed files.* Every path the reviewed two-dot diff touches must be
        byte-identical at ``reviewed_head`` and ``head_sha``
        (``_reviewed_files_unchanged``). The digest compares the lane's own
        diff, so a ``main`` change elsewhere in a touched file leaves it
        identical; this is the only test that sees it. A position-only shift is
        refused too: the file is different even when the lane's content is not.

        Fail-closed in every failure mode: a non-zero return, malformed
        output, or any ``GateError``/``OSError``/``ValueError`` raised by
        the runner (``run_command`` turns a timeout into ``COMMAND_TIMEOUT``
        and a missing binary into ``COMMAND_UNAVAILABLE``) answers ``False``
        rather than propagating. A probe that could not run must degrade to
        "no reuse", not to a hard refusal of a PR that would otherwise fail
        cleanly with ``PR_REVIEW_NOT_APPROVED``.
        """
        try:
            return self._clean_refresh_probe(
                context,
                reviewed_head=reviewed_head,
                reviewed_base=reviewed_base,
                head_sha=head_sha,
                base_sha=base_sha,
            )
        except (GateError, OSError, ValueError):
            return False

    def _clean_refresh_probe(
        self,
        context: WorktreeContext,
        *,
        reviewed_head: str,
        reviewed_base: str,
        head_sha: str,
        base_sha: str,
    ) -> bool:
        """The probe itself; ``_clean_refresh_preserves_review`` owns the catch."""
        shas = (reviewed_head, reviewed_base, head_sha, base_sha)
        if not all(isinstance(sha, str) and SHA_RE.fullmatch(sha) for sha in shas):
            return False
        # A head equal to the reviewed head is the exact-pair case the
        # caller already answered; a head equal to its own base is a
        # fast-forward that carries no lane content of its own.
        if head_sha == reviewed_head or head_sha == base_sha:
            return False

        def _ancestor(candidate: str, descendant: str) -> bool:
            return self._runner(
                [GIT_PATH, "merge-base", "--is-ancestor", candidate, descendant],
                context.cwd,
            ).returncode == 0

        if not (
            _ancestor(reviewed_head, head_sha)
            and _ancestor(base_sha, head_sha)
            and _ancestor(reviewed_base, base_sha)
        ):
            return False

        listed = self._runner(
            [GIT_PATH, "rev-list", head_sha, f"^{reviewed_head}", f"^{base_sha}"],
            context.cwd,
        )
        if listed.returncode != 0 or not isinstance(listed.stdout, str):
            return False
        added = [line.strip().lower() for line in listed.stdout.splitlines() if line.strip()]
        if not added or len(added) > _CLEAN_REFRESH_MAX_MERGES:
            return False
        if any(not SHA_RE.fullmatch(sha) for sha in added):
            return False
        permitted_first_parents = set(added) | {reviewed_head}
        for commit in added:
            parents = self._runner(
                [GIT_PATH, "rev-list", "--parents", "-n", "1", commit], context.cwd
            )
            if parents.returncode != 0 or not isinstance(parents.stdout, str):
                return False
            fields = [field.lower() for field in parents.stdout.split()]
            if len(fields) != 3 or fields[0] != commit:
                return False
            first_parent, second_parent = fields[1], fields[2]
            if first_parent not in permitted_first_parents:
                return False
            if not _ancestor(second_parent, base_sha):
                return False

        reviewed_digest = self._range_diff_digest(context, reviewed_base, reviewed_head)
        current_digest = self._range_diff_digest(context, base_sha, head_sha)
        if reviewed_digest is None or reviewed_digest != current_digest:
            return False
        return self._reviewed_files_unchanged(
            context,
            reviewed_base=reviewed_base,
            reviewed_head=reviewed_head,
            head_sha=head_sha,
        )

    def _reviewed_paths(
        self, context: WorktreeContext, base: str, head: str
    ) -> set[str] | None:
        """Every path the range touches, including both names of a rename.

        ``--name-status -z`` rather than ``--name-only``: a rename has two
        names, and the old one is what the reviewer saw and what ``main`` may
        change, so both have to be compared. NUL separation keeps a path that
        contains a newline intact. Any failure returns ``None``, which refuses
        reuse; a probe that could not run must not become a grant.
        """
        listed = self._runner(
            [
                GIT_PATH, "diff", "--no-ext-diff", "--no-textconv",
                "--name-status", "-z", "-M", "--ignore-submodules=none",
                base, head,
            ],
            context.cwd,
        )
        if listed.returncode != 0 or not isinstance(listed.stdout, str):
            return None
        fields = [field for field in listed.stdout.split("\0") if field]
        paths: set[str] = set()
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            if status[:1] in {"R", "C"}:
                if index + 1 >= len(fields):
                    return None
                paths.add(fields[index])
                paths.add(fields[index + 1])
                index += 2
            else:
                if index >= len(fields):
                    return None
                paths.add(fields[index])
                index += 1
        return paths

    def _reviewed_files_unchanged(
        self,
        context: WorktreeContext,
        *,
        reviewed_base: str,
        reviewed_head: str,
        head_sha: str,
    ) -> bool:
        """Whether every file the reviewer saw is unchanged at the new head.

        The digest above compares the lane's own diff, so a ``main`` change in
        the same file, farther from the lane's hunks than the diff's context,
        leaves it identical. This compares file sets instead: the paths the
        reviewed range touches, against the paths that differ between the
        reviewed head and the refreshed one. That difference is what a clean
        refresh merged in, so a non-empty intersection is exactly "main moved a
        file the review covered", whatever the digests say.

        Two diffs and no pathspec: the second is over the two heads, so a large
        reviewed range cannot overflow argv. A file the lane deleted and
        ``main`` left deleted is absent from both sets and counts as unchanged;
        a file ``main`` re-added appears in the intersection and refuses.
        """
        reviewed = self._reviewed_paths(context, reviewed_base, reviewed_head)
        if reviewed is None:
            return False
        moved = self._reviewed_paths(context, reviewed_head, head_sha)
        if moved is None:
            return False
        return not (reviewed & moved)

    def _range_diff_digest(
        self, context: WorktreeContext, base: str, head: str
    ) -> str | None:
        """A whitespace-exact digest of the two-dot diff ``base..head``, or None.

        Deliberately **not** ``git patch-id``. ``patch-id`` strips whitespace
        from every line before hashing, by construction, and a merge commit's
        tree is not verified by git against its parents -- so a two-parent
        commit whose first parent is the reviewed head and whose second is
        the new base can carry a tree that differs from the reviewed content
        only in indentation, and ``patch-id`` reports it identical. For
        example, a lane commit reviewed as ``LANE`` and a refresh tree
        carrying ``  LANE`` both hash to the same patch id. In Python, YAML
        and Makefiles that whitespace is the meaning, so the structural test
        cannot be asked to compensate: this comparison has to be exact.

        Normalization is therefore the minimum that survives a moved base,
        and nothing more:

        * a ``@@`` hunk header becomes the literal ``@@`` -- its line numbers
          and trailing function context shift with the base even when the
          lane's own change does not;
        * an ``index <old>..<new> <mode>`` line is dropped -- the preimage
          blob id changes with the base;
        * every other line, including each one's leading and trailing
          whitespace, is hashed byte for byte.

        ``--no-ext-diff``/``--no-textconv`` keep a repository-configured
        external differ or textconv filter out of the comparison, since
        either would make the identity depend on configuration rather than
        on content. An empty or unreadable diff returns None, which compares
        equal to nothing.

        A range that touches a path git reports as binary also returns None:
        the digest cannot see those bytes, so it must not vouch for them.
        Without ``--binary`` git prints one ``Binary files a/p and b/p
        differ`` line and nothing else, and the ``index`` line dropped just
        above was the only token that varied with the content -- so two
        different blobs normalize to identical text. For example, a lane
        reviewed with ``89504e470001`` and a refresh tree carrying
        ``89504e470003`` produce byte-identical normalized diffs. A ``.gitattributes``
        ``-diff`` path reports the same way and is covered by the same test.
        The cost of the refusal is one fresh review for any lane that
        touches a binary path.
        """
        diff = self._runner(
            [GIT_PATH, "diff", "--no-ext-diff", "--no-textconv", base, head],
            context.cwd,
        )
        if diff.returncode != 0 or not isinstance(diff.stdout, str) or not diff.stdout:
            return None
        # ``split("\n")`` rather than ``splitlines()``: the latter also breaks
        # on form feed and U+2028, which are ordinary content bytes inside a
        # diff body and must not become line boundaries here.
        lines = diff.stdout.split("\n")
        # Every diff *content* line begins with ``+``, ``-``, a space, or the
        # ``\ No newline`` marker, so either prefix at column zero is always
        # git's own binary notice and never payload. ``GIT binary patch``
        # needs ``--binary``, which this call does not pass; it is refused
        # too so that adding the flag later cannot silently widen what the
        # digest claims to have read.
        if any(
            line.startswith("Binary files ") or line.startswith("GIT binary patch")
            for line in lines
        ):
            return None
        normalized = [
            "@@" if line.startswith("@@ ") else line
            for line in lines
            if not line.startswith("index ")
        ]
        return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
