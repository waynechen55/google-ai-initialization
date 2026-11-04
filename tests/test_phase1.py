import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import phase1_install


class TestPhase1(unittest.TestCase):
    """Tests for phase1_install.py installer logic."""

    def test_setup_gemini_workspace(self):
        """Should create .gemini/ directory and correctly symlink agents and skills."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("phase1_install.WORKSPACE_ROOT", tmpdir):
                # Run the workspace setup
                phase1_install.setup_gemini_workspace()

                # Verify .gemini folder was created
                gemini_dir = os.path.join(tmpdir, ".gemini")
                self.assertTrue(os.path.exists(gemini_dir))

                # Verify agents and skills links/dirs exist in .gemini
                agents_link = os.path.join(gemini_dir, "agents")
                skills_link = os.path.join(gemini_dir, "skills")
                self.assertTrue(os.path.exists(agents_link))
                self.assertTrue(os.path.exists(skills_link))

    @patch("shutil.which")
    def test_check_system_browsers_detects_chrome(self, mock_which):
        """Should detect when a system browser is available."""
        mock_which.side_effect = lambda cmd: "/usr/bin/google-chrome" if cmd == "google-chrome" else None
        # Should execute cleanly without error
        phase1_install.check_system_browsers()

    @patch("subprocess.Popen")
    def test_run_command_success(self, mock_popen):
        """run_command should execute successfully and return without error on exit code 0."""
        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = ["line 1\n", ""]
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # Should complete without raising
        phase1_install.run_command(["echo", "hello"])

    @patch("subprocess.Popen")
    def test_run_command_failure_raises(self, mock_popen):
        """run_command should raise an exception on non-zero returncode."""
        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = ["error message\n", ""]
        mock_proc.poll.return_value = 1
        mock_proc.wait.return_value = 1
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        with self.assertRaises(Exception):
            phase1_install.run_command(["false"])


    def test_required_pip_packages_includes_uv(self):
        """REQUIRED_PIP_PACKAGES must include 'uv' to ensure uvx runtime is installed."""
        self.assertIn("uv", phase1_install.REQUIRED_PIP_PACKAGES)

    def test_required_pip_packages_completeness(self):
        """REQUIRED_PIP_PACKAGES must include core foundational packages."""
        expected = ["python-dotenv", "requests", "pyyaml", "packaging", "google-genai", "playwright"]
        for pkg in expected:
            self.assertIn(pkg, phase1_install.REQUIRED_PIP_PACKAGES, f"Missing {pkg} in REQUIRED_PIP_PACKAGES")

    @patch("phase1_install.run_command", return_value=0)
    @patch("phase1_install.check_system_browsers")
    def test_install_playwright_browsers_with_deps(self, mock_browsers, mock_run):
        """install_playwright_browsers must invoke playwright install with --with-deps flag."""
        phase1_install.install_playwright_browsers()
        called_cmds = [call_args[0][0] for call_args in mock_run.call_args_list]
        self.assertTrue(any("--with-deps" in cmd for cmd in called_cmds))

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/node")
    def test_check_node_version_parses_major(self, mock_which, mock_run):
        """check_node_version must accurately parse major version numbers."""
        mock_proc = MagicMock()
        mock_proc.stdout = "v20.12.2\n"
        mock_run.return_value = mock_proc
        ver = phase1_install.check_node_version()
        self.assertEqual(ver, 20)

    @patch("shutil.which", return_value="/usr/bin/tool")
    @patch("os.path.exists", return_value=True)
    def test_check_system_prerequisites_passes_when_installed(self, mock_exists, mock_which):
        """check_system_prerequisites should pass without error when tools and CA bundle exist."""
        phase1_install.check_system_prerequisites()

    @patch("shutil.which")
    @patch("phase1_install.update_env_file")
    def test_verify_and_save_bin_paths_resolves_uvx(self, mock_update_env, mock_which):
        """verify_and_save_bin_paths should detect and save uvx runner if present."""
        mock_which.side_effect = lambda cmd: {
            "gcloud": "/usr/bin/gcloud",
            "gemini": "/usr/bin/gemini",
            "uvx": "/usr/bin/uvx"
        }.get(cmd)

        success = phase1_install.verify_and_save_bin_paths()
        self.assertTrue(success)
        mock_update_env.assert_any_call("UVX_BIN_PATH", "/usr/bin/uvx")


if __name__ == "__main__":
    unittest.main()
