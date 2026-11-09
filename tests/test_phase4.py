import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import phase4_mcp_auto_discovery
import agent_orchestrator


class TestPhase4(unittest.TestCase):
    """Tests for phase4_mcp_auto_discovery.py and agent_orchestrator.py."""

    def setUp(self):
        self.original_env = os.environ.copy()
        self.disk_patch = patch("phase4_mcp_auto_discovery.verify_prephase_disk_space", return_value=True)
        self.disk_patch.start()

    def tearDown(self):
        self.disk_patch.stop()
        os.environ.clear()
        os.environ.update(self.original_env)

    @patch("os.path.expanduser", new=lambda path: path)
    @patch.dict(os.environ, {"DRY_RUN_MODE": "0"})
    def test_write_mcp_settings_sanitizes_instructions(self):
        """Writing settings should persist command, args, env, but strictly omit instructions to maintain standard MCP compliance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("phase4_mcp_auto_discovery.WORKSPACE_ROOT", tmpdir):
                config = {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "instructions": "DO NOT PERSIST THIS FIELD IN JSON!"
                }
                credentials = {"GITHUB_TOKEN": "secret_pat"}

                phase4_mcp_auto_discovery.write_mcp_settings_file("github", config, credentials)

                mcp_file = os.path.join(tmpdir, ".mcp.json")
                self.assertTrue(os.path.exists(mcp_file))

                with open(mcp_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Assert that instructions field was stripped
                self.assertIn("github", data["mcpServers"])
                self.assertNotIn("instructions", data["mcpServers"]["github"])
                self.assertEqual(data["mcpServers"]["github"]["command"], "npx")
                self.assertEqual(data["mcpServers"]["github"]["env"]["GITHUB_TOKEN"], "secret_pat")

    @patch("subprocess.Popen")
    @patch("select.select")
    def test_validate_mcp_connection_strict_schema_failure(self, mock_select, mock_popen):
        """Double-handshake must reject tools whose inputSchema.type is not 'object'."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()

        # Step 1: initialize response (valid)
        init_response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "test"}}}) + "\n"
        # Step 2: tools/list response (malformed inputSchema, missing or string instead of object)
        malformed_tools_response = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "broken_tool",
                        "description": "Schema bug test",
                        "inputSchema": {"type": "array"}  # BUG: Must be 'object'!
                    }
                ]
            }
        }) + "\n"

        mock_proc.stdout.readline.side_effect = [init_response, malformed_tools_response]
        mock_select.side_effect = [([mock_proc.stdout], [], []), ([mock_proc.stdout], [], [])]
        mock_popen.return_value = mock_proc

        config = {"command": "node", "args": ["server.js"]}
        is_valid = phase4_mcp_auto_discovery.validate_mcp_connection("test-server", config, {})

        # Strict validator MUST catch the invalid schema and return False!
        self.assertFalse(is_valid)

    @patch("subprocess.Popen")
    @patch("select.select")
    def test_validate_mcp_connection_valid_schema_success(self, mock_select, mock_popen):
        """Double-handshake must approve tools when inputSchema.type is 'object'."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()

        init_response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "test"}}}) + "\n"
        valid_tools_response = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "valid_tool",
                        "inputSchema": {"type": "object", "properties": {}}
                    }
                ]
            }
        }) + "\n"

        mock_proc.stdout.readline.side_effect = [init_response, valid_tools_response]
        mock_select.side_effect = [([mock_proc.stdout], [], []), ([mock_proc.stdout], [], [])]
        mock_popen.return_value = mock_proc

        config = {"command": "node", "args": ["server.js"]}
        is_valid = phase4_mcp_auto_discovery.validate_mcp_connection("test-server", config, {})

        self.assertTrue(is_valid)

    @patch("utils.shutil.which")
    @patch("utils.subprocess.run")
    def test_ensure_runner_available_uvx(self, mock_run, mock_which):
        """ensure_runner_available should auto-install uv if uvx is missing from PATH."""
        # First call None (not installed), second call None (before install), third call resolved
        mock_which.side_effect = [None, None, "/usr/local/bin/uvx"]
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        from utils import ensure_runner_available
        import utils
        utils._attempted_jit_runners.clear()

        resolved = ensure_runner_available("uvx")
        self.assertTrue(resolved)
        mock_run.assert_called()

    @patch("utils.subprocess.run")
    def test_auto_heal_missing_python_module(self, mock_run):
        """auto_heal_missing_python_module should install the mapped pip package."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        from utils import auto_heal_missing_python_module
        import utils
        utils._attempted_jit_modules.clear()

        success = auto_heal_missing_python_module("yaml")
        self.assertTrue(success)
        # Should map yaml to pyyaml
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("pyyaml", called_cmd)

    @patch('builtins.input')
    @patch('phase4_mcp_auto_discovery.validate_mcp_connection', return_value=False)
    @patch('phase4_mcp_auto_discovery.discover_all_mcp_candidates')
    @patch('phase4_mcp_auto_discovery.discover_mcp_config')
    def test_discovery_fails_headlessly_without_input(self, mock_disc, mock_all, mock_val, mock_input):
        """Procedural discovery flow on connection failure must complete headlessly without calling input() or touching live registries."""
        from phase4_mcp_auto_discovery import run_mcp_discovery
        mock_disc.return_value = {"command": "python3", "args": ["nonexistent.py"], "env": {"TEST_KEY": ""}}
        mock_all.return_value = [{"config": {"command": "python3", "args": ["nonexistent.py"], "env": {"TEST_KEY": ""}}, "type": "test", "score": 5}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("phase4_mcp_auto_discovery.WORKSPACE_ROOT", tmpdir):
                res = run_mcp_discovery("example.com/api")
                self.assertFalse(res)
                mock_input.assert_not_called()
                # Verify that failed discovery did not commit to .mcp.json (Commit-on-Success)
                mcp_file = os.path.join(tmpdir, ".mcp.json")
                if os.path.exists(mcp_file):
                    with open(mcp_file) as f:
                        data = json.load(f)
                    self.assertNotIn("example-com", data.get("mcpServers", {}))

    def test_redmine_mcp_bridge_tools_and_normalization(self):
        """bridges/redmine_mcp_server.py must normalize issue URLs and expose compliant tool schemas."""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'bridges'))
        import redmine_mcp_server
        
        # 1. Test URL normalization
        self.assertEqual(redmine_mcp_server.normalize_issue_id("https://progress.opensuse.org/issues/12345"), "12345")
        self.assertEqual(redmine_mcp_server.normalize_issue_id("9999"), "9999")
        
        # 2. Test tool schemas are strictly object types
        for tool in redmine_mcp_server.TOOLS:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIsInstance(tool["inputSchema"]["properties"], dict)

    def test_gitlab_mcp_server_record_envelope(self):
        """bridges/gitlab_mcp_server.py list tools must wrap bare arrays into Record envelopes."""
        import subprocess, json, threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class GitLabApiHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps([{"id": 1, "name": "test-project"}]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        api_server = HTTPServer(("127.0.0.1", 0), GitLabApiHandler)
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        api_thread.start()
        env = os.environ.copy()
        env["GITLAB_API_URL"] = f"http://127.0.0.1:{api_server.server_port}/api/v4"
        proc = subprocess.Popen(
            ["python3", "bridges/gitlab_mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            init_cmd = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1.0'}}}
            proc.stdin.write(json.dumps(init_cmd) + '\n')
            proc.stdin.flush()
            proc.stdout.readline()

            call_cmd = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'list_projects', 'arguments': {'limit': 1}}}
            proc.stdin.write(json.dumps(call_cmd) + '\n')
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            
            data = json.loads(resp["result"]["content"][0]["text"])
            self.assertIsInstance(data, dict)
            self.assertIn("projects", data)
            self.assertIn("count", data)
            self.assertIn("structuredContent", resp["result"])
            self.assertIsInstance(resp["result"]["structuredContent"], dict)
        finally:
            proc.kill()
            proc.communicate()
            api_server.shutdown()
            api_server.server_close()
            api_thread.join()

    def test_scored_bridge_detection_accuracy(self):
        """discover_mcp_config must accurately map targets to their dedicated local bridge scripts."""
        import phase4_mcp_auto_discovery as p4
        import utils as utils_module
        
        test_mappings = [
            ("developers.google.com/knowledge/api", ("developers_google_knowledge_bridge.py", "google_developer_knowledge_bridge.py")),
            ("smelt.suse.de", "smelt_suse_mcp_server.py"),
            ("jira.suse.com", "jira_suse_mcp.py"),
            ("sd.suse.com", "sd_suse_mcp_server.py"),
            ("build.suse.de", ("build_suse_de_mcp_server.py", "build_suse_de_mcp.py")),
            ("build.opensuse.org", ("build_opensuse_org_mcp_server.py", "build_opensuse_mcp.py", "build_opensuse_org_mcp.py")),
            ("gitlab.suse.de", "gitlab_mcp_server.py"),
            ("openqa.suse.de", ("openqa_suse_de_mcp_server.py", "openqa_suse_de_mcp_bridge.py")),
            ("progress.opensuse.org", "redmine_mcp_server.py"),
            ("bugzilla.suse.com", "bugzilla_mcp_server.py"),
            ("bugzilla.opensuse.org", "bugzilla_mcp_server.py")
        ]
        
        tls_probe_result = {
            "is_https": True,
            "ssl_verified": True,
            "requires_ssl_resilience": False,
            "error": None,
        }
        with patch.object(p4, "probe_native_mcp_endpoint", return_value={}), patch.object(
            utils_module, "probe_target_tls", return_value=tls_probe_result
        ):
            for target, expected_script in test_mappings:
                cfg = p4.discover_mcp_config(target)
                args_str = " ".join(cfg.get("args", []))
                if isinstance(expected_script, tuple):
                    self.assertTrue(any(exp in args_str for exp in expected_script), f"Target '{target}' failed to resolve to expected script '{expected_script}'. Got: {args_str}")
                else:
                    self.assertIn(expected_script, args_str, f"Target '{target}' failed to resolve to expected script '{expected_script}'. Got: {args_str}")

            # Ensure generic domain tokens like 'suse' do not hijack unrelated targets
            confluence_cfg = p4.discover_mcp_config("confluence.suse.com")
            confluence_args = " ".join(confluence_cfg.get("args", []))
            self.assertNotIn("openqa", confluence_args, "confluence.suse.com was mistakenly hijacked by openqa bridge!")

    def test_obs_universal_bridge_compilation(self):
        """bridges/obs_mcp_server.py must compile and resolve."""
        import py_compile
        py_compile.compile("bridges/obs_mcp_server.py", doraise=True)

    def test_bugzilla_mcp_server_record_envelope(self):
        """bridges/bugzilla_mcp_server.py list tools must wrap bare arrays into Record envelopes."""
        import subprocess, json
        proc = subprocess.Popen(
            ['python3', 'bridges/bugzilla_mcp_server.py'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        try:
            init_cmd = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1.0'}}}
            proc.stdin.write(json.dumps(init_cmd) + '\n')
            proc.stdin.flush()
            proc.stdout.readline()

            call_cmd = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'search_bugs', 'arguments': {'limit': 1}}}
            proc.stdin.write(json.dumps(call_cmd) + '\n')
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            
            data = json.loads(resp["result"]["content"][0]["text"])
            self.assertIsInstance(data, dict)
            self.assertIn("bugs", data)
            self.assertIn("count", data)
            self.assertIn("structuredContent", resp["result"])
            self.assertIsInstance(resp["result"]["structuredContent"], dict)
        finally:
            proc.kill()

    def test_is_valid_mcp_config_guards(self):
        """is_valid_mcp_config must strictly reject commands specifying non-existent Python files."""
        from utils import is_valid_mcp_config
        # 1. Hallucinated script
        self.assertFalse(is_valid_mcp_config({"command": "python3", "args": ["non_existent_fake_script.py"]}))
        # 2. Existing script
        self.assertTrue(is_valid_mcp_config({"command": "python3", "args": ["bridges/sd_suse_mcp_server.py"]}))
        # 3. Verified package runner
        self.assertTrue(is_valid_mcp_config({"command": "uvx", "args": ["openqa-mcp"]}))

    @patch("phase4_mcp_auto_discovery.query_gemini_for_mcp_server")
    def test_discover_mcp_config_rejects_hallucinated_script(self, mock_gemini):
        """discover_mcp_config must not accept a hallucinated non-existent Python script from Gemini."""
        import phase4_mcp_auto_discovery as p4
        mock_gemini.return_value = {
            "command": "python3",
            "args": ["openqa_opensuse_org_mcp_bridge.py"]
        }
        # For a target without native or local bridge, should reject hallucination and fall through to known package or generic
        cfg = p4.discover_mcp_config("nonexistent.test.domain")
        args_str = " ".join(cfg.get("args", []))
        self.assertNotIn("openqa_opensuse_org_mcp_bridge.py", args_str)

    def test_pre_existing_server_skip_guard(self):
        """run_mcp_discovery must immediately preserve and skip targets already registered in .mcp.json."""
        import phase4_mcp_auto_discovery as p4
        with tempfile.TemporaryDirectory() as tmpdir:
            mcp_file = os.path.join(tmpdir, ".mcp.json")
            with open(mcp_file, "w") as f:
                json.dump({"mcpServers": {"github-com": {"command": "npx", "args": ["@modelcontextprotocol/server-github"]}}}, f)
            with patch("phase4_mcp_auto_discovery.WORKSPACE_ROOT", tmpdir):
                res = p4.run_mcp_discovery("github.com")
                self.assertTrue(res)

    def test_discover_all_mcp_candidates_returns_list_of_dicts(self):
        """discover_all_mcp_candidates must return a list of dictionaries with 'config' key."""
        import phase4_mcp_auto_discovery as p4
        with patch("phase4_mcp_auto_discovery.query_gemini_for_mcp_server", return_value=None):
            with patch("phase4_mcp_auto_discovery.probe_native_mcp_endpoint", return_value=None):
                with patch("phase4_mcp_auto_discovery.probe_openapi_spec", return_value=None):
                    cands = p4.discover_all_mcp_candidates("sd.suse.com")
                    self.assertIsInstance(cands, list)
                    self.assertTrue(len(cands) > 0)
                    for c in cands:
                        self.assertIsInstance(c, dict)
                        self.assertIn("config", c)
                        self.assertIsInstance(c["config"], dict)

    def test_deferred_tool_verification_for_token_gated_service(self):
        """validate_mcp_connection must accept token-gated servers that return 0 tools before authentication."""
        import phase4_mcp_auto_discovery as p4
        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
        ]
        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("select.select", return_value=([mock_proc.stdout], [], [])):
                res = p4.validate_mcp_connection(
                    "jira-suse-com",
                    {"command": "uvx", "args": ["mcp-atlassian", "--jira-url", "https://jira.suse.com"]},
                    {"JIRA_PERSONAL_TOKEN": "<OPTIONAL_JIRA_PERSONAL_TOKEN>"}
                )
                self.assertTrue(res)

    def test_validate_mcp_skips_notification_errors(self):
        """validate_mcp_connection must correlate id: 2 and skip unhandled notification errors."""
        import phase4_mcp_auto_discovery as p4
        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}),
            json.dumps({"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method 'notifications/initialized' not supported."}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "test_tool", "inputSchema": {"type": "object"}}]}})
        ]
        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("select.select", return_value=([mock_proc.stdout], [], [])):
                with patch("utils.perform_level3_functional_smoke_test", return_value=(True, "OK")):
                    res = p4.validate_mcp_connection(
                        "custom-server",
                        {"command": "python3", "args": ["server.py"]},
                        {}
                    )
                    self.assertTrue(res)

    def test_native_endpoint_top_score_precedence(self):
        """discover_all_mcp_candidates must award score 100 to native endpoints and sort descending."""
        import phase4_mcp_auto_discovery as p4
        mock_native = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-sse", "https://example.com/sse"]}
        with patch("phase4_mcp_auto_discovery.probe_native_mcp_endpoint", return_value=mock_native):
            with patch("phase4_mcp_auto_discovery.query_gemini_for_mcp_server", return_value=None):
                with patch("phase4_mcp_auto_discovery.query_package_registries", return_value=None):
                    with patch("phase4_mcp_auto_discovery.probe_openapi_spec", return_value=None):
                        candidates = p4.discover_all_mcp_candidates("example.com")
                        self.assertGreater(len(candidates), 0)
                        self.assertEqual(candidates[0]["type"], "native")
                        self.assertEqual(candidates[0]["score"], 100)
                        # Verify candidates are strictly sorted descending by score
                        scores = [c["score"] for c in candidates]
                        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_probe_native_mcp_endpoint_detects_401_403_auth_challenges(self):
        """probe_native_mcp_endpoint must detect 401/403 with JSON error as an authenticated native MCP server."""
        import phase4_mcp_auto_discovery as p4
        import urllib.error
        from io import BytesIO

        mock_fp = BytesIO(b'{"error": "no api key"}')
        mock_hdrs = {"Content-Type": "application/json"}
        http_err = urllib.error.HTTPError("https://example.com/mcp", 403, "Forbidden", mock_hdrs, mock_fp)

        with patch("urllib.request.urlopen", side_effect=http_err):
            res = p4.probe_native_mcp_endpoint("example.com")
            self.assertIn("url", res)
            self.assertEqual(res.get("type"), "http")
            self.assertIn("example.com", res.get("url"))

    def test_validate_mcp_connection_http_transport(self):
        """validate_mcp_connection must support native Streamable HTTP configurations."""
        import phase4_mcp_auto_discovery as p4
        from unittest.mock import MagicMock

        resp1 = MagicMock()
        resp1.read.return_value = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}
        }).encode("utf-8")
        
        resp2 = MagicMock()
        resp2.read.return_value = json.dumps({
            "jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "test_tool", "inputSchema": {"type": "object"}}]}
        }).encode("utf-8")

        mock_cm1 = MagicMock()
        mock_cm1.__enter__.return_value = resp1
        mock_cm2 = MagicMock()
        mock_cm2.__enter__.return_value = resp2

        with patch("urllib.request.urlopen", side_effect=[mock_cm1, mock_cm2]):
            ok = p4.validate_mcp_connection(
                "test-http-server",
                {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer test"}},
                {}
            )
            self.assertTrue(ok)

    def test_validate_mcp_connection_http_deferred_auth(self):
        """validate_mcp_connection must tolerate HTTP 401/403 with API key errors as deferred authentication."""
        import phase4_mcp_auto_discovery as p4
        import urllib.error
        from io import BytesIO

        mock_fp = BytesIO(b'{"error": "no api key"}')
        mock_hdrs = {"Content-Type": "application/json"}
        http_err = urllib.error.HTTPError("https://openqa.opensuse.org/mcp", 403, "Forbidden", mock_hdrs, mock_fp)

        with patch("urllib.request.urlopen", side_effect=http_err):
            ok = p4.validate_mcp_connection(
                "openqa-opensuse-org",
                {"type": "http", "url": "https://openqa.opensuse.org/mcp", "headers": {"Authorization": "Bearer ${OPENQA_USER}:${OPENQA_API_KEY}:${OPENQA_API_SECRET}"}},
                {}
            )
            self.assertTrue(ok)

    def test_probe_native_mcp_endpoint_rejects_html_error_pages(self):
        """probe_native_mcp_endpoint must strictly reject HTML 401/403 pages (e.g. Jira web UI)."""
        import phase4_mcp_auto_discovery as p4
        import urllib.error
        from io import BytesIO

        html_body = b'<html><head><title>Unauthorized (401)</title></head><body><h1>Unauthorized</h1></body></html>'
        mock_fp = BytesIO(html_body)
        mock_hdrs = {"Content-Type": "text/html;charset=UTF-8", "WWW-Authenticate": 'OAuth realm="jira"'}
        http_err = urllib.error.HTTPError("https://jira.suse.com/mcp", 401, "Unauthorized", mock_hdrs, mock_fp)

        with patch("urllib.request.urlopen", side_effect=http_err):
            res = p4.probe_native_mcp_endpoint("jira.suse.com")
            self.assertEqual(res, {})

    def test_discover_all_mcp_candidates_native_strictly_score_100(self):
        """discover_all_mcp_candidates must assign score 100 to native endpoints and sort them as Candidate 1."""
        import phase4_mcp_auto_discovery as p4
        with patch("phase4_mcp_auto_discovery.query_gemini_for_mcp_server", return_value=None):
            with patch("phase4_mcp_auto_discovery.probe_native_mcp_endpoint", return_value={"type": "http", "httpUrl": "https://openqa.suse.de/mcp", "url": "https://openqa.suse.de/mcp"}):
                cands = p4.discover_all_mcp_candidates("openqa.suse.de")
                self.assertTrue(len(cands) >= 2)
                self.assertEqual(cands[0]["type"], "native")
                self.assertEqual(cands[0]["score"], 100)

if __name__ == "__main__":
    unittest.main()
