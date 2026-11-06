import requests
import json
import re
import os
import argparse

# --- Configuration ------------------------------------------------------------
BASE_URL  = "https://gtax-postsi-igk.intel.com/api/v1"
USERNAME  = os.environ.get("ABN_AF_USER")    # Replace with your Intel IDSID
PASSWORD  = os.environ.get("ABN_AF_PASSWORD") # Replace with your Intel password
AUTH      = (USERNAME, PASSWORD)
HEADERS   = {"Content-Type": "application/json"}

# Resource version patterns
MANUAL_VERSION_PATTERN = r'ABN-vdev-\d+-\d{8}-\d{6}-[A-Za-z0-9]+-[a-f0-9]+'
CI_VERSION_PATTERN     = r'ABN-v\d+\.\d+\.\d+'

# --- Argument Parsing ---------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="GTA-X: Rerun Job with Modified Resource Version")
    parser.add_argument("--job-id",      type=int, required=True, help="Original job ID to rerun")
    parser.add_argument("--jobset-id",   type=int, default=None, help="Jobset ID to submit the new job into (auto-detected from original job if omitted)")
    parser.add_argument("--old-version", type=str, default=None,  help="Resource version string to replace (auto-detected from TaskML if omitted). Accepts ABN-manual format (ABN-vdev-...) or ABN-CI format (ABN-v2.9.335)")
    parser.add_argument("--new-version", type=str, required=True, help="New resource version string to use")
    return parser.parse_args()

# --- Step 1: Fetch the Original Job's TaskML ----------------------------------
def get_job_taskml(job_id):
    """Fetch the full job data including TaskML."""
    print(f"[Step 1] Fetching TaskML for job ID: {job_id} ...")
    url = f"{BASE_URL}/jobs/{job_id}"
    params = {"include_taskml": True}

    response = requests.get(url, params=params, auth=AUTH, verify=False, headers=HEADERS)
    if response.status_code == 200:
        job_data = response.json()
        taskml = job_data.get("taskml") or job_data.get("data") 
        if not taskml:
            print("??  Warning: TaskML not found in response. Keys available:", list(job_data.keys()))
        else:
            print(f"? TaskML retrieved successfully (length: {len(str(taskml))} chars)")
        return job_data, taskml
    elif response.status_code == 404:
        raise Exception(f"? Job {job_id} not found!")
    else:
        raise Exception(f"? Failed to fetch job: {response.status_code} — {response.text}")


# --- Step 1b: Deduce the Resource Version from TaskML -----------------------
def deduce_resource_version(taskml):
    """
    Attempt to auto-detect the resource version string from the TaskML.

    Recognises two formats:
      ABN-manual : ABN-vdev-7863-20260421-074054-mtegowsk-f7336ca7
      ABN-CI     : ABN-v2.9.335

    Returns the first match found (manual format takes priority), or raises if
    neither is detected.
    """
    if isinstance(taskml, dict):
        taskml_str = json.dumps(taskml)
    else:
        taskml_str = str(taskml)

    # Try ABN-manual format first
    matches = list(dict.fromkeys(re.findall(MANUAL_VERSION_PATTERN, taskml_str)))
    version_type = "ABN-manual"

    # Fall back to ABN-CI format
    if not matches:
        matches = list(dict.fromkeys(re.findall(CI_VERSION_PATTERN, taskml_str)))
        version_type = "ABN-CI"

    if not matches:
        raise Exception("Could not auto-detect a resource version from the TaskML. "
                        "Please supply --old-version explicitly.")

    if len(matches) > 1:
        print(f"[Auto-detect] Multiple {version_type} version candidates found in TaskML:")
        for i, m in enumerate(matches):
            print(f"  [{i}] {m}")
        print(f"[Auto-detect] Using first candidate: {matches[0]}")
        print("  Pass --old-version explicitly to override.")
    else:
        print(f"[Auto-detect] Detected {version_type} resource version: {matches[0]}")

    return matches[0]


# --- Step 2: Modify the Resource Version in the TaskML ------------------------
def modify_resource_version(taskml, old_version, new_version):
    """
    Modify the resource version in the TaskML.

    Handles two cases:
      ABN-manual : replaces the version string only.
      ABN-CI     : replaces the version string AND renames the resource
                   from 'ABN-CI' to 'ABN-manual'.
    """
    print(f"[Step 2] Modifying resource version: {old_version} → {new_version}")

    if isinstance(taskml, dict):
        taskml_str = json.dumps(taskml)
    else:
        taskml_str = str(taskml)

    # Detect whether old_version is a CI-style version (ABN-v2.9.335)
    is_ci_version = bool(re.fullmatch(CI_VERSION_PATTERN, old_version))

    # Replace the version string
    count = taskml_str.count(old_version)
    if count == 0:
        print(f"⚠️  Warning: Old version '{old_version}' not found in TaskML!")
        print("     Review the TaskML structure and adjust version strings accordingly.")
    else:
        print(f"   Found {count} occurrence(s) of '{old_version}' — replacing all with '{new_version}'")
    taskml_str = taskml_str.replace(old_version, new_version)

    # For CI versions also rename the resource type ABN-CI -> ABN-manual
    if is_ci_version:
        ci_count = taskml_str.count('"ABN-CI"')
        if ci_count == 0:
            print("⚠️  Warning: 'ABN-CI' resource name not found in TaskML after version replacement.")
        else:
            print(f"   Found {ci_count} occurrence(s) of '\"ABN-CI\"' — renaming to '\"ABN-manual\"'")
        taskml_str = taskml_str.replace('"ABN-CI"', '"ABN-manual"')

    return taskml_str


# --- Step 3: Submit a New Job with the Modified TaskML ------------------------
def submit_job_with_modified_taskml(jobset_id, modified_taskml_str, new_version, original_job_name=""):
    """
    Submit a new job to the same jobset with the modified TaskML.
    Uses POST /jobs with job_data containing the modified TaskML.
    """
    print(f"[Step 3] Submitting new job to jobset {jobset_id} with modified TaskML ...")

    url = f"{BASE_URL}/jobs"
    payload = {
        "job_name": f"{original_job_name} [Rerun - Resource v{new_version}]",
        "job_data": modified_taskml_str,  # The modified TaskML
        "submitter": USERNAME,
    }
    if jobset_id is not None:
        payload["jobset_id"] = jobset_id

    response = requests.post(url, json=payload, auth=AUTH, verify=False, headers=HEADERS)

    if response.status_code == 200:
        new_job = response.json()
        new_job_id = new_job.get("id", "unknown")
        print(f"? New job submitted successfully! New Jobset ID: {new_job_id}")
        print(f"   View at: https://gtax-postsi-igk.intel.com/#/jobsets/{new_job_id}?tab=jobs")
        return new_job
    elif response.status_code == 403:
        print("? Forbidden: You are not authorized to submit jobs.")
    elif response.status_code == 400:
        print(f"? Bad Request: {response.text}")
    elif response.status_code == 417:
        print(f"? TaskML Parse Error: {response.text}")
    else:
        print(f"? Error {response.status_code}: {response.text}")

    return None


# --- Optional: Inspect the TaskML Structure -----------------------------------
def inspect_taskml_resources(taskml):
    """
    Helper to print resource-related keys in the TaskML.
    Useful to find the correct path to modify the resource version.
    """
    print("\n[INFO] Inspecting TaskML for resource entries ...")
    if isinstance(taskml, str):
        try:
            taskml = json.loads(taskml)
        except json.JSONDecodeError:
            print("  TaskML is not valid JSON — treating as raw string")
            print(f"  First 500 chars: {taskml[:500]}")
            return

    def find_versions(obj, path=""):
        """Recursively search for 'version' or 'uri' keys."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                current_path = f"{path}.{k}" if path else k
                if k.lower() in ("version", "uri", "resource", "package", "artifact"):
                    print(f"  ?? [{current_path}] = {v}")
                find_versions(v, current_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                find_versions(item, f"{path}[{i}]")

    find_versions(taskml)


# --- Main Workflow -------------------------------------------------------------
if __name__ == "__main__":
    args = parse_args()

    ORIGINAL_JOB_ID      = args.job_id
    JOBSET_ID            = args.jobset_id
    OLD_RESOURCE_VERSION = args.old_version
    NEW_RESOURCE_VERSION = args.new_version

    print("=" * 65)
    print("GTA-X: Rerun Job with Modified Resource Version")
    print(f"Instance : https://gtax-postsi-igk.intel.com")
    print(f"Jobset   : {JOBSET_ID}")
    print(f"Job ID   : {ORIGINAL_JOB_ID}")
    print(f"Resource : {OLD_RESOURCE_VERSION} ? {NEW_RESOURCE_VERSION}")
    print("=" * 65)

    # Step 1: Get the original job's TaskML
    job_data, taskml = get_job_taskml(ORIGINAL_JOB_ID)
    original_job_name = job_data.get("name", f"Job {ORIGINAL_JOB_ID}")

    # Optional: Inspect TaskML to find resource version paths
    inspect_taskml_resources(taskml)

    # Auto-detect old version from TaskML if not provided
    if OLD_RESOURCE_VERSION is None:
        OLD_RESOURCE_VERSION = deduce_resource_version(taskml)
        print(f"   Using auto-detected version: {OLD_RESOURCE_VERSION}")

    # Step 2: Modify the resource version
    modified_taskml = modify_resource_version(taskml, OLD_RESOURCE_VERSION, NEW_RESOURCE_VERSION)

    # Step 3: Submit the new job with the modified TaskML
    new_job = submit_job_with_modified_taskml(JOBSET_ID, modified_taskml, NEW_RESOURCE_VERSION, original_job_name)

    if new_job:
        print("\n? Workflow complete!")
        print(f"   Original Job ID : {ORIGINAL_JOB_ID}")
        print(f"   New Job ID      : {new_job.get('id')}")
        print(f"   Resource changed: {OLD_RESOURCE_VERSION} ? {NEW_RESOURCE_VERSION}")
