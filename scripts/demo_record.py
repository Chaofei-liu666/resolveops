from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def run_cli(*args: str, check: bool = False) -> str:
    cmd = [sys.executable, "resolveops.py", *args]
    print()
    print("$ " + " ".join(cmd))
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    print(output.rstrip())
    if check and completed.returncode != 0:
        raise SystemExit(completed.returncode)
    return output


def extract_case_id(output: str) -> str | None:
    match = re.search(r"case created:\s*([0-9a-fA-F-]{32,})", output)
    if match:
        return match.group(1)
    match = re.search(r"next:\s*python resolveops\.py case show\s+([0-9a-fA-F-]{32,})", output)
    if match:
        return match.group(1)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a short ResolveOps CLI demo for screen recording.")
    parser.add_argument("--order", default="SAL-ORD-2026-00002")
    parser.add_argument("--suite", default="core-v4")
    parser.add_argument("--polls", type=int, default=8)
    parser.add_argument("--poll-interval", type=int, default=8)
    parser.add_argument("--no-create", action="store_true", help="Do not create a new demo Case.")
    args = parser.parse_args()

    banner("ResolveOps CLI demo")
    print("This demo shows the operator CLI, runtime checks, evaluation summary,")
    print("and one Case investigation trail. It does not approve write actions.")

    banner("1. Runtime status")
    run_cli("status")

    banner("2. Recent Cases")
    run_cli("case", "list")

    banner("3. Fixed evaluation snapshot")
    run_cli("eval", "summary", "--suite", args.suite, "--limit", "50")

    case_id: str | None = None
    if not args.no_create:
        banner("4. Create a new demo Case")
        created = run_cli(
            "case",
            "create",
            "--type",
            "inventory_shortage",
            "--order",
            args.order,
            "--reason",
            "screen recording demo: inventory shortage",
        )
        case_id = extract_case_id(created)

    if not case_id:
        banner("4. No new Case created")
        print("Use a Case ID from the list above and run:")
        print("python resolveops.py case show <case-id>")
        print("python resolveops.py case watch <case-id>")
        return 0

    banner("5. Watch the Agent trail")
    print(f"case_id={case_id}")
    print("Polling Case status. Stop here if you only need a short recording.")
    for idx in range(args.polls):
        print()
        print(f"--- poll {idx + 1}/{args.polls} ---")
        run_cli("case", "show", case_id)
        if idx < args.polls - 1:
            time.sleep(max(1, args.poll_interval))

    banner("6. What to explain")
    print("- The Case is durable state, not a chat session.")
    print("- Read tools collect ERP evidence before planning.")
    print("- The model proposes an action plan; it does not directly write ERP data.")
    print("- Policy and bound approval block unsafe execution.")
    print("- Verification reads ERPNext again after writes.")
    print("- Evaluation checks trajectory quality, not only final text.")
    print()
    print("Next manual command if the Case has an approval:")
    print("python resolveops.py case show " + case_id)
    print("python resolveops.py approval approve <approval-id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
