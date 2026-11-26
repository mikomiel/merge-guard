# Pinning a release

A release in this repository is a tag on `main`. `v0.0.1` and `v0.0.2` are
annotated tags written by hand; from the first Release Please version onward the
tag is lightweight and is created when the release pull request merges. The tag
names the commit; [`release-manifest.json`](../release-manifest.json) at that
commit names the closure: the files a consumer copies, and nothing else, each
with a SHA-256 and a size. `scripts/release_manifest.py check` proves it
describes the tree it sits in, on every pull request and every push to `main`.

The manifest is the closure's only owner. `v0.0.1` and `v0.0.2` predate it, so
their closure is whatever their release notes state: `v0.0.1` is one file,
`scripts/assurance.py`, standard library only, and `v0.0.2` names no closure at
all. From the release that introduced the manifest onward, a release note does
not carry a closure, and a tag whose tree has no manifest is not consumable.

A closure can change between releases. This page describes the closure of the
current tree. The manifest at the tag you pin is the only list that counts, and
an older tag can list fewer files.

A consumer pins a release by copying that closure verbatim and recording, per
file, which release it came from. The copy is generated, never authored.

## The closure

The closure is the classifier plus the package:

| Files | What they are |
| --- | --- |
| `scripts/assurance.py` and `scripts/reference-rules.toml` | The classifier, and an example rule set. The classifier needs a rule set from its caller and reads the example only when a caller passes it. |
| Every file under `merge_guard/` | The package: the engine, the forge port with its GitHub and Forgejo adapters, the policy and the command line. |
| `scripts/merge_guard.py` | The launcher of the command line. It puts the directory above `scripts/` on the import path and calls `merge_guard.cli`. |

The manifest's `entrypoints` lists what a consumer runs or imports: the
classifier, the launcher and the package's command line. Read the list in the
manifest, not here. Every other closure file is something an entrypoint needs.

A test keeps the package whole. `tests/test_release_manifest.py` fails, and names
the file, when a file committed under `merge_guard/` has no manifest entry. A
package file can therefore never be released outside the closure.

These stay outside the closure:

- **The action.** A consumer runs `uses: mikomiel/merge-guard@<sha>`, pinned to
  the commit a tag names. GitHub resolves that tree directly, so `action.yml` and
  the helper it runs for its `merged` output, `scripts/merge_guard_envelope.py`,
  are consumed by ref. That is deliberate rather than an omission. Add them to
  the closure only as a new release decision, never as a manifest refresh.
- **The development tools** `scripts/check_estate_identifiers.py` and
  `scripts/release_manifest.py`. No entrypoint imports or runs either.
- `tests/`, the workflows, the policy file `.github/merge-guard.toml`,
  `examples/` and `docs/`.

**Python.** The closure needs Python 3.11 or later. `scripts/assurance.py` and
`merge_guard/cli.py` import `tomllib`, which arrived in 3.11, and `pyproject.toml`
declares `requires-python = ">=3.11"`. The closure needs no third-party Python
package. The GitHub adapter runs the `gh` command; the Forgejo adapter uses the
standard library only. The action installs Python 3.12 for itself. A consumer
that runs the closure supplies its own interpreter: the closure carries none.

**Layout.** A consumer mirrors repository paths under its vendor root. A file the
manifest lists as `scripts/assurance.py` lands at
`<vendor root>/scripts/assurance.py`, never at `<vendor root>/assurance.py`. Do
not flatten the tree. `scripts/merge_guard.py` finds the package at `merge_guard/`
beside `scripts/`. `assurance.py` reads no file of its own: it needs a rule set
from its caller, as `--rules FILE` or `rules=`, and `reference-rules.toml` is an
example of one. The launcher is the runnable form of the command line: with the
vendor root `vendor/merge-guard`, it runs as
`python3 vendor/merge-guard/scripts/merge_guard.py <mode>`. Do not run
`merge_guard/cli.py` as a file: it uses relative imports and fails with
`ImportError`. To call the command line in process, put the vendor root on
`sys.path` and import `merge_guard.cli`. If `<vendor root>/scripts` is also on
`sys.path`, for `import assurance`, the vendor root must come before it. With
`scripts/` first, `scripts/merge_guard.py` is found as the module `merge_guard`,
shadows the package, and `import merge_guard.cli` fails. [`cli.md`](cli.md) owns
the command line's arguments and [`classifier-rules.md`](classifier-rules.md) owns
the classifier's.

## The procedure

A vendoring script automates steps 1 to 4. It reads the closure from the manifest
at the tag, never from a list of its own, and it refuses a tag, version, size or
digest that does not match. Step 3 says when a version can be compared.

1. **Read the tag, not the branch.** `git rev-parse <tag>^{commit}` must name the
   commit the release names, and `git show <tag>:release-manifest.json` must
   resolve. If either fails, stop: the tag is not the release. A tag that
   resolves to a different commit than the pin records is a mismatch: refuse it.
2. **Take the closure from the manifest.** Read the manifest from that commit.
   Refuse it unless `schema` is `release-manifest.v1` and `product` is
   `mikomiel/merge-guard`. Refuse a listed path that is absolute, contains `..` or
   a backslash, or would leave the vendor root. Copy every listed file from that
   commit to `<vendor root>/<path>`, and refuse a copy whose size or SHA-256
   differs from the manifest's. A file the manifest does not list is not part of
   the release, whatever `tests/`, the workflows or the policy file carry.
3. **Check the version.** The manifest names no version, so the tag is the
   version, and step 4 records the tag. When the manifest lists
   `merge_guard/__init__.py`, `__version__` in the copied file must also be the
   tag without its leading `v`: refuse the pin if it is not. When the manifest
   does not list that file, there is no second value to compare, and the tag
   alone is the version. The manifests of `v0.1.0`, `v0.2.0` and `v0.2.1` are
   such cases: they list the classifier only.
4. **Record the pin.** One manifest beside the copy, holding the product, the
   version, the source commit, and for each file its source path and its
   SHA-256. Generate the digests with `sha256sum`; do not type them, and commit
   the manifest with the copy so the record and the bytes move together.
5. **Enforce it in your own checks.** The check must compare the tree against the
   manifest in both directions: a file whose digest differs, a file the manifest
   names and the tree lacks, and a file the tree carries and the manifest does
   not are three separate failures, each naming the file.
6. **Run your suite against the pinned copy** before you remove anything. A
   release is consumable when your tests pass on a clean checkout with no
   `PYTHONPATH`, symlink or path dependency on a checkout of this repository.
7. **Then remove the superseded implementation** and leave the retained
   entrypoint that delegates to the pinned copy.

## Regenerating the manifest

The manifest is generated, so the order matters. Commit the source or
declaration change first, with the old manifest still in place; then run the
generator at the repository root and read what it prints:

```
python3 scripts/release_manifest.py write
```

With no arguments it regenerates every declared path. With paths it replaces
the closure with exactly those files, so a path has to be chosen, not inferred:
to add a file, pass every path that stays and the new one. The generator reads
the committed blobs, not the working tree, and it refuses a declaration it cannot
explain instead of dropping it. Commit the regenerated `release-manifest.json` on
its own, so the closure change is the diff a reviewer reads, and run the same
check the workflow runs:

```
python3 scripts/release_manifest.py check
```

Do not edit a digest or a size by hand: the check compares the manifest with the
committed blobs, and a hand edit fails it with the file named.

To read a commit that is not checked out, pass `--commit REV` before any paths.
`check --commit REV` runs the same check on that commit. `write --commit REV`
prints the regenerated manifest on standard output and writes no file. Both take
the declared manifest, the tree and every blob from that commit, and nothing of
it reaches the working tree. The release workflow uses this on the release
branch. A person rarely needs it.

A change to any closure file changes its digest, so the same pull request
regenerates the manifest, as its own commit. Two cases are easy to miss:

- **A new package file.** The test above fails until the file has an entry.
- **Every release pull request.** Release Please bumps `__version__` in
  `merge_guard/__init__.py`, and that file is in the closure, so its digest
  changes with the version. The workflow `.github/workflows/release-please.yml`
  regenerates the manifest, so nobody runs `write` by hand for this. It is one
  job. It runs the Release Please action, finds the open release pull request,
  and checks out the default branch only. The release branch is data: the job
  fetches that one ref and never checks it out. It runs `write --commit` from
  the default branch, which reads the closure files and the declared manifest
  from the fetched commit and prints the new manifest. The job builds a commit
  on top of the fetched head with git plumbing, `release-manifest.json` alone,
  as `chore(release): regenerate the release manifest`, and runs `check
  --commit` on it. The last step holds the token and sends the commit as a plain
  push, never a forced one. When Release Please updates the pull request it
  rewrites the branch and drops that commit, and the same run regenerates it. A
  run on a branch that is already correct commits nothing. The first `checks`
  run on a head that Release Please has just written fails and names
  `merge_guard/__init__.py`; the run for the regeneration commit is the one that
  counts, and that head needs its own review. If the push fails, for example
  because the branch moved after the fetch, run the `release-please` workflow
  again with `workflow_dispatch` on the default branch: it regenerates only when
  the manifest is stale.

## What the pin guarantees

Provided the manifest's digests were taken from the verified release commit,
matching digests show that the vendored files are byte-for-byte the ones that
release published. They do not establish correctness, and they do not
authenticate provenance on their own: the tag and the release manifest are what
make the baseline mean anything.

## Bumping

A bump is the same procedure with a different tag. Nothing is edited in place:

- a hand-edited vendored file is a defect, not a local optimisation;
- raising the recorded version without regenerating the digests is worse than not
  bumping, because the manifest then attests to something that was never
  published;
- carrying both an authored copy and a vendored copy at the same time is the one
  state to avoid, because which one runs becomes a matter of import order.

A larger closure makes a bump larger to review. To see which closure files
changed, run `git diff <old tag> <new tag> -- release-manifest.json`. The digest
check keeps the copy itself mechanical.

The first bump to a release whose manifest lists the package also fixes the
classifier's place. A consumer that kept the classifier flat, at
`<vendor root>/assurance.py`, moves it to `<vendor root>/scripts/assurance.py`,
where the manifest lists it, and changes its loader in the same bump. The
classifier needs no file beside it.

The first bump past v0.4.0 removes the classifier's default. A call that gives
no rules is refused. Its `reference-rules.toml` becomes an example layout, and
two of its rule ids are renamed
([docs/classifier-rules.md](classifier-rules.md#the-reference-set)). What you see
depends on how you called the classifier before the bump:

- **No rules.** You ran the script without `--rules`, or called a function with
  `rules=None` or without `rules=`. Before the bump, the call classified against
  the old layout. Now the script prints `merge_guard_refused: rules_required: ...`
  on stderr, prints no rows and exits 2, and a function raises `RulesRequired`, a
  subclass of `RulesUnusable`, which is a `ValueError`.
- **The vendored file.** You passed `--rules` or `rules=` with the vendored
  `reference-rules.toml`. Nothing is refused and nothing fails: the script exits 0
  and prints fewer rows, and a path that your old rule file put in C1 takes the
  C2 default, because the file is now the example layout.
- **Your own file.** Nothing changes.

If you called the classifier in either of the first two ways, take these steps:

1. **Keep your rules.** In a change of its own that merges before the bump, copy
   the `scripts/reference-rules.toml` that you vendored at v0.4.0 to a path
   outside the vendor root, so that a bump cannot replace it. The copy keeps your
   classes and rule ids, and the bump finds it on the base branch, where step 3
   reads it.
2. **Pass that file.** After the bump, pass it with `--rules FILE` or `rules=` on
   every call. Do not pass the vendored `reference-rules.toml`: after the bump it
   is the example layout.
3. **Protect that file.** Read it from the base branch, as
   [Supplying a rule file](classifier-rules.md#supplying-a-rule-file) says, and
   keep a C1 rule for its own path, as
   [A worked example](classifier-rules.md#a-worked-example) shows.
4. **Check the result.** Classify one path that only your own C1 rules match, and
   confirm that it is still C1 with the same rule id. Do not use a path like
   `repos.yaml` for this: the example layout has a rule for it too, so the check
   would pass even if you passed the wrong file.

**From v0.1.0.** In v0.1.0 the rules were in the Python code of the classifier.
There was no rule file, no `--rules` and no `rules=`, and the manifest lists
`scripts/assurance.py` alone. If you bump straight from v0.1.0, you have no
`reference-rules.toml` to copy aside, and every call that you make gives no
rules, so each is refused until you pass a file. Take the file from a release
that ships it:

1. **Take the file from a verified tag.** Read the tag and its manifest as steps 1
   and 2 of the procedure say. `v0.2.0` is the first release that ships
   `scripts/reference-rules.toml`: it moved the rules out of the code into that
   file, with the same rule ids. `v0.4.0` carries the same bytes, so take the file
   from either tag. Do not take it from the release that you are bumping to: there
   it is the example layout.
2. **Land it before the bump.** Copy it to a path outside the vendor root, so
   that a bump cannot replace it, in a change of its own, and merge that change
   before the bump. Step 3 reads the file from the base branch: a file that the
   bump itself adds is not there yet, so the classifier cannot read it and
   refuses, and reading the copy in the bump's own pull request instead would let
   that change choose the policy that classifies it.
3. **Pass it on every call.** Give each call `--rules FILE` or `rules=`. Read the
   file from the base branch and keep a C1 rule for its own path, as step 3 of the
   list above says.
4. **Check it against the classifier you now run.** Classify a path that only a
   C1 rule of that file matches, with `--rules` naming the copy, and confirm that
   it is C1 with the rule id you expect. Then run the same command without
   `--rules` and confirm the refusal. Do not use a path like `repos.yaml` for the
   first run: the example layout has a rule for it too, so the check would pass
   even if you passed the wrong file.

## Bumping this repository's own gate

This repository runs its own gate, and the release is pinned in one line:
`uses: mikomiel/merge-guard@<sha>  # <tag>: <text>` in
`.github/workflows/merge-guard-review.yml`. A bump is one edit of that line: the
40-hex SHA and the tag in the trailing comment.

The merge workflow runs the checked-out tree (`uses: ./`) and carries no pin.
The files under `examples/` keep the all-zero placeholder by design: a consumer
replaces it with a real release ref.

`tests/test_release_config.py` proves the pin is consistent: it is the only
`mikomiel/merge-guard@` reference in `.github/workflows/`, the ref is 40
lowercase hex, and the SHA is the commit the comment tag names. The test needs
the tag in the clone, so run `git fetch --tags` first if it fails. CI checks out
with full history for the same reason.

## Rolling back

Restore the previous implementation from your own history, delete the vendor
tree, and put the manifest and the check back with it: a check that still looks
for files you have deleted reports them missing, and an entrypoint that still
delegates has nothing left to delegate to. The earlier release remains
available; a pin is not a migration.
