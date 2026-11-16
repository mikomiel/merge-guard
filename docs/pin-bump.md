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

A consumer pins a release by copying that closure verbatim and recording, per
file, which release it came from. The copy is generated, never authored.

The action is not part of this closure, and that is deliberate rather than an
omission. A consumer runs `uses: mikomiel/merge-guard@<tag>`, which resolves the
tag's tree directly, so `action.yml`, `scripts/merge_guard.py`,
`scripts/merge_guard_envelope.py` and the `merge_guard/` package are consumed by
ref. The closure holds the classifier files `release-manifest.json` names, and
nothing else; add the action to it only as a new release decision, never as a
manifest refresh.

## The procedure

1. **Read the tag, not the branch.** `git rev-parse <tag>^{commit}` must name the
   commit the release names, and `git show <tag>:release-manifest.json` must
   resolve. If either fails, stop: the tag is not the release.
2. **Take the closure from the manifest.** Copy every file it lists from that
   commit into your vendor tree under its repository path, and refuse a manifest
   whose digest or size differs from the bytes you copied. A file the manifest
   does not list is not part of the release, whatever `tests/`, the workflows or
   the policy file carry.
3. **Record the pin.** One manifest beside the copy, holding the product, the
   version, the source commit, and for each file its source path and its
   SHA-256. The version is the tag, because the release manifest does not name
   one. Generate the digests with `sha256sum`; do not type them, and commit the
   manifest with the copy so the record and the bytes move together.
4. **Enforce it in your own checks.** The check must compare the tree against the
   manifest in both directions: a file whose digest differs, a file the manifest
   names and the tree lacks, and a file the tree carries and the manifest does
   not are three separate failures, each naming the file.
5. **Run your suite against the pinned copy** before you remove anything. A
   release is consumable when your tests pass on a clean checkout with no
   `PYTHONPATH`, symlink or path dependency on this repository.
6. **Then remove the superseded implementation** and leave the retained
   entrypoint that delegates to the pinned copy.

## Regenerating the manifest

The manifest is generated, so the order matters. Commit the source or
declaration change first, with the old manifest still in place; then run the
generator at the repository root and read what it prints:

```
python3 scripts/release_manifest.py write
```

With no arguments it regenerates every declared path. With paths it replaces
the closure with exactly those files, so a path has to be chosen, not inferred.
The generator reads the committed blobs, not the working tree, and it refuses a
declaration it cannot explain instead of dropping it. Commit the regenerated
`release-manifest.json` on its own, so the closure change is the diff a reviewer
reads, and run the same check the workflow runs:

```
python3 scripts/release_manifest.py check
```

Do not edit a digest or a size by hand: the check compares the manifest with the
committed blobs, and a hand edit fails it with the file named.

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

## Rolling back

Restore the previous implementation from your own history, delete the vendor
tree, and put the manifest and the check back with it: a check that still looks
for files you have deleted reports them missing, and an entrypoint that still
delegates has nothing left to delegate to. The earlier release remains
available; a pin is not a migration.
