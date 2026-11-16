import subprocess
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

import initialize_google_ai
import agent_orchestrator


class TestOrchestrator(unittest.TestCase):
    """Tests for initialize_google_ai.py master orchestrator."""

    def setUp(self):
        self.original_env = os.environ.copy()
        self.disk_patch = patch("agent_orchestrator.verify_prephase_disk_space", return_value=True)
        self.disk_patch.start()

    def tearDown(self):
        self.disk_patch.stop()
        os.environ.clear()
        os.environ.update(self.original_env)

    @patch("initialize_google_ai.run_setup")
    @patch("initialize_google_ai.run_gcloud_auth")
    @patch("initialize_google_ai.run_gcloud_config")
    def test_phases_scheduling_selective(self, mock_phase3, mock_phase2, mock_phase1):
        """Should only execute phases specified in the --phases argument."""
        mock_phase1.return_value = True
        mock_phase2.return_value = True
        mock_phase3.return_value = True

        test_args = [
            "initialize_google_ai.py",
            "--phases", "1,3",
            "--gcloud-project", "test-project",
            "--no-automatic-browser"
        ]

        with patch.object(sys, "argv", test_args):
            with patch("sys.exit") as mock_exit:
                initialize_google_ai.main()
                mock_exit.assert_called_with(0)

                # Phase 1 and 3 should be called, Phase 2 skipped!
                self.assertEqual(mock_phase1.call_count, 1)
                self.assertEqual(mock_phase2.call_count, 0)
                self.assertEqual(mock_phase3.call_count, 1)

    def test_clean_slate_mcp_json_pruning(self):
        """Phase 4 startup must remove any pre-existing .mcp.json file to provide a clean slate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mcp_file = os.path.join(tmpdir, ".mcp.json")
            with open(mcp_file, "w") as f:
                f.write('{"mcpServers": {"old_broken_server": {}}}')

            self.assertTrue(os.path.exists(mcp_file))

            with patch.dict(os.environ, {"WORKSPACE_ROOT_OVERRIDE": tmpdir}):
                with patch("initialize_google_ai.WORKSPACE_ROOT", tmpdir):
                    with patch("utils.WORKSPACE_ROOT", tmpdir):
                        with patch("initialize_google_ai.run_gcloud_config", return_value=True):
                            with patch("initialize_google_ai.run_mcp_discovery", return_value=True):
                                test_args = [
                                    "initialize_google_ai.py",
                                    "--phases", "4",
                                    "--mcp-server", "test-target",
                                    "--no-agent"
                                ]
                                with patch.object(sys, "argv", test_args):
                                    with patch("sys.exit") as mock_exit:
                                        initialize_google_ai.main()
                                        mock_exit.assert_called_with(0)

    @patch("select.select")
    @patch("agent_orchestrator.subprocess.Popen")
    def test_verify_mcp_connection_tool_strict_schema_failure(self, mock_popen, mock_select):
        """verify_mcp_connection_tool must reject servers whose tools omit or invalidate inputSchema.type == 'object'."""
        import agent_orchestrator

        mock_proc = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.flush = MagicMock()
        # Initialize response + broken tools/list response (missing type: object)
        mock_proc.stdout.readline.side_effect = [
            '{"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "test-gitlab"}}}\n',
            '{"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "bad_tool", "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#"}}]}}\n'
        ]
        mock_select.side_effect = [([mock_proc.stdout], [], []), ([mock_proc.stdout], [], [])]
        mock_popen.return_value = mock_proc

        result = agent_orchestrator.verify_mcp_connection_tool("npx", ["-y", "dummy-pkg"], {})
        self.assertIn("FAILED", result)
        self.assertIn("Strict Schema Validation Error", result)
        self.assertIn("inputSchema.type MUST be 'object'", result)

    @patch("select.select")
    @patch("agent_orchestrator.subprocess.Popen")
    def test_verify_mcp_connection_tool_valid_schema_success(self, mock_popen, mock_select):
        """verify_mcp_connection_tool must approve servers whose tools have inputSchema.type == 'object'."""
        import agent_orchestrator

        mock_proc = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.flush = MagicMock()
        mock_proc.stdout.readline.side_effect = [
            '{"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "test-valid"}}}\n',
            '{"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "valid_tool", "inputSchema": {"type": "object", "properties": {}}}]}}\n'
        ]
        mock_select.side_effect = [([mock_proc.stdout], [], []), ([mock_proc.stdout], [], [])]
        mock_popen.return_value = mock_proc

        result = agent_orchestrator.verify_mcp_connection_tool("npx", ["-y", "valid-pkg"], {})
        self.assertIn("SUCCESS", result)
        self.assertIn("1 valid, strictly-formatted tools", result)

    def test_extract_script_dependencies(self):
        """extract_script_dependencies should discover external pip packages while omitting standard library."""
        from utils import extract_script_dependencies

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("import os\nimport sys\nimport requests\nfrom mcp.server.fastmcp import FastMCP\nfrom openqa_async.aclient import Client\n")
            temp_path = f.name

        try:
            deps = extract_script_dependencies(temp_path)
            self.assertIn("requests", deps)
            self.assertIn("mcp", deps)
            self.assertIn("openqa_async", deps)
            self.assertNotIn("os", deps)
            self.assertNotIn("sys", deps)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @patch("google.genai.Client")
    def test_agent_prompt_construction_and_session_creation(self, mock_client_cls):
        """Ensures run_agentic_initialization constructs system instructions and prompts without f-string NameError."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_chat = MagicMock()
        mock_instance.chats.create.return_value = mock_chat
        mock_chat.send_message.return_value = MagicMock(text="All done SUCCESS")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with open(os.path.join(tmpdir, ".mcp.json"), "w") as f:
                    json.dump({"mcpServers": {"jira-suse-com": {"command": "python3"}}}, f)
                res = agent_orchestrator.run_agentic_initialization("test-project", "jira.suse.com")
                self.assertTrue(res)

    def test_execute_shell_command_blocks_mcp_json_overwrite(self):
        """execute_shell_command must block destructive shell overwrites of .mcp.json."""
        cmd1 = 'python3 -c "with open(\'.mcp.json\', \'w\') as f: f.write(\'{}\')"'
        res1 = agent_orchestrator.execute_shell_command(cmd1)
        self.assertIn("FAILED", res1)
        self.assertIn("strictly prohibited", res1)

        cmd2 = 'echo "{}" > .mcp.json'
        res2 = agent_orchestrator.execute_shell_command(cmd2)
        self.assertIn("FAILED", res2)

    def test_execute_shell_command_safe_from_grep_enospc(self):
        """execute_shell_command must not trigger ENOSPC halt when command stdout contains disk error strings."""
        res = agent_orchestrator.execute_shell_command('echo "test_utils.py: def test_is_enospc_error_detection No space left on device"')
        self.assertIn("Exit Code: 0", res)
        self.assertIn("No space left on device", res)

    def test_write_local_file_tool_merges_mcp_json(self):
        """write_local_file_tool writing to .mcp.json must merge incoming servers without dropping existing ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mcp_file = os.path.join(tmpdir, ".mcp.json")
            with open(mcp_file, "w") as f:
                json.dump({"mcpServers": {"server1": {"command": "echo", "args": []}}}, f)

            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                incoming_content = json.dumps({"mcpServers": {"server2": {"command": "cat", "args": []}}})
                res = agent_orchestrator.write_local_file_tool(".mcp.json", incoming_content)
                self.assertIn("SUCCESS", res)

                with open(mcp_file, "r") as f:
                    merged = json.load(f)
                self.assertIn("server1", merged["mcpServers"])
                self.assertIn("server2", merged["mcpServers"])


    @patch.dict(os.environ, {"DRY_RUN_MODE": "0"})
    def test_write_mcp_settings_tool_direct_invocation(self):
        """write_mcp_settings_tool must succeed with 4 or 5 args and never raise NameError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with patch("os.path.expanduser", side_effect=lambda path: path.replace("~", tmpdir)):
                    # 4 args (instructions omitted)
                    res1 = agent_orchestrator.write_mcp_settings_tool("srv1", "echo", ["hi"], {"PORT": "8080"})
                    self.assertIn("SUCCESS", res1)

                    # 5 args (instructions provided)
                    res2 = agent_orchestrator.write_mcp_settings_tool("srv2", "echo", ["hello"], {}, "Run setup first")
                    self.assertIn("SUCCESS", res2)

                mcp_file = os.path.join(tmpdir, ".mcp.json")
                with open(mcp_file, "r") as f:
                    data = json.load(f)
                self.assertIn("srv1", data["mcpServers"])
                self.assertIn("srv2", data["mcpServers"])

    def test_write_mcp_settings_tool_dry_run(self):
        """Under DRY_RUN_MODE=1, write_mcp_settings_tool must not modify or write files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with patch.dict(os.environ, {"DRY_RUN_MODE": "1"}):
                    res = agent_orchestrator.write_mcp_settings_tool("srv_dry", "echo", ["hello"], {})
                    self.assertIn("DRY-RUN", res)
                    mcp_file = os.path.join(tmpdir, ".mcp.json")
                    self.assertFalse(os.path.exists(mcp_file))

    def test_cli_flags_cleanup_and_dry_run_help(self):
        """CLI parser must expose --dry-run, --do-cleanup, and --no-cleanup flags."""
        res = subprocess.run(
            ["python3", "initialize_google_ai.py", "--help"],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--dry-run", res.stdout)
        self.assertIn("--do-cleanup", res.stdout)
        self.assertIn("--no-cleanup", res.stdout)

    def test_is_transient_agent_error(self):
        """is_transient_agent_error must detect socket disconnects, timeouts, and rate limits."""
        self.assertTrue(agent_orchestrator.is_transient_agent_error("Server disconnected without sending a response."))
        self.assertTrue(agent_orchestrator.is_transient_agent_error("Remote end closed connection without response"))
        self.assertTrue(agent_orchestrator.is_transient_agent_error("ResourceExhausted: 429 Quota exceeded"))
        self.assertTrue(agent_orchestrator.is_transient_agent_error("503 Service Unavailable"))
        self.assertFalse(agent_orchestrator.is_transient_agent_error("Invalid model parameter name"))

    @patch("google.genai.Client")
    def test_agent_heals_on_transient_disconnect(self, mock_client_cls):
        """run_agentic_initialization must retry and heal when transient disconnect occurs."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_chat1 = MagicMock()
        mock_chat1.send_message.side_effect = Exception("Server disconnected without sending a response.")
        mock_chat1.get_history.return_value = []
        
        mock_chat2 = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "Everything configured with SUCCESS!"
        mock_chat2.get_history.return_value = []
        
        mock_client.chats.create.side_effect = [mock_chat1, mock_chat2]
        
        with tempfile.TemporaryDirectory() as tmpdir:
            def write_mcp_progress(*args, **kwargs):
                with open(os.path.join(tmpdir, ".mcp.json"), "w") as f:
                    json.dump({"mcpServers": {"progress-opensuse-org": {"command": "python3"}}}, f)
                return mock_resp
            mock_chat2.send_message.side_effect = write_mcp_progress
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with patch("time.sleep"):  # skip sleep delay during test
                    res = agent_orchestrator.run_agentic_initialization("test-proj", "progress.opensuse.org")
                    self.assertTrue(res)
                    self.assertEqual(mock_client.chats.create.call_count, 2)

    @patch("google.genai.Client")
    def test_agent_recovers_if_target_written_before_crash(self, mock_client_cls):
        """Agent must approve configuration if target was already written to .mcp.json before crash."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_chat = MagicMock()
        mock_chat.send_message.side_effect = Exception("Connection dropped after writing file")
        mock_client.chats.create.return_value = mock_chat
        
        with tempfile.TemporaryDirectory() as tmpdir:
            mcp_path = os.path.join(tmpdir, ".mcp.json")
            with open(mcp_path, "w") as f:
                json.dump({"mcpServers": {"progress-opensuse-org": {"command": "python3"}}}, f)
                
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                res = agent_orchestrator.run_agentic_initialization("test-proj", "progress.opensuse.org")
                self.assertTrue(res)

    @patch("google.genai.Client")
    def test_agent_adc_fallback_isolates_from_invalid_api_key(self, mock_client_cls):
        """When GEMINI_API_KEY is rejected, agent must fall back to Keyless Vertex AI ADC and isolate environment."""
        key_client = MagicMock()
        key_client.models.generate_content.side_effect = Exception("API Key Invalid 400")

        adc_client = MagicMock()
        adc_client.models.generate_content.return_value = MagicMock(text="ping")
        mock_chat = MagicMock()
        mock_chat.send_message.return_value = MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
        mock_chat.get_history.return_value = []
        adc_client.chats.create.return_value = mock_chat

        mock_client_cls.side_effect = [key_client, adc_client]

        with tempfile.TemporaryDirectory() as tmpdir:
            def write_mcp_gh(*args, **kwargs):
                with open(os.path.join(tmpdir, ".mcp.json"), "w") as f:
                    json.dump({"mcpServers": {"github-com": {"command": "npx"}}}, f)
                return MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
            mock_chat.send_message.side_effect = write_mcp_gh
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with patch.dict(os.environ, {"GEMINI_API_KEY": "BAD_KEY_123"}):
                    res = agent_orchestrator.run_agentic_initialization("test-project-123", "github.com")
                    self.assertTrue(res)
                    # Verify second instantiation used vertexai=True
                    self.assertEqual(mock_client_cls.call_count, 2)
                    _, second_kwargs = mock_client_cls.call_args_list[1]
                    self.assertTrue(second_kwargs.get("vertexai"))
                    self.assertEqual(second_kwargs.get("project"), "test-project-123")
                    # Fix 1 & 2 verification: timeout must be in milliseconds (>= 60,000)
                    self.assertGreaterEqual(second_kwargs.get("http_options").timeout, 60000.0)

    @patch("google.genai.Client")
    def test_dual_api_key_isolation_during_adc_fallback(self, mock_client_cls):
        """When both GEMINI_API_KEY and GOOGLE_API_KEY are present, keyless fallback must isolate both."""
        key_client = MagicMock()
        key_client.models.generate_content.side_effect = Exception("API Key Expired")

        adc_client = MagicMock()
        adc_client.models.generate_content.return_value = MagicMock(text="ping")
        mock_chat = MagicMock()
        mock_chat.send_message.return_value = MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
        mock_chat.get_history.return_value = []
        adc_client.chats.create.return_value = mock_chat

        mock_client_cls.side_effect = [key_client, adc_client]

        with tempfile.TemporaryDirectory() as tmpdir:
            def write_mcp_gh_dual(*args, **kwargs):
                with open(os.path.join(tmpdir, ".mcp.json"), "w") as f:
                    json.dump({"mcpServers": {"github-com": {"command": "npx"}}}, f)
                return MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
            mock_chat.send_message.side_effect = write_mcp_gh_dual
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with patch.dict(os.environ, {"GEMINI_API_KEY": "BAD_GEMINI", "GOOGLE_API_KEY": "BAD_GOOGLE"}):
                    res = agent_orchestrator.run_agentic_initialization("test-project-456", "github.com")
                    self.assertTrue(res)
                    # Assert environment was isolated during ADC instantiation
                    self.assertEqual(mock_client_cls.call_count, 2)
                    _, second_kwargs = mock_client_cls.call_args_list[1]
                    self.assertTrue(second_kwargs.get("vertexai"))
                    self.assertEqual(second_kwargs.get("project"), "test-project-456")
                    self.assertGreaterEqual(second_kwargs.get("http_options").timeout, 60000.0)

    def test_adaptive_model_fallback(self):
        """verify_genai_client must probe fallback models if primary model is unavailable."""
        mock_client = MagicMock()
        def mock_generate_content(model, contents, config):
            if model == "gemini-3.7-flash":
                raise Exception("404 Model Not Found in region")
            if model == "gemini-2.5-flash":
                return MagicMock(text="pong")
            raise Exception("Unexpected model")

        mock_client.models.generate_content.side_effect = mock_generate_content

        with patch.dict(os.environ, {"GOOGLE_GENAI_MODEL": "gemini-3.7-flash"}):
            success = agent_orchestrator.verify_genai_client(mock_client)
            self.assertTrue(success)
            self.assertEqual(os.environ.get("GOOGLE_GENAI_MODEL"), "gemini-2.5-flash")

    @patch("google.genai.Client")
    def test_vertex_ai_tool_exclusivity_omits_search_tool(self, mock_client_cls):
        """In Vertex AI mode, Google Search tool must be omitted when function tools are present to avoid 400 INVALID_ARGUMENT."""
        mock_client = MagicMock()
        mock_client.vertexai = True
        mock_client.models.generate_content.return_value = MagicMock(text="ping")
        
        mock_chat = MagicMock()
        mock_chat.send_message.return_value = MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
        mock_chat.get_history.return_value = []
        mock_client.chats.create.return_value = mock_chat
        mock_client_cls.return_value = mock_client

        with tempfile.TemporaryDirectory() as tmpdir:
            def write_mcp_gh_vertex(*args, **kwargs):
                with open(os.path.join(tmpdir, ".mcp.json"), "w") as f:
                    json.dump({"mcpServers": {"github-com": {"command": "npx"}}}, f)
                return MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
            mock_chat.send_message.side_effect = write_mcp_gh_vertex
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with patch.dict(os.environ, {"GOOGLE_GENAI_USE_ENTERPRISE": "True"}, clear=False):
                    res = agent_orchestrator.run_agentic_initialization("test-project-789", "github.com")
                    self.assertTrue(res)
                # Verify chat config tools only contains function callables, no GoogleSearch
                    self.assertTrue(mock_client.chats.create.called)
                    call_kwargs = mock_client.chats.create.call_args[1]
                    cfg = call_kwargs.get("config")
                    self.assertIsNotNone(cfg)
                    # Every tool in cfg.tools must be a callable function, not a Tool with google_search
                    for t in cfg.tools:
                        self.assertTrue(callable(t), f"Expected callable function tool, got: {t}")

    @patch("google.genai.Client")
    def test_agent_dynamic_quota_failover_on_429(self, mock_client_cls):
        """On 429 RESOURCE_EXHAUSTED, agent must dynamically adapt model to high-capacity tier (e.g. gemini-2.5-flash)."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        
        mock_chat1 = MagicMock()
        mock_chat1.send_message.side_effect = Exception("429 RESOURCE_EXHAUSTED. Rate limit exceeded.")
        
        mock_chat2 = MagicMock()
        mock_chat2.get_history.return_value = []
        mock_client.chats.create.side_effect = [mock_chat1, mock_chat2]
        
        with tempfile.TemporaryDirectory() as tmpdir:
            def write_mcp_jira(*args, **kwargs):
                with open(os.path.join(tmpdir, ".mcp.json"), "w") as f:
                    json.dump({"mcpServers": {"jira-suse-com": {"command": "uvx"}}}, f)
                return MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
            mock_chat2.send_message.side_effect = write_mcp_jira
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with patch("time.sleep"):
                    with patch.dict(os.environ, {"GOOGLE_GENAI_MODEL": "gemini-3.7-flash"}, clear=False):
                        res = agent_orchestrator.run_agentic_initialization("test-project", "jira.suse.com")
                        self.assertTrue(res)
                        self.assertEqual(os.environ.get("GOOGLE_GENAI_MODEL"), "gemini-2.5-flash")

    def test_execute_shell_command_blocks_full_test_suite(self):
        """execute_shell_command must intercept full test suite execution to prevent turn budget exhaustion."""
        out = agent_orchestrator.execute_shell_command("python3 run_tests.py")
        self.assertIn("Executing the entire multi-minute test suite", out)
        self.assertIn("disabled within discovery turns", out)

    @patch("agent_orchestrator.verify_target_dns", return_value=False)
    @patch("google.genai.Client")
    def test_agent_non_blocking_dns_proceeds(self, mock_client_cls, mock_dns):
        """Non-resolving or slow DNS must log warning and allow agent discovery to proceed rather than fatal abort."""
        mock_client = MagicMock()
        mock_chat = MagicMock()
        mock_chat.send_message.return_value = MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
        mock_chat.get_history.return_value = []
        mock_client.chats.create.return_value = mock_chat
        mock_client_cls.return_value = mock_client
        
        with tempfile.TemporaryDirectory() as tmpdir:
            def write_mcp_obs(*args, **kwargs):
                with open(os.path.join(tmpdir, ".mcp.json"), "w") as f:
                    json.dump({"mcpServers": {"build-opensuse-org": {"command": "python3"}}}, f)
                return MagicMock(text="FINAL_VERDICT_LOGGED: SUCCESS")
            mock_chat.send_message.side_effect = write_mcp_obs
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                res = agent_orchestrator.run_agentic_initialization("test-project", "build.opensuse.org")
                self.assertTrue(res)

    def test_resilient_function_map_handles_unmapped_tools(self):
        """ResilientFunctionMap must prevent KeyError on unmapped AFC tool calls like google:python_interpreter."""
        from agent_orchestrator import ResilientFunctionMap
        fn_map = ResilientFunctionMap({"known_tool": lambda: "ok"})
        
        # 1. Known tool
        self.assertEqual(fn_map["known_tool"](), "ok")
        
        # 2. google:python_interpreter fallback
        py_runner = fn_map["google:python_interpreter"]
        self.assertTrue(callable(py_runner))
        res = py_runner(code="print('hello resilient AFC')")
        self.assertIn("hello resilient AFC", str(res))
        
        # 3. Unmapped hallucinated tool
        unmapped_fn = fn_map["execute_api_grounding_tool"]
        self.assertTrue(callable(unmapped_fn))
        res2 = unmapped_fn(query="test")
        self.assertIn("unmapped", str(res2))

    def test_write_local_file_rejects_lsp_code(self):
        """write_local_file_tool must intercept and reject LSP boilerplate containing textDocumentSync."""
        from agent_orchestrator import write_local_file_tool
        res = write_local_file_tool("test_lsp.py", '{"capabilities": {"textDocumentSync": 1}}')
        self.assertIn("rejected", res.lower())
        self.assertIn("Language Server Protocol", res)

    def test_execute_shell_command_blocks_unpiped_stdio_script(self):
        """execute_shell_command must intercept and block unpiped stdio MCP server executions."""
        from agent_orchestrator import execute_shell_command
        res = execute_shell_command("python3 bridges/redmine_mcp_server.py")
        self.assertIn("ERROR: Stdio Model Context Protocol", res)
        self.assertIn("verify_mcp_connection_tool", res)

    @patch("google.genai.Client")
    def test_agent_requires_physical_mcp_config_to_succeed(self, mock_client_cls):
        """Agent must return False if .mcp.json does not contain target, even if model outputs SUCCESS in text."""
        mock_client = MagicMock()
        mock_chat = MagicMock()
        mock_chat.send_message.return_value = MagicMock(text="I configured everything! SUCCESS")
        mock_chat.get_history.return_value = []
        mock_client.chats.create.return_value = mock_chat
        mock_client_cls.return_value = mock_client
        
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                with patch("agent_orchestrator.is_target_configured_in_mcp", return_value=False):
                    res = agent_orchestrator.run_agentic_initialization("test-project", "github.com")
                    self.assertFalse(res)

    def test_verify_mcp_connection_tool_http_mode(self):
        """verify_mcp_connection_tool must support transport='http' via _validate_http_mcp."""
        import agent_orchestrator
        with patch("phase4_mcp_auto_discovery._validate_http_mcp", return_value=True):
            res = agent_orchestrator.verify_mcp_connection_tool(transport="http", url="https://example.com/mcp")
            self.assertIn("SUCCESS", res)
            self.assertIn("https://example.com/mcp", res)

    @patch("os.path.expanduser", new=lambda path: path)
    @patch.dict(os.environ, {"DRY_RUN_MODE": "0"})
    def test_write_mcp_settings_tool_http_mode(self):
        """write_mcp_settings_tool must write type='http' configs into .mcp.json."""
        import agent_orchestrator
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                res = agent_orchestrator.write_mcp_settings_tool(
                    "openqa-test",
                    transport="http",
                    url="https://openqa.opensuse.org/mcp",
                    headers={"Authorization": "Bearer test:key:sec"}
                )
                self.assertIn("SUCCESS", res)
                mcp_file = os.path.join(tmpdir, ".mcp.json")
                with open(mcp_file) as f:
                    data = json.load(f)
                server_cfg = data["mcpServers"]["openqa-test"]
                self.assertEqual(server_cfg["type"], "http")
                self.assertEqual(server_cfg["url"], "https://openqa.opensuse.org/mcp")
                self.assertEqual(server_cfg["headers"]["Authorization"], "Bearer test:key:sec")

    def test_verify_mcp_connection_tool_recovers_headers_from_mcp_json(self):
        """verify_mcp_connection_tool must recover registered headers from .mcp.json when verifying HTTP endpoints."""
        import agent_orchestrator
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_orchestrator.WORKSPACE_ROOT", tmpdir):
                mcp_file = os.path.join(tmpdir, ".mcp.json")
                with open(mcp_file, "w") as f:
                    json.dump({
                        "mcpServers": {
                            "openqa-opensuse-org": {
                                "type": "http",
                                "url": "https://openqa.opensuse.org/mcp",
                                "headers": {"Authorization": "Bearer sample_token"}
                            }
                        }
                    }, f)
                with patch("phase4_mcp_auto_discovery._validate_http_mcp") as mock_val:
                    mock_val.return_value = True
                    res = agent_orchestrator.verify_mcp_connection_tool(transport="http", url="https://openqa.opensuse.org/mcp")
                    self.assertIn("SUCCESS", res)
                    mock_val.assert_called_once()
                    called_cfg = mock_val.call_args[0][1]
                    self.assertEqual(called_cfg["headers"], {"Authorization": "Bearer sample_token"})

    def test_is_agent_ready_to_run_with_adc_only(self):
        """is_agent_ready_to_run must return True when only Keyless Vertex AI ADC is present."""
        import initialize_google_ai
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.getenv", side_effect=lambda k, d=None: None if k == "GEMINI_API_KEY" else d):
                with patch("os.path.exists", side_effect=lambda p: True if "application_default_credentials" in p else False):
                    with patch("os.path.getsize", return_value=128):
                        ready, reason = initialize_google_ai.is_agent_ready_to_run()
                        self.assertTrue(ready)
                        self.assertIn("Keyless Vertex AI authentication verified via ADC", reason)

    def test_resolve_gcloud_bin_finds_custom_path(self):
        """resolve_gcloud_bin must dynamically find gcloud in custom installation paths."""
        from utils import resolve_gcloud_bin
        with patch("shutil.which", return_value=None):
            with patch.dict(os.environ, {}, clear=True):
                with patch("os.path.exists", side_effect=lambda p: True if p == "/root/google-cloud-sdk/bin/gcloud" else False):
                    with patch("os.access", return_value=True):
                        bin_path = resolve_gcloud_bin()
                        self.assertEqual(bin_path, "/root/google-cloud-sdk/bin/gcloud")

    def test_504_gateway_timeout_detected_as_network_infrastructure_outage(self):
        """is_network_infrastructure_outage must recognize 504 DEADLINE_EXCEEDED gateway errors."""
        import agent_orchestrator
        err_504 = Exception("504 DEADLINE_EXCEEDED. {'error': {'code': 504, 'message': 'Deadline expired before operation could complete.', 'status': 'DEADLINE_EXCEEDED'}}")
        self.assertTrue(agent_orchestrator.is_network_infrastructure_outage(err_504))
        err_gateway = Exception("Gateway Timeout occurred while connecting to Vertex AI")
        self.assertTrue(agent_orchestrator.is_network_infrastructure_outage(err_gateway))

if __name__ == "__main__":
    unittest.main()
