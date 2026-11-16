"""The login alphabet and the maintainer permissions depend on the forge.

GitHub keeps the rules it had before the forge port: a login of letters,
digits and "-", and the permissions write, maintain and admin. Forgejo also
allows "_" and "." in a login and reports a repository owner as "owner", so
only a Forgejo run widens the rules.
"""

from __future__ import annotations

import pytest

from merge_guard import engine
from merge_guard.forge import ApiError

HEAD = "a" * 40


class Forge:
    """Answers the one permission read that ``maintainer_ids`` makes."""

    def __init__(self, name: str | None, permission: str = "write") -> None:
        if name is not None:
            self.name = name
        self.permission = permission
        self.asked: list[str] = []

    def request(self, path: str) -> dict:
        self.asked.append(path)
        login = path.split("/")[1]
        return {"user": {"id": USERS[login]}, "permission": self.permission}


USERS = {"carol": 99, "a.b": 98, "a_b": 97}


def review(login: str) -> dict:
    return {"id": 1, "state": "APPROVED", "commit_id": HEAD,
            "user": {"id": USERS.get(login, 96), "login": login, "type": "User"}}


@pytest.mark.parametrize("name", ["github", None])
def test_a_github_login_keeps_the_github_alphabet(name):
    api = Forge(name)
    for login in ("a_b", "a.b"):
        with pytest.raises(ApiError, match="maintainer_identity_unusable"):
            engine.maintainer_ids(api, [review(login)], "author")
    assert api.asked == []


@pytest.mark.parametrize("name", ["github", None])
def test_owner_is_not_a_github_maintainer_permission(name):
    assert engine.maintainer_ids(Forge(name, "owner"), [review("carol")], "author") == set()
    assert engine.maintainer_ids(Forge(name, "admin"), [review("carol")], "author") == {99}


def test_a_forgejo_login_may_hold_an_underscore_and_a_dot():
    api = Forge("forgejo")
    assert engine.maintainer_ids(api, [review("a.b"), review("a_b")], "author") == {98, 97}
    assert api.asked == ["collaborators/a.b/permission", "collaborators/a_b/permission"]


@pytest.mark.parametrize("login", [".x", "..", "a..b", "a/b", "a b"])
def test_a_forgejo_login_that_could_name_another_path_is_refused(login):
    api = Forge("forgejo")
    with pytest.raises(ApiError, match="maintainer_identity_unusable"):
        engine.maintainer_ids(api, [review(login)], "author")
    assert api.asked == []


def test_owner_is_a_forgejo_maintainer_permission():
    assert engine.maintainer_ids(Forge("forgejo", "owner"), [review("carol")], "author") == {99}
    assert engine.maintainer_ids(Forge("forgejo", "maintain"), [review("carol")], "author") == {99}
    assert engine.maintainer_ids(Forge("forgejo", "read"), [review("carol")], "author") == set()
