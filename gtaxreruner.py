import requests
import json
import re
import os

# --- Configuration ------------------------------------------------------------
BASE_URL  = "https://gtax-postsi-igk.intel.com/api/v1"
USERNAME  = os.environ.get("ABN_AF_USER")    # Replace with your Intel IDSID
PASSWORD  = os.environ.get("ABN_AF_PASSWORD") # Replace with your Intel password
AUTH      = (USERNAME, PASSWORD)
HEADERS   = {"Content-Type": "application/json"}

# --- Target Job & Resource Configuration -------------------------------------
ORIGINAL_JOB_ID  = 110271955         # ? Replace with your actual job ID from jobset 56199692
JOBSET_ID        = 56166023       # The jobset to rerun the job in

# Resource version replacement details
# These values depend on your TaskML structure — adjust as needed
OLD_RESOURCE_VERSION = "ABN-vdev-7863-20260421-074054-mtegowsk-f7336ca7"    # ? The version string to replace
NEW_RESOURCE_VERSION = "ABN-vdev-7869-20260422-101249-apaliswi-b0e09e6b"    # ? The new version string to use

# --- Step 1: Fetch the Original Job's TaskML ----------------------------------
def get_job_taskml(job_id):
    """Fetch the full job data including TaskML."""
    print(f"[Step 1] Fetching TaskML for job ID: {job_id} ...")
    url = f"{BASE_URL}/jobs/{job_id}"
    params = {"include_taskml": True}

    response = requests.get(url, params=params, auth=AUTH, headers=HEADERS)
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


# --- Step 2: Modify the Resource Version in the TaskML ------------------------
def modify_resource_version(taskml, old_version, new_version):
    """
    Modify the resource version in the TaskML.

    The TaskML can be either:
    - A dict (parsed JSON) — do a deep string replacement
    - A string — do a direct string replacement
    
    Adjust this function based on your actual TaskML structure.
    For example, TaskML often looks like:
    {
      "resources": [
        {"name": "my_package", "version": "1.0.0", "uri": "..."}
      ]
    }
    """
    print(f"[Step 2] Modifying resource version: {old_version} ? {new_version}")

    # Convert to string if it's a dict
    if isinstance(taskml, dict):
        taskml_str = json.dumps(taskml)
    else:
        taskml_str = str(taskml)

    # Count occurrences before replacing
    count = taskml_str.count(old_version)
    if count == 0:
        print(f"??  Warning: Old version '{old_version}' not found in TaskML!")
        print("     Review the TaskML structure and adjust version strings accordingly.")
    else:
        print(f"   Found {count} occurrence(s) of '{old_version}' — replacing all with '{new_version}'")

    modified_taskml_str = taskml_str.replace(old_version, new_version)

    # Return as string (job_data field expects a JSON string)
    return modified_taskml_str


# --- Step 3: Submit a New Job with the Modified TaskML ------------------------
def submit_job_with_modified_taskml(jobset_id, modified_taskml_str, original_job_name=""):
    """
    Submit a new job to the same jobset with the modified TaskML.
    Uses POST /jobs with job_data containing the modified TaskML.
    """
    print(f"[Step 3] Submitting new job to jobset {jobset_id} with modified TaskML ...")

    url = f"{BASE_URL}/jobs"
    payload = {
        "job_name": f"{original_job_name} [Rerun - Resource v{NEW_RESOURCE_VERSION}]",
        "job_data": modified_taskml_str,  # The modified TaskML
        "submitter": USERNAME,
    }

    response = requests.post(url, json=payload, auth=AUTH, headers=HEADERS)

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

    # Step 2: Modify the resource version
    modified_taskml = modify_resource_version(taskml, OLD_RESOURCE_VERSION, NEW_RESOURCE_VERSION)

    # Step 3: Submit the new job with the modified TaskML
    new_job = submit_job_with_modified_taskml(JOBSET_ID, modified_taskml, original_job_name)

    if new_job:
        print("\n? Workflow complete!")
        print(f"   Original Job ID : {ORIGINAL_JOB_ID}")
        print(f"   New Job ID      : {new_job.get('id')}")
        print(f"   Resource changed: {OLD_RESOURCE_VERSION} ? {NEW_RESOURCE_VERSION}")
