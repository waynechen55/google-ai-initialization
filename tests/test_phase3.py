import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import phase3_provision


class TestPhase3(unittest.TestCase):
    """Tests for phase3_provision.py Google Cloud configuration logic."""

    def setUp(self):
        self.original_env = os.environ.copy()
        self.disk_patch = patch("phase3_provision.verify_prephase_disk_space", return_value=True)
        self.disk_patch.start()

    def tearDown(self):
        self.disk_patch.stop()
        os.environ.clear()
        os.environ.update(self.original_env)

    @patch("phase3_provision.run_command")
    def test_set_default_project(self, mock_run):
        """Should execute 'gcloud config set project'."""
        mock_run.return_value = (0, "Updated project [test-proj].", "")
        success = phase3_provision.set_default_project("test-proj")
        self.assertTrue(success)
        mock_run.assert_called_with([phase3_provision.GCLOUD_BIN, "config", "set", "project", "test-proj"])

    @patch("phase3_provision.run_command")
    def test_set_quota_project(self, mock_run):
        """Should execute 'gcloud auth application-default set-quota-project'."""
        mock_run.return_value = (0, "Credentials updated.", "")
        success = phase3_provision.set_quota_project("test-proj")
        self.assertTrue(success)
        mock_run.assert_called_with([phase3_provision.GCLOUD_BIN, "auth", "application-default", "set-quota-project", "test-proj"])

    @patch("phase3_provision.run_command")
    def test_align_active_gcloud_account(self, mock_run):
        """Should detect active account and set it in gcloud config."""
        mock_run.side_effect = [
            (0, "dev@example.com", ""),  # auth list
            (0, "Updated property [core/account].", "")  # config set account
        ]
        success = phase3_provision.align_active_gcloud_account()
        self.assertTrue(success)

    @patch("phase3_provision.align_active_gcloud_account", return_value=True)
    @patch("phase3_provision.set_quota_project", return_value=True)
    @patch("phase3_provision.set_default_project", return_value=True)
    @patch("phase3_provision.set_billing_quota_project", return_value=True)
    @patch("phase3_provision.enable_service_with_retry")
    @patch("phase3_provision.verify_enabled_services")
    @patch("phase3_provision.write_env_file")
    def test_permissive_mode_by_default(self, mock_write_env, mock_verify, mock_enable, mock_set_billing, mock_set_default, mock_set_quota, mock_align):
        """When no_allow_service_disabled is False (default), service enablement failures should be tolerated warningly."""
        # Simulate mandatory service failure
        mock_enable.side_effect = [False, True, True]  # Vertex AI fails
        mock_verify.return_value = False  # Verification scan warns

        with patch("phase3_provision.verify_vertex_ai_access", return_value=True):
            with patch("phase3_provision.verify_python_sdk_access", return_value=True):
                with patch("phase3_provision.verify_antigravity_agent_access", return_value=True):
                    # Should return True because permissive mode is the default!
                    result = phase3_provision.run_gcloud_config(
                        project_id="test-proj",
                        location="global",
                        no_allow_service_disabled=False
                    )
                    self.assertTrue(result)

    @patch("phase3_provision.align_active_gcloud_account", return_value=True)
    @patch("phase3_provision.set_quota_project", return_value=True)
    @patch("phase3_provision.set_default_project", return_value=True)
    @patch("phase3_provision.set_billing_quota_project", return_value=True)
    @patch("phase3_provision.enable_service_with_retry")
    @patch("phase3_provision.write_env_file")
    def test_strict_mode_forces_failure(self, mock_write_env, mock_enable, mock_set_billing, mock_set_default, mock_set_quota, mock_align):
        """When no_allow_service_disabled is True, mandatory service enablement failures must abort strictly with False."""
        mock_enable.side_effect = [False, True, True]  # Mandatory Vertex AI fails

        result = phase3_provision.run_gcloud_config(
            project_id="test-proj",
            location="global",
            no_allow_service_disabled=True
        )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
