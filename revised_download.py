#!/usr/bin/env python3
"""
revised_download.py  -  GTA-X Artifact Downloader

Downloads ABN log files and screenshots from a GTA-X job using the
/api/v1/jobs/{id}/results endpoint, which returns:
  { "phases": [ { "tasks": [ { "artifacts":       { "task_logs": [filenames...] },
                               "runner_artifacts": { "task_logs": [full URLs...] } } ] } ] }

  Note: URLs in runner_artifacts use paths like .../logs/storage/... and .../results/storage/...

Requirements:
    pip install requests urllib3

Usage:
    set ABN_AF_USER=your_idsid
    set ABN_AF_PASSWORD=your_password
    python revised_download.py --job-id 110597889
    python revised_download.py --job-id 110597889 --output-dir C:/logs/my_run

Set DEBUG=1 for verbose output.
"""

import os
import re
import sys
import getpass
import argparse
from pathlib import Path

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuration ------------------------------------------------------------
BASE_URL = os.environ.get("GTAX_BASE_URL", "https://gtax-postsi-igk.intel.com")
DEBUG    = os.environ.get("DEBUG", "0") == "1"

# File type detection
ABN_LOG_KEYWORDS      = ["abn", "stderr", "stdout"]
ABN_LOG_EXTENSIONS    = {".log", ".txt"}
SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}


# --- Argument parsing ---------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Download ABN log files and screenshots from a GTA-X job")
    parser.add_argument("--job-id",     type=int,  required=True,
                        help="GTA-X job ID to download artifacts from")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Destination directory (default: ./job_<ID>_artifacts)")
    parser.add_argument("--cmd-filter", type=str, default="abn_cmd.py",
                        help="Only download from tasks whose command contains this string "
                             "(default: 'abn_cmd.py', use '' to download from all tasks)")
    return parser.parse_args()


# --- Auth & session -----------------------------------------------------------

def build_auth():
    """Basic Auth using ABN_AF_USER / ABN_AF_PASSWORD env vars."""
    username = os.environ.get("ABN_AF_USER")     or input("Intel IDSID (username): ").strip()
    password = os.environ.get("ABN_AF_PASSWORD") or getpass.getpass("Intel Password: ")
    return (username, password)


def create_session() -> requests.Session:
    sess = requests.Session()
    sess.auth   = build_auth()
    sess.verify = False
    sess.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return sess


# --- API call -----------------------------------------------------------------

def fetch_job_results(sess: requests.Session, job_id: int) -> dict:
    """
    Call GET /api/v1/jobs/{job_id}/results and return parsed JSON.
    Expected top-level key: "phases"
    """
    url = f"{BASE_URL}/api/v1/jobs/{job_id}/results"
    print(f"  -> {url}")
    resp = sess.get(url, timeout=60)
    if DEBUG:
        print(f"  [DEBUG] HTTP {resp.status_code}  ({len(resp.content)} bytes)")
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code} - {url}\n{resp.text[:600]}")
    data = resp.json()
    if DEBUG:
        print(f"  [DEBUG] Top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
    return data


# --- Flatten tasks from phases ------------------------------------------------

def iter_tasks(job_results: dict):
    """Yield every task dict from phases[].tasks[], injecting _phase_name."""
    phases = job_results.get("phases", [])
    if DEBUG:
        print(f"  [DEBUG] Phases found: {len(phases)}")
    for phase in phases:
        phase_name = phase.get("name") or phase.get("phase_type", "unknown")
        tasks      = phase.get("tasks", [])
        if DEBUG:
            print(f"  [DEBUG]   Phase '{phase_name}': {len(tasks)} task(s)")
        for task in tasks:
            task["_phase_name"] = phase_name
            yield task


# --- Build (filename, url) pairs from a task ----------------------------------

def get_file_pairs(task: dict, job_id: int) -> list:
    """
    Return (filename, download_url) pairs for a task.

    URLs are built directly via build_proxy_url() using the job_id,
    task order_id, phase_name, and the filename from artifacts.task_logs / task_results.
    """
    artifacts  = task.get("artifacts") or {}
    order_id   = task.get("order_id", 0)
    phase_name = task.get("_phase_name", "tests")
    pairs      = []

    art_type_map = {"task_logs": "logs", "task_results": "results"}
    for art_key, art_type in art_type_map.items():
        names = artifacts.get(art_key) or []
        if DEBUG:
            print(f"    [DEBUG] {art_key}: {len(names)} file(s)  phase={phase_name}  order_id={order_id}")
        for name in names:
            fname = Path(name).name
            url   = build_proxy_url(job_id, order_id, fname,
                                    phase_name=phase_name, artifact_type=art_type)
            pairs.append((fname, url))

    return pairs


# --- File-type classification -------------------------------------------------

def is_abn_log(filename: str) -> bool:
    p = Path(filename)
    return p.suffix.lower() in ABN_LOG_EXTENSIONS and any(kw in p.stem.lower() for kw in ABN_LOG_KEYWORDS)


def is_screenshot(filename: str) -> bool:
    return Path(filename).suffix.lower() in SCREENSHOT_EXTENSIONS


# --- URL construction --------------------------------------------------------

def build_proxy_url(job_id: int, order_id: int, filename: str,
                   phase_name: str = "tests",
                   artifact_type: str = "logs") -> str:
    """
    Build the GTA-X proxy URL for a task artifact file.

    Path structure:
      BASE_URL/logs/jobs/jobs/{padded[0:4]}/{padded[4:8]}/{job_id}
                            /{artifact_type}/{phase_name}/{order_id}/{filename}

    where padded = zero-padded job_id to 12 digits, e.g.
      job_id=110597889  ->  000110597889  ->  0001/1059/110597889

    artifact_type: 'logs' for task_logs, 'results' for task_results.
    phase_name:    'setup', 'tests', or 'teardown' - taken from the phase.
    """
    padded = f"{job_id:012d}"
    url = (
        f"{BASE_URL}/logs/jobs/jobs"
        f"/{padded[0:4]}/{padded[4:8]}/{job_id}"
        f"/{artifact_type}/{phase_name}/{order_id}/{filename}"
    )
    if DEBUG:
        print(f"    [DEBUG] proxy url: {url}")
    return url


# --- Download -----------------------------------------------------------------

def download_file(sess: requests.Session, url: str, dest: Path, output_dir: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if DEBUG:
        print(f"    [DEBUG] GET {url}")
    r = sess.get(url, stream=True, timeout=180)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}  {url}")
    with open(dest, "wb") as fh:
        for chunk in r.iter_content(65_536):
            if chunk:
                fh.write(chunk)
    print(f"    OK  {dest.relative_to(output_dir)}  ({dest.stat().st_size / 1024:.1f} KB)")


# --- Main ---------------------------------------------------------------------

def main():
    args       = parse_args()
    JOB_ID     = args.job_id
    OUTPUT_DIR = args.output_dir if args.output_dir is not None else Path(f"job_{JOB_ID}_artifacts")
    CMD_FILTER = args.cmd_filter

    print("=" * 68)
    print("  GTA-X Artifact Downloader  (ABN logs + screenshots)")
    print(f"  Job      : {JOB_ID}")
    print(f"  Instance : {BASE_URL}")
    print(f"  Output   : {OUTPUT_DIR.resolve()}")
    print(f"  Filter   : {'(none - all tasks)' if not CMD_FILTER else repr(CMD_FILTER)}")
    print(f"  DEBUG    : {'ON  (set DEBUG=0 to disable)' if DEBUG else 'OFF (set DEBUG=1 to enable)'}")
    print("=" * 68)

    sess = create_session()

    # -- 1. Fetch job results --------------------------------------------------
    print(f"\n[1/3] Fetching job results for job {JOB_ID} ...")
    try:
        job_results = fetch_job_results(sess, JOB_ID)
    except Exception as exc:
        print(f"ERROR: Could not fetch job results: {exc}")
        sys.exit(1)

    phases      = job_results.get("phases", [])
    total_tasks = sum(len(p.get("tasks", [])) for p in phases)
    print(f"      Phases: {len(phases)}  |  Tasks: {total_tasks}")

    # -- 2. Scan tasks ---------------------------------------------------------
    print("\n[2/3] Scanning task artifacts ...")
    to_download = {"abn_logs": [], "screenshots": []}

    for task in iter_tasks(job_results):
        task_id    = task.get("id")
        command    = (task.get("command") or f"task_{task_id}").split("\n")[0]
        phase_name = task.get("_phase_name", "")
        order_id   = task.get("order_id", 0)
        status     = task.get("status", "?")

        if CMD_FILTER and CMD_FILTER not in command:
            if DEBUG:
                print(f"  [skip] [{phase_name}] #{order_id} - command does not match filter: {command[:80]}")
            continue

        safe_dir   = re.sub(r"[^\w\-.]", "_", f"{phase_name}_{order_id}_{command[:50]}")

        print(f"\n  [{phase_name:8s}] #{order_id:<3} {status:8s}  id={task_id}")
        print(f"       cmd: {command[:90]}")

        pairs = get_file_pairs(task, JOB_ID)
        print(f"       files available: {len(pairs)}")

        for fname, url in pairs:
            if is_abn_log(fname):
                dest = OUTPUT_DIR / "abn_logs" / safe_dir / fname
                to_download["abn_logs"].append((fname, url, dest))
                print(f"       [ABN LOG]    {fname}")
            elif is_screenshot(fname):
                dest = OUTPUT_DIR / "screenshots" / safe_dir / fname
                to_download["screenshots"].append((fname, url, dest))
                print(f"       [SCREENSHOT] {fname}")
            else:
                if DEBUG:
                    print(f"       [skip]       {fname}")

    # -- 3. Download -----------------------------------------------------------
    n_abn = len(to_download["abn_logs"])
    n_sc  = len(to_download["screenshots"])
    total = n_abn + n_sc

    print(f"\n[3/3] Downloading ...  (ABN logs: {n_abn}  |  Screenshots: {n_sc})")

    if total == 0:
        print("\n  WARNING: No ABN logs or screenshots matched.")
        print("      Possible reasons:")
        print("      * runner_artifacts URLs absent (task not yet uploaded).")
        print("      * File names do not match ABN/screenshot patterns.")
        print("      * Run with DEBUG=1 for detailed per-file output.")
        sys.exit(0)

    errors = []
    for label, items in [("ABN Logs", to_download["abn_logs"]),
                          ("Screenshots", to_download["screenshots"])]:
        if items:
            print(f"\n  -- {label} -----------------------------------------")
            for fname, url, dest in items:
                try:
                    download_file(sess, url, dest, OUTPUT_DIR)
                except Exception as exc:
                    print(f"    FAILED  {fname}: {exc}")
                    errors.append(fname)

    # -- Summary ---------------------------------------------------------------
    ok = total - len(errors)
    print("\n" + "=" * 68)
    print(f"  Downloaded : {ok}/{total}")
    if errors:
        print(f"  Failed     : {', '.join(errors)}")
    print(f"  Location   : {OUTPUT_DIR.resolve()}")
    print("=" * 68)


if __name__ == "__main__":
    main()