import os
import sys
import requests

# -- Configuration -------------------------------------------------------------
BASE_URL    = "https://gtax-postsi-igk.intel.com/api/v1"
JOBSET_ID   = 56673000

INTEL_USERNAME = os.environ["ABN_AF_USER"]
INTEL_PASSWORD = os.environ["ABN_AF_PASSWORD"]
AUTH = (INTEL_USERNAME, INTEL_PASSWORD)

PER_PAGE = 100   # Max items per page — adjust if needed


def get_jobset_details(jobset_id: int) -> dict:
    """Fetch metadata for a single jobset."""
    print(f"[1/2] Fetching details for jobset {jobset_id}...")
    resp = requests.get(
        f"{BASE_URL}/jobsets/{jobset_id}",
        auth=AUTH,
        params={"full_info": False},
    )
    resp.raise_for_status()
    return resp.json()


def get_all_jobs_in_jobset(jobset_id: int) -> list:
    """
    Fetch ALL jobs belonging to the given jobset, handling pagination.
    Returns a flat list of job dicts.
    """
    print(f"[2/2] Fetching all jobs for jobset {jobset_id} (with pagination)...")
    all_jobs = []
    page = 1

    while True:
        resp = requests.get(
            f"{BASE_URL}/jobsets/{jobset_id}/jobs",
            auth=AUTH,
            params={
                "page":     page,
                "per_page": PER_PAGE,
                "order_by": "id",
                "order_type": "asc",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        # The endpoint may return a list directly, or a paginated envelope
        jobs = data if isinstance(data, list) else data.get("data", data)

        if not jobs:
            break   # No more pages

        all_jobs.extend(jobs)
        print(f"  Page {page}: retrieved {len(jobs)} job(s) "
              f"(total so far: {len(all_jobs)})")

        # Stop if fewer results than requested — we're on the last page
        if len(jobs) < PER_PAGE:
            break

        page += 1

    return all_jobs


def print_job_table(jobs: list):
    """Print a formatted table of jobs."""
    print(f"\n{'-' * 100}")
    print(f"{'ID':<15} {'NAME':<50} {'STATUS':<15} {'RESULT':<20} {'SUBMITTED_BY':<15}")
    print(f"{'-' * 100}")
    for job in jobs:
        job_id       = str(job.get("id", "N/A"))
        job_name     = str(job.get("name", "N/A"))[:48]   # truncate long names
        job_status   = str(job.get("status", "N/A"))
        job_result   = str(job.get("result", "N/A"))
        submitted_by = str(job.get("submitted_by", "N/A"))
        print(f"{job_id:<15} {job_name:<50} {job_status:<15} {job_result:<20} {submitted_by:<15}")
    print(f"{'-' * 100}")


def main():
    # -- Step 1: Fetch jobset details ------------------------------------------
    jobset = get_jobset_details(JOBSET_ID)

    jobset_name   = jobset.get("name", "N/A")
    jobset_status = jobset.get("status", "N/A")
    submitted_by  = jobset.get("submitted_by", "N/A")

    print(f"\n{'='*60}")
    print(f"  Jobset ID   : {JOBSET_ID}")
    print(f"  Name        : {jobset_name}")
    print(f"  Status      : {jobset_status}")
    print(f"  Submitted By: {submitted_by}")
    print(f"{'='*60}\n")

    # -- Step 2: Fetch all jobs ------------------------------------------------
    jobs = get_all_jobs_in_jobset(JOBSET_ID)

    if not jobs:
        print("??  No jobs found in this jobset.")
        sys.exit(0)

    print(f"\n? Total jobs found: {len(jobs)}")
    print_job_table(jobs)

    # -- Step 3: Summary statistics --------------------------------------------
    from collections import Counter
    result_counts = Counter(j.get("result", "unknown") for j in jobs)
    status_counts = Counter(j.get("status", "unknown") for j in jobs)

    print(f"\n?? Result Summary:")
    for result, count in sorted(result_counts.items()):
        icon = "?" if result == "passed" else "?"
        print(f"   {icon}  {result}: {count}")

    print(f"\n??  Status Summary:")
    for status, count in sorted(status_counts.items()):
        print(f"   ??  {status}: {count}")


if __name__ == "__main__":
    main()
