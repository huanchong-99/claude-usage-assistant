"""macOS integration checks: run with python -B -m unittest discover -s tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import quota_card as card


@unittest.skipUnless(sys.platform == "darwin", "native macOS launcher")
class NativeLauncherTests(unittest.TestCase):
    def test_native_process_keeps_bundle_and_venv(self):
        with tempfile.TemporaryDirectory(prefix="usage-native-test-") as tmp:
            app = Path(tmp) / "含空格 Test.app"
            script = Path(tmp) / "路径 with spaces.py"
            script.write_text(
                "import json,os,sys,requests,tkinter as tk\nfrom Foundation import NSBundle\n"
                "root=tk.Tk();root.withdraw();root.after(20,root.destroy);root.mainloop()\n"
                "print(json.dumps(dict(exe=sys.executable,prefix=sys.prefix,"
                "bundle=str(NSBundle.mainBundle().bundleIdentifier()),"
                "argv=sys.argv[1:],value=os.environ.get('CODEX_HOME'))))\n")
            real_run = subprocess.run

            def build_only(args, **kwargs):
                if str(args[0]).endswith("/lsregister"):
                    return subprocess.CompletedProcess(args, 0)
                return real_run(args, **kwargs)

            with (patch.object(card, "_mac_app_path", return_value=app),
                  patch.object(card, "_mac_app_paths", return_value=[app]),
                  patch.object(card, "LAUNCH_AGENT_LABEL", "io.github.ai-usage-card.native-test"),
                  patch.object(card, "_python_argv", return_value=[sys.executable, str(script)]),
                  patch.object(card, "autostart_enabled", return_value=False),
                  patch.object(card, "_passthrough_env", return_value={"CODEX_HOME": "saved value"}),
                  patch("subprocess.run", side_effect=build_only),
                  contextlib.redirect_stderr(io.StringIO())):
                self.assertEqual(card.install_app(), app)
                native = card._installed_mac_launcher()
                self.assertEqual(card._launch_argv(), [str(native)])
                env = {**os.environ, "CODEX_HOME": "current value", "PYTHONDONTWRITEBYTECODE": "1"}
                result = real_run([str(native), "argument with spaces", "中文"], env=env,
                                  capture_output=True, text=True, check=True, timeout=30)
                data = json.loads(result.stdout)
                self.assertEqual(data["exe"], sys.executable)
                self.assertEqual(data["prefix"], sys.prefix)
                self.assertEqual(data["bundle"], card.LAUNCH_AGENT_LABEL + ".launcher")
                self.assertEqual(data["argv"], ["argument with spaces", "中文"])
                self.assertEqual(data["value"], "current value")
                # Installation failure must preserve the already working app.
                before = (native.read_bytes(), (app / "Contents/Info.plist").read_bytes())
                def compiler_failure(args, **kwargs):
                    if str(args[0]).endswith("/clang"):
                        raise subprocess.CalledProcessError(1, args)
                    return build_only(args, **kwargs)
                with patch("subprocess.run", side_effect=compiler_failure):
                    self.assertIsNone(card.install_app())
                self.assertEqual(before, (native.read_bytes(), (app / "Contents/Info.plist").read_bytes()))
                # Reject a stale interpreter mapping instead of hijacking another venv.
                info_path = app / "Contents/Info.plist"
                info = plistlib.loads(info_path.read_bytes())
                info["AIUsagePython"] = "/missing/python"
                info_path.write_bytes(plistlib.dumps(info))
                self.assertIsNone(card._installed_mac_launcher())
                failed = real_run([str(native), "--check"], capture_output=True, timeout=10)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("Python", failed.stderr.decode())
                self.assertEqual(list(Path(tmp).glob(".ai-usage-install-*")), [])

    def test_uninstall_restores_python_autostart_and_keeps_unrelated_app(self):
        with tempfile.TemporaryDirectory(prefix="usage-uninstall-test-") as tmp:
            app, other = Path(tmp) / "Owned.app", Path(tmp) / "Unrelated.app"
            exe = app / "Contents/MacOS/launcher"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
            python, script = card._python_argv()
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": card.LAUNCH_AGENT_LABEL + ".launcher",
                "AIUsageNativeLauncher": 1, "AIUsagePython": python,
                "AIUsageScript": script, "AIUsagePythonLibrary": str(card._mac_python_library())}))
            (other / "Contents").mkdir(parents=True)
            info = other / "Contents/Info.plist"
            original = plistlib.dumps({"CFBundleIdentifier": "org.example.unrelated"})
            info.write_bytes(original)
            launch_agent = Path(tmp) / "test.plist"
            with (patch.object(card, "_mac_app_paths", return_value=[app, other]),
                  patch.object(card, "_autostart_file", return_value=launch_agent)):
                self.assertTrue(card.set_autostart(True))
                self.assertEqual(plistlib.loads(launch_agent.read_bytes())["ProgramArguments"], [str(exe)])
                self.assertTrue(card.uninstall_app())
                self.assertFalse(app.exists())
                self.assertEqual(info.read_bytes(), original)
                restored = plistlib.loads(launch_agent.read_bytes())
                self.assertEqual(restored["ProgramArguments"], [python, script])
                self.assertTrue(restored["RunAtLoad"])
                self.assertTrue(card.set_autostart(False))
                self.assertFalse(launch_agent.exists())


if __name__ == "__main__":
    unittest.main()
