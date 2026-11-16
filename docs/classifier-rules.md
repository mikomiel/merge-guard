# Classifier rules

The classifier decides how much assurance a change needs from the paths it
changes. `scripts/assurance.py` is the mechanism: it turns a set of changed
paths into a class (`C1`, `C2` or `C3`), the ids of the rules that matched and
an assurance route. The policy is data: which paths escalate to `C1`, which
paths count as documentation, and which files `C3` may narrow. It lives in a
TOML rule file that the classifier validates before it uses it, so a repository
changes its policy by supplying a file, not by editing code.

With no file supplied the classifier uses the reference rule set,
`scripts/reference-rules.toml`. That is the policy the classifier was extracted
with, kept as it was so its results do not change. It is one repository's
layout, not a recommended default. A repository with another layout supplies its
own file; the [worked example](#a-worked-example) below is not the reference
layout. `tests/test_classifier_parity.py` pins that supplying the reference file
explicitly reports the same rows as supplying nothing, and that both reproduce,
byte for byte, what the classifier printed before its rules became data
(`tests/fixtures/classifier-golden-rows.txt`).

## What a rule file decides

A rule file decides three things:

- which paths make a change `C1` (the `rule` tables);
- which paths count as documentation when a `C2` change picks its route
  (`doc_only`);
- which files, and which fields in them, a `C3` change may touch
  (`model_definition`).

No file can change the rest. The class order is `C1` over `C2` over `C3`. The
ids `C1-UNKNOWN-CONTRADICTORY`, `C3-MODEL-DEFINITION-ONLY` and `DEFAULT-C2` are
the classifier's own. `C1-UNKNOWN-CONTRADICTORY` covers changed paths that could
not be computed, and a symlink, a submodule, a binary file or an unparseable
record in the diff. The check profile each class selects is also fixed.

A rule can only select `C1`, and every rule that matches is applied, so a rule
file can add paths to the strongest class but cannot lower the class that
another rule gave. A file that cannot be used is refused. It never classifies as
a weaker class, and it never falls back to the reference set.

## Supplying a rule file

On the command line:

```
python3 -I scripts/assurance.py --rules FILE < changed-paths
```

The script reads one changed path per line from stdin and prints one row for
each rule that each path matches: the rule id, a tab and the path. Without
`--rules` it uses the reference set. `--rules FILE` is the only argument it
accepts; anything else prints a usage line and exits 2.

A file that cannot be used prints `merge_guard_refused: <reason>` on stderr,
prints no rows and exits 2, like the other refusals of this project
([docs/cli.md](cli.md)). Empty output therefore means "no path matched" only
when the exit status is 0. To check a file without classifying anything, run it
with empty input: exit status 0 means the file is usable.

The script's own `reference-rules.toml` is refused the same way. If it is missing
or unusable, the script prints the refusal before it reads any argument, so a
usable `--rules FILE` does not hide it.

In process:

```python
import assurance  # the vendored scripts/assurance.py

rules = assurance.load_rules("ci/classifier-rules.toml")  # raises assurance.RulesUnusable
paths = frozenset({"src/auth/login.py"})
result = assurance.classify_change(paths, [], rules=rules)
route = assurance.assurance_route_for(result.gate_class, paths, rules=rules)
```

`classify_change`, `c1_path_rules_by_path` and `assurance_route_for` take an
optional `rules=`; `None`, the default, is the reference set, so a caller that
supplies nothing sees no change. `load_rules` reads and parses a file;
`validate_rules` checks a table that is already parsed. Both return the same
validated `Rules` and raise `RulesUnusable` with a reason from the
[refusal table](#refusal-reasons).

Read the rule file from the base branch, not from the change being classified. A
file taken from the change lets that change delete the rule that would escalate
it. The copy on the base branch is the one the change does not control.

The module reads `reference-rules.toml` from its own directory when it is
imported. Copy the two files together. If that file is missing or unusable,
importing the module raises `RulesUnusable` rather than classify without rules,
and nothing falls back to another rule set. Run as a script, the same failure is
the refusal described above: `merge_guard_refused: <reason>` on stderr, no rows
and exit status 2. It needs Python 3.11 or later for `tomllib`.

## The file format

A rule file is a TOML file with three sections. All three are required: an
empty `[doc_only]` or `[model_definition]` table is fine, an absent one is not.

| Section | Kind | Holds |
| --- | --- | --- |
| `rule` | array of tables, `[[rule]]`, at least one | one path rule per table |
| `doc_only` | table | the paths that count as documentation |
| `model_definition` | table | the allowlist for `C3` |

### `rule`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | The name the classifier reports for the rule. Unique in the file, not blank, without whitespace or an invisible format character (Unicode category `Cf`, such as a zero-width space) at either end, free of control characters, and not one of the classifier's own ids. A published id is a contract: callers branch on it. |
| `class` | string | yes | Always `"C1"`. |
| `exact` | list of strings | one of `exact` and `prefixes` | A path matches when it equals an entry. |
| `prefixes` | list of strings | one of `exact` and `prefixes` | A path matches when it starts with an entry. An entry is plain text, so write a directory as `dir/`. |
| `exempt_prefixes` | list of strings | no | A path that starts with an entry is not selected by this rule. |
| `exempt_suffixes` | list of strings | no | A path that ends with an entry is not selected by this rule. |

The `id` is the first column of every row the script prints, and a refusal can
name it. A tab, a newline, a carriage return, any other control character
(Unicode category `Cc`), U+2028 or U+2029 in an id would shift a column or end
the row early, so a file with such an id is refused. Whitespace at either end of
an id is refused too, because it does not show in a row: `AUTH` and `AUTH ` would
read as one id. Spaces inside an id are allowed.

### `doc_only`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `suffixes` | list of strings | no | A path that ends with an entry counts as documentation. |
| `prefixes` | list of strings | no | A path that starts with an entry counts as documentation. |

A `C2` change takes the route `review` when every changed path counts as
documentation, and `tests_and_review` otherwise; a change with no paths takes
`tests_and_review`. An empty list means that kind of path never counts. A `C1`
change always takes `tests_and_review` and a `C3` change takes `tests`.

### `model_definition`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `files` | list of strings | no | The files a `C3` change may touch. |
| `fields` | list of strings | no | The top-level fields a `C3` change may change in them. |

A change is `C3` only when no rule matched, every changed path is in `files`
and there is at least one, the caller supplied the changed top-level fields of
each path, and every one of those fields is in `fields`. With both lists empty
`C3` never applies, which is the right answer for a repository without
model-definition files. Keep `fields` to fields that only choose a model: the
classifier cannot tell that a field name it has never seen grants a tool or a
permission.

### Entries

An entry is plain text, matched exactly and case-sensitively, with `/` as the
separator. The validator refuses an entry that is almost certainly a mistake:

- an empty entry, or one with leading or trailing whitespace. An empty prefix
  would match every path, and an empty exemption would exempt every path;
- an entry that contains a glob character (`*`, `?`, `[` or `]`), a backslash or
  a NUL. A glob would be read as literal text and silently match nothing. To
  cover a path whose name really contains one of these characters, use a prefix
  that stops before it;
- a path entry, in `exact`, `prefixes`, `exempt_prefixes`, `doc_only.prefixes` or
  `files`, that starts with `/` or `./`. Paths are relative to the repository
  root.

There are no globs. Refusing the entry is safer than a pattern that silently
matches nothing.

## How the rules combine

The order of the rules in the file never changes a result.

- Every rule is tested against every changed path. A rule selects a path when
  the path equals an `exact` entry or starts with a `prefixes` entry, and neither
  exemption applies to it.
- One selected path is enough: the whole change is `C1`. Exemptions apply path by
  path, so a change that holds one exempt path and one non-exempt path in the
  same area is still `C1`.
- The result lists the id of every rule that matched, sorted.
- With nothing matched, neither a rule nor `C1-UNKNOWN-CONTRADICTORY`, a change
  that meets the `C3` conditions is `C3`. Every other change is `C2`, reported as
  `DEFAULT-C2`.

## Refusal reasons

Each reason is a stable code, optionally followed by `: ` and a detail that names
the key or value at fault. Indexes in a detail start at zero, as in
`rule[0].prefixes[1]`. A new reason may be added; a published one is never
renamed or reused.

| Reason | Raised when |
| --- | --- |
| `rules_unreadable` | The file cannot be read as UTF-8 text: it is missing, is a directory, is not text, or its path holds a NUL. The detail names the file and the error class. A path that holds a control character is written as `ascii(str(path))`, so the refusal stays one line. |
| `rules_not_toml` | The file is not valid TOML. A repeated key is invalid TOML, and so is an integer with more digits than the interpreter converts (4300 by default). The detail names the file and carries the parser's message. A path that holds a control character is written as `ascii(str(path))`, as for `rules_unreadable`. |
| `rules_wrong_type` | A value has the wrong type: `rule` is not an array of tables, a section is not a table, `id` is not a string, a field is not a list, or an entry is not a string. |
| `rules_unknown_field` | A section or field that the format does not define, including a misspelling. The detail lists every one, sorted. A name that holds a control character, or is not a string (possible only in process), is listed as `ascii(str(name))`, so the refusal stays one line. |
| `rules_missing_section` | `rule`, `doc_only` or `model_definition` is absent, or `rule` holds no tables. The detail lists every missing section, sorted. |
| `rules_missing_field` | A rule has no `id` or no `class`, or selects nothing because `exact` and `prefixes` are both absent or empty. |
| `rules_rule_id_empty` | A rule's `id` is empty or holds only whitespace that is not a control character. An id of only tabs or newlines is `rules_rule_id_invalid`. |
| `rules_rule_id_invalid` | A rule's `id` holds a control character: a tab, a newline, a carriage return, any other character of Unicode category `Cc`, or U+2028 or U+2029. It is also raised for an `id` that starts or ends with whitespace or a format character (Unicode category `Cf`). Spaces inside an id are allowed. The detail names the field, never the id. |
| `rules_rule_id_duplicate` | An `id` is used twice, or is one of the classifier's own ids. The detail names the id. |
| `rules_class_invalid` | A rule's `class` is anything other than `"C1"`. |
| `rules_entry_invalid` | An entry is almost certainly a mistake. See [Entries](#entries). |

## A worked example

`examples/classifier-rules.toml` is the rule file of a small web service. Every
path in it is synthetic. Copy it, then replace each path with your own layout.

```toml
# A worked example for docs/classifier-rules.md: the rules of a small web
# service. Every path here is synthetic. Copy the file, then replace each path
# with your own layout; nothing in it is a default.

# Authentication and session code decides who may do what.
[[rule]]
id = "AUTH"
class = "C1"
prefixes = ["src/auth/", "src/session/"]

# Schema migrations change stored data; the notes beside them do not.
[[rule]]
id = "MIGRATIONS"
class = "C1"
prefixes = ["db/migrations/"]
exempt_suffixes = [".md"]

# The build and release path, and this file itself: a change to what escalates
# is an escalation. Name the path where your repository keeps the file.
[[rule]]
id = "RELEASE-PATH"
class = "C1"
exact = ["Dockerfile", "ci/release.sh", "ci/classifier-rules.toml"]
prefixes = [".ci/"]

# Prose where a broad test suite adds little confidence.
[doc_only]
suffixes = [".md", ".rst"]
prefixes = ["docs/"]

# This service has no model-definition files, so C3 never applies.
[model_definition]
files = []
fields = []
```

The classifier reports this for the changed paths of a few changes:

| Changed paths | Rules matched | Class | Route |
| --- | --- | --- | --- |
| `src/auth/login.py` | `AUTH` | C1 | `tests_and_review` |
| `db/migrations/0042_add_index.sql` | `MIGRATIONS` | C1 | `tests_and_review` |
| `db/migrations/README.md` | `DEFAULT-C2` | C2 | `review` |
| `Dockerfile` | `RELEASE-PATH` | C1 | `tests_and_review` |
| `ci/classifier-rules.toml` | `RELEASE-PATH` | C1 | `tests_and_review` |
| `src/ui/button.py` | `DEFAULT-C2` | C2 | `tests_and_review` |
| `docs/intro.md` | `DEFAULT-C2` | C2 | `review` |
| `docs/intro.md`, `src/ui/button.py` | `DEFAULT-C2` | C2 | `tests_and_review` |
| `src/auth/login.py`, `Dockerfile` | `AUTH`, `RELEASE-PATH` | C1 | `tests_and_review` |

The migration note is exempt from `MIGRATIONS` by its suffix, and `.md` counts
as documentation, so it is a `C2` change that needs `review` alone. The rule
file itself is in `RELEASE-PATH`, so a change to what escalates is itself
escalated, provided the file you supply is the base branch's copy. Documentation
next to code takes the stronger route.

From the command line, the same file gives these rows. `docs/intro.md` prints
none, because no rule selects it:

```
$ printf 'src/auth/login.py\ndocs/intro.md\ndb/migrations/0042_add_index.sql\nDockerfile\n' \
    | python3 -I scripts/assurance.py --rules examples/classifier-rules.toml
AUTH	src/auth/login.py
MIGRATIONS	db/migrations/0042_add_index.sql
RELEASE-PATH	Dockerfile
```

A file that cannot be used prints no rows:

```
$ printf 'Dockerfile\n' | python3 -I scripts/assurance.py --rules missing.toml
merge_guard_refused: rules_unreadable: missing.toml: FileNotFoundError
$ echo $?
2
```

## The reference set

`scripts/reference-rules.toml` is the rule set that runs when no file is
supplied. Its rule ids and the reasons in its comments belong to the policy of
the repository the classifier was extracted from. Read it as a larger example
of the same format, not as a layout to adopt. The C3 allowlists in it are
empty.
