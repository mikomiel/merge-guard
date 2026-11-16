"""Forgejo adapter rules that a recorded case cannot show: URL, token, transport."""

from __future__ import annotations

import socket
import urllib.error

import pytest
from forge_replay import FORGEJO_TOKEN, FORGEJO_URL, Exchanges, ForgejoOpener

from merge_guard import forgejo, github
from merge_guard import policy as mg
from merge_guard.forge import ApiError

HEAD = "a" * 40


def policy(forge="forgejo", name="example/widgets"):
    return mg.load_policy({
        "repository": {"name": name, "base_branch": "main", "forge": forge},
        "review": {"reviewer_id": 4242, "reviewer_login": "review-bot",
                   "required_checks": ["checks"]},
    })


def adapter(exchanges, apply=False):
    return forgejo.Forgejo(policy(), FORGEJO_URL, FORGEJO_TOKEN, apply=apply,
                           opener=ForgejoOpener(Exchanges(exchanges)))


def settings():
    return {"request": {"method": "GET", "path": "/settings/api"},
            "response": {"status": 200, "body": {"max_response_items": 2}}}


def page(path, number, items, total, headers=None):
    joiner = "&" if "?" in path else "?"
    return {"request": {"method": "GET", "path": f"{path}{joiner}limit=2&page={number}"},
            "response": {"status": 200, "body": items,
                         "headers": headers if headers is not None else {"X-Total-Count": str(total)}}}


@pytest.mark.parametrize("url", [
    "http://forgejo.example",          # clear text to another machine
    "https://user:secret@forgejo.example",
    "https://forgejo.example/?x=1",
    "https://forgejo.example/#top",
    "ftp://forgejo.example",
    "forgejo.example",
])
def test_a_url_the_token_must_not_reach_is_refused(url):
    with pytest.raises(mg.Unusable, match="forgejo_url_invalid"):
        forgejo.api_root(url)


@pytest.mark.parametrize("url", [
    "https://[",                       # an IPv6 authority that does not close
    "https://[::1",
    "https://]",
    "https://[forgejo.example]",       # brackets around a name, not an address
    "https://forgejo.example:abc",
    "https://forgejo.example:-1",
    "https://forgejo.example:65536",
    "https://forgejo.example:99999",
    "https://forgejo.example:0",       # parses, but no request can reach it
    "http://localhost:0",
    # urlsplit reads an empty user name, password, query or fragment as none,
    # but urllib requests the string: its host would be "@localhost:3000".
    "http://@localhost:3000",
    "http://:@127.0.0.1:3000",
    "https://@forgejo.example",
    "https://forgejo.example?",
    "https://forgejo.example#",
    # urllib decodes a percent-encoded host before it connects: to port 0, to
    # the host "@forgejo.example" and to "forgejo.example".
    "https://forgejo.example%3A0",
    "https://%40forgejo.example",
    "https://forgejo%2Eexample",
    # urlsplit drops a tab, a line break and leading control characters and
    # spaces, but urllib requests them.
    "http://local\thost:3000",
    "http://local\rhost:3000",
    "http://localhost:3000\n",
    "\x01http://localhost:3000",
    " https://forgejo.example",
    "https://forgejo.example/my git",
    "https://forgejo.example\x00",
    "https://forgejo.example\x7f",
    # A C1 control character (Unicode category Cc, U+0080 to U+009F).
    "https://forgejo.example/\x85",
    "https://forgejo.example\x9f",
    # Text around a bracketed host: refused whether or not urlsplit itself refuses it.
    "http://[::1]suffix:3000",
    "http://x[::1]:3000",
])
def test_a_malformed_url_is_refused(url):
    with pytest.raises(mg.Unusable, match="forgejo_url_invalid"):
        forgejo.api_root(url)


def test_https_and_loopback_http_are_accepted():
    assert forgejo.api_root("https://forgejo.example/") == "https://forgejo.example/api/v1"
    assert forgejo.api_root("https://forgejo.example/git") == "https://forgejo.example/git/api/v1"
    assert forgejo.api_root("https://forgejo.example:8443") == "https://forgejo.example:8443/api/v1"
    assert forgejo.api_root("https://forgejo.example:65535") == "https://forgejo.example:65535/api/v1"
    assert forgejo.api_root("http://localhost:3000") == "http://localhost:3000/api/v1"
    assert forgejo.api_root("http://127.0.0.1:3000") == "http://127.0.0.1:3000/api/v1"
    assert forgejo.api_root("http://[::1]:3000") == "http://[::1]:3000/api/v1"


def test_the_environment_names_the_instance_and_the_token(monkeypatch):
    monkeypatch.delenv("FORGEJO_URL", raising=False)
    monkeypatch.setenv("FORGEJO_TOKEN", "t")
    with pytest.raises(mg.Unusable, match="forgejo_url_missing"):
        forgejo.Forgejo.from_environment(policy())
    monkeypatch.setenv("FORGEJO_URL", "https://forgejo.example")
    monkeypatch.setenv("FORGEJO_TOKEN", "")
    with pytest.raises(mg.Unusable, match="forgejo_token_missing"):
        forgejo.Forgejo.from_environment(policy())
    monkeypatch.setenv("FORGEJO_TOKEN", "t")
    assert forgejo.Forgejo.from_environment(policy()).root == "https://forgejo.example/api/v1"


def test_the_adapter_reads_only_a_policy_that_names_forgejo():
    with pytest.raises(mg.Unusable, match="policy_forge_missing"):
        forgejo.Forgejo(policy(None), FORGEJO_URL, FORGEJO_TOKEN)
    with pytest.raises(mg.Unusable, match="policy_forge_conflict"):
        forgejo.Forgejo(policy("github"), FORGEJO_URL, FORGEJO_TOKEN)
    with pytest.raises(mg.Unusable, match="policy_forge_conflict"):
        github.GitHub(policy("forgejo"))


def test_a_repository_that_names_a_parent_path_is_refused():
    with pytest.raises(mg.Unusable, match="policy_repository_invalid"):
        forgejo.Forgejo(policy(name="../widgets"), FORGEJO_URL, FORGEJO_TOKEN)


def test_redirects_are_not_followed():
    api = forgejo.Forgejo(policy(), FORGEJO_URL, FORGEJO_TOKEN)
    assert any(isinstance(handler, forgejo._NoRedirect) for handler in api._opener.handlers)
    assert forgejo._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x") is None


def connections(monkeypatch, environment):
    """The address of each connection that a request opens, with no network."""
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy",
                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    reached = []

    def connect(address, *args, **kwargs):
        reached.append(address)
        raise ConnectionRefusedError("a test opens no connection")

    monkeypatch.setattr(socket, "create_connection", connect)
    return reached


@pytest.mark.parametrize("url, no_proxy, address", [
    ("http://localhost:3000", None, ("localhost", 3000)),
    ("http://127.0.0.1:3000", None, ("127.0.0.1", 3000)),
    ("http://[::1]:3000", None, ("::1", 3000)),
    ("HTTP://localhost:3000", None, ("localhost", 3000)),
    # no_proxy never matches "[::1]", the host as urllib spells it.
    ("http://[::1]:3000", "localhost,127.0.0.1,::1", ("::1", 3000)),
])
def test_a_plain_http_request_never_goes_through_a_proxy(monkeypatch, url, no_proxy, address):
    # Over http the token travels in clear text, so the request goes to the
    # forge on this machine even when the environment names a proxy.
    environment = {"http_proxy": "http://proxy.invalid:3128"}
    if no_proxy is not None:
        environment["no_proxy"] = no_proxy
    reached = connections(monkeypatch, environment)
    api = forgejo.Forgejo(policy(), url, FORGEJO_TOKEN)
    with pytest.raises(ApiError, match="forgejo_transport_unavailable"):
        api._limit()
    assert reached == [address]


def test_an_https_request_keeps_the_proxy_of_the_environment(monkeypatch):
    # Through a proxy, an https request opens a tunnel, and the token stays inside it.
    reached = connections(monkeypatch, {"https_proxy": "http://proxy.invalid:3128"})
    api = forgejo.Forgejo(policy(), FORGEJO_URL, FORGEJO_TOKEN)
    with pytest.raises(ApiError, match="forgejo_transport_unavailable"):
        api._limit()
    assert reached == [("proxy.invalid", 3128)]


def test_writes_need_apply():
    api = adapter([])
    with pytest.raises(ApiError, match="merge_write_refused"):
        api.merge(7, HEAD)
    with pytest.raises(ApiError, match="comment_write_refused"):
        api.comment(7, "parked")
    with pytest.raises(ApiError, match="write_disabled"):
        api._send("POST", api._repo(f"statuses/{HEAD}"), {"state": "success"})


class Raising:
    def __init__(self, error):
        self.error = error

    def open(self, request, timeout=None):
        raise self.error


@pytest.mark.parametrize("error, code", [
    (urllib.error.URLError("refused"), "forgejo_transport_unavailable"),
    (TimeoutError("slow"), "forgejo_transport_unavailable"),
    (urllib.error.HTTPError(FORGEJO_URL, 500, "error", {}, None), "forgejo_api_failed"),
])
def test_a_transport_failure_is_a_named_refusal(error, code):
    api = forgejo.Forgejo(policy(), FORGEJO_URL, FORGEJO_TOKEN, opener=Raising(error))
    with pytest.raises(ApiError, match=code):
        api.branch_head("main")


def test_an_oversized_or_unreadable_body_is_refused(monkeypatch):
    monkeypatch.setattr(forgejo, "MAX_RESPONSE_BYTES", 10)
    big = adapter([{"request": {"method": "GET", "path": "/repos/example/widgets/branches/main"},
                    "response": {"status": 200, "body": {"name": "main", "commit": {"id": HEAD}}}}])
    with pytest.raises(ApiError, match="forgejo_api_failed"):
        big.branch_head("main")


@pytest.mark.parametrize("raw", [b"<html>maintenance</html>", b"[" * 100_000,
                                 b'{"id": ' + b"9" * 5000 + b"}"])
def test_a_body_that_is_not_json_is_refused(raw):
    # The second body nests deeper than the parser's recursion limit; the third
    # holds an integer longer than Python reads from a string.
    class Text(ForgejoOpener):
        def open(self, request, timeout=None):
            response = super().open(request, timeout)
            response._raw = raw
            return response

    api = forgejo.Forgejo(policy(), FORGEJO_URL, FORGEJO_TOKEN, opener=Text(Exchanges([
        {"request": {"method": "GET", "path": "/repos/example/widgets/branches/main"},
         "response": {"status": 200, "body": {}}}])))
    with pytest.raises(ApiError, match="forgejo_response_unreadable"):
        api.branch_head("main")


def test_a_list_is_read_page_by_page_until_its_count():
    path = "/repos/example/widgets/pulls/7/files"
    api = adapter([settings(),
                   page(path, 1, [{"filename": "a.md"}, {"filename": "b.md"}], 3),
                   page(path, 2, [{"filename": "c.md"}], 3)])
    assert [item["filename"] for item in api._pages("pulls/7/files")] == ["a.md", "b.md", "c.md"]


@pytest.mark.parametrize("second, code", [
    ({"X-Total-Count": "4"}, "forgejo_page_total_changed"),
    ({}, "forgejo_page_total_unusable"),
    ({"X-Total-Count": "three"}, "forgejo_page_total_unusable"),
    # Digits that are not ASCII: int() reads U+0663 as 3 and refuses U+00B2.
    ({"X-Total-Count": "\u0663"}, "forgejo_page_total_unusable"),
    ({"X-Total-Count": "\u00b2"}, "forgejo_page_total_unusable"),
    # Longer than int() reads from a string.
    ({"X-Total-Count": "9" * 5000}, "forgejo_page_total_unusable"),
])
def test_a_list_whose_count_moves_or_is_missing_is_refused(second, code):
    path = "/repos/example/widgets/pulls/7/files"
    api = adapter([settings(),
                   page(path, 1, [{"filename": "a.md"}, {"filename": "b.md"}], 3),
                   page(path, 2, [{"filename": "c.md"}], 3, headers=second)])
    with pytest.raises(ApiError, match=code):
        api._pages("pulls/7/files")


def status(index, context, state="success", creator=None):
    return {"id": index, "status": state, "context": context, "description": "",
            "target_url": "", "creator": creator}


def test_statuses_are_read_in_the_order_of_their_unique_index():
    # The default order is the creation time in seconds, which is not unique,
    # so a status could move between pages. The replay matches the query
    # exactly, so this read proves that the adapter asks for sort=leastindex.
    path = f"/repos/example/widgets/statuses/{HEAD}?sort=leastindex"
    gate = {"id": 900, "login": "merge-gate"}
    api = adapter([settings(),
                   page(path, 1, [status(3, "review", creator=gate), status(2, "lint")], 3),
                   page(path, 2, [status(1, "ci")], 3)])
    assert [item["id"] for item in api._statuses(HEAD)] == [3, 2, 1]


def test_a_list_that_repeats_an_item_is_refused():
    # Page 2 repeats a status of page 1 and so skips another one; the item count
    # still equals X-Total-Count.
    path = f"/repos/example/widgets/statuses/{HEAD}?sort=leastindex"
    api = adapter([settings(),
                   page(path, 1, [status(2, "lint"), status(1, "ci")], 3),
                   page(path, 2, [status(1, "ci")], 3)])
    with pytest.raises(ApiError, match="forgejo_page_item_repeated"):
        api._statuses(HEAD)


def test_a_file_list_that_repeats_a_file_is_refused():
    path = "/repos/example/widgets/pulls/7/files"
    api = adapter([settings(),
                   page(path, 1, [{"filename": "a.md"}, {"filename": "b.md"}], 3),
                   page(path, 2, [{"filename": "a.md"}], 3)])
    with pytest.raises(ApiError, match="forgejo_page_item_repeated"):
        api._pages("pulls/7/files", identity="filename")


CONTENTS = "https://forgejo.example/api/v1/repos/example/widgets/contents/docs/a.md"


@pytest.mark.parametrize("url, ref", [
    (f"{CONTENTS}?ref={HEAD}", HEAD),
    (f"{CONTENTS}?ref={'c' * 40}", "c" * 40),
    (f"{CONTENTS}?ref={HEAD}&ref={'c' * 40}", None),   # the whole query is one ref
    (f"{CONTENTS}?x=1&ref={HEAD}", None),
    (f"{CONTENTS}%3Fref={HEAD}", None),                  # an escaped "?" is part of the path
    (f"{CONTENTS}?ref=main", None),
    (CONTENTS, None),
    (None, None),
])
def test_a_changed_file_names_the_commit_of_its_list(url, ref):
    assert forgejo._ref_of(url) == ref


def test_a_list_longer_than_the_page_bound_is_refused(monkeypatch):
    monkeypatch.setattr(forgejo, "MAX_PAGES", 1)
    path = "/repos/example/widgets/pulls/7/files"
    api = adapter([settings(), page(path, 1, [{"filename": "a.md"}, {"filename": "b.md"}], 3)])
    with pytest.raises(ApiError, match="forgejo_pagination_exhausted"):
        api._pages("pulls/7/files")


APPROVAL = {"id": 101, "state": "APPROVED", "dismissed": False, "commit_id": HEAD,
            "submitted_at": "2026-11-11T08:30:30+01:00",
            "user": {"id": 4242, "login": "review-bot"}}


@pytest.mark.parametrize("returned, total", [([], 1), ([APPROVAL], 2), ([APPROVAL], 0)])
def test_a_review_list_that_does_not_hold_its_count_is_refused(returned, total):
    # Forgejo counts the pending reviews of other accounts but does not return
    # them, and no response tells such a review from one that was left out: a
    # change request, for example. So the review list holds its count exactly.
    path = "/repos/example/widgets/pulls/7/reviews"
    api = adapter([settings(), page(path, 1, returned, total)])
    with pytest.raises(ApiError, match="forgejo_review_list_incomplete"):
        api.reviews(7)
    # Every other list keeps its own code.
    files = "/repos/example/widgets/pulls/7/files"
    api = adapter([settings(), page(files, 1, [], 1)])
    with pytest.raises(ApiError, match="forgejo_pagination_incomplete"):
        api._pages("pulls/7/files")


@pytest.mark.parametrize("state", [[], {}, ["APPROVED"], 1, None])
def test_a_review_state_that_is_not_a_string_is_passed_on_as_none(state):
    # The engine refuses None as unknown_review_state, so a malformed state is a
    # refusal, not a traceback.
    path = "/repos/example/widgets/pulls/7/reviews"
    api = adapter([settings(), page(path, 1, [{**APPROVAL, "state": state}], 1)])
    assert [(item["id"], item["state"]) for item in api.reviews(7)] == [(101, None)]


def test_a_review_list_that_holds_its_count_is_read():
    path = "/repos/example/widgets/pulls/7/reviews"
    api = adapter([settings(), page(path, 1, [APPROVAL], 1)])
    assert [(item["id"], item["state"]) for item in api.reviews(7)] == [(101, "APPROVED")]


def test_an_unusable_page_size_is_refused():
    api = adapter([{"request": {"method": "GET", "path": "/settings/api"},
                    "response": {"status": 200, "body": {"max_response_items": 0}}}])
    with pytest.raises(ApiError, match="forgejo_settings_unusable"):
        api.pulls()


def test_a_comment_is_written_and_confirmed():
    api = adapter([{"request": {"method": "POST", "path": "/repos/example/widgets/issues/7/comments",
                                "body": {"body": "parked: sensitive_paths"}},
                    "response": {"status": 201, "body": {"id": 31, "body": "parked: sensitive_paths"}}}],
                  apply=True)
    api.comment(7, "parked: sensitive_paths")


def test_an_unconfirmed_gate_status_write_is_refused():
    api = adapter([settings(),
                   page(f"/repos/example/widgets/statuses/{HEAD}?sort=leastindex", 1, [], 0),
                   {"request": {"method": "POST", "path": f"/repos/example/widgets/statuses/{HEAD}",
                                "body": {"state": "success", "context": "review",
                                         "description": "approved_at_head"}},
                    "response": {"status": 201, "body": {"id": 1, "status": "pending",
                                                         "context": "review"}}}],
                  apply=True)
    with pytest.raises(ApiError, match="forgejo_status_not_confirmed"):
        api.publish(HEAD, "success", "approved_at_head")


RUNS = f"/repos/example/widgets/actions/runs?head_sha={HEAD}&limit=2&page="


def run(run_id, event="pull_request", trigger_event="pull_request", sha=HEAD):
    return {"id": run_id, "commit_sha": sha, "event": event, "trigger_event": trigger_event,
            "workflow_id": "ci.yml", "status": "success"}


def job(job_id, run_id, name, state):
    return {"id": job_id, "run_id": run_id, "name": name, "status": state, "attempt": 1}


def runs_page(number, items, total):
    return {"request": {"method": "GET", "path": f"{RUNS}{number}"},
            "response": {"status": 200, "body": {"total_count": total, "workflow_runs": items}}}


def jobs_of(run_id, items):
    return {"request": {"method": "GET", "path": f"/repos/example/widgets/actions/runs/{run_id}/jobs"},
            "response": {"status": 200, "body": items}}


def test_a_check_run_is_a_job_of_an_actions_run_for_the_head():
    api = adapter([settings(), runs_page(1, [run(31)], 1),
                   jobs_of(31, [job(301, 31, "checks", "success"),
                                job(302, 31, "deploy", "skipped"),
                                job(303, 31, "lint", "running")])])
    assert api.check_runs(HEAD) == [
        {"id": 301, "name": "checks", "head_sha": HEAD, "status": "completed",
         "conclusion": "success", "app": {"id": mg.ACTIONS_APP_ID}},
        {"id": 302, "name": "deploy", "head_sha": HEAD, "status": "completed",
         "conclusion": "skipped", "app": {"id": mg.ACTIONS_APP_ID}},
        {"id": 303, "name": "lint", "head_sha": HEAD, "status": "in_progress",
         "conclusion": None, "app": {"id": mg.ACTIONS_APP_ID}},
    ]


@pytest.mark.parametrize("states, status, conclusion", [
    (["success", "success"], "completed", "success"),
    (["success", "skipped"], "completed", "success"),
    (["skipped", "skipped"], "completed", "skipped"),
    # A re-run updates the older job in place, so the newer job id does not
    # make the newer result: one red job is the conclusion.
    (["failure", "success"], "completed", "failure"),
    (["success", "cancelled"], "completed", "cancelled"),
    (["success", "waiting"], "in_progress", None),
    (["failure", "blocked"], "in_progress", None),
])
def test_jobs_of_two_runs_with_one_name_are_one_check(states, status, conclusion):
    # The run list is newest first, as Forgejo orders it by id.
    api = adapter([settings(), runs_page(1, [run(32, "push", "push"), run(31)], 2),
                   jobs_of(32, [job(302, 32, "checks", states[1])]),
                   jobs_of(31, [job(301, 31, "checks", states[0])])])
    [check] = api.check_runs(HEAD)
    assert (check["id"], check["name"], check["status"], check["conclusion"]) == (
        302, "checks", status, conclusion)


def test_the_run_list_is_read_until_its_total_count():
    api = adapter([settings(), runs_page(1, [run(33), run(32)], 3), runs_page(2, [run(31)], 3),
                   jobs_of(33, []), jobs_of(32, []), jobs_of(31, [job(301, 31, "checks", "success")])])
    assert [check["id"] for check in api.check_runs(HEAD)] == [301]


@pytest.mark.parametrize("second, code", [
    ({"total_count": 4, "workflow_runs": [run(31)]}, "forgejo_page_total_changed"),
    ({"workflow_runs": [run(31)]}, "forgejo_page_total_unusable"),
    ({"total_count": "3", "workflow_runs": [run(31)]}, "forgejo_page_total_unusable"),
    ({"total_count": 3, "workflow_runs": [run(32)]}, "forgejo_page_item_repeated"),
    ({"total_count": 3}, "forgejo_page_unreadable"),
])
def test_a_run_list_whose_count_or_items_do_not_hold_is_refused(second, code):
    api = adapter([settings(), runs_page(1, [run(33), run(32)], 3),
                   {"request": {"method": "GET", "path": f"{RUNS}2"},
                    "response": {"status": 200, "body": second}}])
    with pytest.raises(ApiError, match=code):
        api.check_runs(HEAD)


@pytest.mark.parametrize("listed, jobs, code", [
    (run(31, sha="c" * 40), [], "forgejo_run_unusable"),          # a run for another commit
    ({**run(31), "id": "31"}, [], "forgejo_run_unusable"),
    (run(31), [job(301, 32, "checks", "success")], "forgejo_job_unusable"),  # another run's job
    (run(31), [job(301, 31, "", "success")], "forgejo_job_unusable"),
    (run(31), [job(301, 31, "checks", ["success"])], "forgejo_job_unusable"),
    (run(31), [job(301, 31, "checks", "exploded")], "forgejo_job_unusable"),
    (run(31), [job(301, 31, "checks", "success")] * 2, "forgejo_page_item_repeated"),
    (run(31), {"jobs": [job(301, 31, "checks", "success")]}, "forgejo_job_unusable"),
])
def test_a_run_or_a_job_that_does_not_name_its_place_is_refused(listed, jobs, code):
    exchanges = [settings(), runs_page(1, [listed], 1)]
    if code != "forgejo_run_unusable":
        exchanges.append(jobs_of(31, jobs))
    with pytest.raises(ApiError, match=code):
        adapter(exchanges).check_runs(HEAD)


@pytest.mark.parametrize("event, trigger_event, label", [
    ("push", "push", "push"),
    ("pull_request", "pull_request", "pull_request"),
    ("pull_request", "pull_request_target", "pull_request_target"),
    ("release", "release", "release"),
    ("workflow_dispatch", "workflow_dispatch", None),
    ("schedule", "schedule", None),
    (None, None, None),
])
def test_the_event_in_the_context_of_a_job_copy(event, trigger_event, label):
    assert forgejo._status_event(run(31, event, trigger_event)) == label


def test_only_a_creatorless_copy_of_a_job_of_the_head_is_left_out_of_the_statuses():
    gate = {"id": 900, "login": "merge-gate"}
    items = [status(6, "review", creator=None),                        # the gate's context
             status(5, "ci / checks (pull_request)", "failure"),        # a copy of the job
             status(4, "ci / checks (push)", "failure"),                # no push run
             status(3, "checks", "success"),                            # not a copy
             status(2, "ext / checks (pull_request)", creator={"id": 7, "login": "carol"}),
             status(1, "review", creator=gate)]
    api = adapter([settings(), page(f"/repos/example/widgets/statuses/{HEAD}?sort=leastindex",
                                    1, items[:2], 6),
                   page(f"/repos/example/widgets/statuses/{HEAD}?sort=leastindex", 2, items[2:4], 6),
                   page(f"/repos/example/widgets/statuses/{HEAD}?sort=leastindex", 3, items[4:], 6),
                   runs_page(1, [run(31)], 1), jobs_of(31, [job(301, 31, "checks", "success")]),
                   {"request": {"method": "GET", "path": "/user"},
                    "response": {"status": 200, "body": {"id": 900, "login": "merge-gate"}}}])
    assert [(item["id"], item["creator"]) for item in api.statuses(HEAD)] == [
        (6, None), (4, None), (3, None), (2, {"login": "carol", "type": "User"}),
        (1, {"login": mg.GATE_PUBLISHER[0], "type": mg.GATE_PUBLISHER[1]})]


def test_the_github_adapter_writes_and_confirms_a_comment():
    api = github.GitHub(policy("github"), apply=True)
    calls = []

    def request(path, method="GET", body=None):
        calls.append((method, path, body))
        return {"id": 31}

    api.request = request
    api.comment(7, "parked: sensitive_paths")
    assert calls == [("POST", "issues/7/comments", {"body": "parked: sensitive_paths"})]
    api.request = lambda path, method="GET", body=None: {"message": "not a comment"}
    with pytest.raises(ApiError, match="comment_not_confirmed"):
        api.comment(7, "parked")
    with pytest.raises(ApiError, match="comment_write_refused"):
        github.GitHub(policy("github")).comment(7, "parked")
