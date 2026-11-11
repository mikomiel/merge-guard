# Pinning a release

A release in this repository is an annotated tag on `main`. Its notes state the
closure: the files a consumer copies, and nothing else. `v0.0.1` is one file,
`scripts/assurance.py`, standard library only.

A consumer pins a release by copying that closure verbatim and recording, per
file, which release it came from. The copy is generated, never authored.

## The procedure

1. **Read the tag, not the branch.** `git rev-parse <tag>^{commit}` must name the
   commit the release notes name. If it does not, stop: the tag is not the
   release.
2. **Take the closure from the notes.** Copy each named file from that commit
   into your vendor tree under its repository path. Nothing from `tests/`, the
   workflows or the policy file is part of a release closure unless the notes
   name it.
3. **Record the pin.** One manifest beside the copy, holding the product, the
   version, the source commit, and for each file its source path and its
   SHA-256. Generate the digests with `sha256sum`; do not type them.
4. **Enforce it in your own checks.** The check must compare the tree against the
   manifest in both directions: a file whose digest differs, a file the manifest
   names and the tree lacks, and a file the tree carries and the manifest does
   not are three separate failures, each naming the file.
5. **Run your suite against the pinned copy** before you remove anything. A
   release is consumable when your tests pass on a clean checkout with no
   `PYTHONPATH`, symlink or path dependency on this repository.
6. **Then remove the superseded implementation** and leave the retained
   entrypoint that delegates to the pinned copy.

## What the pin guarantees

The digest does not prove the release is correct; it proves the bytes you run are
the bytes the release published. Provenance beyond that is the tag and the notes.

## Bumping

A bump is the same procedure with a different tag. Nothing is edited in place:

- a hand-edited vendored file is a defect, not a local optimisation;
- raising the recorded version without regenerating the digests is worse than not
  bumping, because the manifest then attests to something that was never
  published;
- carrying both an authored copy and a vendored copy at the same time is the one
  state to avoid, because which one runs becomes a matter of import order.

## Rolling back

Restore the previous implementation from your own history and delete the vendor
tree. The earlier release remains available; a pin is not a migration.
