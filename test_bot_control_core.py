import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot_control_core as control


class BotControlCoreTests(unittest.TestCase):
    def test_recognizes_only_python_main_process(self):
        self.assertTrue(
            control.is_expected_bot_process(
                {
                    "Name": "pythonw.exe",
                    "CommandLine": '"C:\\bot\\pythonw.exe" -u main.py',
                }
            )
        )
        self.assertFalse(
            control.is_expected_bot_process(
                {"Name": "nginx.exe", "CommandLine": "nginx main.py"}
            )
        )
        self.assertFalse(
            control.is_expected_bot_process(
                {"Name": "python.exe", "CommandLine": "python worker.py"}
            )
        )

    @patch("bot_control_core._run_hidden")
    @patch("bot_control_core.process_info")
    @patch("bot_control_core.listener_pid", return_value=71)
    def test_stop_refuses_to_kill_foreign_listener(self, _pid, info, run):
        info.return_value = {"Name": "other.exe", "CommandLine": "other.exe"}

        with self.assertRaises(control.ControlError):
            control.stop_bot()

        run.assert_not_called()

    @patch("bot_control_core.wait_for_port", return_value=True)
    @patch("bot_control_core._run_hidden")
    @patch("bot_control_core.process_info")
    @patch("bot_control_core.listener_pid", return_value=72)
    def test_stop_kills_only_verified_bot(self, _pid, info, run, _wait):
        info.return_value = {
            "Name": "pythonw.exe",
            "CommandLine": "pythonw.exe -u main.py",
        }
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        result = control.stop_bot()

        self.assertIn("72", result)
        run.assert_called_once_with(["taskkill.exe", "/PID", "72", "/T", "/F"])

    @patch("bot_control_core.process_info")
    @patch("bot_control_core.listener_pid", return_value=73)
    def test_start_returns_when_bot_is_already_running(self, _pid, info):
        info.return_value = {
            "Name": "python.exe",
            "CommandLine": "python.exe main.py",
        }

        result = control.start_bot("unused")

        self.assertIn("уже запущен", result)

    @patch("bot_control_core.wait_for_port", return_value=True)
    @patch("bot_control_core.listener_pid", side_effect=[None, 74])
    @patch("bot_control_core.subprocess.Popen")
    def test_start_uses_pythonw_without_console(self, popen, _pid, _wait):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            pythonw = project / ".venv" / "Scripts" / "pythonw.exe"
            pythonw.parent.mkdir(parents=True)
            pythonw.touch()
            (project / "main.py").touch()

            result = control.start_bot(project)

        self.assertIn("74", result)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(pythonw), "-u", "main.py"])
        self.assertEqual(kwargs["cwd"], str(project.resolve()))
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(
            kwargs["creationflags"],
            control.DETACHED_PROCESS | control.CREATE_NEW_PROCESS_GROUP,
        )


if __name__ == "__main__":
    unittest.main()
