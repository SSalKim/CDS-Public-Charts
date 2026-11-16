import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from public_charts_publish import MANIFEST, PublicationError, publish_manifest


def git(root, *args):
    return subprocess.run(["git", "-c", f"safe.directory={Path(root).as_posix()}",
                           "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                          cwd=root, capture_output=True, text=True, encoding="utf-8", check=True).stdout


def make_repository(root):
    remote, local = root / "remote.git", root / "checkout"
    git(root, "init", "--bare", "--initial-branch=main", str(remote))
    git(root, "clone", str(remote), str(local))
    path = local / MANIFEST
    path.parent.mkdir()
    path.write_text('{"charts":[]}\n', encoding="utf-8")
    git(local, "add", MANIFEST)
    git(local, "commit", "-m", "Initial manifest")
    git(local, "push", "origin", "main")
    return local, remote


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.local, self.remote = make_repository(Path(self.temporary.name))

    def remote_manifest(self):
        return json.loads(git(self.remote, "show", f"main:{MANIFEST}"))

    def test_only_manifest_is_committed_and_unrelated_staging_is_preserved(self):
        unrelated = self.local / "user.txt"
        unrelated.write_text("Keep my staged changes", encoding="utf-8")
        git(self.local, "add", "user.txt")
        expected = {"charts": [{"hour": 24}]}
        (self.local / MANIFEST).write_text(json.dumps(expected), encoding="utf-8")
        self.assertEqual(publish_manifest(self.local), expected)
        self.assertEqual(self.remote_manifest(), expected)
        self.assertEqual(git(self.local, "diff", "--cached", "--name-only").strip(), "user.txt")
        self.assertEqual(git(self.remote, "show", "--pretty=format:", "--name-only", "main").strip(), MANIFEST)

    def test_unchanged_manifest_does_not_make_a_commit(self):
        before = git(self.local, "rev-parse", "HEAD")
        publish_manifest(self.local)
        self.assertEqual(before, git(self.local, "rev-parse", "HEAD"))

    def test_retry_can_publish_manifest_commit_left_by_a_failed_push(self):
        expected = {"charts": [{"hour": 120}]}
        (self.local / MANIFEST).write_text(json.dumps(expected), encoding="utf-8")
        real_run = subprocess.run

        def reject_push(command, **kwargs):
            if "push" in command:
                return subprocess.CompletedProcess(command, 1, "", "simulated network failure")
            return real_run(command, **kwargs)

        with patch("public_charts_publish.subprocess.run", side_effect=reject_push), patch("public_charts_publish.time.sleep"):
            with self.assertRaises(PublicationError):
                publish_manifest(self.local)
        self.assertEqual(self.remote_manifest(), {"charts": []})
        self.assertEqual(publish_manifest(self.local), expected)
        self.assertEqual(self.remote_manifest(), expected)

    def test_unrelated_unpublished_commit_is_not_pushed(self):
        expected = {"charts": [{"hour": 240}]}
        (self.local / MANIFEST).write_text(json.dumps(expected), encoding="utf-8")
        git(self.local, "add", MANIFEST)
        git(self.local, "commit", "-m", "Manifest")
        (self.local / "private.txt").write_text("Do not publish", encoding="utf-8")
        git(self.local, "add", "private.txt")
        git(self.local, "commit", "-m", "Unrelated")
        expected["charts"].append({"hour": 120})
        (self.local / MANIFEST).write_text(json.dumps(expected), encoding="utf-8")
        git(self.local, "add", MANIFEST)
        git(self.local, "commit", "-m", "Another manifest after unrelated changes")
        with self.assertRaises(PublicationError):
            publish_manifest(self.local)
        self.assertEqual(self.remote_manifest(), {"charts": []})

    def test_git_error_reports_stage_without_leaking_raw_output(self):
        real_run = subprocess.run
        def reject_fetch(command, **kwargs):
            if "fetch" in command:
                raise subprocess.CalledProcessError(128, command, stderr="Authentication failed: private-token-value")
            return real_run(command, **kwargs)
        with patch("public_charts_publish.subprocess.run", side_effect=reject_fetch):
            with self.assertRaisesRegex(PublicationError, "fetch failed.*authentication") as failure:
                publish_manifest(self.local)
        self.assertNotIn("private-token-value", str(failure.exception))

    def test_shallow_runner_can_publish_when_code_advances_during_render(self):
        (self.local / "baseline.py").write_text("baseline = 1\n", encoding="utf-8")
        git(self.local, "add", "baseline.py")
        git(self.local, "commit", "-m", "Existing renderer code")
        git(self.local, "push", "origin", "main")
        shallow = self.local.parent / "shallow-runner"
        git(self.local.parent, "clone", "--depth=1", self.remote.as_uri(), str(shallow))
        self.assertEqual(git(shallow, "rev-parse", "--is-shallow-repository").strip(), "true")
        (self.local / "code.py").write_text("value = 2\n", encoding="utf-8")
        git(self.local, "add", "code.py")
        git(self.local, "commit", "-m", "Code update while charts render")
        git(self.local, "push", "origin", "main")
        expected = {"charts": [{"hour": 72}]}
        (shallow / MANIFEST).write_text(json.dumps(expected), encoding="utf-8")
        git(shallow, "config", "core.autocrlf", "true")
        with patch("public_charts_publish.time.sleep"), patch.dict("os.environ", {
                "GIT_CONFIG_GLOBAL": str(self.local.parent / "no-global.gitconfig"),
                "GIT_CONFIG_NOSYSTEM": "1"}):
            self.assertEqual(publish_manifest(shallow), expected)
        self.assertEqual(self.remote_manifest(), expected)
        self.assertEqual(git(self.remote, "show", "main:code.py").strip(), "value = 2")


if __name__ == "__main__":
    unittest.main()
