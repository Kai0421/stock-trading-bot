# Author: Kai0421
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".githooks" / "pre-push"
ZERO = "0" * 40
SHA = "a" * 40

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def run(local_ref: str, local_sha: str, remote_ref: str) -> subprocess.CompletedProcess:
    line = f"{local_ref} {local_sha} {remote_ref} {SHA}\n"
    return subprocess.run(["sh", str(HOOK)], input=line, capture_output=True, text=True)


def test_push_to_main_is_blocked():
    r = run("refs/heads/main", SHA, "refs/heads/main")
    assert r.returncode == 1 and "pushing straight to" in r.stderr


def test_pushing_another_branch_to_main_is_blocked():
    assert run("refs/heads/feature", SHA, "refs/heads/main").returncode == 1


def test_deleting_main_is_blocked():
    r = run("(delete)", ZERO, "refs/heads/main")
    assert r.returncode == 1 and "deleting" in r.stderr


def test_feature_branch_push_and_delete_are_allowed():
    assert run("refs/heads/feature", SHA, "refs/heads/feature").returncode == 0
    assert run("(delete)", ZERO, "refs/heads/feature").returncode == 0


def test_tags_are_allowed():
    assert run("refs/tags/v1", SHA, "refs/tags/v1").returncode == 0
