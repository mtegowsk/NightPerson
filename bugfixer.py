import sys
import time
import asyncio
import argparse
import subprocess
import functools
import winreg
import importlib
from pathlib import Path

# ---------------------------------------------------------------------------
# Allow imports from sibling scripts in the same directory
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

from copilot import CopilotClient
from copilot.generated.session_events import SessionEventType
from copilot.session import PermissionHandler
from job_status import check_job_and_find_first_failed_task
from revised_download import create_session, fetch_job_results, iter_tasks, \
    get_file_pairs, is_abn_log, is_screenshot, download_file, \
    BASE_URL as DL_BASE_URL

# Import helpers from wait_for_build_and run.py (space in filename requires importlib)
_wfb = importlib.import_module("wait_for_build_and run")
get_current_commit_hash = _wfb.get_current_commit_hash
find_artifact_by_commit = _wfb.find_artifact_by_commit
run_gtaxrerunner        = _wfb.run_gtaxrerunner
REPO_PATH               = _wfb.REPO_PATH

# ---------------------------------------------------------------------------
# Default Copilot prompt
# ---------------------------------------------------------------------------
DEFAULT_PROMPT = (
    "Make a fix in a file: scripts/windows/python/workstation/"
    "DaVinciResolveStudio19/include/Setup.py to support both resolve 19 and 20. "
    "I want you to make all necessary edits without any questions."
)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Bugfixer: optionally analyse a GTA-X job then invoke Copilot")
    parser.add_argument(
        "--prompt", type=str, default=None,
        help="Copilot prompt to use (default: built-in DaVinci Resolve prompt)")
    parser.add_argument(
        "--job-id", type=int, default=None,
        help="GTA-X job ID: retrieve failing task info and download logs before "
             "sending to Copilot")
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Directory for downloaded logs (default: ./job_<ID>_artifacts)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Job analysis + log download
# ---------------------------------------------------------------------------
def select_screenshots_for_prompt(all_screenshot_paths: list[Path]) -> list[Path]:
    """
    From the downloaded screenshots, return:
      - The first screenshot whose filename contains 'windows_on_error'
      - The 4 screenshots that immediately precede it (by sort order)
    Total: up to 5 screenshots, ending with the error screenshot.
    If no 'windows_on_error' screenshot exists, returns the last 5.
    """
    # Sort by filename so iteration order matches capture time
    sorted_paths = sorted(all_screenshot_paths, key=lambda p: p.name)

    error_idx = next(
        (i for i, p in enumerate(sorted_paths) if "windows_on_error" in p.name),
        None,
    )

    if error_idx is not None:
        start = max(0, error_idx - 4)
        return sorted_paths[start : error_idx + 1]

    # Fallback: last 5 screenshots
    return sorted_paths[-5:]


def analyse_job(job_id: int, output_dir: Path) -> tuple[str, list[Path], list[Path]]:
    """
    1. Find the first failed task via job_status.
    2. Download all matching logs/screenshots from that task.
    Returns (summary_str, downloaded_log_paths, selected_screenshot_paths).
    """
    print("=" * 68)
    print(f"  Analysing GTA-X job {job_id}")
    print("=" * 68)

    # -- 1. Find failing task --------------------------------------------------
    print("\n[1/2] Checking job status and locating first failed task ...")
    failed = check_job_and_find_first_failed_task(job_id)

    if failed is None:
        task_summary = f"Job {job_id} passed — no failed tasks found."
        print(f"  {task_summary}")
    else:
        task_summary = (
            f"Job {job_id} FAILED.\n"
            f"  First failed task #{failed['task_number']}: {failed['task_name']!r}\n"
            f"  Phase: {failed['phase']},  order_id: {failed['order_id']}"
        )
        print(f"\n  {task_summary}")

    # -- 2. Download logs ------------------------------------------------------
    print(f"\n[2/2] Downloading logs to {output_dir.resolve()} ...")
    sess = create_session()

    try:
        job_results = fetch_job_results(sess, job_id)
    except Exception as exc:
        print(f"  ERROR fetching job results: {exc}")
        return task_summary, [], []

    to_download = {"abn_logs": [], "screenshots": []}
    import re

    for task in iter_tasks(job_results):
        task_id    = task.get("id")
        command    = (task.get("command") or f"task_{task_id}").split("\n")[0]
        phase_name = task.get("_phase_name", "")
        order_id   = task.get("order_id", 0)

        # Only download logs from the failed task
        if failed is not None:
            if phase_name != failed["phase"] or order_id != failed["order_id"]:
                continue

        safe_dir = re.sub(r"[^\w\-.]", "_", f"{phase_name}_{order_id}_{command[:50]}")

        for fname, url in get_file_pairs(task, job_id):
            if is_abn_log(fname):
                dest = output_dir / "abn_logs" / safe_dir / fname
                to_download["abn_logs"].append((fname, url, dest))
            elif is_screenshot(fname):
                dest = output_dir / "screenshots" / safe_dir / fname
                to_download["screenshots"].append((fname, url, dest))

    n_abn = len(to_download["abn_logs"])
    n_sc  = len(to_download["screenshots"])
    print(f"  Files to download — ABN logs: {n_abn}  |  Screenshots: {n_sc}")

    downloaded_logs        = []
    downloaded_screenshots = []
    errors                 = []

    for category, items in to_download.items():
        for fname, url, dest in items:
            try:
                download_file(sess, url, dest, output_dir)
                if category == "abn_logs":
                    downloaded_logs.append(dest)
                else:
                    downloaded_screenshots.append(dest)
            except Exception as exc:
                print(f"    FAILED {fname}: {exc}")
                errors.append(fname)

    ok = (n_abn + n_sc) - len(errors)
    print(f"\n  Downloaded {ok}/{n_abn + n_sc}  →  {output_dir.resolve()}")

    selected_screenshots = select_screenshots_for_prompt(downloaded_screenshots)

    return task_summary, downloaded_logs, selected_screenshots


# ---------------------------------------------------------------------------
# ProxyCap control
# ---------------------------------------------------------------------------
_PROXYCAP_HIVE    = winreg.HKEY_LOCAL_MACHINE
_PROXYCAP_SUBKEY  = r"SOFTWARE\Proxy Labs\ProxyCap"
_PROXYCAP_VALUE   = "Disabled"
_PROXYCAP_CHECK_URL = "https://store.steampowered.com/"
_PROXYCAP_SETTLE_S  = 2   # seconds to wait after each registry change
_PROXYCAP_VERIFY_RETRIES = 6
_PROXYCAP_VERIFY_DELAY_S = 5


def _set_proxycap(disabled: bool) -> None:
    """Write the ProxyCap Disabled registry DWORD (0 = enabled, 1 = disabled)."""
    value = 1 if disabled else 0
    label = "DISABLED" if disabled else "ENABLED"
    try:
        key = winreg.OpenKey(_PROXYCAP_HIVE, _PROXYCAP_SUBKEY,
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, _PROXYCAP_VALUE, 0, winreg.REG_DWORD, value)
        winreg.CloseKey(key)
        print(f"  [ProxyCap] Registry set -> {label}")
    except OSError as exc:
        raise RuntimeError(f"Failed to set ProxyCap registry value: {exc}") from exc


def _proxycap_is_active() -> bool:
    """
    Verify ProxyCap is active by probing Steam store.
    Returns True if the probe succeeds (ProxyCap routing is working).
    """
    import requests as _req
    import urllib3 as _u3
    _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
    try:
        r = _req.get(_PROXYCAP_CHECK_URL, timeout=10, verify=False)
        return r.status_code < 500
    except Exception:
        return False


def _wait_for_proxycap(expected_active: bool) -> None:
    """Poll until ProxyCap reaches the expected state, or raise on timeout."""
    state_label = "ENABLED" if expected_active else "DISABLED"
    print(f"  [ProxyCap] Waiting for ProxyCap to become {state_label} ...", end="", flush=True)
    time.sleep(_PROXYCAP_SETTLE_S)
    for attempt in range(_PROXYCAP_VERIFY_RETRIES):
        active = _proxycap_is_active()
        if active == expected_active:
            print(" OK")
            return
        print(".", end="", flush=True)
        time.sleep(_PROXYCAP_VERIFY_DELAY_S)
    print(" TIMEOUT")
    raise RuntimeError(
        f"ProxyCap did not reach expected state ({state_label}) "
        f"after {_PROXYCAP_VERIFY_RETRIES} retries."
    )


def with_proxycap(async_func):
    """
    Decorator that enables ProxyCap before the wrapped async function runs
    and disables it again afterwards (even on error).
    """
    @functools.wraps(async_func)
    async def wrapper(*args, **kwargs):
        print("\n  [ProxyCap] Enabling ProxyCap for Copilot call ...")
        _set_proxycap(disabled=False)
        _wait_for_proxycap(expected_active=True)
        try:
            return await async_func(*args, **kwargs)
        finally:
            print("\n  [ProxyCap] Disabling ProxyCap after Copilot call ...")
            _set_proxycap(disabled=True)
            _wait_for_proxycap(expected_active=False)
    return wrapper


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
def git_commit_and_push(repo_path: Path, message: str) -> None:
    """
    Stage all changes, commit with the given message, and push.
    Prints a warning (but does not raise) if nothing to commit or push fails.
    """
    def run(args: list) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=repo_path, capture_output=True, text=True)

    print("\n" + "=" * 68)
    print("  Git: checking for changes ...")
    print("=" * 68)

    status = run(["git", "status", "--porcelain"])
    if not status.stdout.strip():
        print("  No changes detected — nothing to commit.")
        return

    print(f"  Changes detected:\n{status.stdout.rstrip()}")

    add = run(["git", "add", "-A"])
    if add.returncode != 0:
        print(f"  WARNING: git add failed:\n{add.stderr}")
        return

    commit = run(["git", "commit", "-m", message])
    if commit.returncode != 0:
        print(f"  WARNING: git commit failed:\n{commit.stderr}")
        return
    print(f"  Committed: {commit.stdout.strip()}")

    push = run(["git", "push"])
    if push.returncode != 0:
        print(f"  WARNING: git push failed:\n{push.stderr}")
        return
    print("  Pushed successfully.")


# ---------------------------------------------------------------------------
# Copilot session  (persistent across iterations for context retention)
# ---------------------------------------------------------------------------
async def start_copilot_session():
    """Create and return (client, session) that stay alive across iterations."""
    print("\n  [ProxyCap] Enabling ProxyCap for Copilot ...")
    _set_proxycap(disabled=False)
    _wait_for_proxycap(expected_active=True)

    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        on_permission_request=PermissionHandler.approve_all,
        model="claude-sonnet-4.6",
        streaming=True,
    )

    def handle(event):
        if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            print(event.data.delta_content, end="", flush=True)

    session.on(handle)
    return client, session


async def send_to_copilot(session, prompt: str):
    """Send a prompt within an existing session and wait for a response."""
    print("\n" + "=" * 68)
    print("  Sending to Copilot ...")
    print("=" * 68)
    print(f"  Prompt: {prompt[:200]}{'...' if len(prompt) > 200 else ''}\n")

    # Temporarily enable ProxyCap for the Copilot call
    _set_proxycap(disabled=False)
    _wait_for_proxycap(expected_active=True)
    try:
        response = await session.send_and_wait(prompt, timeout=600)
        print(response.data.content)
    finally:
        _set_proxycap(disabled=True)
        _wait_for_proxycap(expected_active=False)

    # Commit and push any changes Copilot made to the codebase
    repo_path  = Path.cwd()
    commit_msg = f"bugfixer: apply Copilot fix\n\nPrompt: {prompt[:200]}"
    git_commit_and_push(repo_path, commit_msg)


async def stop_copilot_session(client):
    """Shut down the persistent Copilot client."""
    print("\n  [ProxyCap] Disabling ProxyCap ...")
    _set_proxycap(disabled=True)
    await client.stop()


# ---------------------------------------------------------------------------
# Wait for build artifact on Artifactory
# ---------------------------------------------------------------------------
BUILD_POLL_INTERVAL = 60       # seconds between Artifactory polls
BUILD_TIMEOUT       = 3600     # 1 hour

def wait_for_build() -> str | None:
    """
    Get current commit hash, poll Artifactory for a matching build.
    Returns the artifact version string, or None on timeout.
    """
    commit_hash = get_current_commit_hash(REPO_PATH)
    short_hash  = commit_hash[:8]
    print(f"\n  [Build] Waiting for artifact matching commit {short_hash} ...")

    deadline = time.time() + BUILD_TIMEOUT
    attempt  = 0
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"  [{time.strftime('%H:%M:%S')}] Attempt #{attempt} "
              f"(timeout in {remaining // 60}m {remaining % 60}s)")
        try:
            matches = find_artifact_by_commit(short_hash)
        except Exception as e:
            print(f"    WARNING: {e}")
            matches = []
        if matches:
            print(f"    Found: {matches[0]}")
            return matches[0]
        time.sleep(BUILD_POLL_INTERVAL)

    print("    TIMEOUT: No build artifact found within 1 hour.")
    return None


# ---------------------------------------------------------------------------
# Wait for GTA-X job to finish
# ---------------------------------------------------------------------------
import requests as _requests
import urllib3 as _urllib3
_urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)

JOB_POLL_INTERVAL = 60       # seconds between job status polls
JOB_TIMEOUT       = 7200     # 2 hours

def wait_for_job_completion(job_id: int) -> dict | None:
    """
    Poll a GTA-X job until it reaches a terminal state (completed / aborted / cancelled).
    Returns the job JSON dict, or None on timeout.
    """
    import os
    auth = (os.environ["ABN_AF_USER"], os.environ["ABN_AF_PASSWORD"])
    base = "https://gtax-postsi-igk.intel.com/api/v1"
    url  = f"{base}/jobs/{job_id}"

    print(f"\n  [Job] Waiting for job {job_id} to finish (timeout: 2h) ...")
    deadline = time.time() + JOB_TIMEOUT
    while time.time() < deadline:
        try:
            r = _requests.get(url, auth=auth, verify=False, timeout=30)
            r.raise_for_status()
            job = r.json()
            status = job.get("status", "unknown")
            result = job.get("result", "")
            print(f"    [{time.strftime('%H:%M:%S')}] status={status}, result={result}")
            if status in ("completed", "aborted", "cancelled"):
                return job
        except Exception as exc:
            print(f"    [{time.strftime('%H:%M:%S')}] Poll error: {exc}")
        time.sleep(JOB_POLL_INTERVAL)

    print("    TIMEOUT: Job did not finish within 2 hours.")
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
MAX_ITERATIONS = 6


def build_prompt_for_job(job_id: int, user_prompt: str, output_dir: Path) -> str:
    """Analyse a job and build the full Copilot prompt with @-file references."""
    task_summary, log_paths, screenshot_paths = analyse_job(job_id, output_dir)

    log_refs = "\n".join(f"@{p}" for p in log_paths)
    ss_refs  = "\n".join(f"@{p}" for p in screenshot_paths)

    if screenshot_paths:
        error_ss = next((p for p in screenshot_paths if "windows_on_error" in p.name), None)
        ss_note = (
            "Screenshots (4 preceding the error + the 'windows_on_error' frame):"
            if error_ss else "Screenshots (last 5 available):"
        )
    else:
        ss_note = "No screenshots available."

    return (
        f"GTA-X job {job_id} analysis:\n"
        f"{task_summary}\n\n"
        f"Log file for the failed task:\n{log_refs}\n\n"
        f"{ss_note}\n{ss_refs}\n\n"
        f"{user_prompt}"
    )


def main():
    args = parse_args()
    user_prompt = args.prompt if args.prompt else DEFAULT_PROMPT
    job_id      = args.job_id

    async def run_loop():
        # Start a single Copilot session that persists across all iterations
        client, session = await start_copilot_session()
        # Disable ProxyCap after session is established (network calls below don't need it)
        _set_proxycap(disabled=True)
        _wait_for_proxycap(expected_active=False)

        try:
            for iteration in range(1, MAX_ITERATIONS + 1):
                print("\n" + "#" * 68)
                print(f"  ITERATION {iteration}/{MAX_ITERATIONS}")
                print("#" * 68)

                # -- 1. Analyse failed job & invoke Copilot --------------------
                nonlocal job_id
                if job_id:
                    output_dir = args.output_dir or Path(f"job_{job_id}_artifacts")
                    prompt = build_prompt_for_job(job_id, user_prompt, output_dir)
                else:
                    prompt = user_prompt

                await send_to_copilot(session, prompt)

                # -- 2. Wait for new build on Artifactory (1h) -----------------
                print("\n" + "=" * 68)
                print("  Waiting for new build on Artifactory ...")
                print("=" * 68)
                new_version = wait_for_build()
                if new_version is None:
                    print("  ABORTING: No build artifact appeared in time.")
                    sys.exit(1)

                # -- 3. Trigger GTA-X rerun ------------------------------------
                print("\n" + "=" * 68)
                print(f"  Triggering GTA-X rerun with version: {new_version}")
                print("=" * 68)
                original_job_id = args.job_id

                import re as _re
                cmd = [
                    sys.executable, str(Path(__file__).parent / "gtaxreruner.py"),
                    "--job-id", str(original_job_id),
                    "--new-version", new_version,
                ]
                print(f"  Command: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True)
                print(result.stdout)
                if result.stderr:
                    print(result.stderr)

                match = _re.search(r"New Jobset ID:\s*(\d+)", result.stdout)
                if not match:
                    print("  ABORTING: Could not determine new job ID from gtaxreruner output.")
                    sys.exit(1)
                new_job_id = int(match.group(1))
                print(f"  New job/jobset ID: {new_job_id}")

                # -- 4. Wait for job to complete (2h) --------------------------
                job_data = wait_for_job_completion(new_job_id)
                if job_data is None:
                    print("  ABORTING: Job did not finish in time.")
                    sys.exit(1)

                job_status = job_data.get("status")
                job_result = job_data.get("result")
                print(f"\n  Job {new_job_id} finished: status={job_status}, result={job_result}")

                # -- 5. Check result -------------------------------------------
                if job_result == "passed":
                    print("\n" + "=" * 68)
                    print(f"  SUCCESS! Job {new_job_id} passed after {iteration} iteration(s).")
                    print("=" * 68)
                    return

                print(f"  Job still failing. Will attempt another fix iteration ...")
                job_id = new_job_id

            # Exhausted all iterations
            print("\n" + "=" * 68)
            print(f"  FAILED: Job still not passing after {MAX_ITERATIONS} iterations.")
            print("=" * 68)
            sys.exit(1)

        finally:
            await stop_copilot_session(client)

    asyncio.run(run_loop())


if __name__ == "__main__":
    # Ensure ProxyCap is off before we do any GTA-X / Artifactory network calls
    print("[ProxyCap] Ensuring ProxyCap is disabled at startup ...")
    _set_proxycap(disabled=True)
    main()