import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import phase2_authenticate


class TestPhase2(unittest.TestCase):
    """Tests for phase2_authenticate.py authentication logic."""

    def setUp(self):
        self.original_env = os.environ.copy()
        self.disk_patch = patch("phase2_authenticate.verify_prephase_disk_space", return_value=True)
        self.disk_patch.start()

    def tearDown(self):
        self.disk_patch.stop()
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_no_automatic_browser_bypasses_playwright(self):
        """When NO_AUTOMATIC_BROWSER_OVERRIDE is True, playwright_login should exit immediately without launching browser."""
        os.environ["NO_AUTOMATIC_BROWSER_OVERRIDE"] = "True"
        result = phase2_authenticate.playwright_login("https://accounts.google.com/o/oauth2/auth?test=1")
        self.assertEqual(result, "")

    @patch("phase2_authenticate.sync_playwright", create=True)
    def test_playwright_login_invalidates_cache(self, mock_playwright):
        """playwright_login should call importlib.invalidate_caches() to detect dynamically installed wheels."""
        os.environ.pop("NO_AUTOMATIC_BROWSER_OVERRIDE", None)
        with patch("importlib.invalidate_caches") as mock_invalidate:
            # Force ImportError to simulate Playwright missing or exiting cleanly
            with patch.dict("sys.modules", {"playwright.sync_api": None}):
                phase2_authenticate.playwright_login("https://accounts.google.com/test")
                mock_invalidate.assert_called_once()

    @patch("subprocess.run")
    def test_check_gcloud_auth_status_detected(self, mock_run):
        """Should detect active authenticated account from gcloud auth list."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "dev@example.com\n"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        is_authenticated = phase2_authenticate.check_gcloud_auth_status()
        self.assertTrue(is_authenticated)

    @patch("subprocess.run")
    def test_check_gcloud_auth_status_none(self, mock_run):
        """Should return False if gcloud auth list returns empty."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        is_authenticated = phase2_authenticate.check_gcloud_auth_status()
        self.assertFalse(is_authenticated)

    @patch("phase2_authenticate.orchestrate_gcloud_headless_flow", return_value=True)
    @patch("phase2_authenticate.run_gcloud_adc_auth", return_value=True)
    @patch("subprocess.run")
    def test_force_auth_preserves_existing_account(self, mock_run, mock_adc, mock_headless):
        """--force-auth must preserve previously detected account and inject --account flag."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "dev@example.com\n"
        mock_run.return_value = mock_proc

        success = phase2_authenticate.run_gcloud_auth(force_auth=True, force_auth_with_reset=False)
        self.assertTrue(success)

        called_cmd = mock_headless.call_args[0][0]
        self.assertIn("--account=dev@example.com", called_cmd)

    @patch("phase2_authenticate.orchestrate_gcloud_headless_flow", return_value=True)
    @patch("phase2_authenticate.run_gcloud_adc_auth", return_value=True)
    @patch("subprocess.run")
    def test_force_auth_with_reset_omits_account(self, mock_run, mock_adc, mock_headless):
        """--force-auth-with-reset must NOT inject --account flag, enforcing clean re-entry."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "dev@example.com\n"
        mock_run.return_value = mock_proc

        success = phase2_authenticate.run_gcloud_auth(force_auth=False, force_auth_with_reset=True)
        self.assertTrue(success)

        called_cmd = mock_headless.call_args[0][0]
        self.assertNotIn("--account=dev@example.com", called_cmd)

    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    @patch("phase2_authenticate.orchestrate_gcloud_headless_flow", return_value=True)
    def test_force_auth_with_reset_deletes_adc_cache(self, mock_headless, mock_remove, mock_exists):
        """--force-auth-with-reset must actively delete stale ADC credentials file."""
        success = phase2_authenticate.run_gcloud_adc_auth(force_auth_with_reset=True)
        self.assertTrue(success)
        mock_remove.assert_called_once()

    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    @patch("phase2_authenticate.orchestrate_gcloud_headless_flow", return_value=True)
    def test_force_auth_preserves_adc_file_on_disk(self, mock_headless, mock_remove, mock_exists):
        """--force-auth must NOT delete ADC credentials file before authentication."""
        success = phase2_authenticate.run_gcloud_adc_auth(force_auth=True, force_auth_with_reset=False)
        self.assertTrue(success)
        mock_remove.assert_not_called()

    @patch("shutil.rmtree")
    @patch("os.path.exists", return_value=True)
    @patch("phase2_authenticate.sync_playwright", create=True)
    def test_playwright_login_purges_profile_on_reset(self, mock_playwright, mock_exists, mock_rmtree):
        """playwright_login must purge persistent profile cache when force_auth_with_reset is True."""
        os.environ.pop("NO_AUTOMATIC_BROWSER_OVERRIDE", None)
        with patch.dict("sys.modules", {"playwright.sync_api": None}):
            phase2_authenticate.playwright_login("https://accounts.google.com/test", force_auth_with_reset=True)
            mock_rmtree.assert_called_once()

    @patch("shutil.rmtree")
    @patch("os.path.exists", return_value=True)
    @patch("phase2_authenticate.sync_playwright", create=True)
    def test_playwright_login_preserves_profile_on_force_auth(self, mock_playwright, mock_exists, mock_rmtree):
        """playwright_login must NOT purge persistent profile cache when force_auth_with_reset is False."""
        os.environ.pop("NO_AUTOMATIC_BROWSER_OVERRIDE", None)
        with patch.dict("sys.modules", {"playwright.sync_api": None}):
            phase2_authenticate.playwright_login("https://accounts.google.com/test", force_auth_with_reset=False)
            mock_rmtree.assert_not_called()

    @patch("phase2_authenticate.playwright_login", return_value="test-code")
    @patch("subprocess.Popen")
    @patch.dict(os.environ, {"DRY_RUN_MODE": "0"})
    def test_orchestrate_gcloud_strips_ansi_from_url(self, mock_popen, mock_login):
        """orchestrate_gcloud_headless_flow must strip ANSI escape codes and trailing characters from OAuth URL."""
        mock_proc = MagicMock()
        # Simulate gcloud output containing ANSI color codes
        mock_proc.stdout.readline.side_effect = [
            "\x1b[32mGo to the following link:\x1b[0m\n",
            "    https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=123.apps.googleusercontent.com&code_challenge_method=S256\x1b[0m\n",
            ""
        ]
        mock_proc.communicate.return_value = ("Success", "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        success = phase2_authenticate.orchestrate_gcloud_headless_flow(["gcloud", "auth", "login"])
        self.assertTrue(success)

        called_url = mock_login.call_args[0][0]
        self.assertNotIn("\x1b", called_url)
        self.assertEqual(called_url, "https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=123.apps.googleusercontent.com&code_challenge_method=S256")

    def test_extract_oauth_code_adc_url(self):
        """extract_oauth_code_from_url must extract code from ADC applicationdefaultauthcode.html URL."""
        adc_url = "https://sdk.cloud.google.com/applicationdefaultauthcode.html?code=4/0Acv1234567890abcdef&state=xyz"
        code = phase2_authenticate.extract_oauth_code_from_url(adc_url)
        self.assertEqual(code, "4/0Acv1234567890abcdef")

    def test_extract_oauth_code_standard_url(self):
        """extract_oauth_code_from_url must extract code from standard authcode.html URL."""
        std_url = "https://sdk.cloud.google.com/authcode.html?code=4/0Acv9876543210fedcba&state=abc"
        code = phase2_authenticate.extract_oauth_code_from_url(std_url)
        self.assertEqual(code, "4/0Acv9876543210fedcba")



    @patch.dict(os.environ, {"DRY_RUN_MODE": "0"})
    def test_synchronize_gemini_cli_auth_oauth(self):
        """synchronize_gemini_cli_auth must persist oauth-personal into settings.json."""
        import tempfile, json, os, shutil
        import phase2_authenticate
        
        with tempfile.TemporaryDirectory() as tmp_home:
            gemini_dir = os.path.join(tmp_home, ".gemini")
            os.makedirs(gemini_dir, exist_ok=True)
            settings_path = os.path.join(gemini_dir, "settings.json")
            
            with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", tmp_home)):
                res = phase2_authenticate.synchronize_gemini_cli_auth("oauth-personal")
                self.assertTrue(res)
                
                with open(settings_path) as f:
                    data = json.load(f)
                self.assertEqual(data["security"]["auth"]["selectedType"], "oauth-personal")

    @patch.dict(os.environ, {"DRY_RUN_MODE": "0"})
    def test_synchronize_gemini_cli_auth_api_key(self):
        """synchronize_gemini_cli_auth must persist gemini-api-key and write to global .env."""
        import tempfile, json, os
        import phase2_authenticate
        
        with tempfile.TemporaryDirectory() as tmp_home:
            gemini_dir = os.path.join(tmp_home, ".gemini")
            os.makedirs(gemini_dir, exist_ok=True)
            settings_path = os.path.join(gemini_dir, "settings.json")
            env_path = os.path.join(gemini_dir, ".env")
            
            with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", tmp_home)):
                res = phase2_authenticate.synchronize_gemini_cli_auth(
                    "gemini-api-key",
                    env_vars={"GEMINI_API_KEY": "AIzaSyTestKey123"}
                )
                self.assertTrue(res)
                
                with open(settings_path) as f:
                    data = json.load(f)
                self.assertEqual(data["security"]["auth"]["selectedType"], "gemini-api-key")
                
                with open(env_path) as f:
                    env_content = f.read()
                self.assertIn("GEMINI_API_KEY=AIzaSyTestKey123", env_content)

    @patch.dict(os.environ, {"DRY_RUN_MODE": "0"})
    def test_synchronize_gemini_cli_auth_service_account(self):
        """synchronize_gemini_cli_auth must map key file to standard ADC location."""
        import tempfile, json, os
        import phase2_authenticate
        
        with tempfile.TemporaryDirectory() as tmp_home:
            gemini_dir = os.path.join(tmp_home, ".gemini")
            os.makedirs(gemini_dir, exist_ok=True)
            settings_path = os.path.join(gemini_dir, "settings.json")
            
            key_file = os.path.join(tmp_home, "fake_sa_key.json")
            with open(key_file, "w") as f:
                f.write('{"type": "service_account", "project_id": "test"}')
                
            with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", tmp_home)):
                res = phase2_authenticate.synchronize_gemini_cli_auth(
                    "vertex-ai",
                    env_vars={"GOOGLE_APPLICATION_CREDENTIALS": key_file},
                    adc_source_file=key_file
                )
                self.assertTrue(res)
                
                with open(settings_path) as f:
                    data = json.load(f)
                self.assertEqual(data["security"]["auth"]["selectedType"], "vertex-ai")
                
                adc_dest = os.path.join(tmp_home, ".config", "gcloud", "application_default_credentials.json")
                self.assertTrue(os.path.exists(adc_dest))

    @patch("subprocess.Popen")
    @patch("phase2_authenticate.playwright_login")
    def test_adc_login_succeeds_when_credentials_file_present_despite_nonzero_exit(self, mock_playwright, mock_popen):
        """orchestrate_gcloud_headless_flow must succeed if ADC file exists and is valid, even if gcloud exits code 1."""
        import phase2_authenticate
        mock_playwright.return_value = "4/0MockCode"
        
        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = [
            "Go to: https://accounts.google.com/o/oauth2/auth?code_challenge=xyz\n",
            ""
        ]
        mock_proc.communicate.return_value = ("Credentials saved to file: application_default_credentials.json", "SERVICE_DISABLED")
        mock_proc.returncode = 1  # Simulated exit code 1 due to post-auth quota project warning!
        mock_popen.return_value = mock_proc

        import tempfile, json
        with tempfile.TemporaryDirectory() as tmp_home:
            adc_dir = os.path.join(tmp_home, ".config", "gcloud")
            os.makedirs(adc_dir, exist_ok=True)
            adc_file = os.path.join(adc_dir, "application_default_credentials.json")
            with open(adc_file, "w") as f:
                json.dump({"type": "authorized_user", "refresh_token": "1//mock_token"}, f)
                
            with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", tmp_home)):
                res = phase2_authenticate.orchestrate_gcloud_headless_flow(
                    ["gcloud", "auth", "application-default", "login", "--no-launch-browser", "--disable-quota-project"]
                )
                self.assertTrue(res)

if __name__ == "__main__":
    unittest.main()
