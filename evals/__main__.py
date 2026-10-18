"""Run the MCP evaluation suite: `uv run python -m evals --help`."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.environment import DEFAULT_DATABASE_URL, EVALS_DIR, EvalEnvironment, EvalSettings, Turn
from evals.scenarios import SCENARIOS, Checks, Scenario

DEFAULT_MODEL = "openai/gpt-6-luna"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Evaluate the HealthyEating MCP tools with a real model through opencode.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("EVAL_MODEL", DEFAULT_MODEL),
        help=f"opencode model id (default: $EVAL_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--runs", type=int, default=3, help="runs per scenario; all must pass (default: 3)"
    )
    parser.add_argument("--only", help="comma-separated scenario names to run")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("EVAL_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="database for the eval (recreated on every run!)",
    )
    parser.add_argument("--opencode", default="opencode", help="opencode binary")
    parser.add_argument(
        "--turn-timeout", type=float, default=300.0, help="seconds per message (default: 300)"
    )
    return parser.parse_args(argv)


def select_scenarios(only: str | None) -> list[Scenario]:
    if not only:
        return SCENARIOS
    wanted = [name.strip() for name in only.split(",") if name.strip()]
    known = {scenario.name: scenario for scenario in SCENARIOS}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise SystemExit(f"Unknown scenario(s): {', '.join(unknown)}. Use --list.")
    return [known[name] for name in wanted]


async def run_suite(args: argparse.Namespace, scenarios: list[Scenario]) -> int:
    run_dir = EVALS_DIR / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    settings = EvalSettings(
        model=args.model,
        database_url=args.database_url,
        run_dir=run_dir,
        opencode_bin=args.opencode,
        turn_timeout_s=args.turn_timeout,
    )
    print(f"Model {args.model}, {args.runs} run(s) per scenario. Output: {run_dir}")
    print("Starting the app, the MCP proxy and opencode...", flush=True)

    results: list[dict[str, Any]] = []
    async with EvalEnvironment(settings) as env:
        for scenario in scenarios:
            for attempt in range(1, args.runs + 1):
                await env.reset()
                checks = Checks()
                turns: list[Turn] = []
                started = time.monotonic()
                error: str | None = None
                try:
                    await scenario.run(env, checks, turns)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    checks.require("scenario ran without errors", False, error)
                duration = round(time.monotonic() - started, 1)
                passed = checks.passed
                result = {
                    "scenario": scenario.name,
                    "run": attempt,
                    "passed": passed,
                    "duration_s": duration,
                    "checks": [asdict(check) for check in checks.items],
                    "turns": [asdict(turn) for turn in turns],
                    "tool_calls": [asdict(call) for call in env.proxy.calls] if env.proxy else [],
                    "sheet_rows": env.sheet_rows(),
                }
                results.append(result)
                write_json(run_dir / "transcripts" / f"{scenario.name}-{attempt}.json", result)
                status = "PASS" if passed else "FAIL"
                print(f"  {status}  {scenario.name} #{attempt} ({duration}s)", flush=True)
                for check in checks.items:
                    if check.required and not check.passed:
                        print(f"        failed: {check.name}: {check.detail[:200]}")

    report = build_report(args, scenarios, results)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    write_json(run_dir / "results.json", results)
    print()
    print(report)
    return 0 if all(result["passed"] for result in results) else 1


def build_report(
    args: argparse.Namespace, scenarios: list[Scenario], results: list[dict[str, Any]]
) -> str:
    lines = [
        f"# MCP evaluation: {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"Model `{args.model}`, {args.runs} run(s) per scenario; a scenario passes only if "
        "every run passes.",
        "",
        "| Scenario | Passed runs | Result | Failed checks |",
        "|---|---|---|---|",
    ]
    for scenario in scenarios:
        runs = [r for r in results if r["scenario"] == scenario.name]
        passed = sum(1 for r in runs if r["passed"])
        failed_checks = sorted(
            {
                check["name"]
                for r in runs
                for check in r["checks"]
                if check["required"] and not check["passed"]
            }
        )
        verdict = "PASS" if passed == len(runs) else "FAIL"
        lines.append(
            f"| {scenario.name} | {passed}/{len(runs)} | {verdict} | "
            f"{'; '.join(failed_checks) or '-'} |"
        )
    notes: dict[tuple[str, str], list[bool]] = {}
    for r in results:
        for check in r["checks"]:
            if not check["required"]:
                notes.setdefault((r["scenario"], check["name"]), []).append(check["passed"])
    if notes:
        lines += ["", "Notes (never fail a run; details in transcripts/):", ""]
        lines += [
            f"- {scenario}: {name}: {sum(values)}/{len(values)}"
            for (scenario, name), values in notes.items()
        ]
    total = sum(1 for r in results if r["passed"])
    lines += ["", f"Total: {total}/{len(results)} runs passed."]
    return "\n".join(lines) + "\n"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    scenarios = select_scenarios(args.only)
    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.name}: {scenario.description}")
        return 0
    if shutil.which(args.opencode) is None:
        raise SystemExit(f"opencode not found ({args.opencode}); install it or pass --opencode")
    return asyncio.run(run_suite(args, scenarios))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
