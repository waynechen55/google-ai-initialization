import os
import shutil
import subprocess
import sys
import importlib.metadata
from utils import setup_logger, with_retry, WORKSPACE_ROOT, update_env_file, get_process_timeout, is_dry_run, cleanup_runtime_artifacts, register_temp_path, verify_prephase_disk_space, is_enospc_error, halt_on_enospc

logger = setup_logger("phase1_install")

REQUIRED_PIP_PACKAGES = [
    "google-genai",
    "playwright",
    "google-antigravity",
    "mcp<2",
    "uv",
    "python-dotenv",
    "requests",
    "pyyaml",
    "packaging",
]

def is_pip_package_installed(package_name: str) -> bool:
    """Checks if a python package is installed in the current environment."""
    try:
        importlib.metadata.version(package_name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False

def run_command(cmd, shell=False, check=True):
    """Executes a shell command and yields output in real-time."""
    logger.info(f"Running command: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        # We want stdout and stderr in real-time, but also to wait for completion
        process = subprocess.Popen(
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        # Print output in real-time
        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                clean_line = line.strip()
                if clean_line:
                    logger.info(f"[stdout] {clean_line}")
                    
        # Wait for completion with dynamic timeout limits
        timeout = get_process_timeout()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as err:
            process.kill()
            process.wait()
            logger.error(f"❌ [TIMEOUT] Command exceeded active limit of {timeout} seconds! Process terminated.")
            raise err
            
        if check and process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd)
        return process.returncode
    except Exception as e:
        logger.error(f"Failed to execute command '{cmd}': {e}")
        raise e

@with_retry(max_attempts=3, backoff_seconds=5)
def install_pip_packages(force_install: bool = False, break_dependencies: bool = False, downgrade: bool = False):
    if is_dry_run():
        logger.info("🔍 [DRY-RUN] Simulating pip packages verification/installation (skipped).")
        return
    """Installs required pip packages based on environment constraints with structured real-time console feedback."""
    packages_to_install = []
    
    # Version specifications (handles downgrade request)
    version_map = {
        "google-genai": "==2.10.0" if downgrade else "",
        "playwright": "==1.50.0" if downgrade else ""
    }
    
    for package in REQUIRED_PIP_PACKAGES:
        spec = version_map.get(package, "")
        full_package = f"{package}{spec}"
        if force_install or not is_pip_package_installed(package):
            packages_to_install.append(full_package)
            
    logger.info("------------------------------------------------------")
    logger.info("📦 [INSTALLING] Python Dependencies (via Pip)")
    logger.info("------------------------------------------------------")
    logger.info(f"- Force Reinstall   : {'Yes' if force_install else 'No'}")
    logger.info(f"- Break Dependencies: {'Yes' if break_dependencies else 'No'}")
    logger.info(f"- Downgrade Versions: {'Yes' if downgrade else 'No'}")
    logger.info(f"- Scheduled Packages: {packages_to_install if packages_to_install else 'None (All Up To Date)'}")
            
    if packages_to_install:
        logger.info("- Status : Running Pip installer subprocess...")
        cmd = [sys.executable, "-m", "pip", "install", *packages_to_install]
        
        if force_install:
            cmd.append("--force-reinstall")
            
        if break_dependencies:
            cmd.append("--break-system-packages")
            
        try:
            run_command(cmd)
            logger.info("------------------------------------------------------")
            logger.info("✅ [SUCCESS] Python dependencies successfully installed!")
            logger.info("------------------------------------------------------")
        except Exception as err:
            if "--break-system-packages" not in cmd:
                logger.warning("------------------------------------------------------")
                logger.warning("⚠️ [RECOVERY] Standard install failed (Managed System Python).")
                logger.warning("Retrying automatically with system-dependency overrides (--break-system-packages)...")
                logger.warning("------------------------------------------------------")
                try:
                    run_command(cmd + ["--break-system-packages"])
                    logger.info("------------------------------------------------------")
                    logger.info("✅ [SUCCESS] Python dependencies successfully installed after retry!")
                    logger.info("------------------------------------------------------")
                except Exception as final_err:
                    logger.error("------------------------------------------------------")
                    logger.error(f"❌ [FAILURE] Failed to install Python dependencies: {final_err}")
                    logger.error("======================================================")
                    raise final_err
            else:
                logger.error("------------------------------------------------------")
                logger.error(f"❌ [FAILURE] Failed to install Python dependencies: {err}")
                logger.error("======================================================")
                raise err
    else:
        logger.info("------------------------------------------------------")
        logger.info("✅ [SUCCESS] Python dependencies are already satisfied.")
        logger.info("------------------------------------------------------")

def check_system_browsers():
    """Detects system-wide browsers installed on the host machine."""
    logger.info("Scanning host machine for compatible system browsers...")
    common_browsers = [
        "google-chrome", "google-chrome-stable", "chrome", 
        "chromium", "chromium-browser", "firefox", "microsoft-edge"
    ]
    found = []
    for browser in common_browsers:
        path = shutil.which(browser)
        if path:
            found.append(f"{browser} ({path})")
            
    if found:
        logger.info(f"Detected host system browsers: {', '.join(found)}")
    else:
        logger.warning(
            "No native desktop system browsers found on host PATH. This is typical for server/headless environments. "
            "Our automated Playwright-Chromium sandbox will be used as the exclusive, 100% compatible driver."
        )

@with_retry(max_attempts=2, backoff_seconds=5)
def install_playwright_browsers():
    if is_dry_run():
        logger.info("🔍 [DRY-RUN] Simulating Playwright Chromium browser installation (skipped).")
        return
    """Installs Playwright chromium browser with descriptive telemetry."""
    logger.info("------------------------------------------------------")
    logger.info("🌐 [INSTALLING] Playwright Chromium Browser Engine")
    logger.info("------------------------------------------------------")
    
    check_system_browsers()
    
    logger.info("- Status : Fetching Chromium and FFmpeg hermetic binaries...")
    try:
        # First attempt installing Chromium together with Linux OS-level shared libraries (--with-deps)
        res = run_command([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], check=False)
        if res != 0:
            logger.info("Retrying standard Chromium installation (without system package manager hooks)...")
            run_command([sys.executable, "-m", "playwright", "install", "chromium"])
        logger.info("------------------------------------------------------")
        logger.info("✅ [SUCCESS] Playwright Chromium browser installed successfully!")
        logger.info("------------------------------------------------------")
    except Exception as e:
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Failed to install Playwright browsers: {e}")
        logger.error("======================================================")
        raise e

@with_retry(max_attempts=2, backoff_seconds=5)
def check_and_install_gcloud(force_install: bool = False):
    if is_dry_run():
        logger.info("🔍 [DRY-RUN] Simulating GCloud SDK verification/installation (skipped).")
        return
    """Checks if Google Cloud SDK (gcloud) is installed. If not (or if forced), installs it with descriptive logs, safely handling pre-existing folders."""
    logger.info("------------------------------------------------------")
    logger.info("☁️ [VERIFYING] Google Cloud SDK (gcloud)")
    logger.info("------------------------------------------------------")
    logger.info(f"- Force Reinstall: {'Yes' if force_install else 'No'}")
    
    home_dir = os.path.expanduser("~")
    gcloud_dir = os.path.join(home_dir, "google-cloud-sdk")
    gcloud_bin_dir = os.path.join(gcloud_dir, "bin")
    
    if not force_install and shutil.which("gcloud"):
        logger.info("------------------------------------------------------")
        logger.info("✅ [SUCCESS] Google Cloud SDK is already installed and ready.")
        logger.info("------------------------------------------------------")
        return
        
    # Safeguard: If the directory exists but gcloud is missing from active PATH
    if os.path.exists(gcloud_dir):
        gcloud_executable = os.path.join(gcloud_bin_dir, "gcloud")
        
        if force_install:
            logger.info(f"- Status : Overwriting existing Google Cloud SDK directory at: {gcloud_dir}...")
            try:
                shutil.rmtree(gcloud_dir)
            except Exception as rmtree_err:
                logger.warning(f"⚠️ Warning: Could not remove old GCloud directory: {rmtree_err}. Proceeding with overwrite...")
        elif os.path.exists(gcloud_executable):
            # Healthy installation exists! We can use it.
            logger.info("------------------------------------------------------")
            logger.info(f"✅ [SUCCESS] Google Cloud SDK directory already exists at {gcloud_dir}.")
            logger.info("Automatically registering its bin path inside active process environment PATH.")
            logger.info("------------------------------------------------------")
            if gcloud_bin_dir not in os.environ["PATH"]:
                os.environ["PATH"] = gcloud_bin_dir + os.pathsep + os.environ["PATH"]
            return
        else:
            # Corrupt, empty, or broken installation! Clean it up to prevent installer mkdir crashes.
            logger.warning("------------------------------------------------------")
            logger.warning(f"⚠️  [CORRUPT INSTALL DETECTED] Broken GCloud folder found at: {gcloud_dir}")
            logger.warning("- Status : Wiping corrupt directory to prevent installer 'mkdir failed' crash...")
            try:
                shutil.rmtree(gcloud_dir)
                logger.warning("- Status : Wiped successfully. Ready for a clean re-download!")
                logger.warning("------------------------------------------------------")
            except Exception as rmtree_err:
                logger.error(f"❌ Failed to clean up broken GCloud directory: {rmtree_err}. GCloud install may fail.")

    logger.info("- Status : Fetching and running Google Cloud official installation script...")
    install_script_cmd = "curl -sSL https://sdk.cloud.google.com | bash -s -- --disable-prompts"
    try:
        run_command(install_script_cmd, shell=True)
        
        if os.path.exists(gcloud_bin_dir):
            os.environ["PATH"] = gcloud_bin_dir + os.pathsep + os.environ["PATH"]
            
        if shutil.which("gcloud") or os.path.exists(os.path.join(gcloud_bin_dir, "gcloud")):
            logger.info("------------------------------------------------------")
            logger.info("✅ [SUCCESS] Google Cloud SDK successfully installed and verified!")
            logger.info("------------------------------------------------------")
        else:
            raise RuntimeError("Google Cloud SDK installation script completed but executable is still missing from PATH.")
    except Exception as e:
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Failed to install Google Cloud SDK: {e}")
        logger.error("======================================================")
        raise e

def install_nodejs_and_npm():
    """Tries to install Node.js and NPM using the detected system package manager (OpenSUSE, RedHat, Ubuntu, etc)."""
    logger.info("NPM not found. Attempting to install Node.js and NPM automatically...")
    if shutil.which("zypper"):
        logger.info("Detected openSUSE (zypper). Installing nodejs and npm...")
        run_command(["sudo", "zypper", "--non-interactive", "install", "nodejs", "npm"])
    elif shutil.which("dnf"):
        logger.info("Detected RedHat/Fedora (dnf). Installing nodejs and npm...")
        run_command(["sudo", "dnf", "install", "-y", "nodejs", "npm"])
    elif shutil.which("yum"):
        logger.info("Detected CentOS (yum). Installing nodejs and npm...")
        run_command(["sudo", "yum", "install", "-y", "nodejs", "npm"])
    elif shutil.which("apt-get"):
        logger.info("Detected Debian/Ubuntu (apt-get). Installing nodejs and npm...")
        run_command(["sudo", "apt-get", "update"])
        run_command(["sudo", "apt-get", "install", "-y", "nodejs", "npm"])
    elif shutil.which("brew"):
        logger.info("Detected Homebrew. Installing node...")
        run_command(["brew", "install", "node"])
    else:
        logger.error("No recognized system package manager found to install Node.js/NPM. Please install node manually.")

def check_node_version() -> int:
    """Checks the active Node.js major version. Returns major version integer or 0 if missing."""
    import re
    node_bin = shutil.which("node")
    if not node_bin:
        return 0
    try:
        proc = subprocess.run([node_bin, "--version"], capture_output=True, text=True, timeout=10)
        out = proc.stdout.strip()
        m = re.search(r'v?(\d+)', out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0

def check_system_prerequisites():
    if is_dry_run():
        logger.info("🔍 [DRY-RUN] Simulating system prerequisites verification (skipped).")
        return
    """Verifies baseline OS utilities (git, curl) and corporate CA certificates."""
    logger.info("------------------------------------------------------")
    logger.info("🛠️  [PRE-FLIGHT] Verifying Host System Utilities & Trust Store")
    logger.info("------------------------------------------------------")
    for tool in ["curl", "git"]:
        if shutil.which(tool):
            logger.info(f"✅ Utility '{tool}' verified on PATH: {shutil.which(tool)}")
        else:
            logger.warning(f"⚠️  Utility '{tool}' not found on PATH. Attempting automatic installation...")
            if shutil.which("zypper"):
                run_command(["sudo", "zypper", "--non-interactive", "install", tool], check=False)
            elif shutil.which("dnf"):
                run_command(["sudo", "dnf", "install", "-y", tool], check=False)
            elif shutil.which("apt-get"):
                run_command(["sudo", "apt-get", "install", "-y", tool], check=False)
            elif shutil.which("brew"):
                run_command(["brew", "install", tool], check=False)

    # Trust store verification & persistent profile exporting
    from utils import persist_system_ca_environment
    persist_system_ca_environment(log=logger)

@with_retry(max_attempts=2, backoff_seconds=5)
def check_and_install_gemini_cli(force_install: bool = False):
    if is_dry_run():
        logger.info("🔍 [DRY-RUN] Simulating Gemini CLI verification/installation (skipped).")
        return
    """Checks if Gemini CLI is installed via npm. If missing or forced, installs Gemini CLI with descriptive outputs."""
    logger.info("------------------------------------------------------")
    logger.info("🤖 [VERIFYING] Gemini Developer CLI")
    logger.info("------------------------------------------------------")
    logger.info(f"- Force Reinstall: {'Yes' if force_install else 'No'}")

    node_ver = check_node_version()
    if 0 < node_ver < 18:
        logger.warning("------------------------------------------------------")
        logger.warning(f"⚠️  [NODE VERSION WARNING] Detected Node.js v{node_ver}, but Gemini CLI and modern MCP servers require Node.js v18+.")
        logger.warning("   Please upgrade Node.js on your system (e.g. 'sudo zypper install nodejs20' or NodeSource repository).")
        logger.warning("------------------------------------------------------")
    
    if not force_install and (shutil.which("gemini") or shutil.which("gemini-cli")):
        logger.info("------------------------------------------------------")
        logger.info("✅ [SUCCESS] Gemini Developer CLI is already installed and ready.")
        logger.info("------------------------------------------------------")
        return
        
    if not shutil.which("npm"):
        try:
            install_nodejs_and_npm()
        except Exception as err:
            logger.error(f"Failed to install system Node.js/NPM: {err}")
            
    if not shutil.which("npm"):
        logger.error("------------------------------------------------------")
        logger.error("❌ [FAILURE] NPM/Node.js is missing. Gemini CLI requires NPM for installation.")
        logger.error("======================================================")
        return
        
    logger.info("- Status : Installing Gemini CLI globally via NPM...")
    cmd = ["npm", "install", "-g", "@google/gemini-cli"]
    if force_install:
        cmd.append("--force")
        
    try:
        run_command(cmd)
        if shutil.which("gemini") or shutil.which("gemini-cli"):
            logger.info("------------------------------------------------------")
            logger.info("✅ [SUCCESS] Gemini CLI successfully installed and verified!")
            logger.info("------------------------------------------------------")
        else:
            raise RuntimeError("NPM completed but 'gemini' executable not found in PATH.")
    except Exception as e:
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Failed to install Gemini CLI: {e}")
        logger.error("======================================================")
        raise e

@with_retry(max_attempts=2, backoff_seconds=5)
def check_and_install_antigravity_cli(force_install: bool = False):
    if is_dry_run():
        logger.info("🔍 [DRY-RUN] Simulating Antigravity CLI verification/installation (skipped).")
        return
    """Checks if Antigravity CLI (agy) is installed. If missing or forced, installs it with descriptive logs."""
    logger.info("------------------------------------------------------")
    logger.info("🚀 [VERIFYING] Antigravity CLI (agy)")
    logger.info("------------------------------------------------------")
    logger.info(f"- Force Reinstall: {'Yes' if force_install else 'No'}")
    
    if not force_install and shutil.which("agy"):
        logger.info("------------------------------------------------------")
        logger.info("✅ [SUCCESS] Antigravity CLI (agy) is already installed and ready.")
        logger.info("------------------------------------------------------")
        return
        
    logger.info("- Status : Downloading and executing official Antigravity installation script...")
    install_cmd = "curl -fsSL https://antigravity.google/cli/install.sh | bash"
    try:
        run_command(install_cmd, shell=True)
        
        # Ensure standard bin directory (~/.local/bin or similar) is in PATH
        home_dir = os.path.expanduser("~")
        possible_paths = [
            os.path.join(home_dir, ".local", "bin"),
            "/usr/local/bin"
        ]
        for p in possible_paths:
            if os.path.exists(p) and p not in os.environ["PATH"]:
                os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]
                
        if shutil.which("agy"):
            logger.info("------------------------------------------------------")
            logger.info("✅ [SUCCESS] Antigravity CLI (agy) successfully installed and verified!")
            logger.info("------------------------------------------------------")
        else:
            raise RuntimeError("Installation script completed but 'agy' executable is still missing from PATH.")
    except Exception as e:
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Failed to install Antigravity CLI: {e}")
        logger.error("======================================================")
        raise e

def check_and_install_antigravity_ide(force_install: bool = False):
    if is_dry_run():
        logger.info("🔍 [DRY-RUN] Simulating Antigravity IDE verification/installation (skipped).")
        return
    """Checks and downloads the Antigravity IDE (AppImage) for Linux desktop development, falling back gracefully on headless."""
    logger.info("------------------------------------------------------")
    logger.info("💻 [VERIFYING] Google Antigravity IDE")
    logger.info("------------------------------------------------------")
    logger.info(f"- Force Reinstall: {'Yes' if force_install else 'No'}")
    
    home_dir = os.path.expanduser("~")
    ide_dir = os.path.join(home_dir, "Applications")
    ide_path = os.path.join(ide_dir, "Antigravity-IDE.AppImage")
    
    if not force_install and os.path.exists(ide_path):
        logger.info("------------------------------------------------------")
        logger.info(f"✅ [SUCCESS] Antigravity IDE (AppImage) already exists at {ide_path}.")
        logger.info("------------------------------------------------------")
        return
        
    logger.info("- Status : Fetching latest Antigravity IDE Linux AppImage...")
    try:
        os.makedirs(ide_dir, exist_ok=True)
        # Fetch using curl with standard redirect and fail options
        cmd = ["curl", "-fsSL", "https://antigravity.google/download/linux/AppImage", "-o", ide_path]
        res = run_command(cmd, check=False)
        if res == 0 and os.path.exists(ide_path) and os.path.getsize(ide_path) > 1000:
            os.chmod(ide_path, 0o755) # make executable
            logger.info("------------------------------------------------------")
            logger.info(f"✅ [SUCCESS] Antigravity IDE successfully downloaded and authorized at: {ide_path}")
            logger.info("------------------------------------------------------")
        else:
            # Cleanup any empty/partial file
            if os.path.exists(ide_path):
                try: os.remove(ide_path)
                except Exception: pass
            logger.warning("------------------------------------------------------")
            logger.warning("⚠️  [NOTICE] Desktop Antigravity IDE AppImage not found or blocked on this host.")
            logger.warning("   (This is normal on headless CLI servers. You can download the full desktop IDE")
            logger.warning("   manually on your laptop from: https://antigravity.google/download)")
            logger.warning("------------------------------------------------------")
    except Exception as e:
        logger.warning(f"⚠️  Failed to trigger Antigravity IDE setup: {e}")

def setup_gemini_workspace():
    if is_dry_run():
        logger.info("🔍 [DRY-RUN] Simulating Gemini workspace directory and symlink setup (skipped).")
        return
    """Sets up local agent and skill directories and symlinks them to the workspace .gemini folder."""
    workspace_root = WORKSPACE_ROOT
    # Dynamically resolve the folder containing this active script to allow maximum directory-layout portability!
    google_ai_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 1. Create agent/skill directories in our project folder
    agents_src = os.path.join(google_ai_dir, "agents")
    skills_src = os.path.join(google_ai_dir, "skills")
    os.makedirs(agents_src, exist_ok=True)
    os.makedirs(skills_src, exist_ok=True)
    logger.info(f"Source directories ready: {agents_src}, {skills_src}")
    
    # 2. Create the hidden .gemini directory at workspace root
    gemini_dest = os.path.join(workspace_root, ".gemini")
    agents_dest = os.path.join(gemini_dest, "agents")
    skills_dest = os.path.join(gemini_dest, "skills")
    os.makedirs(gemini_dest, exist_ok=True)
    
    # 3. Create symlinks from .gemini to google_ai_initialization
    for src, dest in [(agents_src, agents_dest), (skills_src, skills_dest)]:
        if os.path.exists(dest):
            if os.path.islink(dest):
                logger.info(f"Symlink {dest} already exists.")
                continue
            elif os.path.isdir(dest):
                logger.info(f"Directory {dest} exists. Moving contents to source and replacing with symlink.")
                # If there are any files, move them to src
                for item in os.listdir(dest):
                    s_item = os.path.join(dest, item)
                    d_item = os.path.join(src, item)
                    if not os.path.exists(d_item):
                        shutil.move(s_item, d_item)
                os.rmdir(dest)
        
        try:
            os.symlink(src, dest)
            logger.info(f"Created symlink: {dest} -> {src}")
        except Exception as e:
            logger.error(f"Failed to create symlink {dest}: {e}")

def verify_and_save_bin_paths() -> bool:
    """
    Verifies that the absolute paths for both gcloud and gemini executables can be resolved.
    Saves these paths into the active workspace .env file.
    If either is missing, raises a fatal error and returns False.
    """
    logger.info("------------------------------------------------------")
    logger.info("🛡️  [VERIFICATION] Resolving CLI Executable Absolute Paths")
    logger.info("------------------------------------------------------")
    
    # 1. Resolve gcloud absolute path
    gcloud_path = shutil.which("gcloud")
    if not gcloud_path:
        # Fallback check standard home path
        home_dir = os.path.expanduser("~")
        candidate = os.path.join(home_dir, "google-cloud-sdk", "bin", "gcloud")
        if os.path.exists(candidate):
            gcloud_path = os.path.abspath(candidate)
            
    # 2. Resolve gemini absolute path
    gemini_path = shutil.which("gemini") or shutil.which("gemini-cli")
    
    if gcloud_path:
        gcloud_abs = os.path.abspath(gcloud_path)
        logger.info(f"✅ GCloud Executable resolved: {gcloud_abs}")
        update_env_file("GCLOUD_BIN_PATH", gcloud_abs)
    else:
        logger.error("❌ FAILED: Google Cloud SDK (gcloud) executable could not be found!")
        
    if gemini_path:
        gemini_abs = os.path.abspath(gemini_path)
        logger.info(f"✅ Gemini CLI Executable resolved: {gemini_abs}")
        update_env_file("GEMINI_BIN_PATH", gemini_abs)
    else:
        logger.error("❌ FAILED: Gemini Developer CLI (gemini) executable could not be found!")

    # 3. Resolve uv / uvx runner absolute path
    uvx_path = shutil.which("uvx") or shutil.which("uv")
    if uvx_path:
        uvx_abs = os.path.abspath(uvx_path)
        logger.info(f"✅ Python MCP Runner (uvx) resolved: {uvx_abs}")
        update_env_file("UVX_BIN_PATH", uvx_abs)
    else:
        logger.warning("⚠️  Note: uvx runner not detected in current PATH (JIT runner resolution available).")
        
    if gcloud_path and gemini_path:
        logger.info("------------------------------------------------------")
        logger.info("✅ [SUCCESS] CLI paths successfully verified and written to .env!")
        logger.info("------------------------------------------------------")
        return True
    else:
        logger.error("------------------------------------------------------")
        logger.error("❌ [FAILURE] Critical verification failed. Missing required CLI binaries.")
        logger.error("======================================================")
        return False

def main(force_install: bool = False, break_dependencies: bool = False, downgrade: bool = False, timeout: int = 180):
    os.environ["PROCESS_TIMEOUT_OVERRIDE"] = str(timeout)
    logger.info("==============================================")
    logger.info("Starting Phase 1: Environment Setup and Verify")
    logger.info("==============================================")
    if not verify_prephase_disk_space("Phase 1: Environment Setup", log=logger):
        return False
    
    try:
        check_system_prerequisites()
        install_pip_packages(force_install, break_dependencies, downgrade)
        install_playwright_browsers()
        check_and_install_gcloud(force_install)
        check_and_install_gemini_cli(force_install)
        check_and_install_antigravity_cli(force_install)
        check_and_install_antigravity_ide(force_install)
        setup_gemini_workspace()
        
        # Execute absolute path verification and save back to environment file
        if not verify_and_save_bin_paths():
            return False
            
        logger.info("Phase 1 completed successfully!")
        return True
    except Exception as e:
        logger.error(f"Phase 1 execution failed: {e}")
        return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Google AI Phase 1 Environment Installer. Dynamically detects, downloads, installs, "
                    "and configures GCloud SDK, Playwright-Chromium engines, Gemini CLI, Antigravity CLI, "
                    "and mandatory Python packages across openSUSE, RedHat, Ubuntu/Debian, and Homebrew."
    )
    parser.add_argument(
        "--force-install", 
        help="Force a clean, fresh reinstallation of all software packages, libraries, and CLI tools. "
             "This completely bypasses the script's automatic 'already installed' detection checks, forcing "
             "a re-download and re-install from scratch.", 
        action="store_true"
    )
    parser.add_argument(
        "--break-dependencies", 
        help="Force-install Python packages bypassing standard system conflict blocks. This directly appends "
             "the '--break-system-packages' flag to pip, allowing direct installation into externally-managed "
             "system Python environments (typical for Homebrew, openSUSE, and RedHat) without throwing warning errors.", 
        action="store_true"
    )
    parser.add_argument(
        "--downgrade", 
        help="Downgrade and install compatible older stable version benchmarks of the key libraries. "
             "This locks your dependencies to highly stable historical releases (google-genai==2.10.0 and "
             "playwright==1.50.0), which is recommended if your current OS kernel cannot support the latest releases.", 
        action="store_true"
    )
    parser.add_argument(
        "--timeout",
        help="Specify the absolute execution timeout in seconds for any spawned package installations, network checks, or download processes (defaults to 180 seconds).",
        type=int,
        default=180
    )
    parser.add_argument(
        "--dry-run",
        help="Simulate execution without installing packages, modifying files, or creating symlinks.",
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
    
    success = main(
        force_install=args.force_install,
        break_dependencies=args.break_dependencies,
        downgrade=args.downgrade,
        timeout=args.timeout
    )
    sys.exit(0 if success else 1)
