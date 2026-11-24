"""Continuous, test-gated self-update: no versions to install, nobody locked into an old one. An
upstream improvement is tested in staging and taken only when green; a broken one is held back; local
edits are never overwritten; "undo the update" goes back in one step."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wright.control import route
from wright.events import EventBus
from wright.updater import Updater

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=ENV, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    (dev / "app.py").write_text("def greet():\n    return 'hello'\n")
    (dev / "test_app.py").write_text("from app import greet\n\ndef test_greet():\n    assert greet().startswith('hello')\n")
    git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", "first"); git(dev, "push", "-q", "origin", "main")
    installed = tmp_path / "installed"
    git(tmp_path, "clone", "-q", str(origin), str(installed))
    return dev, installed, tmp_path


def push(dev, body, msg):
    (dev / "app.py").write_text(body)
    git(dev, "commit", "-qam", msg)
    git(dev, "push", "-q", "origin", "main")


def updater(installed, tmp_path, ev=None):
    return Updater(tmp_path / "home", repo=installed, events=ev,
                   test_cmd=[sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider"])


def test_good_improvement_is_tested_then_taken(repos):
    dev, installed, tmp = repos
    push(dev, "def greet():\n    return 'hello there'\n", "friendlier greeting")
    ev = EventBus()
    r = updater(installed, tmp, ev).apply()
    assert r["status"] == "updated" and r["changes"] == ["friendlier greeting"]
    assert "hello there" in (installed / "app.py").read_text()
    assert [e["status"] for e in ev.of("update")] == ["testing", "updated"]


def test_broken_improvement_is_held_back(repos):
    dev, installed, tmp = repos
    push(dev, "def greet():\n    return 'bye'\n", "breaks the test")
    r = updater(installed, tmp).apply()
    assert r["status"] == "held_back"
    assert (installed / "app.py").read_text() == "def greet():\n    return 'hello'\n"
    assert not (tmp / "home" / "updates").exists() or not any((tmp / "home" / "updates").iterdir())


def test_local_edits_are_never_overwritten(repos):
    dev, installed, tmp = repos
    push(dev, "def greet():\n    return 'hello there'\n", "improvement")
    (installed / "app.py").write_text("def greet():\n    return 'hello, my own edit'\n")
    assert updater(installed, tmp).apply()["status"] == "held"
    assert "my own edit" in (installed / "app.py").read_text()


def test_undo_the_update_in_one_step(repos):
    dev, installed, tmp = repos
    push(dev, "def greet():\n    return 'hello there'\n", "improvement")
    u = updater(installed, tmp)
    u.apply()
    assert u.rollback()["status"] == "rolled_back"
    assert (installed / "app.py").read_text() == "def greet():\n    return 'hello'\n"
    assert u.apply()["status"] == "updated"                         # and forward again


def test_update_requests_in_plain_words():
    assert route("update yourself")["action"] == "update"
    assert route("check for improvements")["action"] == "update"
    assert route("undo the update")["action"] == "update_undo"
    assert route("undo that")["action"] == "undo"
