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
| `scripts/assurance.py` and `scripts/reference-rules.toml` | The classifier, and the reference rules it reads from its own directory. |
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
not flatten the tree. `assurance.py` reads `reference-rules.toml` from its own
directory, and `scripts/merge_guard.py` finds the package at `merge_guard/` beside
`scripts/`. The launcher is the runnable form of the command line: with the vendor
root `vendor/merge-guard`, it runs as
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

A change to any closure file changes its digest, so the same pull request
regenerates the manifest, as its own commit. Two cases are easy to miss:

- **A new package file.** The test above fails until the file has an entry.
- **Every release pull request.** Release Please bumps `__version__` in
  `merge_guard/__init__.py`, and that file is in the closure, so its digest
  changes with the version. The `checks` run on the release pull request fails
  and names `merge_guard/__init__.py` until a maintainer runs `write` with no
  arguments on the release branch and commits the manifest. If Release Please
  updates the branch afterwards, regenerate again: the check names the stale
  file. The new head needs its own CI run and its own review.

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
`<vendor root>/assurance.py`, moves it to `<vendor root>/scripts/assurance.py`
with `reference-rules.toml` beside it, and changes its loader in the same bump.

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
