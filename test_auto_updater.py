import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_updater


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class AutoUpdaterTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        self.source = root / "source"
        self.work = root / "work"

        git(root, "init", "--bare", str(self.remote))
        git(root, "init", "-b", "main", str(self.source))
        git(self.source, "config", "user.name", "Updater Test")
        git(self.source, "config", "user.email", "updater@example.invalid")
        (self.source / "version.txt").write_text("v1", encoding="utf-8")
        git(self.source, "add", "version.txt")
        git(self.source, "commit", "-m", "v1")
        git(self.source, "remote", "add", "origin", str(self.remote))
        git(self.source, "push", "-u", "origin", "main")
        git(root, "clone", "--branch", "main", str(self.remote), str(self.work))

    def push_version(self, version):
        (self.source / "version.txt").write_text(version, encoding="utf-8")
        git(self.source, "add", "version.txt")
        git(self.source, "commit", "-m", version)
        git(self.source, "push", "origin", "main")

    def test_up_to_date_checkout_starts_without_changes(self):
        result = auto_updater.check_and_update(self.work)

        self.assertEqual(result, auto_updater.UP_TO_DATE)
        self.assertEqual((self.work / "version.txt").read_text(encoding="utf-8"), "v1")

    def test_fast_forward_update_is_applied(self):
        self.push_version("v2")

        result = auto_updater.check_and_update(self.work)

        self.assertEqual(result, auto_updater.UPDATED)
        self.assertEqual((self.work / "version.txt").read_text(encoding="utf-8"), "v2")
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), git(self.source, "rev-parse", "HEAD"))

    def test_tracked_local_change_is_never_overwritten(self):
        self.push_version("v2")
        (self.work / "version.txt").write_text("manual", encoding="utf-8")

        result = auto_updater.check_and_update(self.work)

        self.assertEqual(result, auto_updater.SKIPPED)
        self.assertEqual((self.work / "version.txt").read_text(encoding="utf-8"), "manual")

    def test_can_be_disabled_by_environment(self):
        self.push_version("v2")

        with patch.dict(os.environ, {"BOT_AUTO_UPDATE": "0"}):
            result = auto_updater.check_and_update(self.work)

        self.assertEqual(result, auto_updater.SKIPPED)
        self.assertEqual((self.work / "version.txt").read_text(encoding="utf-8"), "v1")

    def test_github_token_is_not_exposed_in_process_arguments(self):
        token = "secret-read-only-token"
        completed = subprocess.CompletedProcess([], 0, "", "")

        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), patch(
            "auto_updater.subprocess.run", return_value=completed
        ) as run_mock:
            auto_updater._git(self.work, "status")

        command = run_mock.call_args.args[0]
        environment = run_mock.call_args.kwargs["env"]
        self.assertNotIn(token, " ".join(command))
        self.assertIn("AUTHORIZATION: basic ", environment["GIT_CONFIG_VALUE_0"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
