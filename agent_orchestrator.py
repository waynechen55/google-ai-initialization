import threading
_thread_local = threading.local()
current_target = None
import os
import sys
import json
import subprocess
import time
import threading
import re
import shutil
from urllib.parse import urlparse, parse_qs
from utils import (
    setup_logger, WORKSPACE_ROOT, get_process_timeout, detect_python_venv_bin,
    ensure_runner_available, auto_heal_missing_python_module, extract_script_dependencies,
    sanitize_stdio_script_content, make_config_portable, detect_system_ca_bundle, probe_target_tls,
    verify_upstream_credentials, match_target_to_registry_key, is_dry_run, cleanup_runtime_artifacts, register_temp_path, verify_prephase_disk_space, is_enospc_error, halt_on_enospc, verify_target_dns
)

logger = setup_logger("agent_orchestrator")
GCLOUD_BIN = os.getenv("GCLOUD_BIN_PATH") or "gcloud"
mcp_write_lock = threading.Lock()
file_write_lock = threading.Lock()

# ----------------------------------------------------------------------
# Resilient AFC Tool Dispatcher & GenAI Patch
# ----------------------------------------------------------------------

class ResilientFunctionMap(dict):
    """
    Indestructible Function Map for Google GenAI SDK.
    Prevents KeyError crashes when the model invokes internal code execution
    (e.g. 'google:python_interpreter') or hallucinated tool names.
    """
    def __missing__(self, key):
        clean_key = str(key).lower()
        if "python_interpreter" in clean_key or "interpreter" in clean_key:
            def _fallback_python_runner(*args, **kwargs):
                code = kwargs.get("code") or kwargs.get("script") or (args[0] if args else "")
                if not code:
                    return {"result": "Python interpreter available. Specify 'code' argument."}
                try:
                    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
                    out = proc.stdout or proc.stderr or "Executed successfully with exit code 0."
                    return {"result": out}
                except Exception as ex:
                    return {"error": f"Execution error: {ex}"}
            return _fallback_python_runner

        def _fallback_unmapped_tool(*args, **kwargs):
            return {
                "result": f"Notice: Tool '{key}' is unmapped. For remote service inspection, use 'execute_shell_command' with curl to inspect HTTP endpoints directly, or 'write_mcp_settings_tool' to register the server."
            }
        return _fallback_unmapped_tool

try:
    from google.genai import _extra_utils
    _orig_get_function_map = _extra_utils.get_function_map

    def _patched_get_function_map(*args, **kwargs):
        orig_map = _orig_get_function_map(*args, **kwargs)
        return ResilientFunctionMap(orig_map)

    _extra_utils.get_function_map = _patched_get_function_map
except Exception:
    pass

# ----------------------------------------------------------------------
# Define the Python Functions that will be exposed as Tools to Gemini
# ----------------------------------------------------------------------

def python_interpreter(code: str) -> str:
    """
    Executes Python code directly in a local subprocess and returns stdout and stderr.
    Use this to run small Python snippets, inspect modules, or test connectivity scripts.
    """
    logger.info(f"[Agent Tool] Executing python snippet: {code[:100]}...")
    try:
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        return proc.stdout or proc.stderr or "Executed successfully with exit code 0."
    except Exception as ex:
        return f"Python execution error: {ex}"

def execute_shell_command(cmd: str) -> str:
    """
    Executes a shell command on the local machine and returns the combined stdout and stderr.
    Use this to run gcloud config, enable services, verify version numbers, or run other terminal utilities.
    """
    logger.info(f"[Agent Tool] Executing shell command: {cmd}")

    # Registry Integrity Guard: Block destructive overwrite/truncation of .mcp.json via raw shell commands
    if re.search(r'(>\s*\.mcp\.json|open\s*\(\s*["\']\.mcp\.json["\']\s*,\s*["\']w["\'])', cmd):
        logger.warning(f"[Agent Tool Registry Guard] Blocked destructive shell overwrite of .mcp.json in command: {cmd}")
        return "FAILED: Direct shell overwrite or truncation of '.mcp.json' is strictly prohibited to preserve existing servers. Use 'write_mcp_settings_tool' instead, which safely merges server entries without data loss."

    # Turn Budget Guard: Block executing multi-minute test suites inside discovery turns
    if re.search(r'(python3?\s+run_tests\.py|python3?\s+-m\s+unittest(\s+discover)?\s*$)', cmd):
        logger.warning(f"[Agent Tool Guard] Intercepted long-running test runner command: {cmd}")
        return "NOTICE: Executing the entire multi-minute test suite ('run_tests.py') is disabled within discovery turns to prevent turn budget exhaustion. To verify a specific component or test, run a targeted unit test command instead."

    # Stdio MCP Server Guard: Block launching stdio MCP servers in shell without pipes (they block indefinitely waiting for stdin)
    cmd_lower = str(cmd or "").strip().lower()
    is_stdio_script = (
        ("python3" in cmd_lower or "python" in cmd_lower) and
        any(sig in cmd_lower for sig in ("_mcp_server.py", "_mcp_bridge.py", "_bridge.py", "_mcp.py")) and
        "|" not in cmd_lower and "<" not in cmd_lower and "echo" not in cmd_lower and "-c" not in cmd_lower
    )
    if is_stdio_script:
        logger.warning(f"[Agent Tool Guard] Blocked attempt to launch unpiped stdio MCP script in shell: {cmd}")
        return (
            "ERROR: Stdio Model Context Protocol (MCP) servers block indefinitely waiting for JSON-RPC input on standard input. "
            "Do NOT run MCP bridge scripts directly via execute_shell_command! "
            "To test and validate your MCP server, call 'verify_mcp_connection_tool(command=..., args=..., env=...)' instead."
        )

    try:
        res = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=get_process_timeout()
        )
        output = []
        if res.stdout:
            output.append(res.stdout)
        if res.stderr:
            output.append(res.stderr)
        
        result_str = "\n".join(output).strip()
        logger.info(f"[Agent Tool] Command finished with exit code {res.returncode}")
        
        # Safeguard: Truncate massive outputs to prevent context token overflows (API Limit Crashes)
        MAX_CHAR_LIMIT = 100000  # High-signal ceiling (~25,000 tokens)
        if len(result_str) > MAX_CHAR_LIMIT:
            truncated_len = len(result_str) - MAX_CHAR_LIMIT
            result_str = (
                result_str[:MAX_CHAR_LIMIT] + 
                f"\n\n... [TRUNCATED {truncated_len} CHARACTERS OF OUTPUT TO PREVENT CONTEXT WINDOW OVERFLOW] ..."
            )
            
        return f"Exit Code: {res.returncode}\nOutput:\n{result_str}"
    except Exception as e:
        logger.error(f"[Agent Tool] Failed to execute command: {e}")
        return f"EXECUTION_ERROR: {str(e)}"

def run_gcloud_auth_login_tool() -> str:
    """
    Executes 'gcloud auth login --no-launch-browser', extracts the OAuth URL,
    launches a headed Playwright browser to intercept the redirect authorization code,
    and completes the authentication. Returns success or failure.
    """
    logger.info("[Agent Tool] Initiating gcloud authentication tool...")
    
    # 1. Check if already authenticated
    try:
        res = subprocess.run(
            [GCLOUD_BIN, "auth", "list", "--format=value(account)"],
            capture_output=True, text=True, timeout=10
        )
        if res.stdout.strip():
            return f"SUCCESS: Already authenticated as active account: {res.stdout.strip()}"
    except Exception:
        pass

    # 2. Spawn gcloud login process
    try:
        process = subprocess.Popen(
            [GCLOUD_BIN, "auth", "login", "--no-launch-browser"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
    except Exception as e:
        return f"ERROR: Failed to launch gcloud process: {e}"

    # 3. Read stdout to capture Google OAuth URL
    import re
    oauth_url = ""
    for line in iter(process.stdout.readline, ""):
        match = re.search(r'(https://accounts\.google\.com/o/oauth2/auth\?\S+)', line)
        if match:
            oauth_url = match.group(1)
            break
        if "Enter authorization code:" in line:
            break

    if not oauth_url:
        process.kill()
        return "ERROR: Could not retrieve OAuth login URL from gcloud standard output."

    # 4. Launch Playwright
    auth_code = ""
    try:
        from playwright.sync_api import sync_playwright
        logger.info(f"[Agent Tool] Intercepting code with Playwright for URL: {oauth_url[:60]}...")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-infobars"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.goto(oauth_url)
            
            # Fill email if provided
            email = os.getenv("GCP_USER_EMAIL")
            password = os.getenv("GCP_USER_PASSWORD")
            if email:
                try:
                    page.wait_for_selector("input[type='email']", timeout=3000)
                    page.fill("input[type='email']", email)
                    page.click("#identifierNext")
                    if password:
                        page.wait_for_selector("input[type='password']", state="visible", timeout=3000)
                        page.fill("input[type='password']", password)
                        page.click("#passwordNext")
                except Exception:
                    pass
            
            # Intercept code from redirect
            start_time = time.time()
            while time.time() - start_time < 300: # 5 mins max
                current_url = page.url
                if "sdk.cloud.google.com/authcode.html" in current_url or "localhost" in current_url or "127.0.0.1" in current_url:
                    parsed_url = urlparse(current_url)
                    queries = parse_qs(parsed_url.query)
                    code_list = queries.get("code")
                    if code_list:
                        auth_code = code_list[0]
                        break
                page.wait_for_timeout(1000)
            browser.close()
    except Exception as e:
        logger.warning(f"[Agent Tool] Playwright headed browser launch failed: {e}. Falling back to manual input.")
        
    # Fallback to manual prompt
    if not auth_code:
        logger.info("[Agent Tool] Opening manual auth fallback. Please follow terminal instructions.")
        print(f"\n[MANUAL AUTHENTICATION REQUIRED]\nPlease open this URL in your browser to log in:\n\n{oauth_url}\n")
        try:
            auth_code = input("Enter the Google Cloud authorization code: ").strip()
        except KeyboardInterrupt:
            process.kill()
            return "ERROR: Manual credential collection cancelled by keyboard interrupt."

    if not auth_code:
        process.kill()
        return "ERROR: No authorization code was obtained."

    # 5. Send code back to gcloud
    try:
        stdout, stderr = process.communicate(input=f"{auth_code}\n", timeout=20)
        output = []
        if stdout: output.append(stdout)
        if stderr: output.append(stderr)
        out_str = "\n".join(output)
        
        if process.returncode == 0:
            return f"SUCCESS: gcloud login finished successfully!\n{out_str}"
        else:
            return f"ERROR: gcloud login failed with exit code {process.returncode}.\n{out_str}"
    except Exception as e:
        process.kill()
        return f"ERROR: Failed to complete token handshake with gcloud process: {e}"

def probe_native_remote_endpoint_tool(target: str) -> dict:
    """
    Autonomously probes a remote target for natively hosted Model Context Protocol (MCP) endpoints.
    Tests standard paths (/mcp, /v1/mcp, /sse, /mcp/sse, etc.) concurrently.
    Validates MIME headers, filters out HTML false positives, and tests JSON-RPC 2.0 handshakes.
    Returns a deterministic JSON object with the result.
    """
    logger.info(f"[Agent Tool] Probing native MCP endpoint for: {target}")
    import phase4_mcp_auto_discovery
    try:
        native = phase4_mcp_auto_discovery.probe_native_mcp_endpoint(target, log=logger)
        if native and ("command" in native or "url" in native or "httpUrl" in native or native.get("type") in ("http", "sse")):
            url_display = native.get("httpUrl") or native.get("url") or "local stdio"
            return {
                "native_detected": True,
                "config": native,
                "message": f"Active native MCP endpoint verified at {url_display}! Use this config natively (Tier 1)."
            }
        return {
            "native_detected": False,
            "due_diligence_proven": True,
            "message": "All candidate paths returned 404 or HTML web pages. No native MCP server exists. Cascade to Tier 2 or Tier 3."
        }
    except Exception as e:
        return {"native_detected": False, "error": str(e)}

def probe_openapi_spec_tool(target: str) -> dict:
    """
    Probes the target server for machine-readable OpenAPI/Swagger specifications (e.g. /openapi.json).
    If found, returns the ready-to-run @modelcontextprotocol/server-openapi configuration.
    """
    logger.info(f"[Agent Tool] Probing for OpenAPI specs on: {target}")
    from utils import probe_openapi_spec
    try:
        spec_url = probe_openapi_spec(target)
        if spec_url:
            return {
                "spec_found": True,
                "config": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-openapi", spec_url],
                    "env": {}
                },
                "message": f"OpenAPI spec discovered at {spec_url}. Use this config to auto-instantiate the tools."
            }
        return {"spec_found": False, "message": "No OpenAPI/Swagger specs discovered."}
    except Exception as e:
        return {"spec_found": False, "error": str(e)}

def search_package_registries_tool(keyword: str) -> dict:
    """
    Queries public package registries (NPM and PyPI) via their JSON APIs to discover
    newly published community MCP servers for the given keyword.
    Returns a dict with package configuration if found, or empty if not.
    """
    logger.info(f"[Agent Tool] Searching package registries for keyword: {keyword}")
    from utils import query_package_registries
    try:
        pkg = query_package_registries(keyword)
        if pkg:
            return {"package_found": True, "config": pkg}
        return {"package_found": False, "message": f"No MCP packages found for '{keyword}' on npm or PyPI."}
    except Exception as e:
        return {"package_found": False, "error": str(e)}

def list_local_bridges_tool(target_hint: str = "") -> dict:
    """
    Scans the bridges/ directory for custom MCP Python scripts, parses them via AST to
    extract required environment variables, and scores them based on domain keywords.
    Returns a structured dict with a 'best_match' recommendation and 'available_bridges'.
    """
    logger.info(f"[Agent Tool] Introspecting local bridge catalog for hint: {target_hint}")
    from utils import introspect_local_bridges, WORKSPACE_ROOT
    try:
        catalog = introspect_local_bridges(WORKSPACE_ROOT, log=logger)
        best_match = None
        best_score = 0
        
        t_domain = target_hint.lower().replace("https://", "").replace("http://", "").split("/")[0]
        
        for c in catalog:
            score = 0
            b_name = c["script_name"].lower()
            if target_hint:
                for part in t_domain.split("."):
                    if part and part in b_name and part not in ("com", "org", "net", "de", "co", "suse", "opensuse", "google"):
                        score += 3
                if t_domain in c.get("content_lower", ""):
                    score += 2
            
            c.pop("content_lower", None)
            
            if score > best_score:
                best_score = score
                best_match = {
                    "script_path": c["script_path"],
                    "confidence": score,
                    "recommended_config": {
                        "command": "python3",
                        "args": [c["script_path"]],
                        "env": {k: "" for k in c["env_keys"]}
                    }
                }
                
        return {"best_match": best_match, "available_bridges": catalog}
    except Exception as e:
        return {"error": str(e)}

def inspect_remote_api_endpoints_tool(target: str) -> dict:
    """
    Probes common REST API endpoints on a novel target service (/api, /api/v1, /health) 
    to extract active routes and sample JSON structures. This data should inform custom bridge tool generation.
    """
    logger.info(f"[Agent Tool] Snooping REST routes on: {target}")
    import urllib.request
    import ssl
    import json
    ctx = ssl._create_unverified_context()
    base_url = target if target.startswith("http") else f"https://{target}"
    base_url = base_url.rstrip("/")
    
    paths_to_test = ["/api", "/api/v1", "/api/v2", "/rest", "/rest/api/2", "/health", "/status", "/api/status", "/api/v1/status", "/info", "/version"]
    discovered = []
    
    def check_route(path):
        try:
            req = urllib.request.Request(f"{base_url}{path}", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, context=ctx, timeout=2.0) as resp:
                if resp.status == 200:
                    data = resp.read().decode("utf-8")
                    try:
                        parsed = json.loads(data)
                        return {"path": path, "sample_keys": list(parsed.keys())[:5]}
                    except json.JSONDecodeError:
                        return {"path": path, "type": "non-json (html/text)"}
        except Exception:
            return None
            
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(check_route, p) for p in paths_to_test]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            if res: discovered.append(res)
            
    if discovered:
        return {"active_routes": discovered, "message": "Use these routes to implement real functions in your custom bridge."}
    return {"message": "No standard REST API routes discovered. You may need to guess based on standard conventions or check documentation."}

def generate_mcp_bridge_template_tool(service_name: str, target_url: str) -> dict:
    """
    Generates a production-grade, zero-dependency Python stdio bridge template for a novel service.
    Outputs the file to bridges/<service_name>_mcp_server.py.
    The template includes valid JSON-RPC 2.0 loops, Record Envelopes, and unbuffered stdout.
    """
    logger.info(f"[Agent Tool] Generating MCP bridge template for {service_name} at {target_url}")
    from utils import WORKSPACE_ROOT
    import os
    
    bridge_dir = os.path.join(WORKSPACE_ROOT, "bridges")
    os.makedirs(bridge_dir, exist_ok=True)
    
    clean_name = "".join([c if c.isalnum() else "_" for c in service_name.lower()])
    file_path = os.path.join(bridge_dir, f"{clean_name}_mcp_server.py")
    
    template = f'''#!/usr/bin/env python3
"""
Custom MCP Server Bridge for {service_name}.
Target: {target_url}
"""

import sys
import json
import urllib.request

def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
            
        try:
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            
            if method == "initialize":
                resp = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "protocolVersion": "2024-11-05",
                        "capabilities": {{"tools": {{}}}},
                        "serverInfo": {{"name": "{clean_name}", "version": "1.0.0"}}
                    }}
                }}
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                resp = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "tools": [
                            {{
                                "name": "example_tool",
                                "description": "Example tool for {service_name}",
                                "inputSchema": {{
                                    "type": "object",
                                    "properties": {{}},
                                    "required": []
                                }}
                            }}
                        ]
                    }}
                }}
            elif method == "tools/call":
                # Ensure we return a Record Envelope!
                resp = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "content": [{{
                            "type": "text",
                            "text": json.dumps({{"count": 0, "items": []}})
                        }}]
                    }}
                }}
            else:
                resp = {{"jsonrpc": "2.0", "id": req_id, "error": {{"code": -32601, "message": "Method not found"}}}}
                
            if req_id is not None:
                sys.stdout.write(json.dumps(resp) + "\\n")
                sys.stdout.flush()
                
        except Exception as e:
            if "req_id" in locals() and req_id is not None:
                err_resp = {{"jsonrpc": "2.0", "id": req_id, "error": {{"code": -32603, "message": str(e)}}}}
                sys.stdout.write(json.dumps(err_resp) + "\\n")
                sys.stdout.flush()

if __name__ == "__main__":
    main()
'''
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(template)
        os.chmod(file_path, 0o755)
        return {
            "success": True, 
            "file_path": file_path, 
            "message": f"Template generated at {file_path}. Please use 'execute_shell_command' to modify it with sed or 'write_local_file_tool' if you need to rewrite specific parts."
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

def write_mcp_settings_tool(server_name: str, command: str = "", args: list = None, env: dict = None, url: str = "", headers: dict = None, transport: str = "stdio", instructions: str = "") -> str:
    instructions = str(instructions or "")
    if is_dry_run():
        logger.info(f"🔍 [DRY-RUN] Simulating write_mcp_settings_tool for '{server_name}' (skipped file write).")
        return f"Simulated configuration write for '{server_name}' (DRY-RUN mode active - no file modified)."
    """
    Writes or updates an MCP server configuration inside '.mcp.json' at the workspace root (Thread-safe).
    Supports both local process transports (stdio) and remote native streamable transports (http / sse).
    """
    # Defensive typing: Ensure args is a list of strings, and env is a dictionary
    if isinstance(args, str):
        try:
            parsed_args = json.loads(args)
            args = parsed_args if isinstance(parsed_args, list) else [args]
        except Exception:
            args = [args]
    elif not isinstance(args, list):
        args = [str(args)] if args is not None else []
    args = [str(a) for a in args]

    if isinstance(env, str):
        try:
            parsed_env = json.loads(env)
            env = parsed_env if isinstance(parsed_env, dict) else {}
        except Exception:
            env = {}
    elif not isinstance(env, dict):
        env = {}

    logger.info(f"[Agent Tool] Writing MCP config for '{server_name}'...")
    workspace_root = WORKSPACE_ROOT
    settings_path = os.path.join(workspace_root, ".mcp.json")
    
    with mcp_write_lock:
        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
            except Exception as e:
                logger.warning(f"Could not parse existing settings. Overwriting: {e}")
                
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}
            
        # Enforce duplicate checking programmatically
        try:
            duplicates = []
            for name, cfg in settings.get("mcpServers", {}).items():
                if name != server_name:
                    if server_name.replace("-", "") in name.replace("-", "") or name.replace("-", "") in server_name.replace("-", ""):
                        duplicates.append(name)
            if duplicates:
                logger.warning(f"⚠️  [Agent Tool DUPLICATE WARNING] Overlapping registries already exist in .mcp.json: {duplicates}")
        except Exception:
            pass

        # Native HTTP Transport Configuration
        if transport in ("http", "sse") or (url and not command):
            server_entry = {
                "type": transport if transport in ("http", "sse") else "http",
                "httpUrl": url,
                "url": url
            }
            if headers and isinstance(headers, dict):
                server_entry["headers"] = headers
            settings["mcpServers"][server_name] = server_entry
            if instructions:
                logger.info(f"📋 [SETUP INSTRUCTIONS for {server_name}]: {instructions}")
            try:
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(settings, f, indent=2)
                global_settings_path = os.path.expanduser("~/.gemini/settings.json")
                if os.path.exists(global_settings_path):
                    try:
                        with open(global_settings_path, "r", encoding="utf-8") as gf:
                            global_settings = json.load(gf)
                        if "mcpServers" not in global_settings:
                            global_settings["mcpServers"] = {}
                        global_settings["mcpServers"][server_name] = settings["mcpServers"][server_name]
                        with open(global_settings_path, "w", encoding="utf-8") as gf:
                            json.dump(global_settings, gf, indent=2)
                    except Exception:
                        pass
                return f"SUCCESS: Written native HTTP config to {settings_path}."
            except Exception as e:
                return f"ERROR: Failed to write native HTTP config: {e}"

        # Stdio Process Transport Configuration
        domain = server_name.replace("-", ".")
        target_referenced = False
        args_str = " ".join(args)
        if domain in args_str:
            target_referenced = True
        else:
            for env_val in env.values():
                if domain in str(env_val):
                    target_referenced = True
                    break
                    
        if not target_referenced:
            # Check if encapsulated in local scripts
            for arg in args:
                if arg.endswith(".py") or arg.endswith(".js"):
                    script_path = os.path.join(WORKSPACE_ROOT, arg) if not os.path.isabs(arg) else arg
                    if os.path.exists(script_path):
                        try:
                            with open(script_path, "r", encoding="utf-8") as f:
                                content = f.read()
                                if domain in content or domain.replace(".", "_") in content:
                                    target_referenced = True
                                    logger.info(f"🛡️  [Agent Tool TRACEABILITY] Verified: FQDN '{domain}' is active and ENCAPSULATED inside script code: '{arg}'.")
                                    break
                        except Exception:
                            pass
                            
        if not target_referenced:
            # Case D: Check shared registrable domain / organization relationship
            def get_registrable_domain(val: str) -> str:
                val_clean = val.lower().replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
                parts = val_clean.split(".")
                if len(parts) >= 3 and parts[-2] in ["co", "com", "org", "net", "gov", "edu", "ac", "sch"]:
                    return ".".join(parts[-3:])
                if len(parts) >= 2:
                    return ".".join(parts[-2:])
                return val_clean

            reg_domain = get_registrable_domain(domain)
            if reg_domain and reg_domain != "":
                if reg_domain in args_str:
                    target_referenced = True
                    logger.info(f"🛡️  [Agent Tool TRACEABILITY] Verified: Config shares registrable domain/organization '{reg_domain}' in arguments (Case D).")
                else:
                    for env_val in env.values():
                        if reg_domain in str(env_val):
                            target_referenced = True
                            logger.info(f"🛡️  [Agent Tool TRACEABILITY] Verified: Config shares registrable domain/organization '{reg_domain}' in environment variable (Case D).")
                            break
                    if not target_referenced:
                        for arg in args:
                            if arg.endswith(".py") or arg.endswith(".js"):
                                script_path = os.path.join(WORKSPACE_ROOT, arg) if not os.path.isabs(arg) else arg
                                if os.path.exists(script_path):
                                    try:
                                        with open(script_path, "r", encoding="utf-8") as f:
                                            content = f.read()
                                            if reg_domain in content or reg_domain.replace(".", "_") in content:
                                                target_referenced = True
                                                logger.info(f"🛡️  [Agent Tool TRACEABILITY] Verified: Registrable domain '{reg_domain}' is active and ENCAPSULATED inside script code: '{arg}' (Case D).")
                                                break
                                    except Exception:
                                        pass
                            
        if not target_referenced:
            logger.warning(f"⚠️  [Agent Tool TRACEABILITY WARNING] Config written for '{server_name}' has no verified active/encapsulated references to target domain '{domain}'!")

        # Centralized Portability, Host System CA Bundle Discovery, and Enterprise TLS Probing
        base_cfg = {
            "command": command,
            "args": args,
            "env": env if isinstance(env, dict) else {},
            "instructions": instructions
        }
        active_target = getattr(_thread_local, "current_target", None) or globals().get("current_target")
        target_hint = active_target if active_target else server_name
        portable_cfg = make_config_portable(base_cfg, target_hint=target_hint)
        
        command = portable_cfg.get("command", command)
        args = portable_cfg.get("args", args)
        env = portable_cfg.get("env", env)
        if isinstance(env, dict) and "instructions" in env:
            del env["instructions"]

        settings["mcpServers"][server_name] = {
            "command": command,
            "args": args,
            "env": env
        }
        
        if instructions:
            logger.info(f"📋 [SETUP INSTRUCTIONS for {server_name}]: {instructions}")
        
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)

            # Synchronize with global Gemini CLI settings if present (~/.gemini/settings.json)
            global_settings_path = os.path.expanduser("~/.gemini/settings.json")
            if os.path.exists(global_settings_path):
                try:
                    with open(global_settings_path, "r", encoding="utf-8") as gf:
                        global_settings = json.load(gf)
                    if "mcpServers" not in global_settings:
                        global_settings["mcpServers"] = {}
                    global_settings["mcpServers"][server_name] = settings["mcpServers"][server_name]
                    with open(global_settings_path, "w", encoding="utf-8") as gf:
                        json.dump(global_settings, gf, indent=2)
                    logger.info(f"Synchronized server '{server_name}' to global settings: {global_settings_path}")
                except Exception as ge:
                    logger.warning(f"Could not update global settings: {ge}")

            return f"SUCCESS: Written config to {settings_path}."
        except Exception as e:
            return f"ERROR: Failed to write config: {e}"

def write_local_file_tool(path: str, content: str) -> str:
    """
    Writes or creates a local file inside the workspace with the specified content.
    Use this to generate custom Python MCP bridge scripts, shell wrappers, or template files in the workspace.
    """
    logger.info(f"[Agent Tool] Writing local file: {path}...")
    try:
        # Active Guardrail: Strictly prevent writing Language Server Protocol (LSP) code instead of Model Context Protocol (MCP)
        lsp_signatures = ("textdocumentsync", "completionprovider", "documentsymbol", "textdocument/didopen", "textdocument/didchange")
        content_lower = str(content or "").lower()
        if any(sig in content_lower for sig in lsp_signatures):
            logger.warning(f"⚠️ [Agent Tool GUARD] Blocked attempt to write LSP code into '{path}'.")
            return (
                "ERROR: File write rejected by safety guardrail! You generated Language Server Protocol (LSP) "
                "capabilities ('textDocumentSync'). MCP servers must declare capabilities: {'tools': {}} "
                "and respond to JSON-RPC method 'tools/list' and 'tools/call'. They MUST NOT implement LSP methods. "
                "Please fix your server code according to the Model Context Protocol (MCP) specifications."
            )

        from utils import bridge_io_lock

        # Generalized auto-sanitization: prevent Python stdio buffering trap and unbounded socket timeouts
        if path.endswith(".py"):
            content = sanitize_stdio_script_content(content)
            
        with bridge_io_lock:
            # Strict schema sanitization & mandatory read-and-merge if writing .mcp.json directly: prevent server loss
            if os.path.basename(path) == ".mcp.json":
                try:
                    incoming_data = json.loads(content)
                    abs_mcp_path = os.path.join(WORKSPACE_ROOT, path)
                    existing_data = {}
                    if os.path.exists(abs_mcp_path):
                        try:
                            with open(abs_mcp_path, "r", encoding="utf-8") as ef:
                                existing_data = json.load(ef)
                        except Exception:
                            existing_data = {}
                    if not isinstance(existing_data, dict) or "mcpServers" not in existing_data:
                        existing_data = {"mcpServers": {}}

                    if isinstance(incoming_data, dict) and "mcpServers" in incoming_data:
                        for s_name, s_cfg in list(incoming_data["mcpServers"].items()):
                            portable = make_config_portable(s_cfg, target_hint=s_name)
                            if "instructions" in portable:
                                del portable["instructions"]
                            if "_instructions" in portable:
                                del portable["_instructions"]
                            existing_data["mcpServers"][s_name] = {
                                "command": portable.get("command"),
                                "args": portable.get("args", []),
                                "env": portable.get("env", {})
                            }
                        content = json.dumps(existing_data, indent=2)

                        # Synchronize with global Gemini CLI settings if present
                        global_settings_path = os.path.expanduser("~/.gemini/settings.json")
                        if os.path.exists(global_settings_path):
                            try:
                                with open(global_settings_path, "r", encoding="utf-8") as gf:
                                    global_settings = json.load(gf)
                                if "mcpServers" not in global_settings:
                                    global_settings["mcpServers"] = {}
                                global_settings["mcpServers"].update(existing_data["mcpServers"])
                                with open(global_settings_path, "w", encoding="utf-8") as gf:
                                    json.dump(global_settings, gf, indent=2)
                            except Exception as ge:
                                logger.warning(f"Could not update global settings: {ge}")
                except Exception as parse_err:
                    logger.warning(f"[Agent Tool] Failed to auto-sanitize .mcp.json: {parse_err}")

            abs_path = os.path.join(WORKSPACE_ROOT, path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"SUCCESS: Local file written successfully to {abs_path}."
    except Exception as e:
        return f"ERROR: Failed to write file: {e}"

def verify_mcp_connection_tool(command: str = "", args: list = None, env: dict = None, url: str = "", headers: dict = None, transport: str = "stdio", _retry_attempt: bool = False) -> str:
    """
    Polymorphic connection verification tool: supports both local process stdio validation
    and remote native Streamable HTTP validation.
    Performs a strict double-handshake JSON-RPC 2.0 validation (requests 'initialize', then requests 'tools/list').
    """
    # Transport Branch 1: Native Streamable HTTP
    if transport in ("http", "sse") or (url and not command):
        logger.info(f"[Agent Tool] Testing Native HTTP MCP connection for URL: {url}...")
        import phase4_mcp_auto_discovery
        server_name = "native_http_server"
        resolved_headers = headers or {}
        # Recover server_name and headers from .mcp.json if available
        settings_path = os.path.join(WORKSPACE_ROOT, ".mcp.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                for s_name, s_cfg in cfg_data.get("mcpServers", {}).items():
                    if isinstance(s_cfg, dict) and (s_cfg.get("url") == url or s_cfg.get("httpUrl") == url):
                        server_name = s_name
                        if not resolved_headers and "headers" in s_cfg:
                            resolved_headers = s_cfg["headers"]
                        break
            except Exception:
                pass
        ok = phase4_mcp_auto_discovery._validate_http_mcp(server_name, {"url": url, "headers": resolved_headers}, env or {}, log=logger)
        if ok:
            return f"SUCCESS: Native HTTP MCP endpoint verified for {url}! Server configuration is valid and active in .mcp.json. You may conclude discovery with report_final_verdict_tool."
        return f"FAILED: Native HTTP MCP handshake failed for {url}. Check URL, headers, and API keys."

    # Transport Branch 2: Standard local process (stdio)
    # Defensive typing: Ensure args is a list of strings, and env is a dictionary
    if isinstance(args, str):
        try:
            parsed_args = json.loads(args)
            args = parsed_args if isinstance(parsed_args, list) else [args]
        except Exception:
            args = [args]
    elif not isinstance(args, list):
        args = [str(args)] if args is not None else []
    args = [str(a) for a in args]

    if isinstance(env, str):
        try:
            parsed_env = json.loads(env)
            env = parsed_env if isinstance(parsed_env, dict) else {}
        except Exception:
            env = {}
    elif not isinstance(env, dict):
        env = {}

    logger.info(f"[Agent Tool] Testing MCP connection for command: {command}...")
    full_env = os.environ.copy()
    full_env.update(env)
    
    # Pre-check runner availability
    if command and not shutil.which(command):
        ensure_runner_available(command, log=logger)
        
    try:
        process = subprocess.Popen(
            [command] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKSPACE_ROOT,
            env=full_env,
            text=True,
            bufsize=1
        )
    except FileNotFoundError as fnf_err:
        if not _retry_attempt and ensure_runner_available(command, log=logger):
            logger.info(f"[Agent Tool] JIT resolved runner '{command}'. Retrying connection test...")
            return verify_mcp_connection_tool(command, args, env, _retry_attempt=True)
        return f"FAILED: Command runner '{command}' not found on host machine: {fnf_err}"
    except Exception as spawn_err:
        return f"FAILED: Could not spawn server command '{command}': {spawn_err}"

    try:
        # 1. Send MCP initialize request
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "google-ai-initializer-client",
                    "version": "1.0.0"
                }
            }
        }
        
        process.stdin.write(json.dumps(init_request) + "\n")
        process.stdin.flush()
        
        import select
        response_line = ""
        
        try:
            timeout = 15
            start_time = time.time()
            # Read line-by-line to gracefully skip non-JSON startup banners
            while time.time() - start_time < timeout:
                ready = select.select([process.stdout], [], [], 1.0)
                if ready[0]:
                    line = process.stdout.readline()
                    if not line:
                        break
                    line_stripped = line.strip()
                    if line_stripped.startswith("{"):
                        try:
                            json.loads(line_stripped)
                            response_line = line
                            logger.info(f"[Agent Tool] Handshake JSON-RPC detected! Scanned: {line_stripped[:120]}...")
                            break
                        except Exception:
                            pass
                    logger.info(f"   [Agent Tool: Skipping raw non-JSON log] {line_stripped[:100]}")
                else:
                    if time.time() - start_time >= timeout:
                        break
        except Exception as read_err:
            logger.error(f"[Agent Tool] Error during pipe read: {read_err}")
            
        if not response_line:
            process.kill()
            process.wait()
            # Capture and report stderr logs for real-time troubleshooting
            stderr_logs = process.stderr.read()
            
            # Check for ModuleNotFoundError to attempt JIT recovery
            if not _retry_attempt and "ModuleNotFoundError" in stderr_logs:
                match = re.search(r"No module named ['\"]([^'\"]+)['\"]", stderr_logs)
                if match:
                    missing_mod = match.group(1)
                    logger.info(f"[Agent Tool] ⚡ Auto-detected missing Python module '{missing_mod}'. Attempting JIT auto-install...")
                    if auto_heal_missing_python_module(missing_mod, log=logger):
                        logger.info(f"[Agent Tool] Retrying connection check for '{command}' after module installation...")
                        return verify_mcp_connection_tool(command, args, env, _retry_attempt=True)

            err_msg = (
                "FAILED: No initialization response captured. Server might have crashed or hung.\n"
                "Captured Subprocess Stderr logs:\n"
                "------------------------------------------------------\n"
                f"{stderr_logs if stderr_logs.strip() else '(Stderr was empty)'}\n"
                "------------------------------------------------------"
            )
            
            # Actionable Diagnostic Enhancements
            if "401" in stderr_logs or "unauthorized" in stderr_logs.lower():
                err_msg += "\nDIAGNOSIS: Local process initialized, but upstream rejected credentials. Ensure required API tokens are mapped in 'env'."
            elif "unrecognized argument" in stderr_logs.lower() or "required argument" in stderr_logs.lower():
                err_msg += "\nDIAGNOSIS: CLI parameter mismatch. Check command line args."
            elif "module" in stderr_logs.lower() and "not found" in stderr_logs.lower():
                err_msg += "\nDIAGNOSIS: Missing Python module. Check if it requires a pip install or 'uvx' execution."
            
            return err_msg
            
        try:
            res_json = json.loads(response_line)
            if "error" in res_json:
                process.kill()
                process.wait()
                return f"FAILED: Server returned initialization error: {res_json['error']}"
        except Exception as e:
            process.kill()
            process.wait()
            return f"FAILED: Could not parse initialization JSON response: {e}. Raw: {response_line}"
            
        # 2. Send 'initialized' notification (required by protocol)
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.flush()
        
        # 3. Send 'tools/list' request to catch deep schema errors!
        tools_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }
        process.stdin.write(json.dumps(tools_request) + "\n")
        process.stdin.flush()
        
        tools_json = None
        raw_tools_line = ""
        deadline = time.time() + 10.0
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            ready_tools, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready_tools:
                break
            line = process.stdout.readline()
            if not line:
                break
            line_str = line.strip()
            raw_tools_line = line_str
            if not line_str.startswith("{") or not line_str.endswith("}"):
                continue
            try:
                msg = json.loads(line_str)
                if not isinstance(msg, dict):
                    continue
                # Correlate directly to tools_request id (id == 2)
                if msg.get("id") == 2:
                    tools_json = msg
                    break
                # Safely ignore notifications or unrecognized notification errors
                if msg.get("id") is None and "notifications" in str(msg.get("error", {})):
                    continue
            except Exception:
                continue
        
        if not tools_json:
            process.kill()
            process.wait()
            err_msg = "FAILED: Server successfully initialized, but hung/crashed when asked to list tools (tools/list).\n"
            stderr_logs = process.stderr.read() if process.stderr else ""
            if "expected record, received array" in str(stderr_logs).lower() or "inputschema" in str(stderr_logs).lower():
                err_msg += "DIAGNOSIS: Schema Validation Error detected in server stderr. Ensure all tool inputSchemas are records, not bare arrays."
            return err_msg
            
        try:
            if "error" in tools_json:
                process.kill()
                process.wait()
                return f"FAILED: Server initialized, but tools/list returned an error: {json.dumps(tools_json['error'])}"
            elif "result" in tools_json and "tools" in tools_json["result"]:
                tools_list = tools_json["result"]["tools"]
                
                # Level 3 Deep Functional Smoke Testing BEFORE killing process
                try:
                    import utils
                    dry_ok, dry_msg = utils.perform_level3_functional_smoke_test(process, tools_list)
                    if not dry_ok:
                        process.kill()
                        process.wait()
                        return f"DEGRADED: Connection successful, but Level 3 Functional Smoke Test failed!\n{dry_msg}\n\nTools available: {len(tools_list)}"
                except Exception as e:
                    logger.warning(f"Smoke test skipped: {e}")
                    
                process.kill()
                process.wait()
                if not tools_list:
                    cmd_str = str(command or "").lower() + " " + " ".join(str(a) for a in (args or []))
                    env_dict = env if isinstance(env, dict) else {}
                    requires_auth = (
                        "atlassian" in cmd_str or
                        any("TOKEN" in k.upper() or "KEY" in k.upper() or "SECRET" in k.upper() for k in env_dict.keys()) or
                        any("<OPTIONAL_" in str(v) for v in env_dict.values()) or
                        any(v == "" for v in env_dict.values())
                    )
                    if requires_auth:
                        logger.info(f"ℹ️  [DEFERRED TOOLS] Server '{server_name}' initialized successfully with valid JSON-RPC protocol. Tools will populate once authentication credentials are provided in .env.")
                        return f"SUCCESS: MCP server initialized successfully via standard stdio JSON-RPC protocol! (Note: Tools list is currently deferred/empty pending user authentication token in .env)."
                    return "FAILED: Server successfully initialized, but tools/list returned an empty tools array (0 tools). An operational MCP server must expose at least one valid tool."
                # Strict Schema Validation Check!
                for tool in tools_list:
                    input_schema = tool.get("inputSchema", {})
                    if input_schema.get("type") != "object":
                        return f"FAILED: Strict Schema Validation Error in tool '{tool.get('name', 'unknown')}': inputSchema.type MUST be 'object' (Current value: {input_schema.get('type')}). The server package is broken and incompatible with standard clients."

                result_msg = f"SUCCESS: Handshake and tools/list completed! Server returned {len(tools_list)} valid, strictly-formatted tools."

                # Active Level 2 Upstream Credential Verification
                target_candidate = ""
                for v in env.values():
                    if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
                        target_candidate = v
                        break
                if not target_candidate:
                    for a in args:
                        if isinstance(a, str) and (a.startswith("http://") or a.startswith("https://")):
                            target_candidate = a
                            break

                upstream_check = verify_upstream_credentials(target_candidate, full_env)
                if upstream_check.get("probed"):
                    if upstream_check.get("valid"):
                        logger.info(f"🔑 [UPSTREAM AUTH VERIFIED] Live remote service accepted credentials: {upstream_check['message']}")
                    else:
                        logger.warning(f"⚠️  [UPSTREAM AUTH WARNING] Process started, but live remote service rejected credentials: {upstream_check['message']}")
                        result_msg += f" (Note: Upstream authentication check reported: {upstream_check['message']})"

                return result_msg
            else:
                resp_str = json.dumps(tools_json) if tools_json else raw_tools_line
                return f"FAILED: Unexpected tools/list response format: {resp_str}"
        except Exception as e:
            resp_str = json.dumps(tools_json) if tools_json else raw_tools_line
            return f"FAILED: Could not parse tools/list JSON response: {e}. Raw: {resp_str}"
            
    except Exception as e:
        return f"ERROR: Failed to spawn or verify MCP server: {e}"

def report_final_verdict_tool(verdict_text: str) -> str:
    """
    You MUST call this tool as your absolute final action before finishing.
    Pass a detailed summary of what you did, whether you invented scripts, and end it with 'SUCCESS' if completed.
    """
    logger.info(f"[Agent Tool] Reporting Final Verdict...")
    # We prefix it with a special tag so we can parse it reliably in the main loop
    return f"FINAL_VERDICT_LOGGED: {verdict_text}"

# ----------------------------------------------------------------------
# AI Orchestrator Execution Loop
# ----------------------------------------------------------------------

def verify_genai_client(client, log=None) -> bool:
    """Verifies that a GenAI client instance can successfully complete a cheap 1-token ping using the customizable target model with adaptive fallback and retries."""
    dest_log = log or logger
    primary_model = os.getenv("GOOGLE_GENAI_MODEL", "gemini-3.7-flash")
    candidate_models = [primary_model]
    for fallback in ["gemini-2.5-flash"]:
        if fallback not in candidate_models:
            candidate_models.append(fallback)
            
    last_err = None
    for retry_attempt in range(3):
        for model_name in candidate_models:
            try:
                from google.genai import types
                # Generate exactly 1 token to keep cost and latency virtually zero
                client.models.generate_content(
                    model=model_name,
                    contents="ping",
                    config=types.GenerateContentConfig(max_output_tokens=1)
                )
                if model_name != primary_model:
                    dest_log.info(f"  | 🔄 [MODEL ADAPTED] Primary model '{primary_model}' unavailable; dynamically adapted to '{model_name}'.")
                    os.environ["GOOGLE_GENAI_MODEL"] = model_name
                return True
            except Exception as e:
                err_str = str(e)
                last_err = e
                # If model is 404 not found in region, skip immediately
                if "404" in err_str or "not_found" in err_str.lower():
                    continue
                dest_log.debug(f"Model ping rejected for '{model_name}': {e}")
        if retry_attempt < 2:
            time.sleep(2.0)
            
    dest_log.warning(f"  | ⚠️  [PING REJECTED] Client ping failed for all candidate models ({', '.join(candidate_models)}). Last error: {last_err}")
    return False


def is_transient_agent_error(err) -> bool:
    """Detects transient network, socket, API rate limit, and server disconnect errors."""
    err_str = str(err).lower()
    triggers = [
        "disconnected",
        "server disconnected",
        "remote end closed",
        "connection closed",
        "connection reset",
        "broken pipe",
        "econnreset",
        "temporary failure",
        "name resolution",
        "timeout",
        "timed out",
        "resource exhausted",
        "rate limit",
        "quota",
        "python_interpreter",
        "unexpected eof",
        "unexpected_eof",
        "500",
        "502",
        "503",
        "504",
        "429",
        "overloaded"
    ]
    return any(k in err_str for k in triggers)


def is_network_infrastructure_outage(err) -> bool:
    """Detects low-level host transport and DNS network dropouts (e.g. Wi-Fi drops, DNS outages)."""
    err_str = str(err).lower()
    net_triggers = [
        "temporary failure in name resolution",
        "[errno -3]",
        "read operation timed out",
        "read timed out",
        "server disconnected without sending a response",
        "connection reset by peer",
        "remotedisconnected",
        "connection refused",
        "network is unreachable"
    ]
    return any(k in err_str for k in net_triggers)

def is_target_configured_in_mcp(target: str) -> bool:
    """Verifies whether the target is already successfully configured and present in .mcp.json."""
    mcp_path = os.path.join(WORKSPACE_ROOT, ".mcp.json")
    if os.path.exists(mcp_path):
        try:
            with open(mcp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get("mcpServers", {})
            matched_key, _ = match_target_to_registry_key(target, servers)
            return bool(matched_key)
        except Exception:
            pass
    return False

def run_agentic_initialization(project_id: str, target_mcp_service: str, log=None) -> bool:
    """
    Initializes Gemini Client, binds local tools, and runs the autonomous 
    AI Agent loop to complete authentication, configuration, discovery, and verification.
    Performs a robust, sequential verification and failover: Key-based -> Keyless GCloud ADC.
    """
    global current_target
    current_target = target_mcp_service
    _thread_local.current_target = target_mcp_service

    # Shadow the logger locally for thread-safe buffered logging
    logger = log if log is not None else globals()['logger']

    if not verify_prephase_disk_space("Phase 4: Agent Orchestrator", log=logger):
        return False

    if not verify_target_dns(target_mcp_service):
        logger.warning(f"⚠️  [DNS WARNING] Pre-flight DNS resolution for target domain '{target_mcp_service}' was slow or unresolvable via local DNS. Proceeding with agent discovery anyway...")
    
    try:
        from google import genai
        from google.genai import types
        from google.genai.types import HttpOptions
    except ImportError:
        logger.error("google-genai Python SDK is not installed.")
        return False

    api_key = os.getenv("GEMINI_API_KEY")
    client = None
    auth_method = None
    
    # 1. Cascade Step 1: Verify Key-Based Auth (if key is present in environment)
    if api_key:
        logger.info("------------------------------------------------------")
        logger.info("🔑 [AUTH CHECK] Verifying active GEMINI_API_KEY credentials...")
        logger.info("------------------------------------------------------")
        try:
            test_client = genai.Client(api_key=api_key, http_options=HttpOptions(api_version="v1", timeout=180000.0))
            if verify_genai_client(test_client, log=logger):
                client = test_client
                auth_method = "Key-based (GEMINI_API_KEY)"
                logger.info("- Status: GEMINI_API_KEY successfully verified as active and valid!")
            else:
                logger.warning("⚠️  [WARNING] Configured GEMINI_API_KEY is present but rejected as invalid by Google.")
        except Exception as err:
            logger.warning(f"⚠️  [WARNING] Failed to instantiate key-based client: {err}")
            
    # 2. Cascade Step 2: Try Keyless Auth fallback (using local GCloud ADC / Vertex AI)
    if not client:
        logger.info("------------------------------------------------------")
        logger.info("⚡ [AUTH CHECK] Attempting keyless GCloud Credentials (ADC) fallback...")
        logger.info("------------------------------------------------------")
        try:
            # Resolve active GCP project and location
            target_proj = project_id or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT")
            target_loc = os.getenv("GOOGLE_CLOUD_LOCATION") or "global"
            
            # Temporarily isolate environment from all lingering API keys so SDK routes cleanly to Vertex AI ADC
            saved_gemini_key = os.environ.pop("GEMINI_API_KEY", None)
            saved_google_key = os.environ.pop("GOOGLE_API_KEY", None)
            os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "True"
            if target_proj:
                os.environ["GOOGLE_CLOUD_PROJECT"] = target_proj
            if target_loc:
                os.environ["GOOGLE_CLOUD_LOCATION"] = target_loc

            try:
                test_client = genai.Client(
                    vertexai=True,
                    project=target_proj,
                    location=target_loc,
                    http_options=HttpOptions(api_version="v1", timeout=180000.0)
                )
                if verify_genai_client(test_client, log=logger):
                    client = test_client
                    auth_method = "Keyless GCloud / Vertex AI"
                    logger.info("- Status: Keyless GCloud ADC session successfully verified as active!")
                else:
                    logger.warning("⚠️  [WARNING] Keyless GCloud ADC verification ping failed.")
            finally:
                if not client:
                    if saved_gemini_key:
                        os.environ["GEMINI_API_KEY"] = saved_gemini_key
                    if saved_google_key:
                        os.environ["GOOGLE_API_KEY"] = saved_google_key
        except Exception as err:
            logger.warning(f"⚠️  [WARNING] Failed to instantiate keyless client: {err}")
            
    # 3. Cascade Step 3: Fatal early breakout if BOTH failed
    if not client:
        logger.error("------------------------------------------------------")
        logger.error("❌ [FAILURE] Cannot launch Autonomous AI Agent.")
        logger.error("Both key-based and keyless GCloud ADC authentication pings failed.")
        logger.error("======================================================")
        return False
        
    logger.info("------------------------------------------------------")
    logger.info(f"✅ [SUCCESS] Setup Agent initialized successfully using {auth_method}!")
    logger.info("------------------------------------------------------")
    
    is_vertex = getattr(client, "vertexai", False) or os.getenv("GOOGLE_GENAI_USE_ENTERPRISE") == "True"

    # Bundle of available tools
    tools = [
        execute_shell_command,
        python_interpreter,
        run_gcloud_auth_login_tool,
        probe_native_remote_endpoint_tool,
        probe_openapi_spec_tool,
        search_package_registries_tool,
        list_local_bridges_tool,
        inspect_remote_api_endpoints_tool,
        generate_mcp_bridge_template_tool,
        write_mcp_settings_tool,
        write_local_file_tool,
        verify_mcp_connection_tool,
        report_final_verdict_tool
    ]
    
    # Resolve dynamic conversational turns boundaries (with a default of 20)
    try:
        max_turns_env = os.getenv("AGENT_MAX_TURNS")
        max_turns = int(max_turns_env) if max_turns_env else 20
    except ValueError:
        max_turns = 20
    
    # Programmatically import and format COMMON_MCP_SERVERS fallback database
    # to eliminate hardcoding and ensure absolute dynamic consistency.
    # Done inline to safely avoid circular imports.
    from phase4_mcp_auto_discovery import COMMON_MCP_SERVERS
    
    shortcuts = []
    for key, val in COMMON_MCP_SERVERS.items():
        command = val.get("command", "")
        args = val.get("args", [])
        env_keys = ", ".join(val.get("env", {}).keys()) if val.get("env") else "None required"
        shortcuts.append(
            f"    - If target is '{key}': Use command '{command}' with args {args}. Required env variables: {env_keys}."
        )
    knowledge_base_str = "\n".join(shortcuts)

    system_instruction = f"""
    You are the Autonomous Google AI Setup Agent. Your sole purpose is to initialize, configure, and discover Model Context Protocol (MCP) servers in a developer workspace.
    
    HIGH-SIGNAL KNOWLEDGE BASE (Pre-configured Fallbacks):
    To conserve your execution turn budget, leverage these predefined setups instead of generating them:
{knowledge_base_str}
    
    You MUST autonomously execute discovery for the target service strictly following this PRIORITY ORDER:
    
    1. TIER 1 (MANDATORY NATIVE PROBE): 
       Call 'probe_native_remote_endpoint_tool'. If 'native_detected' is True, connect to the endpoint natively (it supports both Streamable HTTP 'type: http' and stdio transports).
       When a native endpoint is discovered, write the native HTTP config ('type': 'http', 'httpUrl': ..., 'url': ...) immediately with 'write_mcp_settings_tool', verify it with 'verify_mcp_connection_tool', and finish with 'report_final_verdict_tool'. Bypassing native endpoints is strictly forbidden.
       
    1.5 TIER 1.5 (OPENAPI / SWAGGER SPEC PROBING):
       If no native endpoint exists, call 'probe_openapi_spec_tool'. If an OpenAPI/Swagger spec is discovered, use the returned '@modelcontextprotocol/server-openapi' configuration to auto-instantiate the tools natively!
       
    2. TIER 2 (OFFICIAL & COMMUNITY PACKAGES): 
       If no native endpoint exists, check the HIGH-SIGNAL KNOWLEDGE BASE above. If the target isn't listed, call 'search_package_registries_tool' to check NPM and PyPI for community packages (e.g. searching for 'redmine' or 'gitlab'). If a package is returned, use it.
       
    3. TIER 3 (LOCAL ENTERPRISE BRIDGES): 
       If no native endpoint or community package exists, call 'list_local_bridges_tool(target_hint=...)'. This will return a structured JSON catalog with a 'best_match' recommendation and the exact environment variables required. If you find a matching bridge, prioritize configuring it!
       
    4. TIER 4 (CUSTOM BRIDGE GENERATION): 
       As an absolute last resort, if Tiers 1, 2, and 3 fail, call 'inspect_remote_api_endpoints_tool' to sniff active REST routes on the target, then call 'generate_mcp_bridge_template_tool' to generate a 100% correct, zero-dependency, JSON-RPC 2.0 compliant Python stdio server template. You then only need to edit it via 'execute_shell_command' (e.g. sed) or 'write_local_file_tool' to add specific API/HTTP logic.
       
    5. VERIFICATION & COMMIT (MANDATORY WRITE-BEFORE-VERIFY ORDER): 
       Once you have identified a configuration, you MUST call 'write_mcp_settings_tool' FIRST to persist it into '.mcp.json'.
       Then, immediately call 'verify_mcp_connection_tool' to ensure it connects and returns tools correctly.
       Finally, call 'report_final_verdict_tool' to conclude the session.
       CRITICAL: You are strictly required to write the configuration to .mcp.json BEFORE running verification so that your work is protected on disk against any sudden network disconnects.
       
    TIER ESCALATION & MULTI-TIER CASCADING RULES:
    - If Tier 1 ('probe_native_remote_endpoint_tool') returns 'native_detected: False', cascade immediately to Tier 1.5.
    - If Tier 1.5 ('probe_openapi_spec_tool') finds no spec, cascade immediately to Tier 2.
    - If Tier 2 ('search_package_registries_tool' / Knowledge Base) finds no package or if verification fails, cascade immediately to Tier 3.
    - If Tier 3 ('list_local_bridges_tool') has no matching bridge or verification fails, cascade immediately to Tier 4 ('inspect_remote_api_endpoints_tool' & 'generate_mcp_bridge_template_tool').
    - You must exhaustively try all tiers before concluding failure!
       
    CRITICAL GUARDRAILS:
    1. (Server Naming Guardrail): You MUST generate a unique, domain-derived hyphenated name as the 'server_name' (e.g., use 'bugzilla-suse-com' instead of 'bugzilla').
    2. (Zero-Interactivity): This program runs in 100% HEADLESS mode. NEVER halt to ask the user for API keys or passwords. Immediately write the config using empty/placeholder credentials ('', '<REQUIRED_TOKEN>') and document the setup instructions in the 'instructions' argument.
    3. (Universal Record Envelope): Modern MCP clients strictly require 'structuredContent' to be a JSON Object (Record), NEVER a bare JSON Array. All python scripts must wrap list responses like: {{{{ "count": len(items), "items": items }}}}.
    4. (Indestructible Registry): NEVER use shell scripts to overwrite '.mcp.json' directly. ONLY use 'write_mcp_settings_tool'.
    5. (No Conversational Looping): You MUST emit a tool call on every turn until completion. DO NOT reply with conversational text describing what you plan to do next.
    
    DISCIPLINE & TURN BUDGET CONSTRAINTS:
    - You have a strict limit of {max_turns} conversational turns total. Do NOT waste turns running exploratory terminal commands (like checking node versions or listing directories) if you can immediately use a shortcut or generate the code directly.
    - ABSOLUTE FINAL STEP: Once all objectives are met and verified, you MUST call the 'report_final_verdict_tool'. This is the only way to confirm completion. Pass a string summarizing what you did, explicitly listing any required API keys the user must set, warn them if you generated duplicate configs for the same service, and end the string with the word 'SUCCESS'.
    """
    is_vertex = getattr(client, "vertexai", False) or os.getenv("GOOGLE_GENAI_USE_ENTERPRISE") == "True"
    agent_tools = list(tools)
    if not is_vertex:
        try:
            agent_tools.append(types.Tool(google_search=types.GoogleSearch()))
        except Exception as e:
            logger.warning(f"Could not append search grounding tool: {e}")
    else:
        logger.info("ℹ️  [VERTEX AI MODE] Operating in strict function calling mode (Google Search tool omitted to satisfy Vertex AI tool exclusivity).")

    def _build_agent_config(m_name: str, sys_inst: str, t_tools: list, m_turns: int):
        thinking_cfg = None
        if "2.5" in str(m_name) or "thinking" in str(m_name).lower():
            try:
                thinking_cfg = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass
        return types.GenerateContentConfig(
            system_instruction=sys_inst,
            tools=t_tools,
            temperature=0.1,  # Lower temperature means high conformance
            thinking_config=thinking_cfg,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=m_turns
            )
        )
        
    model_name = getattr(_thread_local, "active_model", None) or os.getenv("GOOGLE_GENAI_MODEL", "gemini-3.7-flash")
    config = _build_agent_config(model_name, system_instruction, agent_tools, max_turns)
    mode_the_best = os.getenv("MCP_MODE_THE_BEST") == "True"
    mode_exhaustive = os.getenv("MCP_MODE_EXHAUSTIVE") == "True"
    
    prompt_mode_instructions = ""
    if mode_the_best:
        prompt_mode_instructions = """
    SPECIAL EXECUTION MODE: --the-best-mcp (THE BEST MODE)
    * You must discover and evaluate MULTIPLE potential MCP candidates (e.g. native, official package, and custom scripts).
    * Perform active validation handshakes on all of them using 'verify_mcp_connection_tool'.
    * Compare them based on: tool richness, connection security/stability, and source reliability.
    * Only write the SINGLE highest-quality candidate to '.mcp.json' using your standard server name. Do not commit lower-quality candidates.
    """
    elif mode_exhaustive:
        prompt_mode_instructions = """
    SPECIAL EXECUTION MODE: --exhaustive-mcp (EXHAUSTIVE MODE)
    * You must find and validate ALL potential MCP candidates for this target (e.g. native, package, and custom bridge scripts).
    * Verify each candidate using 'verify_mcp_connection_tool'.
    * For every candidate that successfully passes the connection handshake, write its configuration into '.mcp.json'.
    * To differentiate them, you MUST write them with distinct suffix-based keys: concatenate all parts of the address (e.g., jira.suse.com -> jira-suse-com), and append the type (e.g. jira-suse-com-native, jira-suse-com-npm, jira-suse-com-custom).
    """
    else:
        prompt_mode_instructions = """
    STANDARD MODE:
    * Find the first viable, functioning MCP configuration for this target, validate it, and write it to '.mcp.json' under your standard server name.
    """

    prompt = f"""
    Please perform Phase 4: MCP Auto-Discovery and Connection Handshake.
    - Google Cloud Project ID: '{project_id if project_id else "detect/prompt"}'
    - MCP discovery target: '{target_mcp_service}'
    
    {prompt_mode_instructions}
    
    CRITICAL CONTEXT:
    Google Cloud Authentication, project default configurations, and Vertex AI service enablement (Phases 1-3) have ALREADY been successfully completed and verified on this machine. You do NOT need to repeat GCloud configurations or services enablement.
    
    YOUR OBJECTIVES:
    1. STRICTLY follow the 5-Tier priority cascade in your instructions (Native Probe -> Packages -> Bridges -> Custom Gen -> Verify).
    2. Use your high-speed Python tools ('probe_native_remote_endpoint_tool', 'probe_openapi_spec_tool', 'search_package_registries_tool', 'list_local_bridges_tool', 'inspect_remote_api_endpoints_tool') to discover the target deterministically.
    3. Retrieve any necessary API keys or environment variables from your active environment (such as BUGZILLA_API_KEY, GITHUB_PERSONAL_ACCESS_TOKEN, etc).
    4. Write the discovered MCP config block directly into '.mcp.json' in your workspace using 'write_mcp_settings_tool'.
    5. Run a live JSON-RPC 2.0 stdio 'initialize' handshake test on the discovered server using 'verify_mcp_connection_tool' to guarantee it is working.
    
    If the connection check fails, read the DIAGNOSIS hint, self-heal your arguments or command, and retry.
    Only call 'report_final_verdict_tool' once all objectives are achieved and the MCP server handshake is verified.
    """
    
    session_attempt = 1
    max_session_attempts = 3
    network_retry_count = 0
    max_network_retries = 5
    while session_attempt <= max_session_attempts:
        if session_attempt > 1 and is_target_configured_in_mcp(target_mcp_service):
            logger.info(f"🎉 [SUCCESS] Target '{target_mcp_service}' is confirmed active in .mcp.json!")
            return True

        current_active_model = getattr(_thread_local, "active_model", None) or os.getenv("GOOGLE_GENAI_MODEL", model_name)
        attempt_config = _build_agent_config(current_active_model, system_instruction, agent_tools, max_turns)
        logger.info(f"Spawning agent session ({current_active_model}) [Attempt {session_attempt}/{max_session_attempts}]...")
        chat = client.chats.create(
            model=current_active_model,
            config=attempt_config
        )
        logger.info("Sending command to Agent...")
        try:
            response = chat.send_message(prompt)
            
            agent_text = ""
            try:
                # 1. Check chat history in reverse order
                for msg in reversed(chat.get_history()):
                    if getattr(msg, "role", "") == "model" and getattr(msg, "parts", None):
                        for part in msg.parts:
                            fc = getattr(part, "function_call", None)
                            if fc is not None and getattr(fc, "name", None) == "report_final_verdict_tool":
                                fc_args = getattr(fc, "args", {}) or {}
                                if "verdict_text" in fc_args:
                                    agent_text = str(fc_args["verdict_text"])
                                    break
                    elif getattr(msg, "role", "") == "model" and getattr(msg, "text", None):
                        if "FINAL_VERDICT_LOGGED" in msg.text:
                            agent_text = msg.text
                            break
                    if agent_text:
                        break

                # 2. Check response.function_calls if chat history did not capture it
                if not agent_text and hasattr(response, "function_calls") and response.function_calls:
                    for fc in response.function_calls:
                        if getattr(fc, "name", None) == "report_final_verdict_tool":
                            fc_args = getattr(fc, "args", {}) or {}
                            if "verdict_text" in fc_args:
                                agent_text = str(fc_args["verdict_text"])
                                break
            except Exception as hist_err:
                logger.debug(f"History parsing notice: {hist_err}")
                
            # Multi-Turn Conversational Follow-Up: If the agent returned a conversational plan without executing tools,
            # send follow-up turns urging it to immediately invoke write_mcp_settings_tool and verify_mcp_connection_tool.
            followup_turns = 0
            max_followup_turns = 3
            while followup_turns < max_followup_turns and not is_target_configured_in_mcp(target_mcp_service):
                if "FINAL_VERDICT_LOGGED" in agent_text:
                    break
                followup_turns += 1
                logger.info(f"Continuing agent conversation (turn {followup_turns + 1})...")
                followup_prompt = (
                    "CRITICAL INSTRUCTION: Do not reply with conversational text or merely describe what you plan to do! "
                    "You must emit an IMMEDIATE function call: invoke 'write_mcp_settings_tool' now to persist the MCP configuration into .mcp.json, "
                    "then invoke 'verify_mcp_connection_tool' to verify it, and finally invoke 'report_final_verdict_tool'. "
                    "Execute the function calls now!"
                )
                forced_config = None
                try:
                    forced_config = types.GenerateContentConfig(
                        tool_config=types.ToolConfig(
                            function_calling_config=types.FunctionCallingConfig(
                                mode=types.FunctionCallingConfigMode.ANY
                            )
                        )
                    )
                except Exception:
                    pass
                if forced_config is not None:
                    try:
                        response = chat.send_message(followup_prompt, config=forced_config)
                    except Exception:
                        response = chat.send_message(followup_prompt)
                else:
                    response = chat.send_message(followup_prompt)
                
                # Re-extract verdict and text from updated history
                agent_text = ""
                try:
                    for msg in reversed(chat.get_history()):
                        if getattr(msg, "role", "") == "model" and getattr(msg, "parts", None):
                            for part in msg.parts:
                                fc = getattr(part, "function_call", None)
                                if fc is not None and getattr(fc, "name", None) == "report_final_verdict_tool":
                                    fc_args = getattr(fc, "args", {}) or {}
                                    if "verdict_text" in fc_args:
                                        agent_text = str(fc_args["verdict_text"])
                                        break
                        elif getattr(msg, "role", "") == "model" and getattr(msg, "text", None):
                            if "FINAL_VERDICT_LOGGED" in msg.text:
                                agent_text = msg.text
                                break
                        if agent_text:
                            break
                    if not agent_text and hasattr(response, "function_calls") and response.function_calls:
                        for fc in response.function_calls:
                            if getattr(fc, "name", None) == "report_final_verdict_tool":
                                fc_args = getattr(fc, "args", {}) or {}
                                if "verdict_text" in fc_args:
                                    agent_text = str(fc_args["verdict_text"])
                                    break
                except Exception as hist_err:
                    logger.debug(f"History parsing notice: {hist_err}")
                if not agent_text:
                    raw_text = response.text if response.text else ""
                    agent_text = raw_text.strip()

            if not agent_text:
                raw_text = response.text if response.text else ""
                agent_text = raw_text.strip()
                
            if not agent_text or agent_text == "" or agent_text.isspace() or agent_text == "None":
                finish_reason = "UNKNOWN"
                if hasattr(response, "candidates") and response.candidates:
                    finish_reason = str(getattr(response.candidates[0], "finish_reason", "UNKNOWN"))
                elif hasattr(response, "prompt_feedback") and getattr(response.prompt_feedback, "block_reason", None):
                    finish_reason = f"PROMPT_BLOCKED_{response.prompt_feedback.block_reason}"
                    
                if finish_reason and finish_reason not in ("STOP", "UNKNOWN", "FinishReason.STOP"):
                    agent_text = f"❌ [ERROR] Agent execution halted unexpectedly by the SDK (Finish Reason: {finish_reason})."
                else:
                    agent_text = "⚠️ [WARNING] The agent completed its tool executions but reached its maximum turn budget before it could output a final verdict."
                
            logger.info("\n==============================================")
            logger.info("  AUTONOMOUS AGENT RUN COMPLETED")
            logger.info("==============================================")
            logger.info(f"Agent Final Verdict:\n{agent_text}")
            logger.info("==============================================\n")
            
            # Ground-truth physical verification: Target MUST be written and verified in .mcp.json
            if is_target_configured_in_mcp(target_mcp_service):
                logger.info(f"🎉 [SUCCESS] Target '{target_mcp_service}' is confirmed active in .mcp.json!")
                return True
                
            logger.warning(f"⚠️  [VALIDATION REJECTED] Agent finished attempt {session_attempt}/{max_session_attempts}, but target '{target_mcp_service}' is physically missing from .mcp.json. Rejecting claim of success.")
            session_attempt += 1
            if session_attempt <= max_session_attempts:
                logger.warning(f"⚠️ Agent attempt {session_attempt - 1} did not write target to .mcp.json. Re-spawning agent session (Attempt {session_attempt}/{max_session_attempts})...")
                time.sleep(2)
                continue
            return False
            
        except Exception as e:
            logger.warning(f"⚠️ [Agent Session Error] Attempt {session_attempt}/{max_session_attempts} encountered error: {e}")
            
            # Critical Resilience Check: Was target already configured before socket dropped?
            if is_target_configured_in_mcp(target_mcp_service):
                logger.info(f"🎉 [RESILIENT RECOVERY] Target '{target_mcp_service}' was successfully written to .mcp.json before the network drop! Approving configuration.")
                return True

            err_str = str(e)

            # 1. Transport-level host network / DNS outage decoupling (does not consume session attempt)
            if is_network_infrastructure_outage(e):
                network_retry_count += 1
                if network_retry_count <= max_network_retries:
                    import random
                    net_backoff = 20.0 + random.uniform(5.0, 15.0)
                    logger.warning(
                        f"📡 [NETWORK RECOVERY {network_retry_count}/{max_network_retries}] "
                        f"Host DNS or network connection to Vertex AI dropped ('{e}'). "
                        f"Waiting {net_backoff:.1f}s for host network/DNS to recover before retrying attempt {session_attempt}/{max_session_attempts}..."
                    )
                    time.sleep(net_backoff)
                    continue
                else:
                    logger.error(f"❌ [NETWORK EXHAUSTED] Host network/DNS failed {max_network_retries} consecutive times. Aborting agent attempt.")
                    return False

            # 2. Dynamic Quota Adaption: Fail over to high-capacity model tier on 429 / Resource Exhausted
            if "429" in err_str or "resource_exhausted" in err_str.lower() or "resource exhausted" in err_str.lower() or "quota" in err_str.lower():
                import random
                # Stagger parallel threads on rate limit with jitter across rolling quota window
                backoff_wait = 15.0 + random.uniform(5.0, 10.0) * session_attempt
                logger.warning(f"🔄 [QUOTA RECOVERY] Rate limit (429) on '{model_name}'. Staggering thread for {backoff_wait:.1f}s...")
                time.sleep(backoff_wait)

                candidate_fallbacks = ["gemini-2.5-flash"]
                current_m = os.getenv("GOOGLE_GENAI_MODEL", model_name)
                next_model = None
                for fb in candidate_fallbacks:
                    if fb != current_m:
                        next_model = fb
                        break
                if next_model:
                    logger.warning(f"🔄 [QUOTA RECOVERY] Dynamically adapting thread model from '{current_m}' to '{next_model}'...")
                    model_name = next_model
                    _thread_local.active_model = next_model
                    os.environ["GOOGLE_GENAI_MODEL"] = next_model
                session_attempt += 1
                continue

            # 3. Tool Healing: Model attempted internal code execution
            if "python_interpreter" in err_str.lower():
                logger.warning(f"🔄 [TOOL HEALING] Model attempted internal code execution ('{err_str}'). Re-spawning fresh agent session...")
                time.sleep(2)
                session_attempt += 1
                continue

            if "Multiple tools are supported only when they are all search tools" in err_str:
                logger.warning("🔄 [TOOL EXCLUSIVITY HEALING] Retrying with strictly functional tools (removed conflicting search tool)...")
                config.tools = list(tools)
                session_attempt += 1
                continue
                
            if session_attempt < max_session_attempts and is_transient_agent_error(e):
                backoff = session_attempt * 3
                logger.info(f"🔄 [SELF-HEALING] Re-spawning fresh agent session in {backoff} seconds to recover from transient API glitch: {e}")
                time.sleep(backoff)
                session_attempt += 1
                continue
            elif session_attempt >= max_session_attempts:
                logger.error(f"❌ [AGENT FAILURE] All {max_session_attempts} agent attempts exhausted for '{target_mcp_service}'. Final error: {e}")
                return False
            else:
                session_attempt += 1
                if is_target_configured_in_mcp(target_mcp_service):
                    return True
                return False
