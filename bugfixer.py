import sys
import asyncio
import argparse
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
# Copilot session
# ---------------------------------------------------------------------------
async def run_copilot(prompt: str):
    print("\n" + "=" * 68)
    print("  Sending to Copilot ...")
    print("=" * 68)
    print(f"  Prompt: {prompt}\n")

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
    response = await session.send_and_wait(prompt, timeout=600)
    print(response.data.content)
    await client.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()

    # Build base prompt
    prompt = args.prompt if args.prompt else DEFAULT_PROMPT

    # If a job ID was supplied: analyse the job and prepend findings to prompt
    if args.job_id:
        output_dir = args.output_dir or Path(f"job_{args.job_id}_artifacts")
        task_summary, log_paths, screenshot_paths = analyse_job(args.job_id, output_dir)

        # Build @-file references for all downloaded logs
        log_refs = "\n".join(f"@{p}" for p in log_paths)

        # Build @-file references for the selected screenshots
        # (windows_on_error + 4 preceding, sorted by filename/capture time)
        ss_refs = "\n".join(f"@{p}" for p in screenshot_paths)

        if screenshot_paths:
            error_ss = next((p for p in screenshot_paths if "windows_on_error" in p.name), None)
            ss_note = (
                f"Screenshots (4 preceding the error + the 'windows_on_error' frame):"
                if error_ss else "Screenshots (last 5 available):"
            )
        else:
            ss_note = "No screenshots available."

        prompt = (
            f"GTA-X job {args.job_id} analysis:\n"
            f"{task_summary}\n\n"
            f"Log file for the failed task:\n{log_refs}\n\n"
            f"{ss_note}\n{ss_refs}\n\n"
            f"{prompt}"
        )

    asyncio.run(run_copilot(prompt))


if __name__ == "__main__":
    main()