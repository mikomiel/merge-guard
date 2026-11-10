"""Tests for ``scripts.assurance``, the R×C change classifier.

Pure-function tests only -- no worktree, no forge, no git subprocess. The
end-to-end operation that derives changed paths and the bounded diff shape
belongs to whoever calls this module; the classifier never reads them itself.
The one exception is the entrypoint class at the end, which pins the line
format a caller that executes this file depends on.

The rule IDs and the section labels in these docstrings are the vocabulary of
the policy this classifier was extracted from. See the module docstring's
provenance note.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from scripts import assurance


class ClassifyChangeC1PathRuleTests(unittest.TestCase):
    """One test per §7.5 C1 rule ID, each asserting the exact matched set."""

    def _classify(self, paths):
        return assurance.classify_change(frozenset(paths), [])

    def test_gate_authority_shim(self):
        result = self._classify({"opencode-config/scripts/git_maintainer_gate.py"})
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-GATE-AUTHORITY", result.matched_rule_ids)

    def test_gate_authority_package(self):
        result = self._classify({"opencode-config/scripts/git_gate/catalog.py"})
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-GATE-AUTHORITY", result.matched_rule_ids)

    def test_gate_authority_mcp_adapter(self):
        result = self._classify({"MCPs/servers/git_maintainer_gate_mcp.py"})
        self.assertIn("C1-GATE-AUTHORITY", result.matched_rule_ids)

    def test_gate_authority_classifier_file_itself(self):
        """§15 negative test: the classifier changing its own rules is
        C1-triggering -- covered by the git_gate/ prefix, not a special case,
        but named explicitly since §15 lists it as its own required test."""
        result = self._classify({"opencode-config/scripts/git_gate/assurance.py"})
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-GATE-AUTHORITY", result.matched_rule_ids)

    def test_repository_authority_repos_yaml(self):
        result = self._classify({"repos.yaml"})
        self.assertEqual(result.matched_rule_ids, ("C1-REPOSITORY-AUTHORITY",))

    def test_the_github_workflow_path_is_no_longer_ci_trust(self):
        """FJ-06 subtracted GitHub Actions: the offsite copy runs no CI the
        gate can read, so its workflow path takes the C2 default. The
        Forgejo path below carries the C1-CI-TRUST rule now."""
        result = self._classify({".github/workflows/checks.yml"})
        self.assertEqual(result.gate_class, "C2")
        self.assertNotIn("C1-CI-TRUST", result.matched_rule_ids)

    def test_ci_trust_recognises_the_live_forgejo_workflow(self):
        """The authoritative CI entrypoint is the Forgejo one (FJ-01).

        Since the authority flip, `.forgejo/workflows/checks.yml` is the
        workflow that judges a pull request on the writable forge. While the
        rule listed only the GitHub prefix, editing the live workflow took
        the C2 default -- the strongest assurance class stopped covering the
        trust boundary it exists for, silently, at the moment authority
        moved. FJ-06 removed the GitHub prefix with GitHub Actions (the
        workflows were deleted; the offsite copy runs none the gate can
        read), so the Forgejo prefix is the whole rule.
        """
        result = self._classify({".forgejo/workflows/checks.yml"})
        self.assertEqual(result.gate_class, "C1")
        self.assertEqual(result.matched_rule_ids, ("C1-CI-TRUST",))

    def test_ci_trust_does_not_sweep_the_whole_forgejo_directory(self):
        """A prefix, not the dot-directory: sibling Forgejo configuration that
        is not a workflow keeps the C2 default, matching how the GitHub prefix
        has always behaved."""
        result = self._classify({".forgejo/issue_template.md"})
        self.assertEqual(result.gate_class, "C2")
        self.assertNotIn("C1-CI-TRUST", result.matched_rule_ids)

    def test_ci_trust_repository_ci_entrypoint(self):
        """The repository-owned CI entrypoint is C1 (FJ-01 §7).

        scripts/ci-profile is the single definition CI, a terminal and the
        OpenCode agents all call (WCI-02 3.1), and the workflows invoke it —
        changing it changes what CI trusts, exactly like a workflow change.
        """
        result = self._classify({"scripts/ci-profile"})
        self.assertEqual(result.gate_class, "C1")
        self.assertEqual(result.matched_rule_ids, ("C1-CI-TRUST",))

    def test_ci_trust_covers_what_the_entrypoint_runs_under(self):
        """The entrypoint's own machinery is CI trust, not ordinary code.

        scripts/validation-env is sourced by ci-profile and owns the
        environment every check runs under; scripts/validation_lease.py gates
        every pytest check through the conftest adapters. A change to either
        changes what the trusted entrypoint does, so neither can sit at the
        C2 default while ci-profile is C1.
        """
        for path in (
            "scripts/validation-env",
            "scripts/validation_lease.py",
            "scripts/node-install-fresh.sh",
            "scripts/toolgate.py",
        ):
            with self.subTest(path=path):
                result = self._classify({path})
                self.assertEqual(result.gate_class, "C1")
                self.assertEqual(result.matched_rule_ids, ("C1-CI-TRUST",))

    def test_ci_trust_entrypoint_does_not_sweep_sibling_scripts(self):
        """Only the exact entrypoint is C1, never a scripts/ prefix.

        A sibling repository script is ordinary code and keeps the C2
        default; the entrypoint rule must not silently absorb the whole
        directory it lives in.
        """
        result = self._classify({"scripts/change-digest"})
        self.assertEqual(result.gate_class, "C2")
        self.assertNotIn("C1-CI-TRUST", result.matched_rule_ids)

    def test_ci_trust_entrypoint_mixed_with_sibling_is_still_c1(self):
        """A change touching both the entrypoint and an ordinary script is
        C1 by the mixed-change combining rule."""
        result = self._classify({"scripts/ci-profile", "scripts/change-digest"})
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-CI-TRUST", result.matched_rule_ids)

    def test_secrets_permissions_credential_routing(self):
        result = self._classify({"platform/config/credential-routing.yaml"})
        self.assertEqual(result.matched_rule_ids, ("C1-SECRETS-PERMISSIONS",))

    def test_secrets_permissions_claude_settings(self):
        for path in (".claude/settings.json", "harnesses/claude/settings.json"):
            with self.subTest(path=path):
                result = self._classify({path})
                self.assertEqual(result.matched_rule_ids, ("C1-SECRETS-PERMISSIONS",))

    def test_network_pve_pve_prefix(self):
        result = self._classify({"MCPs/servers/pve_broker_worker.py"})
        self.assertEqual(result.matched_rule_ids, ("C1-NETWORK-PVE",))

    def test_deploy_activate_oc_deploy(self):
        result = self._classify({"opencode-config/scripts/oc_deploy.py"})
        self.assertEqual(result.matched_rule_ids, ("C1-DEPLOY-ACTIVATE",))

    def test_deploy_activate_deploy_chain_prefix(self):
        result = self._classify({"opencode-config/scripts/deploy_chain/planning.py"})
        self.assertEqual(result.matched_rule_ids, ("C1-DEPLOY-ACTIVATE",))

    def test_ansible_runtime_role_change(self):
        result = self._classify({"ansible-homelab/roles/pve_firewall/tasks/main.yml"})
        self.assertEqual(result.matched_rule_ids, ("C1-ANSIBLE-RUNTIME",))

    def test_moved_pi_role_input_keeps_ansible_runtime_class(self):
        result = self._classify({"harnesses/pi/role/templates/settings.json.j2"})
        self.assertEqual(result.matched_rule_ids, ("C1-ANSIBLE-RUNTIME",))
        extension = self._classify({"harnesses/pi/extensions/todo.js"})
        self.assertEqual(extension.gate_class, "C2")

    def test_moved_codex_inputs_keep_ansible_runtime_class(self):
        for path in (
            "harnesses/codex/role/templates/config.toml.j2",
            "harnesses/codex/role/files/requirements.toml",
            "harnesses/codex/role/vars/main.yml",
            "harnesses/deepseek/role/templates/cordis.patch.yml.j2",
            "harnesses/deepseek/role/templates/delegate-worker.patch.yml.j2",
        ):
            with self.subTest(path=path):
                result = self._classify({path})
                self.assertEqual(result.matched_rule_ids, ("C1-ANSIBLE-RUNTIME",))

    def test_ansible_runtime_exempts_tests_only(self):
        result = self._classify({"ansible-homelab/tests/test_role_contracts.py"})
        self.assertEqual(result.gate_class, "C2")

    def test_moved_claude_inputs_preserve_prior_classification(self):
        for path in (
            "harnesses/claude/role/templates/macos-settings.json.j2",
            "harnesses/claude/role/files/managed-rules.json",
            "harnesses/claude/role/files/claude/settings.local.json",
        ):
            with self.subTest(path=path):
                result = self._classify({path})
                self.assertEqual(result.matched_rule_ids, ("C1-ANSIBLE-RUNTIME",))
        for path in (
            "harnesses/claude/scripts/mcp_auto_allow.py",
            "harnesses/claude/scripts/managed_checkout_guard.py",
            "harnesses/claude/scripts/memory_writeback.py",
            "harnesses/claude/role/files/claude/agents/coder.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(self._classify({path}).gate_class, "C2")

    def test_ansible_runtime_exempts_docs_only(self):
        result = self._classify({"ansible-homelab/README.md"})
        self.assertEqual(result.gate_class, "C2")

    def test_ansible_runtime_mixed_test_and_role_still_c1(self):
        """One non-exempt path escalates the whole change, per §7.5's
        "mixed changes containing any C1 trigger" combining rule."""
        result = self._classify({
            "ansible-homelab/tests/test_role_contracts.py",
            "ansible-homelab/roles/pve_firewall/tasks/main.yml",
        })
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-ANSIBLE-RUNTIME", result.matched_rule_ids)

    def test_multiple_rules_all_reported(self):
        result = self._classify({"repos.yaml", ".forgejo/workflows/checks.yml"})
        self.assertEqual(
            result.matched_rule_ids, ("C1-CI-TRUST", "C1-REPOSITORY-AUTHORITY")
        )

    def test_mixed_c1_and_unrelated_path_still_c1(self):
        result = self._classify({"repos.yaml", "README.md"})
        self.assertEqual(result.gate_class, "C1")


class ClassifyChangeDiffShapeTests(unittest.TestCase):
    def test_symlink_mode_triggers_unknown_contradictory(self):
        raw = [":000000 120000 000000 abc1234 A\tsome/symlink"]
        result = assurance.classify_change(frozenset({"some/symlink"}), raw)
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-UNKNOWN-CONTRADICTORY", result.matched_rule_ids)

    def test_submodule_mode_triggers_unknown_contradictory(self):
        raw = [":000000 160000 000000 abc1234 A\tvendor/sub"]
        result = assurance.classify_change(frozenset({"vendor/sub"}), raw)
        self.assertIn("C1-UNKNOWN-CONTRADICTORY", result.matched_rule_ids)

    def test_ordinary_file_mode_does_not_trigger(self):
        raw = [":100644 100644 abc1234 def5678 M\tREADME.md"]
        result = assurance.classify_change(frozenset({"README.md"}), raw)
        self.assertEqual(result.gate_class, "C2")

    def test_unparseable_diff_line_is_conservative_c1(self):
        result = assurance.classify_change(frozenset({"README.md"}), ["not a raw diff line"])
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-UNKNOWN-CONTRADICTORY", result.matched_rule_ids)

    def test_short_diff_record_is_conservative_c1(self):
        result = assurance.classify_change(frozenset({"README.md"}), [":100644 100644 M\tREADME.md"])
        self.assertEqual(result.gate_class, "C1")

    def test_blank_lines_ignored(self):
        result = assurance.classify_change(frozenset({"README.md"}), ["", ""])
        self.assertEqual(result.gate_class, "C2")

    def test_binary_file_triggers_unknown_contradictory(self):
        """§15 negative test: a binary diff. Invisible to the raw-diff mode
        check (a binary file's mode is an ordinary 100644) -- only numstat's
        '-' placeholder reveals it."""
        raw = [":100644 100644 abc1234 def5678 M\tassets/logo.png"]
        numstat = ["-\t-\tassets/logo.png"]
        result = assurance.classify_change(frozenset({"assets/logo.png"}), raw, numstat)
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-UNKNOWN-CONTRADICTORY", result.matched_rule_ids)

    def test_ordinary_numstat_does_not_trigger(self):
        raw = [":100644 100644 abc1234 def5678 M\tREADME.md"]
        numstat = ["3\t1\tREADME.md"]
        result = assurance.classify_change(frozenset({"README.md"}), raw, numstat)
        self.assertEqual(result.gate_class, "C2")

    def test_unparseable_numstat_line_is_conservative_c1(self):
        raw = [":100644 100644 abc1234 def5678 M\tREADME.md"]
        result = assurance.classify_change(
            frozenset({"README.md"}), raw, ["not a numstat line"]
        )
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-UNKNOWN-CONTRADICTORY", result.matched_rule_ids)

    def test_no_numstat_supplied_defaults_to_empty(self):
        """The numstat parameter is optional -- callers that never pass it
        (matching classify_change's pre-2.5 signature) see no behavior
        change from its absence."""
        raw = [":100644 100644 abc1234 def5678 M\tREADME.md"]
        result = assurance.classify_change(frozenset({"README.md"}), raw)
        self.assertEqual(result.gate_class, "C2")


class ClassifyChangeMissingBaseTests(unittest.TestCase):
    def test_none_changed_paths_is_c1_unknown_contradictory(self):
        result = assurance.classify_change(None, [])
        self.assertEqual(
            result, assurance.ClassificationResult("C1", ("C1-UNKNOWN-CONTRADICTORY",))
        )


class ClassifyChangeDefaultC2Tests(unittest.TestCase):
    def test_empty_change_is_default_c2(self):
        result = assurance.classify_change(frozenset(), [])
        self.assertEqual(result, assurance.ClassificationResult("C2", ("DEFAULT-C2",)))

    def test_ordinary_source_file_is_default_c2(self):
        result = assurance.classify_change(
            frozenset({"MCPs/servers/knowledge_mcp.py"}), []
        )
        self.assertEqual(result.gate_class, "C2")
        self.assertEqual(result.matched_rule_ids, ("DEFAULT-C2",))


class C3AllowlistTests(unittest.TestCase):
    """§7.5/§15: the model-definition-only rule must never fire while its
    allowlist is empty, including for a plausible-looking candidate path."""

    def test_plausible_profile_path_still_falls_through_to_c2(self):
        result = assurance.classify_change(
            frozenset({"example/config/profiles/build/SOUL.md"}), []
        )
        self.assertEqual(result.gate_class, "C2")

    def test_empty_allowlist_constants(self):
        self.assertEqual(assurance._APPROVED_MODEL_DEFINITION_FILES, frozenset())
        self.assertEqual(assurance._APPROVED_MODEL_DEFINITION_FIELDS, frozenset())


class MonotonicClassTests(unittest.TestCase):
    def test_stronger_orders_c1_over_c2_over_c3(self):
        self.assertEqual(assurance.stronger("C1", "C2"), "C1")
        self.assertEqual(assurance.stronger("C2", "C1"), "C1")
        self.assertEqual(assurance.stronger("C2", "C3"), "C2")
        self.assertEqual(assurance.stronger("C3", "C2"), "C2")
        self.assertEqual(assurance.stronger("C3", "C3"), "C3")
        self.assertEqual(assurance.stronger("C1", "C1"), "C1")

    def test_effective_class_with_no_proposal(self):
        self.assertEqual(assurance.effective_class("C2", None), "C2")

    def test_proposal_can_raise_effective_class(self):
        self.assertEqual(assurance.effective_class("C2", "C1"), "C1")

    def test_proposal_cannot_lower_effective_class(self):
        self.assertEqual(assurance.effective_class("C1", "C3"), "C1")

    def test_matching_proposal_is_a_no_op(self):
        self.assertEqual(assurance.effective_class("C2", "C2"), "C2")


class AssuranceRouteTests(unittest.TestCase):
    def test_c1_is_always_tests_and_review(self):
        self.assertEqual(assurance.assurance_route_for("C1", frozenset({"repos.yaml"})), "tests_and_review")
        self.assertEqual(assurance.assurance_route_for("C1", None), "tests_and_review")

    def test_c3_is_tests_only(self):
        self.assertEqual(assurance.assurance_route_for("C3", frozenset({"x.md"})), "tests")

    def test_c2_doc_only_is_review(self):
        self.assertEqual(
            assurance.assurance_route_for("C2", frozenset({"docs/plans/foo.md", "README.md"})),
            "review",
        )

    def test_c2_openspec_prefix_counts_as_doc_only(self):
        self.assertEqual(
            assurance.assurance_route_for(
                "C2", frozenset({"opencode-config/openspec/changes/x/tasks.md"})
            ),
            "review",
        )

    def test_c2_mixed_code_and_docs_is_tests_and_review(self):
        self.assertEqual(
            assurance.assurance_route_for(
                "C2", frozenset({"README.md", "opencode-config/scripts/foo.py"})
            ),
            "tests_and_review",
        )

    def test_c2_code_only_is_tests_and_review_default(self):
        self.assertEqual(
            assurance.assurance_route_for("C2", frozenset({"opencode-config/scripts/foo.py"})),
            "tests_and_review",
        )

    def test_c2_empty_changed_paths_is_tests_and_review_default(self):
        self.assertEqual(assurance.assurance_route_for("C2", frozenset()), "tests_and_review")
        self.assertEqual(assurance.assurance_route_for("C2", None), "tests_and_review")


class RequiredCheckProfileTests(unittest.TestCase):
    def test_not_onboarded_is_always_null(self):
        for change_class in ("C1", "C2", "C3"):
            self.assertIsNone(
                assurance.required_check_profile_for(change_class, "not_onboarded")
            )

    def test_required_maps_class_to_profile_name(self):
        self.assertEqual(assurance.required_check_profile_for("C1", "required"), "checks/c1")
        self.assertEqual(assurance.required_check_profile_for("C2", "required"), "checks/c2")
        self.assertEqual(assurance.required_check_profile_for("C3", "required"), "checks/c3")

    def test_advisory_also_maps_class_to_profile_name(self):
        self.assertEqual(assurance.required_check_profile_for("C2", "advisory"), "checks/c2")


class IndependentReviewRequiredTests(unittest.TestCase):
    def test_review_routes_require_independent_review(self):
        self.assertTrue(assurance.independent_review_required_for("review"))
        self.assertTrue(assurance.independent_review_required_for("tests_and_review"))

    def test_tests_only_route_does_not(self):
        self.assertFalse(assurance.independent_review_required_for("tests"))


class C3RuleStructureTests(unittest.TestCase):
    """§15: "the model-definition-only C3 rule rejects every adjacent
    unsafe variation." The real allowlist is empty (see assurance.py's
    module docstring), so this exercises the rule's actual logic via a
    monkeypatched allowlist -- the only way §15's C3 negative tests can be
    written meaningfully at all.
    """

    MODEL_FILE = "example/config/profiles/test-agent/model.yaml"

    def setUp(self):
        self._orig_files = assurance._APPROVED_MODEL_DEFINITION_FILES
        self._orig_fields = assurance._APPROVED_MODEL_DEFINITION_FIELDS
        assurance._APPROVED_MODEL_DEFINITION_FILES = frozenset({self.MODEL_FILE})
        assurance._APPROVED_MODEL_DEFINITION_FIELDS = frozenset({"model", "provider"})

    def tearDown(self):
        assurance._APPROVED_MODEL_DEFINITION_FILES = self._orig_files
        assurance._APPROVED_MODEL_DEFINITION_FIELDS = self._orig_fields

    def test_approved_file_and_approved_field_is_c3(self):
        result = assurance.classify_change(
            frozenset({self.MODEL_FILE}), [],
            field_diffs={self.MODEL_FILE: frozenset({"model"})},
        )
        self.assertEqual(result.gate_class, "C3")
        self.assertEqual(result.matched_rule_ids, ("C3-MODEL-DEFINITION-ONLY",))

    def test_unapproved_field_falls_through_to_c2(self):
        """§15 negative test: model file changes an unapproved field."""
        result = assurance.classify_change(
            frozenset({self.MODEL_FILE}), [],
            field_diffs={self.MODEL_FILE: frozenset({"tool_permissions"})},
        )
        self.assertEqual(result.gate_class, "C2")

    def test_mixed_approved_and_unapproved_field_falls_through(self):
        result = assurance.classify_change(
            frozenset({self.MODEL_FILE}), [],
            field_diffs={self.MODEL_FILE: frozenset({"model", "tool_permissions"})},
        )
        self.assertEqual(result.gate_class, "C2")

    def test_generated_consumer_alongside_model_definition_falls_through(self):
        """§15 negative test: generated consumer changed alongside model
        definition. Caught by condition 1 alone -- the consumer path is
        never itself in the approved-files allowlist."""
        result = assurance.classify_change(
            frozenset({self.MODEL_FILE, "example/config/generated_consumer.yaml"}), [],
            field_diffs={self.MODEL_FILE: frozenset({"model"})},
        )
        self.assertEqual(result.gate_class, "C2")

    def test_no_field_diffs_supplied_never_grants_c3(self):
        """A real caller can never supply field_diffs for a path that fails
        condition 1 -- condition 1 alone must never be enough."""
        result = assurance.classify_change(frozenset({self.MODEL_FILE}), [], field_diffs=None)
        self.assertEqual(result.gate_class, "C2")

    def test_permission_or_secret_path_mixed_with_c3_candidate_is_c1(self):
        """§15 negative test: permission or secret path mixed with a C3
        candidate. C1 checks run first and win regardless of how the C3
        rule itself would have resolved."""
        result = assurance.classify_change(
            frozenset({self.MODEL_FILE, ".claude/settings.json"}), [],
            field_diffs={self.MODEL_FILE: frozenset({"model"})},
        )
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-SECRETS-PERMISSIONS", result.matched_rule_ids)

    def test_workflow_file_mixed_with_c3_candidate_is_c1(self):
        result = assurance.classify_change(
            frozenset({self.MODEL_FILE, ".forgejo/workflows/checks.yml"}), [],
            field_diffs={self.MODEL_FILE: frozenset({"model"})},
        )
        self.assertEqual(result.gate_class, "C1")
        self.assertIn("C1-CI-TRUST", result.matched_rule_ids)


# One negative case the classifier cannot answer belongs to the layer above
# it: whether a repository whose control policy closes its mutation map is
# refused a governed merge is a question about that policy, not about a
# changed-path set. It is not duplicated here because this module has no
# opinion on it.


class EntryPointTests(unittest.TestCase):
    """The one contract that is not a pure function: a caller may execute this
    file with changed paths on stdin and read ``<rule_id>\\t<path>`` rows."""

    def _run(self, lines):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = assurance.main(lines)
        return status, out.getvalue().splitlines()

    def test_one_row_per_matching_path_and_rule(self):
        status, rows = self._run([
            "opencode-config/scripts/git_gate/assurance.py\n",
            "README.md\n",
        ])
        self.assertEqual(status, 0)
        self.assertEqual(rows, [
            "C1-GATE-AUTHORITY\topencode-config/scripts/git_gate/assurance.py",
        ])

    def test_blank_lines_and_whitespace_are_not_paths(self):
        status, rows = self._run(["\n", "  \n", "README.md\n"])
        self.assertEqual(status, 0)
        self.assertEqual(rows, [])

    def test_every_matching_rule_is_printed_for_one_path(self):
        status, rows = self._run(["platform/scripts/git_gate/ci.py\n"])
        self.assertEqual(status, 0)
        self.assertTrue(rows)
        for row in rows:
            rule, path = row.split("\t")
            self.assertTrue(rule.startswith("C1-"))
            self.assertEqual(path, "platform/scripts/git_gate/ci.py")


if __name__ == "__main__":
    unittest.main()
