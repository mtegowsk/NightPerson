import os
import argparse
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://gtax-postsi-igk.intel.com/api/v1"

INTEL_USERNAME = os.environ["ABN_AF_USER"]
INTEL_PASSWORD = os.environ["ABN_AF_PASSWORD"]

AUTH = (INTEL_USERNAME, INTEL_PASSWORD)


def check_job_and_find_first_failed_task(job_id: int):
    """
    Fetches a GTA-X job by ID.
    - If the job succeeded (status=completed AND result=passed), returns None.
    - Otherwise, returns the name and 1-based task number of the first failed task.
    """

    # -- Step 1: Fetch job details ---------------------------------------------
    job_resp = requests.get(
        f"{BASE_URL}/jobs/{job_id}",
        auth=AUTH,
        verify=False,
    )
    job_resp.raise_for_status()
    job = job_resp.json()

    status = job.get("status")
    result = job.get("result")

    print(f"Job {job_id} ? status={status!r}, result={result!r}")

    # -- Step 2: Check for success ---------------------------------------------
    if status == "completed" and result == "passed":
        print("? Job succeeded!")
        return None

    print("? Job did NOT succeed. Searching for first failed task...")

    # -- Step 3: Fetch all task results (all phases) ---------------------------
    PHASE_ORDER = ["setup", "tests", "teardown"]   # canonical phase ordering

    all_tasks = []   # will collect dicts: {phase, order_id, command, status}

    tr_resp = requests.get(
        f"{BASE_URL}/jobs/{job_id}/results",
        auth=AUTH,
        verify=False,
    )
    tr_resp.raise_for_status()
    data = tr_resp.json()

    # Response structure: {"phases": [{"name": "setup", "tasks": [...]}]}
    for phase_obj in data.get("phases", []):
        phase = phase_obj.get("name") or phase_obj.get("phase_type", "unknown")
        for task in phase_obj.get("tasks", []):
            all_tasks.append({
                "phase":    phase,
                "order_id": task.get("order_id", 0),
                "command":  (task.get("command") or "").split("\n")[0],
                "status":   task.get("status", ""),
            })

    # -- Step 4: Sort tasks globally (phase order × order_id) -----------------
    phase_rank = {p: i for i, p in enumerate(PHASE_ORDER)}
    all_tasks.sort(key=lambda t: (phase_rank.get(t["phase"], 99), t["order_id"]))

    # -- Step 5: Find the first failed task -----------------------------------
    for one_based_number, task in enumerate(all_tasks, start=1):
        if task["status"] == "failed":
            print(
                f"First failed task ? #{one_based_number}  "
                f"(phase={task['phase']}, order_id={task['order_id']}): "
                f"{task['command']!r}"
            )
            return {
                "task_number": one_based_number,   # 1-based, global across all phases
                "task_name":   task["command"],
                "phase":       task["phase"],
                "order_id":    task["order_id"],
            }

    print("No failed tasks found (job may have been aborted or cancelled).")
    return None


# -- Entry point --------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check GTA-X job status and find the first failed task")
    parser.add_argument("--job-id", type=int, required=True,
                        help="GTA-X job ID to inspect")
    args = parser.parse_args()

    result = check_job_and_find_first_failed_task(args.job_id)
    if result:
        print(f"\nSummary: First failed task is #{result['task_number']}: {result['task_name']!r}")
