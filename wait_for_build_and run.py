import subprocess
import requests
import os
import sys
import time
import argparse

REPO_PATH        = r"G:\repos\applications.validation.autobench-next.core"
ARTIFACTORY_BASE = "https://gfx-assets.igk.intel.com/artifactory"
ARTIFACT_PATH    = "gfx-3d-assets-igk/AutoBenchNext/ABN-manual"
GTAXRERUNNER     = r"g:\repos\NightPerson\gtaxreruner.py"
USERNAME         = os.environ.get("ABN_AF_USER")
PASSWORD         = os.environ.get("ABN_AF_PASSWORD")

POLL_INTERVAL_SEC = 60
TIMEOUT_SEC       = 3600  # 1 hour

def parse_args():
    parser = argparse.ArgumentParser(description="Wait for build artifact and trigger GTA-X rerun")
    parser.add_argument("--job-id",    type=int, required=True, help="Original job ID to rerun in gtaxreruner.py")
    parser.add_argument("--jobset-id", type=int, default=None,  help="Jobset ID (optional, passed through to gtaxreruner.py)")
    return parser.parse_args()

def get_current_commit_hash(repo_path: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()

def find_artifact_by_commit(short_hash: str) -> list[str]:
    """List directories under ARTIFACT_PATH and return those ending with short_hash."""
    url = f"{ARTIFACTORY_BASE}/api/storage/{ARTIFACT_PATH}?list&deep=0&listFolders=1"
    response = requests.get(url, auth=(USERNAME, PASSWORD))
    response.raise_for_status()

    data = response.json()
    files = data.get("files", [])
    matches = [
        entry["uri"].lstrip("/")
        for entry in files
        if entry["uri"].rstrip("/").endswith(short_hash)
    ]
    return matches

def run_gtaxrerunner(job_id: int, new_version: str, jobset_id: int | None):
    cmd = [
        sys.executable, GTAXRERUNNER,
        "--job-id",      str(job_id),
        "--new-version", new_version,
    ]
    if jobset_id is not None:
        cmd += ["--jobset-id", str(jobset_id)]

    print(f"\n🚀 Launching gtaxreruner.py ...")
    print(f"   Command: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    return result.returncode

if __name__ == "__main__":
    args = parse_args()

    commit_hash = get_current_commit_hash(REPO_PATH)
    short_hash  = commit_hash[:8]

    print("=" * 65)
    print("Waiting for Artifactory build artifact")
    print(f"Repo commit  : {commit_hash}")
    print(f"Short hash   : {short_hash}")
    print(f"Polling every {POLL_INTERVAL_SEC}s, timeout {TIMEOUT_SEC // 60} min")
    print("=" * 65)

    deadline = time.time() + TIMEOUT_SEC
    attempt  = 0

    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[{time.strftime('%H:%M:%S')}] Attempt #{attempt} — checking Artifactory ... (timeout in {remaining // 60}m {remaining % 60}s)")

        try:
            matches = find_artifact_by_commit(short_hash)
        except Exception as e:
            print(f"  ⚠️  Error querying Artifactory: {e}")
            matches = []

        if matches:
            new_version = matches[0]  # use first match
            print(f"  ✅ Found artifact: {new_version}")
            if len(matches) > 1:
                print(f"  ℹ️  Multiple matches found, using first. Others: {matches[1:]}")

            rc = run_gtaxrerunner(args.job_id, new_version, args.jobset_id)
            sys.exit(rc)

        print(f"  ❌ Not found yet. Retrying in {POLL_INTERVAL_SEC}s ...")
        time.sleep(POLL_INTERVAL_SEC)

    print(f"\n⏰ Timeout reached ({TIMEOUT_SEC // 60} min). No artifact found for short hash '{short_hash}'.")
    sys.exit(1)
