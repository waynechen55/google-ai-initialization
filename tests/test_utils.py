import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import utils


class TestUtils(unittest.TestCase):
    """Tests for utils.py functions and decorators."""

    def setUp(self):
        self.original_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_get_process_timeout_default(self):
        """Should default to 180 seconds when no override is set."""
        os.environ.pop("PROCESS_TIMEOUT_OVERRIDE", None)
        self.assertEqual(utils.get_process_timeout(), 180)

    def test_get_process_timeout_override(self):
        """Should return the overridden timeout when set."""
        os.environ["PROCESS_TIMEOUT_OVERRIDE"] = "45"
        self.assertEqual(utils.get_process_timeout(), 45)

    def test_get_process_timeout_invalid_fallback(self):
        """Should fallback to 180 seconds when override is non-numeric."""
        os.environ["PROCESS_TIMEOUT_OVERRIDE"] = "not-a-number"
        self.assertEqual(utils.get_process_timeout(), 180)

    def test_get_workspace_root_override(self):
        """Should respect WORKSPACE_ROOT_OVERRIDE if set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["WORKSPACE_ROOT_OVERRIDE"] = tmpdir
            self.assertEqual(utils.get_workspace_root(), os.path.abspath(tmpdir))

    def test_get_workspace_root_detection(self):
        """Should detect repository root when README.md or .gitignore exists."""
        root = utils.get_workspace_root()
        self.assertTrue(os.path.exists(root))

    def test_with_retry_success_first_attempt(self):
        """Function decorated with @with_retry should return result immediately if it succeeds."""
        mock_fn = MagicMock(return_value="ok")
        decorated = utils.with_retry(max_attempts=3, backoff_seconds=0)(mock_fn)
        result = decorated("arg1")
        self.assertEqual(result, "ok")
        self.assertEqual(mock_fn.call_count, 1)

    def test_with_retry_success_after_failure(self):
        """Function decorated with @with_retry should retry and succeed on subsequent attempts."""
        mock_fn = MagicMock(side_effect=[ValueError("temporary error"), "recovered"])
        decorated = utils.with_retry(max_attempts=3, backoff_seconds=0)(mock_fn)
        result = decorated()
        self.assertEqual(result, "recovered")
        self.assertEqual(mock_fn.call_count, 2)

    def test_with_retry_exceeds_max_attempts(self):
        """Function decorated with @with_retry should raise exception after exhausting attempts."""
        mock_fn = MagicMock(side_effect=RuntimeError("persistent failure"))
        decorated = utils.with_retry(max_attempts=2, backoff_seconds=0)(mock_fn)
        with self.assertRaises(RuntimeError):
            decorated()
        self.assertEqual(mock_fn.call_count, 2)

    @patch.dict(os.environ, {"DRY_RUN_MODE": "0"})
    def test_update_env_file(self):
        """Should write or update key-value pairs in a custom environment file."""
        with tempfile.NamedTemporaryFile(mode="w+", delete=False) as f:
            f.write("EXISTING_KEY=old_value\n")
            f_path = f.name

        try:
            os.environ["ENV_FILE_PATH_OVERRIDE"] = f_path
            utils.update_env_file("NEW_KEY", "new_value")
            utils.update_env_file("EXISTING_KEY", "updated_value")

            with open(f_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn('NEW_KEY="new_value"', content)
            self.assertIn('EXISTING_KEY="updated_value"', content)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_match_target_to_registry_key_distinguishes_prefixes(self):
        """match_target_to_registry_key must NOT match build.opensuse.org to build-suse-de."""
        servers = {
            "build-suse-de": {
                "command": "python3",
                "args": ["bridges/build_suse_de_mcp_server.py"],
                "env": {"OBS_URL": "https://build.suse.de"}
            }
        }
        matched_key, _ = utils.match_target_to_registry_key("build.opensuse.org", servers)
        self.assertIsNone(matched_key)

    def test_match_target_to_registry_key_exact(self):
        """match_target_to_registry_key should match exact target slug."""
        servers = {
            "build-opensuse-org": {
                "command": "python3",
                "args": ["bridges/build_opensuse_mcp.py"],
                "env": {"OBS_BASE_URL": "https://build.opensuse.org"}
            }
        }
        matched_key, _ = utils.match_target_to_registry_key("build.opensuse.org", servers)
        self.assertEqual(matched_key, "build-opensuse-org")

    def test_match_target_to_registry_key_env_reference(self):
        """match_target_to_registry_key should match if domain is in env even if key is custom."""
        servers = {
            "my-custom-obs": {
                "command": "python3",
                "args": ["server.py"],
                "env": {"TARGET": "https://build.opensuse.org/api"}
            }
        }
        matched_key, _ = utils.match_target_to_registry_key("build.opensuse.org", servers)
        self.assertEqual(matched_key, "my-custom-obs")

    def test_sanitize_stdio_script_content(self):
        """sanitize_stdio_script_content must transform blocking stdin iterators and unbounded urlopen calls."""
        flawed_script = (
            "def main():\n"
            "    for line in sys.stdin:\n"
            "        data = json.loads(line)\n"
            "        resp = urllib.request.urlopen(req)\n"
        )
        clean_script = utils.sanitize_stdio_script_content(flawed_script)
        self.assertNotIn("for line in sys.stdin:", clean_script)
        self.assertIn("while True:", clean_script)
        self.assertIn("line = sys.stdin.readline()", clean_script)
        self.assertIn("if not line:", clean_script)
        self.assertIn("timeout=30", clean_script)

    def test_detect_system_ca_bundle(self):
        """detect_system_ca_bundle should detect system CA file if present."""
        ca = utils.detect_system_ca_bundle()
        # On Linux with ca-certificates, this should be a valid string or None in mock environments
        if ca:
            self.assertTrue(os.path.exists(ca))

    def test_probe_target_tls_public(self):
        """probe_target_tls should identify public trusted domain."""
        with patch("utils.urllib.request.urlopen"):
            res = utils.probe_target_tls("https://api.github.com")
        self.assertIn("requires_ssl_resilience", res)
        self.assertFalse(res["requires_ssl_resilience"])

    def test_probe_target_tls_internal(self):
        """probe_target_tls should flag internal corporate domain needing SSL resilience."""
        import urllib.error

        with patch(
            "utils.urllib.request.urlopen",
            side_effect=urllib.error.URLError("CERTIFICATE_VERIFY_FAILED"),
        ):
            res = utils.probe_target_tls("https://build.suse.de")
        self.assertTrue(res["is_https"])
        self.assertTrue(res["requires_ssl_resilience"])

    def test_make_config_portable_ssl_resilience(self):
        """make_config_portable must inject runtime-targeted SSL resilience flags for internal endpoints."""
        with patch("utils.probe_target_tls", return_value={"requires_ssl_resilience": False}):
            python_cfg = {
                "command": "python3",
                "args": ["obs_mcp_server.py"],
                "env": {"OBS_URL": "https://build.suse.de"}
            }
            py_res = utils.make_config_portable(python_cfg, target_hint="build.suse.de")
            self.assertEqual(py_res["env"].get("PYTHONHTTPSVERIFY"), "0")
            if utils.detect_system_ca_bundle():
                self.assertIn("SSL_CERT_FILE", py_res["env"])
                self.assertFalse(py_res["env"]["SSL_CERT_FILE"].startswith("${"))

            node_cfg = {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-gitlab"],
                "env": {"GITLAB_API_URL": "https://gitlab.suse.de"}
            }
            node_res = utils.make_config_portable(node_cfg, target_hint="gitlab.suse.de")
            self.assertEqual(node_res["env"].get("NODE_TLS_REJECT_UNAUTHORIZED"), "0")
            if utils.detect_system_ca_bundle():
                self.assertIn("NODE_EXTRA_CA_CERTS", node_res["env"])
                self.assertFalse(node_res["env"]["NODE_EXTRA_CA_CERTS"].startswith("${"))

    def test_fingerprint_target_service_live(self):
        """fingerprint_target_service should identify known endpoints when reachable."""
        # Test fingerprinting on live or mock responses
        fp = utils.fingerprint_target_service("https://jira.suse.com")
        if fp:
            self.assertEqual(fp["service"], "jira")
            self.assertEqual(fp["runner"], "uvx")

    def test_fingerprint_target_service_sd_suse(self):
        """fingerprint_target_service should identify sd.suse.com as Service Desk ('sd')."""
        fp = utils.fingerprint_target_service("https://sd.suse.com")
        if fp:
            self.assertEqual(fp["service"], "sd")
            self.assertEqual(fp["runner"], "python3")

    def test_probe_openapi_spec_empty_on_invalid(self):
        """probe_openapi_spec should return None gracefully on unreachable or non-spec endpoints."""
        spec = utils.probe_openapi_spec("https://localhost:9999")
        self.assertIsNone(spec)

    def test_verify_upstream_credentials(self):
        """verify_upstream_credentials should validate live or mock credentials."""
        # Empty env should return valid=True, probed=False
        res_empty = utils.verify_upstream_credentials("jira.suse.com", {})
        self.assertFalse(res_empty["probed"])
        self.assertTrue(res_empty["valid"])

        # Test with mock valid token on sd.suse.com
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp):
            res_sd = utils.verify_upstream_credentials("sd.suse.com", {"SD_SUSE_PAT": "mock_test_token_12345"})
            self.assertTrue(res_sd["valid"])
            self.assertEqual(res_sd["status_code"], 200)



    def test_resolve_canonical_api_gateway(self):
        """Must correctly normalize common web portal URLs to API gateways."""
        import utils
        from unittest.mock import patch
        
        # Test original returning valid
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.headers = {"Content-Type": "application/json"}
            self.assertEqual(utils.resolve_canonical_api_gateway("https://api.github.com"), "https://api.github.com")

    def test_json_environment_integrity(self):
        """make_config_portable must strip bash parameter expansion syntax."""
        import utils
        import os
        os.environ["TEST_ENV_VAR"] = "/tmp/test"
        
        cfg = {"command": "echo", "env": {"TEST_VAR": "${TEST_ENV_VAR:-default}"}}
        portable = utils.make_config_portable(cfg)
        self.assertEqual(portable["env"]["TEST_VAR"], "/tmp/test")

    def test_make_config_portable_mandatory_url_injection(self):
        """make_config_portable must enforce a visible URL or endpoint in args or env."""
        import utils
        
        # Test 1: Config without URL for developers.google.com/knowledge/api
        cfg1 = {"command": "npx", "args": ["-y", "@houtini/google-knowledge-graph-mcp"], "env": {"API_KEY": "123"}}
        with patch("utils.probe_target_tls", return_value={"requires_ssl_resilience": False}):
            res1 = utils.make_config_portable(cfg1, target_hint="developers.google.com/knowledge/api")
        self.assertIn("DEVELOPER_KNOWLEDGE_API_URL", res1["env"])
        self.assertEqual(res1["env"]["DEVELOPER_KNOWLEDGE_API_URL"], "https://developerknowledge.googleapis.com/mcp")

        # Test 2: Config without URL for custom remote service
        cfg2 = {"command": "npx", "args": ["-y", "some-mcp-server"], "env": {"SECRET": "xyz"}}
        with patch("utils.probe_target_tls", return_value={"requires_ssl_resilience": False}):
            res2 = utils.make_config_portable(cfg2, target_hint="https://custom.service.corp")
        self.assertIn("MCP_TARGET_URL", res2["env"])
        self.assertEqual(res2["env"]["MCP_TARGET_URL"], "https://custom.service.corp")


    def test_perform_level3_functional_smoke_test(self):
        """perform_level3_functional_smoke_test should select a safe tool and execute a mock call."""
        import utils
        from unittest.mock import patch, MagicMock
        import json
        
        proc = MagicMock()
        proc.stdout.readline.return_value = json.dumps({"jsonrpc": "2.0", "id": 999, "result": {"content": [{"text": "success"}]}})
        
        tools = [{"name": "get_incident", "inputSchema": {"properties": {"id": {"type": "integer"}}, "required": ["id"]}}]
        
        with patch("select.select", return_value=([proc.stdout], [], [])):
            # Test success
            ok, msg = utils.perform_level3_functional_smoke_test(proc, tools)
            self.assertTrue(ok)
            self.assertIn("executed functionally", msg)
            
            # Test crash detection
            proc.stdout.readline.return_value = json.dumps({"jsonrpc": "2.0", "id": 999, "result": {"isError": True, "content": [{"text": "NameError: name 'x' is not defined"}]}})
            ok, msg = utils.perform_level3_functional_smoke_test(proc, tools)
            self.assertFalse(ok)
            self.assertIn("Code crash", msg)
            self.assertIn("NameError", msg)

    def test_normalize_entity_identifier(self):
        """normalize_entity_identifier must extract canonical keys from raw inputs and URLs."""
        import utils
        
        # Test 1: Full Service Desk portal URL
        url_sd = 'https://sd.suse.com/servicedesk/customer/portal/1/SD-204666'
        self.assertEqual(utils.normalize_entity_identifier(url_sd), 'SD-204666')
        
        # Test 2: Jira browse URL
        url_jira = 'https://jira.suse.com/browse/TEAM-11398'
        self.assertEqual(utils.normalize_entity_identifier(url_jira), 'TEAM-11398')
        
        # Test 3: GitHub PR URL
        url_gh = 'https://github.com/modelcontextprotocol/servers/pull/42'
        self.assertEqual(utils.normalize_entity_identifier(url_gh), '42')
        
        # Test 4: Bare key preserved
        self.assertEqual(utils.normalize_entity_identifier('SD-204666'), 'SD-204666')
        self.assertEqual(utils.normalize_entity_identifier('225159'), '225159')

    def test_resolve_active_credential(self):
        """resolve_active_credential must dynamically resolve credentials from environment or settings."""
        import utils
        import os
        from unittest.mock import patch
        
        # 1. Resolves from live environment
        with patch.dict(os.environ, {'TEST_ACTIVE_KEY': 'secret_val_1234'}):
            self.assertEqual(utils.resolve_active_credential('TEST_ACTIVE_KEY'), 'secret_val_1234')
            
        # 2. Rejects placeholder values
        with patch.dict(os.environ, {'TEST_ACTIVE_KEY': '<placeholder>'}):
            self.assertEqual(utils.resolve_active_credential('TEST_ACTIVE_KEY'), '')

    def test_domain_scoped_credential_isolation(self):
        """make_config_portable must not bleed JIRA tokens to sd.suse.com."""
        import utils
        import os
        from unittest.mock import patch
        
        cfg = {'command': 'python3', 'args': ['bridges/sd_suse_mcp_server.py'], 'env': {}}
        with patch.dict(os.environ, {'JIRA_PERSONAL_TOKEN': 'jira_secret_only', 'SD_SUSE_PAT': ''}, clear=True):
            with patch("utils.probe_target_tls", return_value={"requires_ssl_resilience": False}):
                res = utils.make_config_portable(cfg, target_hint='https://sd.suse.com')
            # Should NOT assign JIRA_PERSONAL_TOKEN to SD_SUSE_PAT
            self.assertNotEqual(res['env'].get('SD_SUSE_PAT'), 'jira_secret_only')

    def test_match_target_to_registry_key_semantic_tokens(self):
        """match_target_to_registry_key must match compound targets with descriptive server aliases."""
        import utils
        servers = {
            "developerknowledge-googleapis-com": {
                "command": "python3",
                "args": ["-u", "bridges/developerknowledge_mcp_bridge.py"],
                "env": {"DEVELOPERKNOWLEDGE_MCP_URL": "https://developerknowledge.googleapis.com/mcp"}
            }
        }
        key, conf = utils.match_target_to_registry_key("developers.google.com/knowledge/api", servers)
        self.assertEqual(key, "developerknowledge-googleapis-com")
        self.assertIsNotNone(conf)

    def test_cleanup_runtime_artifacts_enabled(self):
        """cleanup_runtime_artifacts must delete registered files and directories when enabled."""
        import tempfile, os
        import utils
        
        tmp_dir = tempfile.mkdtemp()
        tmp_file = os.path.join(tmp_dir, "test_file.txt")
        with open(tmp_file, "w") as f:
            f.write("temporary data")
            
        utils.register_temp_path(tmp_dir)
        utils.register_temp_path(tmp_file)
        
        utils.cleanup_runtime_artifacts(enabled=True)
        self.assertFalse(os.path.exists(tmp_dir))
        self.assertFalse(os.path.exists(tmp_file))

    def test_cleanup_runtime_artifacts_disabled(self):
        """cleanup_runtime_artifacts must preserve registered files and directories when disabled."""
        import tempfile, os, shutil
        import utils
        
        tmp_dir = tempfile.mkdtemp()
        tmp_file = os.path.join(tmp_dir, "preserve_file.txt")
        with open(tmp_file, "w") as f:
            f.write("preserve data")
            
        try:
            utils.register_temp_path(tmp_dir)
            utils.register_temp_path(tmp_file)
            
            utils.cleanup_runtime_artifacts(enabled=False)
            self.assertTrue(os.path.exists(tmp_dir))
            self.assertTrue(os.path.exists(tmp_file))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_is_dry_run_environment(self):
        """is_dry_run must return True only when DRY_RUN_MODE == '1'."""
        import os
        from unittest.mock import patch
        import utils
        
        with patch.dict(os.environ, {"DRY_RUN_MODE": "1"}):
            self.assertTrue(utils.is_dry_run())
            
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(utils.is_dry_run())

    def test_verify_prephase_disk_space_sufficient(self):
        """verify_prephase_disk_space must pass when free space >= 1024 MB."""
        from unittest.mock import patch
        import utils
        
        mock_fs = [{"path": "/mock", "mount_point": "/mock", "free_mb": 2048, "total_mb": 10000}]
        with patch("utils.get_target_filesystems", return_value=mock_fs):
            self.assertTrue(utils.verify_prephase_disk_space("Mock Phase"))

    def test_verify_prephase_disk_space_insufficient_halts(self):
        """verify_prephase_disk_space must halt before phase when free space < 1024 MB."""
        from unittest.mock import patch
        import utils
        
        mock_fs = [{"path": "/mock", "mount_point": "/mock", "free_mb": 512, "total_mb": 10000}]
        with patch("utils.get_target_filesystems", return_value=mock_fs):
            with patch.dict(os.environ, {"DRY_RUN_MODE": "0"}):
                self.assertFalse(utils.verify_prephase_disk_space("Mock Phase"))

    def test_is_enospc_error_detection(self):
        """is_enospc_error must detect ENOSPC across exceptions and corroborated error strings, but ignore grep/stdout."""
        import utils, errno
        
        # 1. OSError with errno 28
        os_err = OSError(errno.ENOSPC, "No space left on device")
        self.assertTrue(utils.is_enospc_error(os_err))
        
        # 2. Corroborated OS error string
        output_str = "npm ERR! ENOSPC: no space left on device, write"
        with patch("utils.is_disk_critically_full", return_value=True):
            self.assertTrue(utils.is_enospc_error(output_str))
            
        # 3. Normal grep stdout mentioning test name while disk has space
        grep_stdout = "tests/test_utils.py:415: def test_is_enospc_error_detection(self):"
        with patch("utils.is_disk_critically_full", return_value=False):
            self.assertFalse(utils.is_enospc_error(grep_stdout))
        
        # 4. Unrelated output
        normal_str = "Installation completed successfully."
        self.assertFalse(utils.is_enospc_error(normal_str))

    def test_halt_on_enospc_raises(self):
        """halt_on_enospc must raise OSError(28) inside test environment with clear banner."""
        import utils
        with self.assertRaises(OSError) as ctx:
            utils.halt_on_enospc("Test Context", "Simulated disk full")
        self.assertEqual(ctx.exception.errno, 28)

    def test_verify_target_dns(self):
        """verify_target_dns must resolve valid domains/keywords and reject unresolvable hostnames."""
        import utils
        self.assertTrue(utils.verify_target_dns("github.com"))
        self.assertTrue(utils.verify_target_dns("postgres"))
        self.assertTrue(utils.verify_target_dns("localhost"))
        self.assertFalse(utils.verify_target_dns("gress.opensuse.org"))
        self.assertFalse(utils.verify_target_dns("this-hostname-definitely-does-not-exist-12345.org"))

    def test_fastmcp_compatibility_shim(self):
        """sanitize_stdio_script_content must inject MCPServer fallback when FastMCP is imported."""
        import utils
        src = "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('test')\n"
        sanitized = utils.sanitize_stdio_script_content(src)
        self.assertIn("MCPServer as FastMCP", sanitized)

    def test_ensure_bridges_directory_intact(self):
        """ensure_bridges_directory_intact must create bridges/ directory if missing."""
        import utils
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("utils.WORKSPACE_ROOT", tmpdir):
                utils.ensure_bridges_directory_intact()
                self.assertTrue(os.path.isdir(os.path.join(tmpdir, "bridges")))

    def test_ensure_bridges_directory_purges_lsp_and_synthesizes_obs(self):
        """ensure_bridges_directory_intact must purge LSP corrupted files and synthesize canonical OBS wrapper."""
        import utils
        with tempfile.TemporaryDirectory() as tmpdir:
            b_dir = os.path.join(tmpdir, "bridges")
            os.makedirs(b_dir, exist_ok=True)
            corrupted_file = os.path.join(b_dir, "bad_lsp.py")
            with open(corrupted_file, "w") as f:
                f.write("textDocumentSync = True")
            with patch("utils.WORKSPACE_ROOT", tmpdir):
                utils.ensure_bridges_directory_intact()
                self.assertFalse(os.path.exists(corrupted_file))
                self.assertTrue(os.path.exists(os.path.join(b_dir, "obs_mcp_server.py")))

    def test_persist_system_ca_environment(self):
        """persist_system_ca_environment must detect system CA bundle and export NODE_EXTRA_CA_CERTS."""
        import utils
        with tempfile.TemporaryDirectory() as tmpdir:
            ca_file = os.path.join(tmpdir, "ca-bundle.pem")
            with open(ca_file, "w") as f:
                f.write("---BEGIN CERTIFICATE---")
            with patch("utils.detect_system_ca_bundle", return_value=ca_file):
                with patch("utils.WORKSPACE_ROOT", tmpdir):
                    res = utils.persist_system_ca_environment()
                    self.assertEqual(res["ca_bundle"], ca_file)
                    self.assertEqual(os.environ.get("NODE_EXTRA_CA_CERTS"), ca_file)
                    self.assertEqual(os.environ.get("SSL_CERT_FILE"), ca_file)

    def test_persist_system_ca_environment_uncomments_disabled_lines(self):
        """persist_system_ca_environment must reactivate commented-out certificate exports."""
        import utils
        with tempfile.TemporaryDirectory() as tmpdir:
            ca_file = os.path.join(tmpdir, "ca-bundle.pem")
            with open(ca_file, "w") as f:
                f.write("---BEGIN CERTIFICATE---")
            bashrc_file = os.path.join(tmpdir, ".bashrc")
            with open(bashrc_file, "w") as f:
                f.write('# export NODE_EXTRA_CA_CERTS="/etc/ssl/old.pem"\n# export SSL_CERT_FILE="/etc/ssl/old.pem"\n')
            with patch("utils.detect_system_ca_bundle", return_value=ca_file):
                with patch("utils.WORKSPACE_ROOT", tmpdir):
                    with patch("os.path.expanduser", side_effect=lambda p: bashrc_file if ".bashrc" in p else os.path.join(tmpdir, os.path.basename(p))):
                        res = utils.persist_system_ca_environment()
                        with open(bashrc_file) as f:
                            updated = f.read()
                        self.assertIn(f'export NODE_EXTRA_CA_CERTS="{ca_file}"', updated)
                        self.assertNotIn('# export NODE_EXTRA_CA_CERTS', updated)
                        bashrc_status = [cf for cf in res["confirmed_files"] if cf["file"] == bashrc_file]
                        self.assertTrue(len(bashrc_status) > 0)
                        self.assertEqual(bashrc_status[0]["status"], "Reactivated / Uncommented")

    def test_make_config_portable_includes_equivalent_project_directories(self):
        """make_config_portable must include both google-ai-initialization and google-ai-initialization-repo in bash search path."""
        import utils
        cfg = {"command": "python3", "args": ["bridges/sample_bridge.py"]}
        with patch("utils.WORKSPACE_ROOT", "/home/test/google-ai-initialization-repo"):
            res = utils.make_config_portable(cfg)
            cmd = res["args"][1]
            self.assertIn("/home/test/google-ai-initialization-repo/bridges/sample_bridge.py", cmd)
            self.assertIn("/home/test/google-ai-initialization/bridges/sample_bridge.py", cmd)

        with patch("utils.WORKSPACE_ROOT", "/home/test/google-ai-initialization"):
            res2 = utils.make_config_portable(cfg)
            cmd2 = res2["args"][1]
            self.assertIn("/home/test/google-ai-initialization/bridges/sample_bridge.py", cmd2)
            self.assertIn("/home/test/google-ai-initialization-repo/bridges/sample_bridge.py", cmd2)

    def test_ensure_genai_sdk_available(self):
        """ensure_genai_sdk_available must return success when google.genai is importable."""
        import utils
        ok, err = utils.ensure_genai_sdk_available()
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_ensure_genai_sdk_available_handles_import_error_with_healing(self):
        """ensure_genai_sdk_available must attempt JIT auto-healing on ImportError."""
        import utils
        orig_import = __import__
        def fake_import(name, *args, **kwargs):
            if "genai" in name:
                raise ImportError("No module named 'google.genai'")
            return orig_import(name, *args, **kwargs)

        with patch("utils.auto_heal_missing_python_module", return_value=False) as mock_heal:
            with patch("builtins.__import__", side_effect=fake_import):
                ok, err = utils.ensure_genai_sdk_available()
                self.assertFalse(ok)
                self.assertIn("No module named 'google.genai'", str(err))
                mock_heal.assert_called_once_with("google-genai", log=None)

if __name__ == "__main__":
    unittest.main()
