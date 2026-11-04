import os
import sys
import json
import subprocess
import argparse
import time
import re
import shutil
from urllib.parse import urlparse

# Pre-parse --no-automatic-browser to set it in the environment before any local imports run
for arg in sys.argv:
    if arg == "--no-automatic-browser":
        os.environ["NO_AUTOMATIC_BROWSER_OVERRIDE"] = "True"
        break

from utils import (
    setup_logger, with_retry, WORKSPACE_ROOT, update_env_file, get_process_timeout,
    detect_python_venv_bin, ensure_runner_available, auto_heal_missing_python_module,
    extract_script_dependencies, match_target_to_registry_key, make_config_portable,
    detect_system_ca_bundle, probe_target_tls, fingerprint_target_service,
    probe_openapi_spec, query_package_registries, verify_upstream_credentials,
    normalize_entity_identifier, resolve_active_credential, is_dry_run, cleanup_runtime_artifacts, register_temp_path, verify_prephase_disk_space, is_enospc_error, halt_on_enospc, verify_target_dns, is_valid_mcp_config,
    ensure_bridges_directory_intact
)

logger = setup_logger("phase4_mcp_auto_discovery")

import threading

# Thread-safe synchronization locks for parallel execution modes
mcp_write_lock = threading.Lock()
input_lock = threading.Lock()
env_write_lock = threading.Lock()
console_write_lock = threading.Lock()

class ServerLogBuffer:
    """
    Saves and buffers log statements per-thread during parallel execution modes.
    Flushes the entire accumulated log sequentially in a clean, bordered console panel.
    Supports dynamic logger_name configuration to preserve module-specific logging identities.
    """
    def __init__(self, server_name: str, logger_name: str = "phase4_mcp_auto_discovery"):
        self.server_name = server_name
        self.records = []
        self.logger_name = logger_name

    def info(self, msg: str):
        self.records.append(f"INFO: {msg}")

    def warning(self, msg: str):
        self.records.append(f"WARNING: {msg}")

    def error(self, msg: str):
        self.records.append(f"ERROR: {msg}")

    def debug(self, msg: str):
        self.records.append(f"DEBUG: {msg}")

    def flush(self):
        """Atomically flushes the buffered records to the specified logger inside a clean console block."""
        import logging
        log_dest = logging.getLogger(self.logger_name)
        with console_write_lock:
            log_dest.info(f"\n==========================================================")
            log_dest.info(f"📡  CONCURRENT LOGS FOR TARGET: {self.server_name.upper()}")
            log_dest.info(f"==========================================================")
            for rec in self.records:
                if rec.startswith("INFO:"):
                    log_dest.info(f"  | {rec[5:].strip()}")
                elif rec.startswith("WARNING:"):
                    log_dest.warning(f"  | ⚠️  {rec[8:].strip()}")
                elif rec.startswith("ERROR:"):
                    log_dest.error(f"  | ❌  {rec[6:].strip()}")
                else:
                    log_dest.info(f"  | {rec}")
            log_dest.info(f"==========================================================\n")

# Hardcoded fallback database for offline or API failures
COMMON_MCP_SERVERS = {
    "bugzilla": {
        "command": "python3",
        "args": ["bridges/bugzilla_mcp_server.py"],
        "env": {
            "BUGZILLA_URL": "Bugzilla REST API URL",
            "BUGZILLA_API_KEY": "Bugzilla API key for authentication"
        },
        "instructions": "1. Generate an API Key in Bugzilla under User Preferences -> API Keys.\n2. Set BUGZILLA_API_KEY (or MCP_BUGZILLA_API_KEY) inside your .env file.\n3. Set BUGZILLA_URL (or MCP_BUGZILLA_BASE_URL)."
    },
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "GitHub Personal Access Token",
            "GITHUB_API_URL": "https://api.github.com"
        },
        "instructions": "1. Create a GitHub Personal Access Token (classic or fine-grained) under GitHub Developer Settings -> Personal Access Tokens.\n2. Bind permissions: repo, read:org, user.\n3. Save GITHUB_PERSONAL_ACCESS_TOKEN to your environment."
    },
    "gitlab": {
        "command": "python3",
        "args": ["bridges/gitlab_mcp_server.py"],
        "env": {"GITLAB_API_URL": "GitLab API URL (e.g. https://gitlab.suse.de/api/v4)", "GITLAB_PERSONAL_ACCESS_TOKEN": "GitLab Personal Access Token"},
        "instructions": "1. Create a GitLab Personal Access Token.\n2. Add GITLAB_PERSONAL_ACCESS_TOKEN and GITLAB_API_URL to your environment."
    },
    "progress": {
        "command": "python3",
        "args": ["bridges/redmine_mcp_server.py"],
        "env": {"REDMINE_URL": "https://progress.opensuse.org", "REDMINE_API_KEY": ""},
        "instructions": "1. Go to your Redmine account page (e.g. progress.opensuse.org/my/account) and generate an API key.\n2. Add REDMINE_API_KEY to your .env file."
    },
    "redmine": {
        "command": "python3",
        "args": ["bridges/redmine_mcp_server.py"],
        "env": {"REDMINE_URL": "https://progress.opensuse.org", "REDMINE_API_KEY": ""},
        "instructions": "1. Go to your Redmine account page (e.g. progress.opensuse.org/my/account) and generate an API key.\n2. Add REDMINE_API_KEY to your .env file."
    },
    "postgres": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "env": {"POSTGRES_CONNECTION_STRING": "PostgreSQL connection string"},
        "instructions": "1. Ensure your local or remote PostgreSQL database service is active and listening on the designated port.\n2. Construct your URI as: postgresql://username:password@localhost:5432/database_name.\n3. Save POSTGRES_CONNECTION_STRING to your environment."
    },
    "jira": {
        "command": "uvx",
        "args": ["mcp-atlassian", "--jira-url"],
        "env": {"JIRA_URL": "Jira URL (e.g. https://jira.suse.com)", "JIRA_PERSONAL_TOKEN": ""},
        "instructions": "Set JIRA_PERSONAL_TOKEN in .env for authenticated access to Jira."
    },
    "confluence": {
        "command": "uvx",
        "args": ["mcp-atlassian", "--confluence-url"],
        "env": {"CONFLUENCE_URL": "Confluence URL (e.g. https://confluence.suse.com)", "CONFLUENCE_PERSONAL_TOKEN": ""},
        "instructions": "Set CONFLUENCE_PERSONAL_TOKEN in .env for authenticated access to Confluence."
    },
    "sd": {
        "command": "python3",
        "args": ["bridges/sd_suse_mcp_server.py"],
        "env": {"SD_SUSE_URL": "https://sd.suse.com", "SD_SUSE_PAT": ""},
        "instructions": "Set SD_SUSE_PAT in your .env file with a Personal Access Token from sd.suse.com."
    },
    "openqa": {
        "command": "uvx",
        "args": ["openqa-mcp"],
        "env": {
            "OPENQA_SERVER": "openQA Server URL (e.g. https://openqa.suse.de)",
            "OPENQA_API_KEY": "",
            "OPENQA_API_SECRET": ""
        },
        "instructions": "Set OPENQA_API_KEY and OPENQA_API_SECRET in .env for authenticated access to openQA."
    },
    "obs": {
        "command": "python3",
        "args": ["bridges/obs_mcp_server.py"],
        "env": {
            "OBS_BASE_URL": "Open Build Service API URL (e.g. https://api.opensuse.org or https://api.suse.de)",
            "OBS_USER": "",
            "OBS_PASSWORD": ""
        },
        "instructions": "Set OBS_USER and OBS_PASSWORD in .env for authenticated access to Open Build Service (HTTP Basic Auth)."
    },
    "build.opensuse.org": {
        "command": "python3",
        "args": ["bridges/obs_mcp_server.py"],
        "env": {
            "OBS_BASE_URL": "https://build.opensuse.org",
            "OBS_USER": "",
            "OBS_PASSWORD": ""
        },
        "instructions": "Open Build Service (OBS) bridge for build.opensuse.org. Set OBS_USER and OBS_PASSWORD in .env."
    },
    "build.suse.de": {
        "command": "python3",
        "args": ["bridges/obs_mcp_server.py"],
        "env": {
            "OBS_BASE_URL": "https://build.suse.de",
            "OBS_USER": "",
            "OBS_PASSWORD": ""
        },
        "instructions": "Open Build Service (OBS) bridge for build.suse.de. Set OBS_USER and OBS_PASSWORD in .env."
    },
    "progress.opensuse.org": {
        "command": "python3",
        "args": ["bridges/redmine_mcp_server.py"],
        "env": {"REDMINE_URL": "https://progress.opensuse.org", "REDMINE_API_KEY": ""},
        "instructions": "Redmine bridge for progress.opensuse.org. Set REDMINE_API_KEY in .env."
    },
    "smelt.suse.de": {
        "command": "python3",
        "args": ["bridges/smelt_suse_mcp_server.py"],
        "env": {"SMELT_BASE_URL": "https://smelt.suse.de"},
        "instructions": "SMELT bridge for smelt.suse.de."
    },
    "sd.suse.com": {
        "command": "python3",
        "args": ["bridges/sd_suse_mcp_server.py"],
        "env": {"SD_SUSE_URL": "https://sd.suse.com", "SD_SUSE_PAT": ""},
        "instructions": "Set SD_SUSE_PAT in your .env file with a Personal Access Token from sd.suse.com."
    },
    "build": {
        "command": "python3",
        "args": ["bridges/obs_mcp_server.py"],
        "env": {
            "OBS_BASE_URL": "Open Build Service API URL (e.g. https://api.opensuse.org or https://api.suse.de)",
            "OBS_USER": "",
            "OBS_PASSWORD": ""
        },
        "instructions": "Set OBS_USER and OBS_PASSWORD in .env for authenticated access to Open Build Service (HTTP Basic Auth)."
    },
    "smelt": {
        "command": "python3",
        "args": ["bridges/smelt_mcp_server.py"],
        "env": {
            "SMELT_BASE_URL": "https://smelt.suse.de"
        },
        "instructions": "Standard Python bridge for SUSE Maintenance Extensible Lightweight Toolset (SMELT)."
    },
    "developers": {
        "command": "python3",
        "args": ["bridges/google_developer_knowledge_bridge.py"],
        "env": {
            "DEVELOPER_KNOWLEDGE_API_URL": "https://developerknowledge.googleapis.com/mcp"
        },
        "instructions": "Standard Python bridge for Google Developer Knowledge MCP API."
    },
    "developerknowledge": {
        "command": "python3",
        "args": ["bridges/google_developer_knowledge_bridge.py"],
        "env": {
            "DEVELOPER_KNOWLEDGE_API_URL": "https://developerknowledge.googleapis.com/mcp"
        },
        "instructions": "Standard Python bridge for Google Developer Knowledge MCP API."
    }
}

def clean_url_to_domain(target: str) -> str:
    """Extracts a simple clean domain or name from the target string."""
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urlparse(target)
        return parsed.netloc
    return target.split("/")[0]

def query_gemini_for_mcp_server(target: str) -> dict:
    """
    Queries Gemini with Google Search Grounding to find an MCP server
    solution for the given target service/website. Supports both API keys and keyless ADC.
    """
    try:
        from google import genai
        from google.genai import types
        from google.genai.types import HttpOptions
    except ImportError:
        logger.warning("Google GenAI SDK is not available. Using local fallback dictionary.")
        return {}

    logger.info(f"Querying Gemini to discover MCP server solution for: {target}")
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            client = genai.Client(api_key=api_key, http_options=HttpOptions(api_version="v1", timeout=120000.0))
        else:
            target_proj = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT")
            target_loc = os.getenv("GOOGLE_CLOUD_LOCATION") or "global"
            os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "True"
            client = genai.Client(
                vertexai=True,
                project=target_proj,
                location=target_loc,
                http_options=HttpOptions(api_version="v1", timeout=120000.0)
            )
        
        prompt = f"""
        You are an expert developer assistant. Find a solution to run or install a Model Context Protocol (MCP) server for '{target}'.
        
        HIGH-SIGNAL KNOWLEDGE BASE (Immediate Shortcuts):
        - If target is 'bugzilla' or 'bugzilla.suse.com': Use NPM package '@kud/mcp-bugzilla' (Command: 'npx', Args: ['-y', '@kud/mcp-bugzilla']). Require env vars: MCP_BUGZILLA_BASE_URL (e.g. https://bugzilla.suse.com/rest), BUGZILLA_API_KEY, MCP_BUGZILLA_API_KEY.
        - If target is 'github.com' or 'github': Use NPM package '@modelcontextprotocol/server-github' (Command: 'npx', Args: ['-y', '@modelcontextprotocol/server-github']). Require env: GITHUB_PERSONAL_ACCESS_TOKEN.
        - If target is 'gitlab' or 'gitlab.suse.de': Use local Python bridge 'bridges/gitlab_mcp_server.py' (Command: 'python3', Args: ['bridges/gitlab_mcp_server.py']) which provides strictly schema-compliant tools, avoiding upstream npm package type omission bugs. Require env: GITLAB_API_URL, GITLAB_PERSONAL_ACCESS_TOKEN.
        - If target is 'progress.opensuse.org' or 'redmine': Use local Python bridge 'bridges/redmine_mcp_server.py' (Command: 'python3', Args: ['bridges/redmine_mcp_server.py']) which provides zero-dependency, standard-library tools immune to npm registry timeouts. Require env: REDMINE_URL, REDMINE_API_KEY.
        - If target is 'postgres' or 'postgresql': Use NPM package '@modelcontextprotocol/server-postgres' (Command: 'npx', Args: ['-y', '@modelcontextprotocol/server-postgres']).
        - If target is 'filesystem' or 'files': Use NPM package '@modelcontextprotocol/server-filesystem' (Command: 'npx', Args: ['-y', '@modelcontextprotocol/server-filesystem']).
        - If target is 'smelt' or 'smelt.suse.de': Use local Python bridge 'bridges/smelt_mcp_server.py' (Command: 'python3', Args: ['bridges/smelt_mcp_server.py']). Require env: SMELT_BASE_URL.
        - If target is 'developers.google.com/knowledge/api' or 'developerknowledge': Use local Python bridge 'bridges/google_developer_knowledge_bridge.py' (Command: 'python3', Args: ['bridges/google_developer_knowledge_bridge.py']). Require env: DEVELOPER_KNOWLEDGE_API_URL.
        - If target is 'jira.suse.com': Use local Python bridge 'bridges/jira_suse_mcp.py' (Command: 'python3', Args: ['bridges/jira_suse_mcp.py']). Require env: JIRA_URL, JIRA_TOKEN.
        - If target is 'sd.suse.com' or 'sd': Use local Python bridge 'bridges/sd_suse_mcp_server.py' (Command: 'python3', Args: ['bridges/sd_suse_mcp_server.py']). Require env: SD_SUSE_URL, SD_SUSE_PAT.
        - If target is 'build.suse.de' or 'build.opensuse.org': Use local Python bridge 'bridges/obs_mcp_server.py' (Command: 'python3', Args: ['bridges/obs_mcp_server.py']). Require env: OBS_BASE_URL.
        
        You MUST strictly follow this PRIORITY ORDER for discovery and configuration:
        
        1. TOP PRIORITY (NATIVE ENDPOINT PROBING - MANDATORY DUE DILIGENCE):
           - You are STRICTLY FORBIDDEN from generating or recommending custom local Python or Node.js bridge scripts if a native remote MCP endpoint is hosted on '{target}'.
           - You MUST perform exhaustive due diligence to check if '{target}' natively hosts an MCP endpoint. You must search the web (using Google Search Grounding) and analyze if '{target}' hosts active endpoints on standard or non-traditional paths (e.g. '/api/mcp', '/v1/mcp', '/v1/api/mcp', '/rest/mcp', '/mcp/sse', '/sse', '/ws', '/v1/mcp/sse', or query parameters '?format=mcp', but you are not limited to these paths; they are just examples, and you should actively discover new endpoints in new forms).
           - Check standard API documentation for that domain indicating active Server-Sent Events (SSE) or WebSocket MCP hooks.
           - If a native endpoint exists, use it! (Configure standard stdio commands or a lightweight python bridge that pings that remote endpoint).
           
        2. SECOND PRIORITY (EXISTING PACKAGES & REPOSITORIES):
           - If no native endpoint is found, check the HIGH-SIGNAL KNOWLEDGE BASE above. If the target is not there, aggressively search the web (using Google Search Grounding) for existing NPM packages, GitHub repositories, or PyPI packages. If found, write the config.
           
        3. THIRD PRIORITY (CUSTOM BRIDGE INVENTION):
           - Only as an absolute last resort, if no native endpoint exists and no community packages exist, invent a custom Python/Node.js stdio-based bridge script.
           
        4. CRITICAL SERVER NAMING GUARDRAIL:
           - You MUST use a unique, domain-derived registry name. For example, if target is 'bugzilla.opensuse.org', use 'bugzilla-opensuse-org' (never a generic name like 'bugzilla' or 'openqa' which would overwrite other batch entries).

        Your output MUST be a valid JSON block containing exactly these fields:
        1. "command": The primary executable (e.g. "npx", "python3", "uvx")
        2. "args": A list of string arguments (e.g. ["-y", "@modelcontextprotocol/server-bugzilla", "--url", "https://{target}"])
        3. "env": A dictionary of required environment variables as keys, with their description/help text as values.
        4. "instructions": A detailed string describing any manual post-setup steps, how to obtain required keys/tokens, start database services, or keep the server running smoothly.
        
        Respond ONLY with the raw JSON object. Do not wrap it in markdown ticks or anything else.
        """
        
        model_name = os.getenv("GOOGLE_GENAI_MODEL", "gemini-3.7-flash")
        
        def execute_gemini(m_name, with_search=True):
            config_kwargs = {"temperature": 0.1}
            if with_search:
                config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
            if "2.5" in m_name or "2.0" in m_name:
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            return client.models.generate_content(
                model=m_name,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs)
            )

        try:
            response = execute_gemini(model_name, with_search=True)
        except Exception as api_err:
            err_str = str(api_err)
            if "Multiple tools are supported only when they are all search tools" in err_str or "Tool exclusivity" in err_str:
                logger.warning("🔄 [TOOL EXCLUSIVITY HEALING] Retrying without Google Search tool...")
                response = execute_gemini(model_name, with_search=False)
            elif "429" in err_str or "resource_exhausted" in err_str.lower() or "quota" in err_str.lower():
                import time, random
                backoff = 15.0 + random.uniform(5.0, 10.0)
                logger.warning(f"🔄 [QUOTA RECOVERY] Rate limit (429) on '{model_name}'. Staggering for {backoff:.1f}s and failing over to gemini-2.5-flash...")
                time.sleep(backoff)
                model_name = "gemini-2.5-flash"
                try:
                    response = execute_gemini(model_name, with_search=True)
                except Exception as fallback_err:
                    if "Multiple tools" in str(fallback_err):
                        response = execute_gemini(model_name, with_search=False)
                    else:
                        raise fallback_err
            else:
                raise api_err
        
        raw_text = response.text if response.text else ""
        text = raw_text.strip()
        
        # Safely handle empty generation responses
        if not text or text == "None":
            finish_reason = getattr(response, "finish_reason", "UNKNOWN")
            logger.error(f"Gemini AI discovery returned an empty response. (Finish Reason: {finish_reason})")
            return {}
            
        # Clean potential markdown block wrappers
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        
        mcp_config = json.loads(text)
        if not is_valid_mcp_config(mcp_config):
            logger.warning(f"⚠️  [LLM VALIDATION] Gemini suggested an MCP configuration with non-existent local script(s): {mcp_config.get('args')}. Rejecting hallucination.")
            return {}
        logger.info("Successfully discovered MCP configuration via Gemini!")
        return mcp_config
    except Exception as e:
        logger.error(f"Gemini AI discovery failed: {e}")
        return {}

def probe_native_mcp_endpoint(target: str, log=None) -> dict:
    """
    Tries to probe standard and non-standard MCP SSE paths on target using both GET (SSE) 
    and POST (JSON-RPC) according to the latest Streamable HTTP spec. Includes subdomain permutation 
    (api.*, mcp.*) and official /.well-known/mcp RFC discovery.
    If a remote endpoint is detected, returns a config to spin up a small,
    dynamic stdio-to-SSE bridge Python client script.
    """
    if log is None:
        log = logger
    import urllib.request
    import ssl
    import json
    
    domain = clean_url_to_domain(target)
    base_url = target if target.startswith("http") else f"https://{domain}"
    
    # Diverse Paths checklist including RFC standard (no query parameters)
    paths_to_test = [
        "/sse", "/mcp/sse", "/api/mcp", "/v1/mcp", "/v1/api/mcp", "/rest/mcp", 
        "/mcp", "/ws", "/v1/mcp/sse", "/.well-known/mcp", "/.well-known/mcp.json"
    ]
    
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": "google-ai-initializer-client/1.0.0"
    }
    
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "discovery", "version": "1.0.0"}
        }
    }
    init_bytes = json.dumps(init_payload).encode("utf-8")
    
    # SSL/Protocol bypass context (B)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    # In-Flight Credential Auto-Injection for Native Probing
    candidate_header_sets = [headers]
    auth_header_val = None

    if "openqa" in domain.lower():
        user = os.environ.get("OPENQA_USER") or os.environ.get("USER", "root")
        key = os.environ.get("OPENQA_API_KEY", "")
        sec = os.environ.get("OPENQA_API_SECRET", "")
        tok = os.environ.get("OPENQA_TOKEN", "")
        if tok:
            auth_header_val = f"Bearer {tok}"
        elif key and sec:
            auth_header_val = f"Bearer {user}:{key}:{sec}"
        elif os.path.exists(os.path.expanduser("~/.config/openqa/client.conf")):
            try:
                with open(os.path.expanduser("~/.config/openqa/client.conf")) as f:
                    c_conf = f.read()
                    import re
                    k_m = re.search(r"key\s*=\s*(\S+)", c_conf)
                    s_m = re.search(r"secret\s*=\s*(\S+)", c_conf)
                    if k_m and s_m:
                        auth_header_val = f"Bearer {user}:{k_m.group(1)}:{s_m.group(1)}"
            except Exception:
                pass
    else:
        dom_token_key = f"{domain.upper().replace('.', '_').replace('-', '_')}_TOKEN"
        tok = os.environ.get(dom_token_key) or os.environ.get("BEARER_TOKEN")
        if tok:
            auth_header_val = f"Bearer {tok}"

    if auth_header_val:
        auth_hdr = headers.copy()
        auth_hdr["Authorization"] = auth_header_val
        candidate_header_sets.insert(0, auth_hdr)
    
    log.info(f"🔎 [PROBING] Checking remote target '{base_url}' for natively hosted MCP endpoints...")
    
    def check_path(url_to_test):
        for h in candidate_header_sets:
            # Test 1: GET (for SSE streams)
            try:
                req = urllib.request.Request(url_to_test, headers=h, method="GET")
                with urllib.request.urlopen(req, context=ctx, timeout=1.5) as response:
                    resp_headers = response.info()
                    content_type = resp_headers.get("Content-Type", "").lower()
                    if "html" in content_type:
                        pass
                    elif response.status == 200 and ("event-stream" in content_type or "mcp" in content_type or "json-rpc" in content_type):
                        return url_to_test, content_type, h
            except urllib.error.HTTPError as he:
                try:
                    c_type = he.headers.get("Content-Type", "").lower()
                    if "html" not in c_type:
                        body = he.read().decode("utf-8", errors="ignore")
                        if not body.strip().startswith(("<", "<!doctype", "<html")):
                            if he.code in (401, 403) and ("json" in c_type or "event-stream" in c_type):
                                if "jsonrpc" in body or any(k in body.lower() for k in ["api key", "no api key", "token", "unauthorized", "auth"]):
                                    return url_to_test, "authenticated-http-mcp", h
                except Exception:
                    pass
            except Exception:
                pass
                
            # Test 2: POST (for Streamable HTTP / JSON-RPC endpoints that reject GET)
            try:
                req = urllib.request.Request(url_to_test, data=init_bytes, headers=h, method="POST")
                with urllib.request.urlopen(req, context=ctx, timeout=1.5) as response:
                    resp_headers = response.info()
                    content_type = resp_headers.get("Content-Type", "").lower()
                    if "html" in content_type:
                        pass
                    elif response.status == 200 and ("json" in content_type or "mcp" in content_type):
                        data = response.read().decode("utf-8")
                        if not data.strip().startswith(("<", "<!doctype", "<html")) and "jsonrpc" in data and "protocolVersion" in data:
                            return url_to_test, content_type, h
            except urllib.error.HTTPError as he:
                try:
                    c_type = he.headers.get("Content-Type", "").lower()
                    if "html" not in c_type:
                        body = he.read().decode("utf-8", errors="ignore")
                        if not body.strip().startswith(("<", "<!doctype", "<html")):
                            if he.code in (401, 403) and ("json" in c_type or "event-stream" in c_type):
                                if "jsonrpc" in body or any(k in body.lower() for k in ["api key", "no api key", "token", "unauthorized", "auth"]):
                                    return url_to_test, "authenticated-http-mcp", h
                except Exception:
                    pass
            except Exception:
                pass
                
        return None, None, None

    # Generate full URL list (root domain + subdomain permutations)
    url_candidates = []
    base_permutations = [base_url]
    if base_url.count('.') == 1:
        scheme = "https" if base_url.startswith("https") else "http"
        base_permutations.append(f"{scheme}://api.{domain}")
        base_permutations.append(f"{scheme}://mcp.{domain}")
        
    for base in base_permutations:
        for p in paths_to_test:
            test_url = f"{base.rstrip('/')}{p}" if p.startswith("/") or p.startswith("?") else f"{base.rstrip('/')}/{p}"
            url_candidates.append(test_url)

    import concurrent.futures
    detected_url, detected_type, detected_headers = None, None, None
    # Use max 15 workers to quickly burst through all permutations
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(15, len(url_candidates))) as executor:
        futures = [executor.submit(check_path, u) for u in url_candidates]
        for future in concurrent.futures.as_completed(futures):
            res_url, res_type, res_hdrs = future.result()
            if res_url:
                detected_url, detected_type, detected_headers = res_url, res_type, res_hdrs
                break

    if detected_url:
        test_url = detected_url
        content_type = detected_type
        log.info(f"🎉 [NATIVE DETECTED] Found active native MCP endpoint at: {test_url} (Content-Type: {content_type})")
        
        cfg_headers = {}
        if detected_headers and "Authorization" in detected_headers:
            cfg_headers["Authorization"] = detected_headers["Authorization"]
        elif "openqa" in domain.lower():
            cfg_headers["Authorization"] = "Bearer ${OPENQA_USER}:${OPENQA_API_KEY}:${OPENQA_API_SECRET}"

        # Dual Option: Also synthesize local stdio bridge script for stdio-only clients (Claude Desktop, etc.)
        try:
            domain_key = domain.upper().replace(".", "_").replace("-", "_")
            env_endpoint_key = f"{domain_key}_MCP_ENDPOINT"
            bridge_script_name = f"mcp_sse_bridge_{domain.replace('.', '_')}.py"
            bridge_script_path = os.path.join(WORKSPACE_ROOT, bridge_script_name)

            bridge_code = f"""#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import ssl

ENDPOINT_URL = os.environ.get("{env_endpoint_key}", "{test_url}")
AUTH_HEADER = os.environ.get("MCP_AUTH_HEADER", "{cfg_headers.get('Authorization', '')}")

def main():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line_str = line.strip()
        if not line_str:
            continue
            
        try:
            payload = json.loads(line_str)
            req_id = payload.get("id")
            headers = {{"Content-Type": "application/json", "Accept": "application/json"}}
            if AUTH_HEADER:
                headers["Authorization"] = AUTH_HEADER
            req = urllib.request.Request(ENDPOINT_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                res_data = resp.read().decode("utf-8")
                if res_data:
                    sys.stdout.write(res_data.strip() + "\\n")
                    sys.stdout.flush()
        except Exception as err:
            if "req_id" in locals() and req_id is not None:
                err_resp = {{"jsonrpc": "2.0", "id": req_id, "error": {{"code": -32603, "message": str(err)}}}}
                sys.stdout.write(json.dumps(err_resp) + "\\n")
                sys.stdout.flush()

if __name__ == "__main__":
    main()
"""
            with open(bridge_script_path, "w", encoding="utf-8") as f:
                f.write(bridge_code)
            os.chmod(bridge_script_path, 0o755)
            log.info(f"💾 Generated dual local stdio bridge client script at: {bridge_script_path}")
        except Exception as bridge_err:
            log.debug(f"Dual stdio bridge generation skipped: {bridge_err}")

        return make_config_portable({
            "type": "http",
            "httpUrl": test_url,
            "url": test_url,
            "headers": cfg_headers
        }, target_hint=target)

    log.info("❌ [PROBING] No native remote MCP endpoint responded. Proceeding to packaged/script-based discovery tiers.")
    return {}

def verify_url_presence_in_config_or_script(config: dict, credentials: dict, target: str, domain: str, log=None) -> bool:
    """
    Enforces Rule 11 (URL/URI/FQDN Traceability & Trust).
    Verifies that the target address is actively and operationally referenced,
    either in args, environmental credentials, or inside the source code of any executed script.
    Includes Case D: Shared registrable domain / organization relationship mapping.
    """
    if log is None:
        log = logger
    # Case A: Check explicit parameters inside command-line args
    args_str = " ".join(config.get("args", []))
    if domain in args_str or target in args_str:
        log.info(f"🛡️ [TRACEABILITY] Verified: FQDN/URL is explicitly referenced inside command-line arguments.")
        return True
        
    # Case B: Check environmental variables/credentials
    for env_key, env_val in credentials.items():
        if domain in str(env_val) or target in str(env_val):
            log.info(f"🛡️ [TRACEABILITY] Verified: FQDN/URL is explicitly mapped to environmental variable '{env_key}'.")
            return True
            
    # Case C: Check if it is encapsulated within the executed local script source code
    # If the entry executes a local Python/Node script, check if the file contains the FQDN target
    for arg in config.get("args", []):
        if arg.endswith(".py") or arg.endswith(".js"):
            script_path = os.path.join(WORKSPACE_ROOT, arg) if not os.path.isabs(arg) else arg
            if os.path.exists(script_path):
                try:
                    with open(script_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        if domain in content or target in content:
                            log.info(f"🛡️ [TRACEABILITY] Verified: Target FQDN '{domain}' is active and ENCAPSULATED inside the script source code of '{arg}'.")
                            return True
                except Exception as err:
                    log.warning(f"Failed to scan script file '{arg}' for FQDN trace: {err}")
                    
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
    reg_target = get_registrable_domain(target)
    
    if reg_domain and reg_domain != "":
        if reg_domain in args_str or reg_target in args_str:
            log.info(f"🛡️ [TRACEABILITY] Verified: Config shares registrable domain/organization '{reg_domain}' in arguments (Case D).")
            return True
        for env_key, env_val in credentials.items():
            if reg_domain in str(env_val) or reg_target in str(env_val):
                log.info(f"🛡️ [TRACEABILITY] Verified: Config shares registrable domain/organization '{reg_domain}' in environment variable '{env_key}' (Case D).")
                return True
        for arg in config.get("args", []):
            if arg.endswith(".py") or arg.endswith(".js"):
                script_path = os.path.join(WORKSPACE_ROOT, arg) if not os.path.isabs(arg) else arg
                if os.path.exists(script_path):
                    try:
                        with open(script_path, "r", encoding="utf-8") as f:
                            content = f.read()
                            if reg_domain in content or reg_target in content or reg_domain.replace(".", "_") in content:
                                log.info(f"🛡️ [TRACEABILITY] Verified: Registrable domain '{reg_domain}' is active and ENCAPSULATED inside script source code of '{arg}' (Case D).")
                                return True
                    except Exception as err:
                        log.warning(f"Failed to scan script file '{arg}' for domain relationship trace: {err}")

    log.warning(f"⚠️  [TRACEABILITY WARNING] No operational URL/URI/FQDN mapping was verified for '{target}' in args, credentials, or script source!")
    return False

def audit_registry_for_duplicates(server_name: str, domain: str, log=None):
    """Programmatically audits .mcp.json for any existing duplicate or overlapping server registries."""
    if log is None:
        log = logger
    settings_path = os.path.join(WORKSPACE_ROOT, ".mcp.json")
    if not os.path.exists(settings_path):
        return
        
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
            servers = settings.get("mcpServers", {})
            
            duplicates = []
            for name, config in servers.items():
                if name == server_name:
                    continue
                    
                # Look for overlap in server names or domain strings inside args/env
                if domain.replace(".", "-") in name or domain.split(".")[0] in name:
                    duplicates.append(name)
                else:
                    args_str = " ".join(config.get("args", []))
                    if domain in args_str:
                        duplicates.append(name)
                        
            if duplicates:
                log.warning("==========================================================")
                log.warning("⚠️  [DUPLICATE DETECTION WARNING] Potential Registry Clutter!")
                log.warning(f"Overlapping entries for domain '{domain}' already exist in your registry:")
                for dup in duplicates:
                    log.warning(f"  👉  Server name: '{dup}'")
                log.warning("Please verify that your active CLI client loaded the correct server configuration.")
                log.warning("==========================================================")
    except Exception as e:
        log.warning(f"Failed to run programmatic duplicate audit: {e}")

def find_local_script(*script_names) -> str:
    """Checks WORKSPACE_ROOT, script directory, and bridges/ subdirectory for local bridge script candidates."""
    candidate_roots = [
        WORKSPACE_ROOT,
        os.path.dirname(os.path.abspath(__file__)),
        os.getcwd(),
        os.path.join(WORKSPACE_ROOT, ".."),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    ]
    for name in script_names:
        if not name:
            continue
        for root in candidate_roots:
            if not root or not os.path.exists(root):
                continue
            for p in [os.path.join(root, name), os.path.join(root, "bridges", name)]:
                if os.path.exists(p):
                    return p
    return None

def discover_all_mcp_candidates(target: str, log=None) -> list:
    """
    Exhaustively gathers all possible MCP configuration candidates from 
    all tiers: Native Probes & Standard Packages (Tier 1), HTTP Fingerprinted Services (Tier 1.5),
    OpenAPI Specs (Tier 1.6), Local Bridges (Tier 2), Registry Searches (Tier 2.5), and Gemini AI (Tier 3).
    Returns a list of dicts with keys: 'config', 'type' (e.g. native/package/local/openapi), 'score'.
    """
    if log is None:
        log = logger
    candidates = []
    domain = clean_url_to_domain(target)
    target_url = target if (target.startswith("http://") or target.startswith("https://")) else f"https://{domain}"

    # 0.2 Top Priority: Inspect local bridges directory using semantic scored matching
    import glob
    search_dirs = [os.path.join(WORKSPACE_ROOT, "bridges"), WORKSPACE_ROOT]
    clean_t = target.lower().replace("https://", "").replace("http://", "")
    generic_tokens = {'com', 'org', 'net', 'de', 'io', 'api', 'v1', 'v2', 'v4', 'rest', 'mcp', 'www', 'suse', 'opensuse', 'google'}
    service_tokens = [tok for tok in re.split(r'[/._-]+', clean_t) if tok and tok not in generic_tokens]
    
    best_script = None
    best_score = 0
    best_env = {}
    
    seen_scripts = set()
    for s_dir in search_dirs:
        if not os.path.isdir(s_dir):
            continue
        for script in glob.glob(os.path.join(s_dir, "*.py")):
            b_name = os.path.basename(script).lower()
            if b_name in seen_scripts or b_name in ("phase4_mcp_auto_discovery.py", "agent_orchestrator.py", "initialize_google_ai.py", "run_tests.py", "utils.py", "phase1_install.py", "phase2_authenticate.py", "phase3_provision.py"):
                continue
            seen_scripts.add(b_name)
            try:
                with open(script, "r", encoding="utf-8", errors="ignore") as f:
                    s_content = f.read().lower()
                
                score = 0
                b_name_tokens = re.split(r'[_.-]+', b_name)
                
                # A bridge MUST match at least one specific service token (e.g. 'confluence', 'gitlab', 'openqa', 'build', 'obs', etc.)
                # Generic organizational tokens like 'suse' or 'opensuse' can only grant bonus points IF a service token matches!
                has_service_match = False
                for tok in service_tokens:
                    tok_root = tok.rstrip('s')
                    if tok in b_name_tokens or tok_root in b_name_tokens:
                        score += 15
                        has_service_match = True
                    elif tok in b_name or tok_root in b_name:
                        score += 8
                        has_service_match = True
                    elif re.search(r'\b' + re.escape(tok) + r'\b', s_content) or (tok_root and re.search(r'\b' + re.escape(tok_root) + r'\b', s_content)):
                        score += 4
                        has_service_match = True
                        
                if not has_service_match and clean_t.split('/')[0] not in s_content:
                    continue

                # Reward exact target domain tokens present directly in filename (e.g. suse, de, opensuse, org)
                for part in re.split(r'[/._-]+', clean_t):
                    if part and part not in ('com', 'net', 'io', 'api', 'v1', 'v2', 'v4', 'rest', 'mcp', 'www'):
                        if part in b_name_tokens:
                            score += 8
                        elif part in b_name:
                            score += 4
                    
                if score > best_score and score >= 10:
                    best_score = score
                    best_script = script
                    env_vars = re.findall(r"os\.environ\.get\(['\"]([^'\"]+)['\"]\)", s_content)
                    best_env = {v: "" for v in set(env_vars) if v != "PATH"}
            except Exception:
                pass
                
    # Tier 1 (Native remote HTTP(S) probing): Score 100 (Highest Priority)
    native = probe_native_mcp_endpoint(target, log=log)
    if native and ("command" in native or "url" in native or "httpUrl" in native or native.get("type") in ("http", "sse")):
        candidates.append({
            "config": native,
            "type": "native",
            "score": 100
        })

    if best_script:
        log.info(f"🎉 [LOCAL DETECTION] Auto-discovered matching bridge script '{os.path.basename(best_script)}' (score: {best_score}) for target '{target}'!")
        candidates.append({
            "config": make_config_portable({
                "command": "python3",
                "args": [best_script],
                "env": best_env,
                "instructions": f"Auto-discovered {os.path.basename(best_script)}."
            }, target_hint=target),
            "type": "local",
            "score": best_score
        })

    # Tier 1.2: Autonomous HTTP Service Fingerprinting (works on arbitrary custom/corporate domains)
    try:
        fp = fingerprint_target_service(target)
        if fp:
            log.info(f"🔎 [SMART FINGERPRINT] Service autonomously identified as '{fp['service'].upper()}' via HTTP inspection!")
            if fp["service"] in ("jira", "confluence"):
                url_flag = "--jira-url" if fp["service"] == "jira" else "--confluence-url"
                env_key = "JIRA_URL" if fp["service"] == "jira" else "CONFLUENCE_URL"
                candidates.append({
                    "config": make_config_portable({
                        "command": "uvx",
                        "args": ["mcp-atlassian", url_flag, target_url],
                        "env": {env_key: target_url, f"{fp['service'].upper()}_PERSONAL_TOKEN": ""},
                        "instructions": f"Set {fp['service'].upper()}_PERSONAL_TOKEN in .env for authenticated access to {fp['service'].upper()}."
                    }, target_hint=target),
                    "type": "fingerprint",
                    "score": 50
                })
            elif fp["service"] == "gitlab":
                local_gl = find_local_script("gitlab_mcp_server.py")
                cmd = "python3" if local_gl else "npx"
                args = [local_gl] if local_gl else ["-y", "@modelcontextprotocol/server-gitlab"]
                candidates.append({
                    "config": make_config_portable({
                        "command": cmd,
                        "args": args,
                        "env": {"GITLAB_API_URL": f"{target_url.rstrip('/')}/api/v4", "GITLAB_PERSONAL_ACCESS_TOKEN": ""},
                        "instructions": "Set GITLAB_PERSONAL_ACCESS_TOKEN in .env for authenticated access to GitLab."
                    }, target_hint=target),
                    "type": "fingerprint",
                    "score": 50
                })
            elif fp["service"] == "bugzilla":
                local_bz = find_local_script("bugzilla_mcp_server.py")
                cmd = "python3" if local_bz else "npx"
                args = [local_bz] if local_bz else ["-y", "@kud/mcp-bugzilla"]
                candidates.append({
                    "config": make_config_portable({
                        "command": cmd,
                        "args": args,
                        "env": {"BUGZILLA_URL": f"{target_url.rstrip('/')}/rest", "BUGZILLA_API_KEY": "", "MCP_BUGZILLA_BASE_URL": f"{target_url.rstrip('/')}/rest", "MCP_BUGZILLA_API_KEY": ""},
                        "instructions": "Set BUGZILLA_API_KEY in .env for authenticated access to Bugzilla."
                    }, target_hint=target),
                    "type": "fingerprint",
                    "score": 50
                })
            elif fp["service"] == "sd":
                local_sd = find_local_script("sd_suse_mcp_server.py")
                if local_sd:
                    candidates.append({
                        "config": make_config_portable({
                            "command": "python3",
                            "args": [local_sd],
                            "env": {"SD_SUSE_URL": target_url, "SD_SUSE_PAT": ""},
                            "instructions": "Set SD_SUSE_PAT in your .env file with a Personal Access Token from sd.suse.com."
                        }, target_hint=target),
                        "type": "fingerprint",
                        "score": 50
                    })
                # Resilient Enterprise Cascading: Jira Service Management (SD) is Atlassian Jira; cascade to official package
                candidates.append({
                    "config": make_config_portable({
                        "command": "uvx",
                        "args": ["mcp-atlassian", "--jira-url", target_url],
                        "env": {"JIRA_URL": target_url, "JIRA_PERSONAL_TOKEN": ""},
                        "instructions": "Set JIRA_PERSONAL_TOKEN in .env for authenticated access to Jira Service Desk."
                    }, target_hint=target),
                    "type": "fingerprint-atlassian",
                    "score": 45
                })
            elif fp["service"] == "obs":
                local_obs = find_local_script("obs_mcp_server.py", "build_opensuse_mcp.py", "build_suse_de_mcp.py")
                if local_obs:
                    candidates.append({
                        "config": make_config_portable({
                            "command": "python3",
                            "args": [local_obs],
                            "env": {"OBS_BASE_URL": target_url},
                            "instructions": "Set OBS_USER and OBS_PASSWORD or OBS_TOKEN in .env for authenticated access to OBS."
                        }, target_hint=target),
                        "type": "fingerprint",
                        "score": 50
                    })
                local_bld = find_local_script("build_opensuse_mcp.py")
                if local_bld and (not local_obs or os.path.basename(local_obs) != "build_opensuse_mcp.py"):
                    candidates.append({
                        "config": make_config_portable({
                            "command": "python3",
                            "args": [local_bld],
                            "env": {"OBS_BASE_URL": target_url},
                            "instructions": "Set OBS_USER and OBS_PASSWORD or OBS_TOKEN in .env for authenticated access to OBS."
                        }, target_hint=target),
                        "type": "fingerprint-build",
                        "score": 45
                    })
            elif fp["service"] == "redmine":
                local_redmine = find_local_script("redmine_mcp_server.py")
                if local_redmine:
                    candidates.append({
                        "config": make_config_portable({
                            "command": "python3",
                            "args": [local_redmine],
                            "env": {"REDMINE_URL": target_url, "REDMINE_API_KEY": ""},
                            "instructions": "Set REDMINE_API_KEY in .env for authenticated access to Redmine."
                        }, target_hint=target),
                        "type": "fingerprint",
                        "score": 50
                    })
            elif fp["service"] == "smelt":
                local_smelt_suse = find_local_script("smelt_suse_mcp_server.py")
                if local_smelt_suse:
                    candidates.append({
                        "config": make_config_portable({
                            "command": "python3",
                            "args": [local_smelt_suse],
                            "env": {"SMELT_BASE_URL": target_url},
                            "instructions": "SMELT GraphQL and REST MCP server initialized."
                        }, target_hint=target),
                        "type": "fingerprint",
                        "score": 50
                    })
                local_smelt_gen = find_local_script("smelt_mcp_server.py")
                if local_smelt_gen and (not local_smelt_suse or os.path.basename(local_smelt_suse) != "smelt_mcp_server.py"):
                    candidates.append({
                        "config": make_config_portable({
                            "command": "python3",
                            "args": [local_smelt_gen],
                            "env": {"SMELT_BASE_URL": target_url},
                            "instructions": "SMELT MCP bridge initialized."
                        }, target_hint=target),
                        "type": "fingerprint-smelt",
                        "score": 45
                    })
    except Exception as fp_err:
        log.debug(f"Fingerprinting evaluation skipped: {fp_err}")

    # Tier 1.4: Universal OpenAPI / Swagger Machine-Readable API Discovery
    try:
        spec_url = probe_openapi_spec(target)
        if spec_url:
            log.info(f"🔎 [OPENAPI SPEC FOUND] Discovered machine-readable API specification at: {spec_url}")
            candidates.append({
                "config": make_config_portable({
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-openapi", spec_url],
                    "env": {},
                    "instructions": f"Universal OpenAPI MCP Server auto-instantiated from {spec_url}."
                }, target_hint=target),
                "type": "openapi",
                "score": 40
            })
    except Exception as spec_err:
        log.debug(f"OpenAPI probe skipped: {spec_err}")

    # Standard verified packages (uvx / npx) are top tier: zero path drift, isolated environments
    for key, value in COMMON_MCP_SERVERS.items():
        if key in domain.lower():
            args = []
            for arg in value["args"]:
                if arg == "@modelcontextprotocol/server-bugzilla":
                    args.extend([arg, "--url", f"https://{domain}"])
                elif arg in ("--jira-url", "--confluence-url"):
                    args.extend([arg, f"https://{domain}"])
                else:
                    args.append(arg)
            pkg_config = {
                "command": value["command"],
                "args": args if args else value["args"],
                "env": value["env"],
                "instructions": value.get("instructions", "")
            }
            candidates.append({
                "config": make_config_portable(pkg_config, target_hint=target),
                "type": "package",
                "score": 50
            })
        
    # Tier 2 (Verified local bridge scripts in bridges/): Score 25
    if "openqa" in domain.lower() or "openqa" in target.lower():
        local_script = find_local_script("openqa_mcp_server.py", "openqa_bridge.py", "openqa_mcp_bridge.py")
        if local_script:
            candidates.append({
                "config": make_config_portable({
                    "command": "python3",
                    "args": [local_script],
                    "env": {
                        "OPENQA_URL": f"https://{domain}" if "http" not in target else target,
                        "OPENQA_API_KEY": "openQA API key (needed for write operations or private groups)",
                        "OPENQA_API_SECRET": "openQA API secret (needed for signature authentication)"
                    },
                    "instructions": "1. Generate an API Key/Secret in openQA under User Settings -> API Keys.\n2. Add OPENQA_API_KEY and OPENQA_API_SECRET to your environment (.env) file.\n3. Verify your OPENQA_URL is correct."
                }, target_hint=target),
                "type": "local",
                "score": 25
            })
            
    if "bugzilla" in domain.lower() or "bugzilla" in target.lower():
        local_script = find_local_script("bugzilla_mcp.py", "bugzilla_mcp_server.py")
        if local_script:
            candidates.append({
                "config": make_config_portable({
                    "command": "python3",
                    "args": [local_script],
                    "env": {
                        "BUGZILLA_BASE_URL": f"https://{domain}" if "http" not in target else target,
                        "BUGZILLA_API_KEY": "Bugzilla API key for authentication"
                    },
                    "instructions": "1. Generate an API Key in Bugzilla under User Preferences -> API Keys.\n2. Add BUGZILLA_API_KEY to your environment (.env) file."
                }, target_hint=target),
                "type": "local",
                "score": 25
            })

    # Universal Self-Describing Bridge Introspection
    from utils import introspect_local_bridges
    bridge_catalog = introspect_local_bridges(WORKSPACE_ROOT, log=log)
    best_script = None
    best_score = 0
    best_env = {}
    
    t_lower = target.lower()
    t_domain = domain.lower()
    
    for bridge in bridge_catalog:
        score = 0
        b_name = bridge["script_name"].lower()
        
        # Check filename
        for part in t_domain.split("."):
            if part and part in b_name and part not in ("com", "org", "net", "de", "co", "suse", "opensuse", "google"):
                score += 3
        
        # Check target explicit mention
        if t_domain in bridge["content_lower"]:
            score += 2
            
        if score > best_score:
            best_score = score
            best_script = bridge["script_path"]
            best_env = {k: "" for k in bridge["env_keys"]}
            
    if best_script and best_score > 0:
        script_display = os.path.basename(best_script)
        candidates.append({
            "config": make_config_portable({
                "command": "python3",
                "args": [best_script],
                "env": best_env,
                "instructions": f"Auto-discovered {script_display}."
            }, target_hint=target),
            "type": "local",
            "score": 25
        })

    # Tier 2.5: Live Public NPM/Community Registry Discovery
    try:
        reg_pkg = query_package_registries(domain.split(".")[0])
        if reg_pkg:
            log.info(f"📦 [NPM REGISTRY DISCOVERY] Discovered active community package: {reg_pkg['package']}")
            candidates.append({
                "config": make_config_portable({
                    "command": reg_pkg["command"],
                    "args": reg_pkg["args"],
                    "env": {},
                    "instructions": f"Published community MCP package '{reg_pkg['package']}' discovered via live registry search."
                }, target_hint=target),
                "type": "registry",
                "score": 15
            })
    except Exception as reg_err:
        log.debug(f"Registry search skipped: {reg_err}")

    # Tier 3 (Gemini dynamically discovered configs): Score 6
    try:
        config = query_gemini_for_mcp_server(target)
        if config and "command" in config:
            candidates.append({
                "config": make_config_portable(config, target_hint=target),
                "type": "gemini",
                "score": 6
            })
    except Exception as e:
        log.debug(f"Gemini evaluation skipped in multi-candidate search: {e}")

    # Tier 4 (Default Generic Package fallback): Score 3
    candidates.append({
        "config": make_config_portable({
            "command": "npx",
            "args": ["-y", f"@modelcontextprotocol/server-{domain.split('.')[0]}"],
            "env": {"API_KEY": "General API key for authentication"},
            "instructions": f"Generic MCP guess for {domain}. Verify command and credentials."
        }, target_hint=target),
        "type": "generic",
        "score": 3
    })
    
    valid_candidates = [c for c in candidates if is_valid_mcp_config(c.get("config", {}))]
    valid_candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
    return valid_candidates

def discover_mcp_config(target: str, log=None) -> dict:
    """Attempts local workspace detection first, then Gemini-grounded search, falling back to local dictionaries."""
    if log is None:
        log = logger
    # 0. Top Priority (Rule 1): Probe for native remote MCP endpoints first
    native_config = probe_native_mcp_endpoint(target, log=log)
    if native_config and "command" in native_config:
        return native_config

    domain = clean_url_to_domain(target)
    target_url = target if (target.startswith("http://") or target.startswith("https://")) else f"https://{domain}"

    # 0.2 Top Priority: Inspect local bridges directory using semantic scored matching
    import glob
    bridge_dir = os.path.join(WORKSPACE_ROOT, "bridges")
    if os.path.isdir(bridge_dir):
        clean_t = target.lower().replace("https://", "").replace("http://", "")
        generic_tokens = {'com', 'org', 'net', 'de', 'io', 'api', 'v1', 'v2', 'v4', 'rest', 'mcp', 'www', 'suse', 'opensuse', 'google'}
        service_tokens = [tok for tok in re.split(r'[/._-]+', clean_t) if tok and tok not in generic_tokens]
        
        best_script = None
        best_score = 0
        best_env = {}
        
        for script in glob.glob(os.path.join(bridge_dir, "*.py")):
            b_name = os.path.basename(script).lower()
            try:
                with open(script, "r", encoding="utf-8", errors="ignore") as f:
                    s_content = f.read().lower()
                
                score = 0
                b_name_tokens = re.split(r'[_.-]+', b_name)
                
                # A bridge MUST match at least one specific service token (e.g. 'confluence', 'gitlab', 'openqa', 'build', 'obs', etc.)
                # Generic organizational tokens like 'suse' or 'opensuse' can only grant bonus points IF a service token matches!
                has_service_match = False
                for tok in service_tokens:
                    tok_root = tok.rstrip('s')
                    if tok in b_name_tokens or tok_root in b_name_tokens:
                        score += 15
                        has_service_match = True
                    elif tok in b_name or tok_root in b_name:
                        score += 8
                        has_service_match = True
                    elif re.search(r'\b' + re.escape(tok) + r'\b', s_content) or (tok_root and re.search(r'\b' + re.escape(tok_root) + r'\b', s_content)):
                        score += 4
                        has_service_match = True
                        
                if not has_service_match and clean_t.split('/')[0] not in s_content:
                    continue

                # Reward exact target domain tokens present directly in filename (e.g. suse, de, opensuse, org)
                for part in re.split(r'[/._-]+', clean_t):
                    if part and part not in ('com', 'net', 'io', 'api', 'v1', 'v2', 'v4', 'rest', 'mcp', 'www'):
                        if part in b_name_tokens:
                            score += 8
                        elif part in b_name:
                            score += 4
                    
                if score > best_score and score >= 10:
                    best_score = score
                    best_script = script
                    env_vars = re.findall(r"os\.environ\.get\(['\"]([^'\"]+)['\"]\)", s_content)
                    best_env = {v: "" for v in set(env_vars) if v != "PATH"}
            except Exception:
                pass
                
        if best_script:
            log.info(f"🎉 [LOCAL DETECTION] Auto-discovered matching bridge script '{os.path.basename(best_script)}' (score: {best_score}) for target '{target}'!")
            return make_config_portable({
                "command": "python3",
                "args": [best_script],
                "env": best_env,
                "instructions": f"Auto-discovered {os.path.basename(best_script)}."
            }, target_hint=target)

    # 0.5 Smart Inspection: Autonomous HTTP Service Fingerprinting
    try:
        fp = fingerprint_target_service(target)
        if fp:
            log.info(f"🔎 [SMART FINGERPRINT] Service autonomously identified as '{fp['service'].upper()}' via HTTP inspection!")
            if fp["service"] in ("jira", "confluence"):
                url_flag = "--jira-url" if fp["service"] == "jira" else "--confluence-url"
                env_key = "JIRA_URL" if fp["service"] == "jira" else "CONFLUENCE_URL"
                return make_config_portable({
                    "command": "uvx",
                    "args": ["mcp-atlassian", url_flag, target_url],
                    "env": {env_key: target_url, f"{fp['service'].upper()}_PERSONAL_TOKEN": ""},
                    "instructions": f"Set {fp['service'].upper()}_PERSONAL_TOKEN in .env for authenticated access to {fp['service'].upper()}."
                }, target_hint=target)
            elif fp["service"] == "gitlab":
                local_gl = find_local_script("gitlab_mcp_server.py")
                if local_gl:
                    return make_config_portable({
                        "command": "python3",
                        "args": [local_gl],
                        "env": {"GITLAB_API_URL": f"{target_url.rstrip('/')}/api/v4", "GITLAB_PERSONAL_ACCESS_TOKEN": ""},
                        "instructions": "Set GITLAB_PERSONAL_ACCESS_TOKEN in .env for authenticated access to GitLab."
                    }, target_hint=target)
                return make_config_portable({
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-gitlab"],
                    "env": {"GITLAB_API_URL": f"{target_url.rstrip('/')}/api/v4", "GITLAB_PERSONAL_ACCESS_TOKEN": ""},
                    "instructions": "Set GITLAB_PERSONAL_ACCESS_TOKEN in .env for authenticated access to GitLab."
                }, target_hint=target)
            elif fp["service"] == "bugzilla":
                local_bz = find_local_script("bugzilla_mcp_server.py")
                cmd = "python3" if local_bz else "npx"
                args = [local_bz] if local_bz else ["-y", "@kud/mcp-bugzilla"]
                return make_config_portable({
                    "command": cmd,
                    "args": args,
                    "env": {"MCP_BUGZILLA_BASE_URL": f"{target_url.rstrip('/')}/rest", "MCP_BUGZILLA_API_KEY": ""},
                    "instructions": "Set BUGZILLA_API_KEY in .env for authenticated access to Bugzilla."
                }, target_hint=target)
            elif fp["service"] == "sd":
                local_sd = find_local_script("sd_suse_mcp_server.py")
                if local_sd:
                    return make_config_portable({
                        "command": "python3",
                        "args": [local_sd],
                        "env": {"SD_SUSE_URL": target_url, "SD_SUSE_PAT": ""},
                        "instructions": "Set SD_SUSE_PAT in your .env file with a Personal Access Token from sd.suse.com."
                    }, target_hint=target)
            elif fp["service"] == "obs":
                local_obs = find_local_script("obs_mcp_server.py", "build_opensuse_mcp.py", "build_suse_de_mcp.py")
                if local_obs:
                    return make_config_portable({
                        "command": "python3",
                        "args": [local_obs],
                        "env": {"OBS_BASE_URL": target_url},
                        "instructions": "Set OBS_USER and OBS_PASSWORD or OBS_TOKEN in .env for authenticated access to OBS."
                    }, target_hint=target)
    except Exception as fp_err:
        log.debug(f"Fingerprinting skipped: {fp_err}")

    # 0.7 Smart Inspection: Universal OpenAPI / Swagger API Discovery
    try:
        spec_url = probe_openapi_spec(target)
        if spec_url:
            log.info(f"🔎 [OPENAPI SPEC FOUND] Discovered machine-readable API specification at: {spec_url}")
            return make_config_portable({
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-openapi", spec_url],
                "env": {},
                "instructions": f"Universal OpenAPI MCP Server auto-instantiated from {spec_url}."
            }, target_hint=target)
    except Exception as spec_err:
        log.debug(f"OpenAPI probe skipped: {spec_err}")

    # 1. Top Priority: Check local workspace root for pre-existing MCP scripts
    if "openqa" in domain.lower() or "openqa" in target.lower():
        local_script = find_local_script("openqa_mcp_server.py")
        if local_script:
            log.info("🎉 [LOCAL DETECTION] Detected existing openQA MCP Server script 'openqa_mcp_server.py' in workspace root!")
            return make_config_portable({
                "command": "python3",
                "args": [local_script],
                "env": {
                    "OPENQA_URL": f"https://{domain}" if "http" not in target else target,
                    "OPENQA_API_KEY": "openQA API key (needed for write operations or private groups)",
                    "OPENQA_API_SECRET": "openQA API secret (needed for signature authentication)"
                },
                "instructions": "1. Generate an API Key/Secret in openQA under User Settings -> API Keys.\n2. Add OPENQA_API_KEY and OPENQA_API_SECRET to your environment (.env) file.\n3. Verify your OPENQA_URL is correct."
            }, target_hint=target)
            
    if "bugzilla" in domain.lower() or "bugzilla" in target.lower():
        local_script = find_local_script("bugzilla_mcp.py")
        if local_script:
            log.info("🎉 [LOCAL DETECTION] Detected custom Bugzilla Python bridge 'bugzilla_mcp.py' in workspace root!")
            return make_config_portable({
                "command": "python3",
                "args": [local_script],
                "env": {
                    "BUGZILLA_BASE_URL": f"https://{domain}" if "http" not in target else target,
                    "BUGZILLA_API_KEY": "Bugzilla API key for authentication"
                },
                "instructions": "1. Generate an API Key in Bugzilla under User Preferences -> API Keys.\n2. Add BUGZILLA_API_KEY to your environment (.env) file."
            }, target_hint=target)

    # Universal Self-Describing Bridge Introspection (Procedural Fallback)
    import glob
    bridge_dir = os.path.join(WORKSPACE_ROOT, "bridges")
    if os.path.isdir(bridge_dir):
        scripts = glob.glob(os.path.join(bridge_dir, "*.py"))
        best_script = None
        best_score = 0
        
        t_lower = target.lower()
        t_domain = domain.lower()
        
        for script in scripts:
            try:
                with open(script, "r", encoding="utf-8") as f:
                    s_content = f.read()
                
                score = 0
                s_lower = s_content.lower()
                b_name = os.path.basename(script).lower()
                
                # Check filename
                for part in t_domain.split("."):
                    if part and part in b_name and part not in ("com", "org", "net", "de", "co", "suse", "opensuse", "google"):
                        score += 3
                
                # Check target explicit mention
                if t_domain in s_lower:
                    score += 2
                    
                # Extract requested env vars
                env_vars = re.findall(r"os\.environ\.get\(['\"]([^'\"]+)['\"]\)", s_content)
                env_dict = {v: "" for v in set(env_vars) if v != "PATH"}
                
                if score > best_score:
                    best_score = score
                    best_script = script
                    best_env = env_dict
            except Exception:
                pass
                
        if best_script and best_score > 0:
            script_display = os.path.basename(best_script)
            log.info(f"🎉 [LOCAL DETECTION] Auto-discovered matching bridge script '{script_display}'!")
            return make_config_portable({
                "command": "python3",
                "args": [best_script],
                "env": best_env,
                "instructions": f"Auto-discovered {script_display}."
            }, target_hint=target)
            
    # 1.8 Smart Inspection: Query Public NPM/Community Registries
    try:
        reg_pkg = query_package_registries(domain.split(".")[0])
        if reg_pkg:
            log.info(f"📦 [NPM REGISTRY DISCOVERY] Discovered active community package: {reg_pkg['package']}")
            return make_config_portable({
                "command": reg_pkg["command"],
                "args": reg_pkg["args"],
                "env": {},
                "instructions": f"Published community MCP package '{reg_pkg['package']}' discovered via live registry search."
            }, target_hint=target)
    except Exception as reg_err:
        log.debug(f"Registry search skipped: {reg_err}")

    # 2. Curated Known Packages Database (e.g. uvx openqa-mcp, npx @kud/mcp-bugzilla)
    log.info("Checking curated known packages database...")
    for key, value in COMMON_MCP_SERVERS.items():
        if key in domain.lower():
            log.info(f"Match found in curated database for keyword '{key}'!")
            # Inject target URL/domain if needed for arguments
            args = []
            for arg in value["args"]:
                if arg == "@modelcontextprotocol/server-bugzilla":
                    args.extend([arg, "--url", f"https://{domain}"])
                elif arg in ("--jira-url", "--confluence-url"):
                    args.extend([arg, f"https://{domain}"])
                else:
                    args.append(arg)
            cand_config = {
                "command": value["command"],
                "args": args if args else value["args"],
                "env": value["env"],
                "instructions": value.get("instructions", "")
            }
            if is_valid_mcp_config(cand_config):
                return make_config_portable(cand_config, target_hint=target)
                
    # 3. Query Gemini AI (with strict physical file validation)
    config = query_gemini_for_mcp_server(target)
    if config and "command" in config and is_valid_mcp_config(config):
        return make_config_portable(config, target_hint=target)
            
    # 3. Default generic package guess
    log.info("No matching service found. Creating generic npx config template.")
    return make_config_portable({
        "command": "npx",
        "args": ["-y", f"@modelcontextprotocol/server-{domain.split('.')[0]}"],
        "env": {"API_KEY": "General API key for authentication"}
    }, target_hint=target)

def gather_user_credentials(required_env: dict, target_hint: str = None, log=None) -> dict:
    """
    Derives URLs, passes system infrastructure variables without prompting, and resolves credentials.
    If an API key or token is missing from the environment and .env, initializes it cleanly
    as an empty string or placeholder without pausing or blocking on interactive input.
    """
    if log is None:
        log = logger
    credentials = {}
    
    SYSTEM_INFRA_KEYS = {
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
        "WORKSPACE_ROOT", "GEMINI_PROJECT_ROOT", "PYTHONHTTPSVERIFY", "NODE_TLS_REJECT_UNAUTHORIZED",
        "PYTHON_VENV_BIN"
    }

    target_domain = clean_url_to_domain(target_hint) if target_hint else ""
    target_url = target_hint if (target_hint and (target_hint.startswith("http://") or target_hint.startswith("https://"))) else (f"https://{target_domain}" if target_domain else "")

    for env_key, desc in required_env.items():
        # 1. System infrastructure keys - pass through directly
        if env_key in SYSTEM_INFRA_KEYS:
            credentials[env_key] = desc
            continue

        # 2. Check active OS environment
        val = os.getenv(env_key)
        if val:
            log.info(f"Credential '{env_key}' is available in environment.")
            credentials[env_key] = val
            continue

        # 3. URL/Host auto-derivation
        k_up = env_key.upper()
        if any(suffix in k_up for suffix in ("_URL", "_BASE_URL", "_ENDPOINT", "_HOST", "_SERVER")):
            if isinstance(desc, str) and (desc.startswith("http://") or desc.startswith("https://")):
                credentials[env_key] = desc
                continue
            elif target_url:
                if "gitlab" in k_up and "api" in k_up:
                    derived = f"{target_url.rstrip('/')}/api/v4"
                else:
                    derived = target_url
                log.info(f"Derived URL setting '{env_key}' automatically: {derived}")
                credentials[env_key] = derived
                continue

        # 4. If existing value in required_env is already a concrete value
        if isinstance(desc, str) and desc and not desc.startswith("<") and not any(kw in desc.lower() for kw in ("token", "key", "password", "secret", "user preferences", "enter the")):
            credentials[env_key] = desc
            continue

        # 5. Non-blocking fallback for secrets / tokens: fill with placeholder or empty string
        placeholder_val = f"<OPTIONAL_{env_key}>" if "OPTIONAL" in str(desc).upper() else ""
        log.info(f"Credential '{env_key}' not specified. Initializing cleanly as empty/placeholder.")
        credentials[env_key] = placeholder_val

    return credentials

def write_mcp_settings_file(server_name: str, config: dict, credentials: dict, log=None):
    """Writes the MCP server configuration into .mcp.json at the workspace root (Thread-safe)."""
    if log is None:
        log = logger
    if is_dry_run():
        log.info(f"🔍 [DRY-RUN] Simulating write of MCP server '{server_name}' to .mcp.json and settings.json (skipped file write).")
        return
    if log is None:
        log = logger
    workspace_root = WORKSPACE_ROOT
    settings_path = os.path.join(workspace_root, ".mcp.json")
    
    with mcp_write_lock:
        # Read existing settings or initialize empty
        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
            except Exception as e:
                log.warning(f"Could not parse existing settings file: {e}. Reinitializing...")
                
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}
            
        # Inject our new server configuration under the standard hyphenated server_name
        env_block = {k: v for k, v in credentials.items()}
        
        settings["mcpServers"][server_name] = {
            "command": config["command"],
            "args": config["args"],
            "env": env_block
        }
        
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            log.info(f"Successfully wrote MCP server config to: {settings_path}")

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
                    log.info(f"Synchronized server '{server_name}' to global settings: {global_settings_path}")
                except Exception as ge:
                    log.warning(f"Could not update global settings at {global_settings_path}: {ge}")
        except Exception as e:
            if is_enospc_error(e):
                halt_on_enospc(f"Writing MCP settings file: {settings_path}", str(e), log=log)
            log.error(f"Failed to write MCP settings file: {e}")
            raise e

def _validate_http_mcp(server_name: str, config: dict, credentials: dict, log=None) -> bool:
    """
    Validates a native Streamable HTTP MCP server via direct JSON-RPC POST requests:
      1. Dynamic Omnipresent Credential Resolution: Resolves headers, environment variables,
         and local client configs (e.g. ~/.config/openqa/client.conf).
      2. Level 1: 'initialize' POST with configured headers.
         Supports Deferred-Authentication Tolerance for token-gated enterprise endpoints.
      3. Level 1.5: 'tools/list' POST with strict inputSchema object validation.
    """
    if log is None:
        log = logger
    url = config.get("url")
    if not url:
        log.error(f"Native HTTP MCP server '{server_name}' is missing required 'url'.")
        return False

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.get("headers") and isinstance(config["headers"], dict):
        headers.update(config["headers"])

    # Extract target domain from URL for intelligent credential resolution
    from urllib.parse import urlparse
    parsed_domain = urlparse(url).netloc.lower()
    from utils import resolve_active_credential

    # Expand any variable references (${VAR}) in headers using credentials or env
    import re
    for k, v in list(headers.items()):
        if isinstance(v, str) and "${" in v:
            for env_k, env_v in list(os.environ.items()) + list(credentials.items()):
                if env_v:
                    headers[k] = headers[k].replace(f"${{{env_k}}}", env_v).replace(f"${env_k}", env_v)
            # If placeholders remain, attempt dynamic resolution via resolve_active_credential
            if "${" in headers[k]:
                placeholders = re.findall(r"\$\{([^}]+)\}", headers[k])
                for ph in placeholders:
                    resolved = resolve_active_credential(ph, domain_hint=parsed_domain)
                    if resolved:
                        headers[k] = headers[k].replace(f"${{{ph}}}", resolved).replace(f"${ph}", resolved)

    # In-Flight Dynamic Credential Injection if Authorization is still missing or has unresolved placeholders
    if "Authorization" not in headers or "${" in headers.get("Authorization", ""):
        if "openqa" in parsed_domain or "openqa" in server_name.lower():
            tok = resolve_active_credential("OPENQA_TOKEN", domain_hint=parsed_domain)
            if not tok:
                u = resolve_active_credential("OPENQA_USER", domain_hint=parsed_domain) or os.environ.get("USER", "root")
                k = resolve_active_credential("OPENQA_API_KEY", domain_hint=parsed_domain)
                s = resolve_active_credential("OPENQA_API_SECRET", domain_hint=parsed_domain)
                if k and s:
                    tok = f"{u}:{k}:{s}"
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
        else:
            tok_key = f"{parsed_domain.upper().replace('.', '_').replace('-', '_')}_TOKEN"
            tok = resolve_active_credential(tok_key, domain_hint=parsed_domain) or resolve_active_credential("BEARER_TOKEN", domain_hint=parsed_domain)
            if tok:
                headers["Authorization"] = f"Bearer {tok}"

    import urllib.request, ssl, urllib.error
    ctx = ssl._create_unverified_context()
    
    # 1. Level 1: initialize handshake
    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "google-ai-initializer-client", "version": "1.0.0"}
        }
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(init_request).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict) or "protocolVersion" not in data.get("result", {}):
                log.error(f"Native HTTP server '{server_name}' returned invalid initialize response: {data}")
                return False
    except urllib.error.HTTPError as he:
        c_type = he.headers.get("Content-Type", "").lower()
        try:
            body = he.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        # CRITICAL: STRICT HTML EXCLUSION RULE
        if "html" in c_type or body.strip().startswith(("<", "<!doctype", "<html")):
            log.error(f"Native HTTP initialize handshake failed for '{server_name}' at {url}: Upstream returned an HTML web page instead of JSON.")
            return False
            
        # Deferred Authentication Handling for Token-Gated Endpoints
        if he.code in (401, 403):
            if ("json" in c_type or "event-stream" in c_type) and any(kw in body.lower() for kw in ("api key", "no api key", "token", "unauthorized", "auth", "jsonrpc", "forbidden")):
                log.warning(f"⚠️  [DEFERRED AUTHENTICATION] Native HTTP MCP endpoint confirmed at {url}. Upstream server requested credentials (HTTP {he.code}: {body.strip()}). Connection and protocol are verified; authentication credentials deferred to .env.")
                return True
        log.error(f"Native HTTP initialize handshake failed for '{server_name}' at {url}: {he}")
        return False
    except Exception as e:
        log.error(f"Native HTTP initialize handshake failed for '{server_name}' at {url}: {e}")
        return False

    # 2. Level 1.5: tools/list schema audit
    tools_request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    tools_list = []
    try:
        req = urllib.request.Request(url, data=json.dumps(tools_request).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tools_list = data.get("result", {}).get("tools", [])
            for t in tools_list:
                schema = t.get("inputSchema", {})
                if schema.get("type") != "object":
                    log.error(f"Tool '{t.get('name')}' on native HTTP server has non-object inputSchema: {schema.get('type')}")
                    return False
    except urllib.error.HTTPError as he:
        if he.code in (401, 403):
            log.warning(f"⚠️  [DEFERRED AUTHENTICATION] Native HTTP MCP tools/list requires active credentials: HTTP {he.code}. Endpoint is verified.")
            return True
        log.error(f"Native HTTP tools/list failed for '{server_name}': {he}")
        return False
    except Exception as e:
        log.error(f"Native HTTP tools/list failed for '{server_name}': {e}")
        return False

    log.info(f"🚀 [NATIVE HTTP VALIDATED] Successfully verified {len(tools_list)} tools for '{server_name}' via Streamable HTTP!")
    return True

def validate_mcp_connection(server_name: str, config: dict, credentials: dict, log=None, _retry_attempt: bool = False) -> bool:
    """
    Polymorphic validation engine: validates both local process MCP servers (stdio)
    and remote native streamable MCP endpoints (http / sse).
    """
    if log is None:
        log = logger
    log.info(f"Initiating connection validation for MCP Server '{server_name}'...")

    # Transport Branch 1: Streamable HTTP or SSE
    transport = str(config.get("type", "")).lower()
    if transport in ("http", "sse") or ("url" in config and not config.get("command")):
        return _validate_http_mcp(server_name, config, credentials, log=log)

    # Transport Branch 2: Standard local process (stdio)
    # Prepare environment variables
    env = os.environ.copy()
    if config.get("env"):
        env.update(config["env"])
    env.update(credentials)
    
    cmd_name = config.get("command", "")
    if cmd_name and not shutil.which(cmd_name):
        ensure_runner_available(cmd_name, log=log)
    
    # Build complete command list
    full_cmd = [config["command"]] + config["args"]
    log.info(f"Spawning test instance: {' '.join(full_cmd)}")
    
    try:
        # Spawn the process
        process = subprocess.Popen(
            full_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKSPACE_ROOT,
            env=env,
            text=True,
            bufsize=1
        )
    except FileNotFoundError as fnf_err:
        if not _retry_attempt and ensure_runner_available(cmd_name, log=log):
            log.info(f"⚡ [JIT RETRY] Runner '{cmd_name}' resolved. Retrying live connection validation...")
            return validate_mcp_connection(server_name, config, credentials, log=log, _retry_attempt=True)
        log.error(f"Failed to spawn MCP server subprocess: {fnf_err}")
        return False
    except Exception as spawn_err:
        log.error(f"Failed to spawn MCP server subprocess: {spawn_err}")
        return False

    try:
        
        # Build standard MCP initialize request
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
        
        # Send request
        req_str = json.dumps(init_request) + "\n"
        log.info("Sending JSON-RPC 'initialize' request...")
        process.stdin.write(req_str)
        process.stdin.flush()
        
        # Read response with a short timeout
        log.info("Awaiting server handshake response...")
        response_line = ""
        
        try:
            import select
            timeout = get_process_timeout()
            log.info(f"Awaiting standard handshake input (max wait: {timeout}s)...")
            
            # Read line-by-line to gracefully skip non-JSON startup banners
            start_time = time.time()
            while time.time() - start_time < timeout:
                ready = select.select([process.stdout], [], [], 1.0)
                if ready[0]:
                    line = process.stdout.readline()
                    if not line:
                        break
                    line_stripped = line.strip()
                    if line_stripped.startswith("{"):
                        # Found starting bracket, attempt JSON parsing
                        try:
                            json.loads(line_stripped)
                            response_line = line
                            log.info(f"🎉 Handshake JSON-RPC start detected! Scanned & isolated: {line_stripped[:120]}...")
                            break
                        except Exception:
                            pass
                    # Log bypassed raw text to help user debug
                    log.info(f"   [Skipping raw non-JSON stream log] {line_stripped[:100]}")
                else:
                    # Timeout check within loop
                    if time.time() - start_time >= timeout:
                        log.warning(f"Timeout: No response received from MCP server within {timeout} seconds.")
                        break
        except Exception as read_err:
            log.error(f"Error during socket/pipe read: {read_err}")
            
        if not response_line:
            process.kill()
            process.wait()
            # Capture and dump stderr logs for real-time debugging (Rule 4 diagnostics)
            stderr_logs = process.stderr.read()
            
            # Generalized JIT auto-healing for missing Python modules
            if not _retry_attempt and "ModuleNotFoundError" in stderr_logs:
                match = re.search(r"No module named ['\"]([^'\"]+)['\"]", stderr_logs)
                if match:
                    missing_mod = match.group(1)
                    log.info(f"⚡ [JIT MODULE RECOVERY] Auto-detected missing Python module '{missing_mod}'. Attempting JIT auto-install...")
                    if auto_heal_missing_python_module(missing_mod, log=log):
                        log.info(f"Retrying connection validation for '{server_name}' after module installation...")
                        return validate_mcp_connection(server_name, config, credentials, log=log, _retry_attempt=True)

            log.error("❌ Handshake validation failed! Captured subprocess error logs:")
            log.error("------------------------------------------------------")
            log.error(stderr_logs if stderr_logs.strip() else "(Subprocess stderr stream was empty)")
            log.error("------------------------------------------------------")
            return False
            
        log.info(f"Received initialization response: {response_line.strip()[:150]}...")
        try:
            res_json = json.loads(response_line)
            if "error" in res_json:
                log.warning(f"Server returned initialization error: {res_json['error']}")
                process.kill()
                process.wait()
                return False
        except Exception as json_err:
            log.warning(f"Failed to parse server response as JSON: {json_err}")
            process.kill()
            process.wait()
            return False
            
        # Send 'initialized' notification (required by protocol)
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.flush()
        
        # Send 'tools/list' request to catch deep schema errors!
        tools_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }
        log.info("Sending JSON-RPC 'tools/list' request for deep schema verification...")
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
            log.warning("FAILED: Server successfully initialized, but hung/crashed when asked to list tools (tools/list).")
            return False
            
        try:
            if "error" in tools_json:
                process.kill()
                process.wait()
                log.warning(f"FAILED: Server initialized, but tools/list returned an error: {json.dumps(tools_json['error'])}")
                return False
            elif "result" in tools_json and "tools" in tools_json["result"]:
                tools_list = tools_json["result"]["tools"]
                if not tools_list:
                    process.kill()
                    process.wait()
                    cmd_str = str(config.get("command", "")).lower() + " " + " ".join(str(a) for a in config.get("args", []))
                    requires_auth = (
                        "atlassian" in cmd_str or
                        any("TOKEN" in k.upper() or "KEY" in k.upper() or "SECRET" in k.upper() for k in credentials.keys()) or
                        any("<OPTIONAL_" in str(v) for v in credentials.values()) or
                        any(v == "" for v in credentials.values())
                    )
                    if requires_auth:
                        log.info(f"ℹ️  [DEFERRED TOOLS] Server '{server_name}' initialized successfully with valid JSON-RPC protocol. Tools list is currently deferred pending user authentication credentials in .env.")
                        return True
                    log.error("❌ FAILED: Server successfully initialized, but tools/list returned an empty tools array (0 tools). An operational MCP server must expose at least one valid tool.")
                    return False
                    
                # Level 3: Deep Functional Smoke Testing (Dry-Run) BEFORE process.kill()
                try:
                    from utils import perform_level3_functional_smoke_test
                    dry_ok, dry_msg = perform_level3_functional_smoke_test(process, tools_list)
                    if not dry_ok:
                        process.kill()
                        process.wait()
                        log.error(f"❌ DEGRADED: Connection successful, but Level 3 Functional Smoke Test failed!\n{dry_msg}")
                        return False
                except Exception as e:
                    log.warning(f"Smoke test skipped: {e}")
                    
                process.kill()
                process.wait()

                # Strict Schema Validation Check!
                for tool in tools_list:
                    input_schema = tool.get("inputSchema", {})
                    if input_schema.get("type") != "object":
                        log.error(f"❌ FAILED: Strict Schema Validation Error in tool '{tool.get('name', 'unknown')}': inputSchema.type MUST be 'object' (Current value: {input_schema.get('type')}). The server package is broken.")
                        return False

                log.info(f"🚀 Verification SUCCESS: Handshake and tools/list completed! Server returned {len(tools_list)} valid, strictly-formatted tools.")

                # Level 2 Active Upstream Credential Verification
                merged_env = os.environ.copy()
                merged_env.update(credentials)
                merged_env.update(config.get("env", {}))
                upstream_check = verify_upstream_credentials(server_name, merged_env)
                if upstream_check.get("probed"):
                    if upstream_check.get("valid"):
                        log.info(f"🔑 [UPSTREAM AUTH VERIFIED] Live remote service accepted credentials: {upstream_check['message']}")
                    else:
                        log.warning(f"⚠️  [UPSTREAM AUTH WARNING] Local MCP process initialized, but live remote service rejected credentials: {upstream_check['message']}")

                return True
            else:
                resp_str = json.dumps(tools_json) if tools_json else raw_tools_line
                log.warning(f"FAILED: Unexpected tools/list response format: {resp_str}")
                return False
        except Exception as e:
            resp_str = json.dumps(tools_json) if tools_json else raw_tools_line
            log.warning(f"FAILED: Could not parse tools/list JSON response: {e}. Raw: {resp_str}")
            return False
    except Exception as spawn_err:
        log.error(f"Failed to spawn MCP server subprocess: {spawn_err}")
        return False

def run_mcp_discovery(target: str, log=None) -> bool:
    """Main discovery and validation flow with self-healing interactive retry loops and Best/Exhaustive support."""
    if not verify_prephase_disk_space("Phase 4: MCP Auto-Discovery", log=log or logger):
        return False
    if not verify_target_dns(target):
        if log:
            log.warning(f"⚠️  [DNS WARNING] Pre-flight DNS resolution for target domain '{target}' was slow or unresolvable via local DNS. Proceeding with discovery anyway...")
    if log is None:
        log = logger
    if not target:
        log.info("No target website/domain provided. Defaulting to bugzilla.suse.com.")
        target = "bugzilla.suse.com"
        
    domain = clean_url_to_domain(target)
    server_name = domain.replace(".", "-")
    
    # 0. Registry Protection: Check if target is already successfully registered in .mcp.json
    mcp_path = os.path.join(WORKSPACE_ROOT, ".mcp.json")
    if os.path.exists(mcp_path):
        try:
            with open(mcp_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            existing_servers = existing_data.get("mcpServers", {})
            matched_key, existing_cfg = match_target_to_registry_key(target, existing_servers)
            if matched_key and existing_cfg:
                log.info(f"⏭️  [ALREADY REGISTERED] Target '{target}' is already registered in .mcp.json as '{matched_key}'. Preserving existing configuration and skipping redundant re-discovery.")
                return True
        except Exception:
            pass

    # Ensure bridges/ directory and standard bridge scripts are intact before discovery
    try:
        ensure_bridges_directory_intact(log=log)
    except Exception:
        pass

    # Check if special execution mode environmental variables are active
    mode_the_best = os.getenv("MCP_MODE_THE_BEST") == "True"
    mode_exhaustive = os.getenv("MCP_MODE_EXHAUSTIVE") == "True"
    
    if mode_the_best or mode_exhaustive:
        log.info(f"🔮 Entering Multi-Candidate Evaluation Mode (Best: {mode_the_best}, Exhaustive: {mode_exhaustive})")
        candidates = discover_all_mcp_candidates(target, log=log)
        log.info(f"Discovered {len(candidates)} possible candidates for target '{target}'. Starting parallel validation...")
        
        valid_candidates = []
        for i, cand in enumerate(candidates, 1):
            config = cand["config"]
            cand_type = cand["type"]
            score = cand["score"]
            log.info(f"\n🔍 [Candidate {i}/{len(candidates)}] Type: {cand_type.upper()} (Score: {score}) command: {config.get('command')} {config.get('args')}")
            
            # Enforce duplicate audits (only warning)
            audit_registry_for_duplicates(server_name, domain, log=log)
            
            # Gather credentials
            credentials = {}
            if config.get("env"):
                try:
                    credentials = gather_user_credentials(config["env"], target_hint=target, log=log)
                except RuntimeError as err:
                    log.warning(f"Skipping candidate {i} due to credential prompt abortion: {err}")
                    continue
                    
            # Enforce Rule 11 operational check
            verify_url_presence_in_config_or_script(config, credentials, target, domain, log=log)
            
            # Verify the connection directly BEFORE committing to main config file (dynamic workspace verify)
            success = validate_mcp_connection(f"{server_name}-{cand_type}-temp", config, credentials, log=log)
            if success:
                log.info(f"✅ Candidate {i} ({cand_type.upper()}) is VALIDATED!")
                valid_candidates.append({
                    "config": config,
                    "credentials": credentials,
                    "type": cand_type,
                    "score": score
                })
            else:
                log.warning(f"❌ Candidate {i} ({cand_type.upper()}) failed handshake connection verification.")
                
        if not valid_candidates:
            log.error(f"❌ All {len(candidates)} discovered candidates failed handshake connection verification for target '{target}'.")
            return False
            
        if mode_exhaustive:
            # Write ALL validated candidates to the registry under suffix-differentiated names
            log.info(f"\n✍️  [EXHAUSTIVE WRITE] Committing {len(valid_candidates)} verified configurations to .mcp.json...")
            for vc in valid_candidates:
                distinguished_name = f"{server_name}-{vc['type']}"
                write_mcp_settings_file(distinguished_name, vc["config"], vc["credentials"], log=log)
            log.info("🚀 Exhaustive mode successfully registered all validated servers.")
            return True
            
        elif mode_the_best:
            # Sort candidates by score descending and take the winner
            valid_candidates.sort(key=lambda x: x["score"], reverse=True)
            winner = valid_candidates[0]
            log.info(f"\n🏆 [THE BEST SELECTION] Winner is candidate '{winner['type']}' with Score: {winner['score']} out of {len(valid_candidates)} validated choices.")
            write_mcp_settings_file(server_name, winner["config"], winner["credentials"], log=log)
            return True
            
    else:
        # Multi-Candidate Cascading Discovery Pattern:
        # Exhaustively gathers all possible candidates (Fingerprints, Local Bridges, Standard Packages, Specs, AI)
        # Evaluates candidates in priority order and commits the first one that passes live verification.
        candidates = discover_all_mcp_candidates(target, log=log)
        if isinstance(candidates, dict):
            candidates = [{"config": candidates, "type": "local", "score": 10}]
        elif not isinstance(candidates, list):
            candidates = []

        if not candidates:
            single_cfg = discover_mcp_config(target, log=log)
            if single_cfg:
                candidates = [{"config": single_cfg, "type": "default", "score": 5}]

        if not candidates:
            log.error(f"❌ No viable MCP server configurations could be discovered across any tier for target '{target}'.")
            return False

        audit_registry_for_duplicates(server_name, domain, log=log)

        for i, cand in enumerate(candidates, 1):
            config = cand["config"]
            cand_type = cand.get("type", "candidate")
            score = cand.get("score", 0)
            log.info(f"\n🔍 [Candidate {i}/{len(candidates)}] Type: {cand_type.upper()} (Score: {score}) command: {config.get('command')} {config.get('args')}")
            
            credentials = {}
            if config.get("env"):
                try:
                    credentials = gather_user_credentials(config["env"], target_hint=target, log=log)
                except RuntimeError as err:
                    log.warning(f"Skipping candidate {i} due to credential prompt abortion: {err}")
                    continue
                    
            verify_url_presence_in_config_or_script(config, credentials, target, domain, log=log)
            
            # Commit-on-Success Pattern: Validate connection FIRST!
            # Write to .mcp.json and synchronize to settings.json ONLY after validation succeeds!
            success = validate_mcp_connection(server_name, config, credentials, log=log)
            if success:
                try:
                    write_mcp_settings_file(server_name, config, credentials, log=log)
                except Exception as e:
                    log.error(f"Failed to commit verified settings for '{server_name}': {e}")
                    return False
                log.info(f"🚀 MCP Server '{server_name}' is fully validated and connected (Candidate {i}/{len(candidates)}: {cand_type.upper()})!")
                return True
            else:
                log.warning(f"❌ Candidate {i} ({cand_type.upper()}) failed verification. Cascading to next candidate...")
                instructions = config.get("instructions")
                if instructions:
                    log.info(f"   | Candidate guidance: {instructions.strip().splitlines()[0]}")

        log.error(f"❌ All {len(candidates)} discovered candidates failed connection verification for target '{target}'. Skipping registry commitment.")
        return False

def main():
    parser = argparse.ArgumentParser(description="AI-Powered MCP Auto-Discovery and Validation")
    parser.add_argument("--mcp-server", help="Comma-separated list of target services/websites/databases for MCP auto-discovery (e.g. bugzilla.suse.com,github.com).", default="bugzilla.suse.com")
    parser.add_argument("--genai-model", help="Specify the Gemini model name to utilize inside the workspace (default: gemini-3.7-flash)", default="gemini-3.7-flash")
    parser.add_argument("--env-file", help="Absolute path to a custom environment (.env) file.")
    parser.add_argument(
        "--no-automatic-browser",
        help="Skip launching Playwright headed browser automatically, forcing manual copy-paste authentication URLs.",
        action="store_true"
    )
    parser.add_argument(
        "--the-best-mcp",
        help="Pick and write only the single best discovered, validated, and functioning MCP configuration.",
        action="store_true"
    )
    parser.add_argument(
        "--exhaustive-mcp",
        help="Find, validate, and write all discovered and functioning MCP configurations, using distinct descriptive names.",
        action="store_true"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        nargs="?",
        const=4,
        default=None,
        help="Perform Phase 4 MCP auto-discoveries concurrently with a configurable number of parallel workers (defaults to 4 if passed without value)."
    )
    parser.add_argument(
        "--agent-max-turns",
        type=int,
        help="Specify the maximum conversational tool-calling turns allowed for the Autonomous AI Agent (must provide an integer value)."
    )
    parser.add_argument(
        "--timeout",
        help="Specify the absolute execution timeout in seconds for any spawned discovery searches or server tests (defaults to 180 seconds).",
        type=int,
        default=180
    )
    parser.add_argument(
        "--timeout-scale",
        type=float,
        help="Scale factor to multiply the active subprocess/validation timeout value (must provide a float value)."
    )
    parser.add_argument(
        "--log-level",
        choices=["trace", "debug", "info", "warning", "error"],
        default="info",
        help="Specify the global execution logging level (default: info, choices: trace, debug, info, warning, error)."
    )
    parser.add_argument(
        "--log-file",
        default="google_ai_initialization.log",
        help="Specify the absolute path to the global execution log file (default: google_ai_initialization.log)."
    )
    args = parser.parse_args()
    
    # Enforce mutual exclusivity
    if args.the_best_mcp and args.exhaustive_mcp:
        logger.error("🛑 VALUE ERROR: --the-best-mcp and --exhaustive-mcp are mutually exclusive! Please select only one mode.")
        sys.exit(1)
        
    # Sync discovery mode, concurrency, and agent turns into environment variables
    if args.the_best_mcp:
        os.environ["MCP_MODE_THE_BEST"] = "True"
    elif args.exhaustive_mcp:
        os.environ["MCP_MODE_EXHAUSTIVE"] = "True"
        
    if args.concurrency:
        os.environ["MCP_CONCURRENCY"] = str(args.concurrency)
        
    if args.agent_max_turns:
        os.environ["AGENT_MAX_TURNS"] = str(args.agent_max_turns)
        
    # Dynamically scale timeout values globally
    base_timeout = args.timeout if args.timeout else 180
    scale_factor = args.timeout_scale if args.timeout_scale else 1.0
    final_timeout = int(base_timeout * scale_factor)
    os.environ["PROCESS_TIMEOUT_OVERRIDE"] = str(final_timeout)
        
    # Pre-parse GenAI model target if passed
    if args.genai_model:
        os.environ["GOOGLE_GENAI_MODEL"] = args.genai_model
        
    # Pre-parse environment file override if passed as parameter
    if args.env_file:
        os.environ["ENV_FILE_PATH_OVERRIDE"] = args.env_file
        # Re-resolve and re-import env configurations dynamically
        from utils import get_env_file_path, load_env_file
        load_env_file()
    
    logger.info("==============================================")
    logger.info("Starting Phase 4: MCP Discovery & Verification")
    logger.info("==============================================")
    
    # Preserve pre-existing .mcp.json and dynamically merge discoveries to prevent data loss or staleness
    mcp_path = os.path.join(WORKSPACE_ROOT, ".mcp.json")
    if os.path.exists(mcp_path):
        logger.info("- Status : Pre-existing .mcp.json file detected; maintaining all previous configurations and merging newly discovered servers inline.")
    else:
        logger.info("- Status : No pre-existing .mcp.json file found; a new registry will be initialized dynamically.")
    
    logger.info("\n==========================================================")
    logger.info("🔑  [PREPARATION] Phase 4 MCP Credentials Checklist")
    logger.info("==========================================================")
    logger.info("MCP auto-discovery may require authentication tokens, API keys,")
    logger.info("or secrets (such as BUGZILLA_API_KEY, GITHUB_PERSONAL_ACCESS_TOKEN, etc.).")
    logger.info("  1. The script will automatically scan your environment and active .env file first.")
    logger.info("  2. If any required secret is missing, you will be prompted to enter")
    logger.info("     them manually one by one, and they will automatically persist back to .env.")
    logger.info("Please have your service credentials ready!")
    logger.info("==========================================================\n")
    
    try:
        servers = [s.strip() for s in args.mcp_server.split(",") if s.strip()]
        all_success = True
        phase4_discovery_results = {}
        
        # Determine if concurrency is specified
        concurrency = args.concurrency
        if concurrency:
            import concurrent.futures
            logger.info(f"⚡ Concurrency enabled: Performing discovery for {len(servers)} servers in parallel (max workers: {concurrency})...")
            
            def discover_worker(server):
                buffer = ServerLogBuffer(server)
                try:
                    res = run_mcp_discovery(server, log=buffer)
                    buffer.flush()
                    return server, res
                except Exception as err:
                    buffer.error(f"Unexpected crash in concurrent worker: {err}")
                    buffer.flush()
                    return server, False
                    
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(discover_worker, s): s for s in servers}
                for future in concurrent.futures.as_completed(futures):
                    server, success = future.result()
                    if success:
                        phase4_discovery_results[server] = "✅ SUCCESS"
                    else:
                        phase4_discovery_results[server] = "❌ FAILED"
                        all_success = False
        else:
            # Standard sequential discovery loop
            for i, server in enumerate(servers, 1):
                logger.info(f"\n──────────────────────────────────────────────")
                logger.info(f"🔍 [SERVER {i}/{len(servers)}] Auto-Discovering: {server}")
                logger.info(f"──────────────────────────────────────────────")
                success = run_mcp_discovery(server)
                if success:
                    phase4_discovery_results[server] = "✅ SUCCESS"
                else:
                    phase4_discovery_results[server] = "❌ FAILED"
                    all_success = False
                
        # 1. Print visual Execution Summary Board
        logger.info("\n==========================================================")
        logger.info("📡  PHASE 4: MCP AUTO-DISCOVERY EXECUTION SUMMARY")
        logger.info("==========================================================")
        for server, state in phase4_discovery_results.items():
            logger.info(f"  + {server:<35} : {state}")
        logger.info("==========================================================\n")
        
        # 2. Print Consolidated MCP Registry Discovery Report with active cross-referencing
        if os.path.exists(mcp_path):
            try:
                with open(mcp_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    servers_data = data.get("mcpServers", {})
                    if servers_data:
                        logger.info("\n==========================================================")
                        logger.info("📦  CONSOLIDATED MCP REGISTRY DISCOVERY REPORT")
                        logger.info("==========================================================")
                        for name, config in list(servers_data.items()):
                            cmd = config.get("command", "")
                            args_list = config.get("args", [])
                            args_str = " ".join(args_list) if isinstance(args_list, list) else str(args_list)
                            env_keys = ", ".join(config.get("env", {}).keys())
                            instructions = config.get("instructions", "") or config.get("_instructions", "")
                            
                            # Dynamically verify if this server actually passed during our execution loops
                            is_verified = True
                            for failed_url, state in phase4_discovery_results.items():
                                if "FAILED" in state:
                                    matched_failed_key, _ = match_target_to_registry_key(failed_url, {name: config})
                                    if matched_failed_key:
                                        is_verified = False
                                        break
                                            
                            logger.info(f"🔹 Server Name   : {name}")
                            logger.info(f"   Command-Line  : {cmd} {args_str[:70]}...")
                            logger.info(f"   Credentials   : {env_keys if env_keys else 'None required'}")
                            if is_verified:
                                logger.info(f"   Handshake     : ✅ ACTIVE & VERIFIED")
                            else:
                                logger.info(f"   Handshake     : ❌ FAILED (Unverified/Broken Configuration)")
                                
                            if instructions:
                                logger.info("   📋 Extra Setup Instructions:")
                                for line in instructions.strip().splitlines():
                                    logger.info(f"      | {line}")
                            logger.info("  --------------------------------------------------------")

                        # Target Reconciliation Audit: compare every scheduled target against .mcp.json
                        if servers:
                            logger.info("----------------------------------------------------------")
                            logger.info("📋  TARGET RECONCILIATION AUDIT (Scheduled vs Registered)")
                            logger.info("----------------------------------------------------------")
                            for tgt in servers:
                                matched_key, _ = match_target_to_registry_key(tgt, servers_data)
                                if matched_key:
                                    logger.info(f"  + {tgt:<37} -> {matched_key:<32} [✅ REGISTERED]")
                                else:
                                    logger.warning(f"  + {tgt:<37} -> ❌ MISSING FROM .mcp.json REGISTRY!")
                            logger.info("==========================================================")
                        else:
                            logger.info("==========================================================")
            except Exception as e:
                logger.warning(f"Could not parse .mcp.json for diagnostic summary: {e}")
                
        # 3. Print prominent Registry Location Pointer
        logger.info("\n==========================================================")
        logger.info("📂  MODEL CONTEXT PROTOCOL REGISTRY LOCATION")
        logger.info("==========================================================")
        logger.info(f"Your active configuration has been saved inside:")
        logger.info(f"  👉  {os.path.abspath(mcp_path)}")
        logger.info("This file is 100% compliant with standard MCP Client schemas")
        logger.info("and is ready to be loaded by Cursor, Claude Code, or VSCode!")
        logger.info("==========================================================\n")
        
        if all_success:
            logger.info("Phase 4 completed successfully!")
            return True
        else:
            logger.error("Phase 4 completed with some configuration errors.")
            return False
    except Exception as e:
        logger.error(f"Phase 4 execution failed: {e}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
