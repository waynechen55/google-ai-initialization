def synchronize_gemini_cli_auth(auth_type: str, env_vars: dict = None, adc_source_file: str = None) -> bool:
    """
    Guarantees that manual execution of 'gemini' CLI in any terminal/directory
    will NOT require interactive authentication again across all auth modes:
    1. Sets 'security.auth.selectedType' in ~/.gemini/settings.json (bypassing the auth prompt).
    2. Synchronizes required auth environment variables into ~/.gemini/.env (for global access).
    3. For service accounts, links credentials to standard ADC path (~/.config/gcloud/application_default_credentials.json).
    """
    import json
    import shutil
    
    gemini_dir = os.path.expanduser("~/.gemini")
    os.makedirs(gemini_dir, exist_ok=True)
    settings_path = os.path.join(gemini_dir, "settings.json")
    if is_dry_run():
        logger.info(f"🔍 [DRY-RUN] Simulating Gemini CLI global auth sync for '{auth_type}' (skipped disk write).")
        return True
    
    # 1. Update settings.json with security.auth.selectedType
    try:
        data = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("security", {}).setdefault("auth", {})["selectedType"] = auth_type
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(f"🔒 [CLI SYNC] Synchronized Gemini CLI global auth type: '{auth_type}' in {settings_path}")
    except Exception as err:
        logger.warning(f"⚠️  Could not update {settings_path}: {err}")
        
    # 2. Dual-tier environment persistence (~/.gemini/.env)
    if env_vars:
        global_env = os.path.join(gemini_dir, ".env")
        try:
            lines = []
            if os.path.exists(global_env):
                with open(global_env, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            env_dict = {}
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_dict[k.strip()] = v.strip()
            for k, v in env_vars.items():
                if v and not str(v).startswith("<"):
                    env_dict[k] = str(v).strip()
            with open(global_env, "w", encoding="utf-8") as f:
                for k, v in env_dict.items():
                    f.write(f"{k}={v}\n")
            logger.info(f"🌐 [GLOBAL ENV] Synchronized {len(env_vars)} credentials to global Gemini CLI env at {global_env}")
        except Exception as err:
            logger.warning(f"⚠️  Could not update global .env at {global_env}: {err}")

    # 3. For service account, link/copy to standard ADC path
    if adc_source_file and os.path.exists(adc_source_file):
        adc_dir = os.path.expanduser("~/.config/gcloud")
        os.makedirs(adc_dir, exist_ok=True)
        adc_path = os.path.join(adc_dir, "application_default_credentials.json")
        try:
            shutil.copy2(adc_source_file, adc_path)
            logger.info(f"📁 [ADC SYNC] Registered Service Account key to standard ADC location: {adc_path}")
        except Exception as err:
            logger.warning(f"⚠️  Could not copy to {adc_path}: {err}")
            
    return True

import os
import re
import sys
import subprocess
import time
import json
import shutil
from urllib.parse import urlparse, parse_qs

# Pre-parse --no-automatic-browser / --headless to set it in the environment before any local imports run
for arg in sys.argv:
    if arg in ("--no-automatic-browser", "--headless"):
        os.environ["NO_AUTOMATIC_BROWSER_OVERRIDE"] = "True"
        break

from utils import setup_logger, with_retry, WORKSPACE_ROOT, ENV_FILE_PATH, ENV_FILE_EXISTS, update_env_file, get_process_timeout, is_dry_run, cleanup_runtime_artifacts, register_temp_path, verify_prephase_disk_space, is_enospc_error, halt_on_enospc, resolve_gcloud_bin

logger = setup_logger("phase2_authenticate")
GCLOUD_BIN = resolve_gcloud_bin()

def extract_oauth_code_from_url(url: str) -> str:
    """Extracts the auth code parameter from the google redirect url."""
    parsed_url = urlparse(url)
    queries = parse_qs(parsed_url.query)
    code_list = queries.get("code")
    if code_list:
        return code_list[0]
    return ""

def playwright_login(oauth_url: str, check_success_callback=None, force_auth_with_reset: bool = False) -> str:
    """
    Attempts to launch Playwright in headed mode to let the user login.
    Intercepts the redirect URL to retrieve the auth code automatically.
    Supports a background success callback to prevent hangs if redirection freezes.
    Preserves and auto-fills existing account info under --force-auth, while omitting auto-fill under --force-auth-with-reset for clean-slate entry.
    """
    if os.environ.get("NO_AUTOMATIC_BROWSER_OVERRIDE") == "True":
        logger.info("- Status : Headless/No-Browser mode active. Bypassing browser launch and forcing manual fallback.")
        return ""

    import importlib
    importlib.invalidate_caches()

    # Resolve persistent browser session state path
    profile_dir = os.environ.get("AUTH_BROWSER_PROFILE_DIR") or os.path.expanduser("~/.config/google_ai_auth_profile")
    auth_state_path = os.environ.get("AUTH_BROWSER_STATE_PATH") or os.path.join(profile_dir, "auth_state.json")
    
    # If hard reset is requested, purge any stored browser session state and profile dir for clean-slate re-entry
    if force_auth_with_reset:
        if os.path.exists(profile_dir):
            try:
                import shutil
                shutil.rmtree(profile_dir, ignore_errors=True)
                logger.info("- Status : Purged persistent browser profile cache for clean-slate re-entry (--force-auth-with-reset).")
            except Exception as e:
                logger.warning(f"Could not purge browser profile directory: {e}")
        if os.path.exists(auth_state_path):
            try:
                os.remove(auth_state_path)
            except Exception:
                pass
    elif not force_auth_with_reset and os.path.exists(auth_state_path):
        logger.info("- Status : Preserving persistent browser profile session and cookies (--force-auth).")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright library not installed. Cannot run automated browser auth.")
        return ""

    logger.info("Launching Playwright browser for Google Cloud Authentication...")
    
    # We set headless=False so the user can interact if there is a captcha/2FA
    # However, we catch any errors if X11/Display is not available on headless servers.
    try:
        with sync_playwright() as p:
            # We launch the browser hermetically to guarantee Chromium never triggers OS profile wizards,
            # theme picker dialogs, or onboarding popups.
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--password-store=basic"
            ]
            browser = p.chromium.launch(
                headless=False,
                args=launch_args
            )
            
            context_kwargs = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            # Under --force-auth, restore the persistent session storage state if available!
            if not force_auth_with_reset and os.path.exists(auth_state_path):
                try:
                    context_kwargs["storage_state"] = auth_state_path
                except Exception as e:
                    logger.debug(f"Could not load storage state: {e}")
                    
            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            logger.info("Navigating to Google OAuth page...")
            try:
                page.goto(oauth_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as goto_err:
                logger.warning(f"Initial navigation notice: {goto_err}. Retrying clean navigation...")
                page.goto(oauth_url, timeout=30000)
            
            # If force_auth_with_reset is True, do NOT auto-fill existing account info to ensure a true clean-slate re-entry.
            # If standard login or non-reset force_auth is used, resolve and preserve any available existing account info.
            email = None
            password = None
            if not force_auth_with_reset:
                email = os.getenv("GCP_USER_EMAIL")
                if not email:
                    try:
                        res = subprocess.run([GCLOUD_BIN, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], capture_output=True, text=True, timeout=10, check=False)
                        if res.returncode == 0 and res.stdout.strip():
                            email = res.stdout.strip().splitlines()[0].strip()
                    except Exception:
                        pass
                password = os.getenv("GCP_USER_PASSWORD")
            
            if email:
                logger.info(f"Preserving existing login information: attempting auto-fill for user '{email}'...")
                try:
                    page.wait_for_selector("input[type='email']", timeout=5000)
                    page.fill("input[type='email']", email)
                    page.click("#identifierNext")
                    
                    if password:
                        page.wait_for_selector("input[type='password']", state="visible", timeout=5000)
                        page.fill("input[type='password']", password)
                        page.click("#passwordNext")
                except Exception as auto_fill_err:
                    logger.warning(f"Auto-fill bypassed or completed: {auto_fill_err}")
            elif force_auth_with_reset:
                logger.info("- Status : Clean-slate reset active (--force-auth-with-reset). Stored account info omitted for fresh user entry.")
            
            logger.info("------------------------------------------------------------")
            logger.info("Please interact with the opened browser window to complete login.")
            logger.info("Once completed and authorized, this script will automatically")
            logger.info("detect the authorization code and proceed.")
            logger.info("------------------------------------------------------------")
            
            # Wait for redirect to sdk.cloud.google.com/authcode.html, applicationdefaultauthcode.html, or localhost
            auth_code = ""
            start_time = time.time()
            timeout_limit = 300  # 5 minutes
            
            while time.time() - start_time < timeout_limit:
                # Check if the browser or all pages were closed by the user
                if not context.pages or not browser.is_connected():
                    logger.info("- Status : Browser window was closed. Proceeding to manual fallback...")
                    break

                # Scan all currently active pages/tabs in the context for the redirect URL
                for p in list(context.pages):
                    try:
                        current_url = p.url
                        # Support standard login (authcode.html), ADC login (applicationdefaultauthcode.html),
                        # general GCloud callback domains, and local port callbacks
                        if "sdk.cloud.google.com" in current_url or "authcode" in current_url or "localhost" in current_url or "127.0.0.1" in current_url:
                            auth_code = extract_oauth_code_from_url(current_url)
                            
                            # If not found in query parameters, inspect page DOM elements
                            if not auth_code:
                                for sel in ["#code", "input[type='text']", "textarea", "code"]:
                                    try:
                                        el = p.query_selector(sel)
                                        if el:
                                            val = el.input_value() if "input" in sel or "textarea" in sel else el.inner_text()
                                            val = (val or "").strip()
                                            if val.startswith("4/") and len(val) > 20:
                                                auth_code = val
                                                logger.info("Successfully extracted Google authorization code from page content!")
                                                break
                                    except Exception:
                                        pass
                                        
                            if auth_code:
                                logger.info("Successfully intercepted Google authorization code!")
                                break
                    except Exception:
                        pass
                        
                if auth_code:
                    break
                        
                # Active background status polling safeguard
                if check_success_callback and check_success_callback():
                    logger.info("Authentication detected via background active-account status poll! Bypassing browser wait.")
                    auth_code = "ALREADY_AUTHENTICATED_BY_POLL"
                    break
                    
                active_page = context.pages[-1] if context.pages else None
                if active_page:
                    try:
                        active_page.wait_for_timeout(1000)
                    except Exception:
                        time.sleep(1)
                else:
                    time.sleep(1)
                
            if auth_code and auth_code != "ALREADY_AUTHENTICATED_BY_POLL":
                try:
                    os.makedirs(os.path.dirname(auth_state_path), exist_ok=True)
                    context.storage_state(path=auth_state_path)
                    logger.info("- Status : Saved browser authentication session state for future seamless logins.")
                except Exception as save_err:
                    logger.debug(f"Could not save storage state: {save_err}")
                    
            browser.close()
            return auth_code
            
    except Exception as browser_err:
        logger.warning(f"Failed to run headed Playwright browser: {browser_err}")
        logger.info("This is expected if you are running on a remote headless server without an active display (X11).")
        return ""

def manual_fallback(oauth_url: str) -> str:
    """Fallback mechanism prompting the user to paste the auth code manually."""
    logger.info("\n=== MANUAL AUTHENTICATION FALLBACK ===")
    logger.info("Please copy and paste this URL into your browser to log in:")
    logger.info(f"\n{oauth_url}\n")
    logger.info("After completing the login, copy the authorization code shown on screen.")
    try:
        # Ask for input on the active terminal
        code = input("Enter the Google Cloud authorization code: ").strip()
        return code
    except KeyboardInterrupt:
        logger.error("\nAuthentication interrupted by user.")
        return ""
    except Exception as e:
        logger.error(f"Error during manual input: {e}")
        return ""

def check_gcloud_auth_status() -> bool:
    """Checks if gcloud has a valid authenticated account active with a strict timeout fail-safe."""
    gcloud_bin = resolve_gcloud_bin()
    try:
        res = subprocess.run(
            [gcloud_bin, "auth", "list", "--format=value(account)"],
            capture_output=True,
            text=True,
            timeout=10
        )
        active_account = res.stdout.strip()
        if active_account:
            logger.info(f"Verified active authenticated account: {active_account}")
            return True
        return False
    except Exception:
        return False

def configure_api_key(api_key: str) -> bool:
    """Saves and configures a lightweight Gemini API Key with descriptive console outputs."""
    masked_key = api_key[:6] + "..." + api_key[-4:] if len(api_key) > 10 else "..."
    logger.info("------------------------------------------------------")
    logger.info("🔑 [AUTH METHOD] Direct Gemini API Key Activation")
    logger.info("------------------------------------------------------")
    logger.info(f"- API Key: {masked_key} (Security Masked)")
    logger.info(f"- Target : {ENV_FILE_PATH}")
    logger.info("- Status : Writing key safely to your workspace environment file...")
    
    try:
        update_env_file("GEMINI_API_KEY", api_key)
        synchronize_gemini_cli_auth("gemini-api-key", env_vars={"GEMINI_API_KEY": api_key})
        logger.info("------------------------------------------------------")
        logger.info("✅ [SUCCESS] Gemini API Key successfully saved and verified in .env!")
        logger.info("------------------------------------------------------")
        return True
    except Exception as e:
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Failed to save Gemini API key: {e}")
        logger.error("======================================================")
        return False

def activate_service_account(key_file_path: str) -> bool:
    """Activates GCloud using a Service Account JSON Key file with descriptive console outputs."""
    logger.info("------------------------------------------------------")
    logger.info("👤 [AUTH METHOD] GCloud Service Account JSON Key Activation")
    logger.info("------------------------------------------------------")
    logger.info(f"- Key File: {key_file_path}")
    logger.info("- Status  : Activating headless credentials via 'gcloud auth activate-service-account'...")
    
    if not os.path.exists(key_file_path):
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Service account key file not found: {key_file_path}")
        logger.error("======================================================")
        return False
        
    cmd = [GCLOUD_BIN, "auth", "activate-service-account", f"--key-file={key_file_path}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
        if res.returncode == 0:
            abs_path = os.path.abspath(key_file_path)
            update_env_file("GOOGLE_APPLICATION_CREDENTIALS", abs_path)
            synchronize_gemini_cli_auth("vertex-ai", env_vars={"GOOGLE_APPLICATION_CREDENTIALS": abs_path}, adc_source_file=abs_path)
            logger.info("- Status  : Key activated. Registering Application Default Credentials (ADC) path...")
            logger.info("------------------------------------------------------")
            logger.info("✅ [SUCCESS] GCloud Service Account activated and mapped successfully in .env!")
            logger.info("------------------------------------------------------")
            return True
        else:
            err_msg = res.stderr.strip() if res.stderr else "Unknown GCloud activation error"
            logger.error("------------------------------------------------------")
            logger.error(f"❌ [FAILURE] Service account activation failed!")
            logger.error(f"- GCloud Error: {err_msg}")
            logger.error("======================================================")
            return False
    except Exception as e:
        logger.error(f"Subprocess crash while activating service account: {e}")
        return False

def orchestrate_gcloud_headless_flow(gcloud_cmd: list, force_auth: bool = False, force_auth_with_reset: bool = False) -> bool:
    """Executes a gcloud login subprocess, handles authentication dynamically via Playwright, and pipes code back with comprehensive telemetry."""
    if is_dry_run():
        logger.info(f"🔍 [DRY-RUN] Simulating GCloud headless execution: {' '.join(gcloud_cmd)} (skipped execution).")
        return True
    is_adc_cmd = "application-default" in gcloud_cmd
    auth_title = "GCloud Application Default Credentials (ADC) Login" if is_adc_cmd else "Standard GCloud User Browser Login"
    any_force = force_auth or force_auth_with_reset
    
    logger.info("------------------------------------------------------")
    logger.info(f"🌐 [AUTH METHOD] {auth_title}")
    logger.info("------------------------------------------------------")
    logger.info(f"- Command: {' '.join(gcloud_cmd)}")
    logger.info("- Status : Spawning GCloud background process...")
    
    try:
        process = subprocess.Popen(
            gcloud_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
    except Exception as e:
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Failed to spawn gcloud process: {e}")
        logger.error("======================================================")
        return False
        
    oauth_url = ""
    # Regex to strip ANSI escape codes that can corrupt URL parameters
    ansi_regex = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    
    # We scan stdout to find the accounts.google.com URL
    for line in iter(process.stdout.readline, ""):
        clean_line = ansi_regex.sub('', line).strip()
        if clean_line:
            logger.info(f"[gcloud stdout] {clean_line}")
            
        match = re.search(r'(https://accounts\.google\.com/o/oauth2/auth\?\S+)', clean_line)
        if match:
            raw_url = match.group(1)
            # Thoroughly sanitize: strip control characters, quotes, or trailing punctuation
            oauth_url = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', raw_url).rstrip("'\".,);")
            break
        if "Enter authorization code:" in clean_line or "Enter verification code:" in clean_line:
            break
            
    if not oauth_url:
        logger.error("------------------------------------------------------")
        logger.error("❌ [FAILURE] Could not retrieve OAuth login URL from GCloud process output.")
        logger.error("======================================================")
        process.kill()
        return False
        
    logger.info("- Status : Secure OAuth login URL captured successfully.")
    
    start_proc_time = time.time()
    
    # Define background success polling callback
    # If the local gcloud server successfully receives the callback on port 8085 (or other),
    # or generates the ADC credentials file during this run,
    # the user is logged in instantly, even if the browser itself is frozen/hung.
    def is_authenticated_callback():
        if is_adc_cmd:
            adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
            if os.path.exists(adc_path):
                try:
                    if os.path.getmtime(adc_path) >= start_proc_time - 2:
                        return True
                except Exception:
                    pass
            return False
            
        if not any_force:
            return check_gcloud_auth_status()
        return False
        
    # Try automating with Playwright first, passing our background status checker
    logger.info("======================================================================")
    logger.info("🔑  SECURE GOOGLE CLOUD USER AUTHENTICATION")
    logger.info("======================================================================")
    logger.info("A secure Playwright browser window is opening on your desktop screen.")
    logger.info("  1. Please interact with the opened browser to sign in to Google Cloud.")
    logger.info("  2. Grant the requested permissions and click 'Allow' to authorize.")
    logger.info("  3. This script will AUTOMATICALLY capture your login from the browser!")
    logger.info("     (No terminal copy-pasting is required during browser sign-in)")
    logger.info("  ------------------------------------------------------------------")
    logger.info("  NOTE: If the browser fails to open, or if you close it manually,")
    logger.info("        the terminal will instantly fall back to standard copy-paste prompts.")
    logger.info("======================================================================\n")
    
    auth_code = playwright_login(oauth_url, check_success_callback=is_authenticated_callback, force_auth_with_reset=force_auth_with_reset)
    
    if auth_code == "ALREADY_AUTHENTICATED_BY_POLL":
        logger.info("------------------------------------------------------")
        logger.info("⚡ [HANDSHAKE] Authentication detected via background GCloud port receiver!")
        logger.info("- Status : Successfully logged in. Finalizing background process...")
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        logger.info("------------------------------------------------------")
        logger.info(f"✅ [SUCCESS] {auth_title} Completed Successfully!")
        logger.info("------------------------------------------------------")
        return True
    
    # If playwright failed (e.g. no display) or timeout, fall back to manual copy-paste
    if not auth_code:
        logger.info("- Status : Browser capture unavailable or bypassed. Running manual fallback...")
        auth_code = manual_fallback(oauth_url)
        
    if not auth_code:
        logger.error("------------------------------------------------------")
        logger.error("❌ [FAILURE] No authorization code was provided. Handshake aborted.")
        logger.error("======================================================")
        process.kill()
        return False
        
    logger.info("- Status : Authorization code captured. Executing token exchange...")
    try:
        stdout, stderr = process.communicate(input=f"{auth_code}\n", timeout=get_process_timeout())
        if stdout:
            for line in stdout.splitlines():
                clean_l = line.strip()
                if clean_l: logger.info(f"[gcloud stdout] {clean_l}")
        if stderr:
            for line in stderr.splitlines():
                clean_l = line.strip()
                if clean_l: logger.error(f"[gcloud stderr] {clean_l}")
    except Exception as e:
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Failed to communicate with gcloud subprocess: {e}")
        logger.error("======================================================")
        process.kill()
        return False
        
    # Ground-Truth Credential Verification: Check if valid credentials were persisted to disk
    adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    credentials_confirmed = False
    
    if is_adc_cmd and os.path.exists(adc_path):
        try:
            with open(adc_path, "r", encoding="utf-8") as f:
                adc_data = json.load(f)
            if adc_data.get("refresh_token") or adc_data.get("client_id") or adc_data.get("type"):
                credentials_confirmed = True
        except Exception:
            pass
    elif not is_adc_cmd:
        credentials_confirmed = check_gcloud_auth_status()

    if process.returncode == 0 or credentials_confirmed:
        logger.info("------------------------------------------------------")
        if process.returncode != 0 and credentials_confirmed:
            logger.info(f"🎉 [CREDENTIALS CONFIRMED] Valid credentials verified on disk! (Ignored non-zero post-auth exit code: {process.returncode})")
        logger.info(f"✅ [SUCCESS] {auth_title} Completed Successfully!")
        logger.info("------------------------------------------------------")
        return True
    else:
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] GCloud token exchange exited with code {process.returncode}.")
        logger.error("======================================================")
        return False

def run_gcloud_auth(force_auth: bool = False, force_auth_with_reset: bool = False, api_key: str = None, client_file: str = None, adc_user: bool = False) -> bool:
    """Spawns standard or custom gcloud authentication channels based on passed arguments.
    Sequentially cascades and automatically configures GCloud ADC after standard login.
    Preserves and auto-fills existing login credentials under force_auth=True,
    while enforcing a clean-slate re-entry under force_auth_with_reset=True.
    """
    if not verify_prephase_disk_space("Phase 2: Google Cloud Authentication", log=logger):
        return False
    if api_key:
        return configure_api_key(api_key)
    elif client_file:
        return activate_service_account(client_file)
    elif adc_user:
        return run_gcloud_adc_auth(force_auth=force_auth, force_auth_with_reset=force_auth_with_reset)
        
    any_force = force_auth or force_auth_with_reset
    # Default standard flow:
    # 1. Standard user browser auth login
    standard_ok = True
    if not any_force and check_gcloud_auth_status():
        logger.info("Already authenticated with Google Cloud standard login. (Pass --force-auth to force re-authentication preserving account info, or --force-auth-with-reset for clean-slate reset)")
        synchronize_gemini_cli_auth("oauth-personal")
    else:
        login_cmd = [GCLOUD_BIN, "auth", "login", "--no-launch-browser"]
        if force_auth and not force_auth_with_reset:
            existing_account = os.getenv("GCP_USER_EMAIL")
            if not existing_account:
                try:
                    res = subprocess.run([GCLOUD_BIN, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], capture_output=True, text=True, timeout=10, check=False)
                    if res.returncode == 0 and res.stdout.strip():
                        existing_account = res.stdout.strip().splitlines()[0].strip()
                except Exception:
                    pass
            if existing_account and not any(arg.startswith("--account") for arg in login_cmd):
                login_cmd.append(f"--account={existing_account}")
                logger.info(f"- Status : Preserving existing login account '{existing_account}' (--force-auth).")
                
        standard_ok = orchestrate_gcloud_headless_flow(login_cmd, force_auth=force_auth, force_auth_with_reset=force_auth_with_reset)
        
    if not standard_ok:
        logger.error("❌ [FAILURE] Standard Google Cloud login failed. Aborting cascade.")
        return False
        
    # 2. Sequential cascade: Ensure GCloud Application Default Credentials (ADC) are configured
    logger.info("------------------------------------------------------")
    logger.info("🔄 [CASCADE] Ensuring GCloud Application Default Credentials (ADC) are configured...")
    logger.info("------------------------------------------------------")
    
    # We pass the flags down to the ADC flow!
    success_adc = run_gcloud_adc_auth(force_auth=force_auth, force_auth_with_reset=force_auth_with_reset)
    return success_adc

def run_gcloud_adc_auth(force_auth: bool = False, force_auth_with_reset: bool = False) -> bool:
    """Spawns gcloud auth application-default login, handles token exchange via Playwright or fallback.
    Preserves existing ADC file under force_auth=True (overwrites in place),
    while actively deleting stale ADC file under force_auth_with_reset=True for clean-slate reset.
    """
    adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    any_force = force_auth or force_auth_with_reset
    if not any_force and os.path.exists(adc_path):
        logger.info("Application Default Credentials (ADC) already configured. Skipping login. (Pass --force-auth to overwrite)")
        synchronize_gemini_cli_auth("oauth-personal")
        return True

    # Under forced re-authentication with reset, cleanly remove the old credentials file first
    # to prevent the background polling checker from triggering false-positive matches!
    if force_auth_with_reset and os.path.exists(adc_path):
        try:
            os.remove(adc_path)
            logger.info("- Status : Removed stale ADC credentials file to prepare for clean-slate re-authentication (--force-auth-with-reset).")
        except Exception as err:
            logger.warning(f"⚠️  Could not remove stale ADC file: {err}")
    elif force_auth and os.path.exists(adc_path):
        logger.info("- Status : Preserving existing credentials file; will overwrite in-place with renewed tokens (--force-auth).")

    res = orchestrate_gcloud_headless_flow([GCLOUD_BIN, "auth", "application-default", "login", "--no-launch-browser", "--disable-quota-project", "--quiet"], force_auth=force_auth, force_auth_with_reset=force_auth_with_reset)
    if res:
        synchronize_gemini_cli_auth("oauth-personal")
    return res

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Multi-Channel Google Cloud Authentication Manager")
    parser.add_argument(
        "--api-key", 
        help="Lightweight Gemini API Key string. This provides direct API access to Gemini models bypassing GCloud completely."
    )
    parser.add_argument(
        "--client-file", 
        help="Absolute path to your Google Cloud Service Account JSON credentials key file. This file contains secure private keys generated in the GCP Console under IAM -> Service Accounts, enabling fully automated, key-based headless authentication."
    )
    parser.add_argument(
        "--adc-user", 
        help="Trigger standard GCloud Application Default Credentials (ADC) user login flow instead of standard user login. Best for local development client libraries.", 
        action="store_true"
    )
    parser.add_argument(
        "--env-file", 
        help="Absolute path to a custom environment (.env) file. If not provided, defaults to looking for '.env' inside your workspace directory."
    )
    parser.add_argument(
        "--force-auth", 
        help="Force GCloud re-authentication even if an active account is already detected on this machine (without deleting previous cache files).", 
        action="store_true"
    )
    parser.add_argument(
        "--force-auth-with-reset", 
        help="Force GCloud re-authentication AND actively delete previous application default credentials files on disk first.", 
        action="store_true"
    )
    parser.add_argument(
        "--no-automatic-browser",
        "--headless",
        dest="no_automatic_browser",
        help="Skip launching Playwright headed browser automatically, forcing manual copy-paste authentication URLs (headless mode).",
        action="store_true"
    )
    parser.add_argument(
        "--timeout",
        help="Specify the absolute execution timeout in seconds for GCloud token exchange processes (defaults to 180 seconds).",
        type=int,
        default=180
    )
    parser.add_argument(
        "--dry-run",
        help="Simulate execution without modifying credentials, browser logins, or writing to .env/settings.json.",
        action="store_true"
    )
    parser.add_argument(
        "--do-cleanup",
        dest="cleanup",
        action="store_true",
        default=True,
        help="Clean up temporary files upon completion (default: enabled)."
    )
    parser.add_argument(
        "--no-cleanup",
        dest="cleanup",
        action="store_false",
        help="Preserve temporary diagnostic files upon completion."
    )
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN_MODE"] = "1"
        logger.info("🔍 [DRY-RUN SIMULATION] Operating in read-only simulation mode.")
    os.environ["CLEANUP_TEMP_FILES"] = "1" if args.cleanup else "0"

    # Pre-parse timeout override if passed
    if args.timeout:
        os.environ["PROCESS_TIMEOUT_OVERRIDE"] = str(args.timeout)

    # Pre-parse environment file override if passed as parameter
    if args.env_file:
        os.environ["ENV_FILE_PATH_OVERRIDE"] = args.env_file
        # Re-resolve and re-import
        global ENV_FILE_PATH, ENV_FILE_EXISTS
        from utils import get_env_file_path, load_env_file
        ENV_FILE_PATH = get_env_file_path()
        ENV_FILE_EXISTS = os.path.exists(ENV_FILE_PATH)
        load_env_file()

    logger.info("==============================================")
    logger.info("Starting Phase 2: Google Cloud Authentication")
    logger.info("==============================================")
    
    # Environment file verification safeguard
    is_initializing_now = bool(args.api_key or args.client_file or args.adc_user)
    
    if not ENV_FILE_EXISTS:
        # Check if we can confirm existing active credentials
        auth_confirmed = check_gcloud_auth_status() or os.getenv("GEMINI_API_KEY") is not None
        
        if not auth_confirmed and not is_initializing_now:
            logger.error("======================================================================")
            logger.error("🛑 FATAL ERROR: ENVIRONMENT FILE NOT FOUND")
            logger.error("======================================================================")
            logger.error(f"Could not locate your environment (.env) file at: {ENV_FILE_PATH}")
            logger.error("No existing active GCloud account or GEMINI_API_KEY was detected.")
            logger.error("We cannot proceed safely as subsequent initialization steps require active credentials.")
            logger.error("----------------------------------------------------------------------")
            logger.error("REMEDIATION STEPS:")
            logger.error("1. Place a valid .env file in your active workspace directory.")
            logger.error("2. Specify the absolute path to your env file using: --env-file <path>")
            logger.error("3. Authenticate first to generate a new environment automatically.")
            logger.error("======================================================================")
            return False
        else:
            logger.warning(f"⚠️ Warning: Environment (.env) file not found at: {ENV_FILE_PATH}")
            logger.warning("Active authentication is confirmed in memory. Continuing setup...")
            
    try:
        success = run_gcloud_auth(
            force_auth=args.force_auth,
            force_auth_with_reset=args.force_auth_with_reset,
            api_key=args.api_key,
            client_file=args.client_file,
            adc_user=args.adc_user
        )
            
        if success:
            logger.info("Phase 2 completed successfully!")
            return True
        else:
            logger.error("Phase 2 failed.")
            return False
    except Exception as e:
        logger.error(f"Phase 2 execution failed: {e}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
