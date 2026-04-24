import subprocess
import requests
import os

REPO_PATH     = r"G:\repos\applications.validation.autobench-next.core"
ARTIFACTORY_BASE = "https://gfx-assets.igk.intel.com/artifactory"
ARTIFACT_PATH    = "gfx-3d-assets-igk/AutoBenchNext/ABN-manual"
USERNAME = os.environ.get("ABN_AF_USER")
PASSWORD = os.environ.get("ABN_AF_PASSWORD")

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

if __name__ == "__main__":
    commit_hash = get_current_commit_hash(REPO_PATH)
    short_hash  = commit_hash[:8]
    print(f"Current commit hash : {commit_hash}")
    print(f"Short hash (8 chars): {short_hash}")
    print(f"Searching in        : {ARTIFACTORY_BASE}/{ARTIFACT_PATH} ...")

    matches = find_artifact_by_commit(short_hash)

    if matches:
        print(f"\n✅ Found {len(matches)} matching directory/ies:")
        for m in matches:
            print(f"   {ARTIFACTORY_BASE}/{ARTIFACT_PATH}/{m}")
    else:
        print(f"\n❌ No directory ending with '{short_hash}' found. Build may not be ready yet.")
