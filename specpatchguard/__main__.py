"""CLI entrypoint for specpatchguard (`check-config`, `doctor`, `select-dataset`)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .core.config_loader import ConfigError, configuration_status, load_settings
from .core.doctor import run_doctor
from .data import DataLoadError, FurinaDataLoader, PatchBlindViolationError, load_intent_packs_for_dataset

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.json"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m specpatchguard",
        description="SpecPatchGuard configuration validation, preflight, and dataset selection CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser(
        "check-config",
        help="Validate config/config.json and .env loading without inspecting data files or invoking LLMs.",
    )
    check_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.json")
    check_parser.add_argument("--env-file", type=Path, default=None, help="Optional path to .env")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run read-only preflight checks for credentials, data files, output isolation, and server Docker/D4J/LSP.",
    )
    doctor_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.json")
    doctor_parser.add_argument("--env-file", type=Path, default=None, help="Optional path to .env")
    doctor_parser.add_argument(
        "--mode",
        choices=("auto", "local", "server"),
        default="auto",
        help="local skips Docker/D4J/jdtls checks; server enforces them; auto picks local on Windows and server on POSIX.",
    )
    doctor_parser.add_argument(
        "--check-api",
        action="store_true",
        help="Perform a lightweight GET /models reachability check against the configured API endpoint.",
    )

    select_parser = subparsers.add_parser(
        "select-dataset",
        help="Selectively load a Defects4J subset from preprocessed .pkl files, strip Patch-Blind, and route Tiers.",
    )
    select_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.json")
    select_parser.add_argument("--env-file", type=Path, default=None, help="Optional path to .env")
    select_parser.add_argument(
        "--dataset",
        type=str,
        default=None,
        help="Dataset directory (e.g. Closure/<id> or Lang/Lang_12), manifest (.txt/.csv/.json), or .diff/.patch path.",
    )
    select_parser.add_argument("--projects", nargs="+", default=None, help="Official Defects4J project names (e.g. Lang Math)")
    select_parser.add_argument("--bugs", nargs="+", default=None, help="Defects4J bug IDs (e.g. Lang-12 Math_1 Closure/101)")
    select_parser.add_argument("--patches", nargs="+", default=None, help="Target selectors (e.g. Lang-12/patch1.diff Lang-12::random)")
    select_parser.add_argument("--methods", nargs="+", default=None, help="Filter by pre-patch Java method names")
    select_parser.add_argument("--files", nargs="+", default=None, help="Filter by Java source file paths or suffixes")
    select_parser.add_argument(
        "--tier",
        choices=("auto", "tier_a", "tier_b"),
        default="auto",
        help="Filter selected packs by Tier classification (default: auto).",
    )
    select_parser.add_argument("--limit", type=int, default=None, help="Maximum number of patch records to load")
    select_parser.add_argument("--require-tests", action="store_true", help="Skip bugs lacking failing tests in .pkl")
    select_parser.add_argument("--strict", action="store_true", help="Fail if any requested bug or patch selector is missing")
    select_parser.add_argument(
        "--docker-fallback",
        action="store_true",
        help="Hydrate missing source functions, failing tests, and bug reports online from Docker Defects4J.",
    )
    select_parser.add_argument(
        "--docker-container",
        type=str,
        default=None,
        help="Optional running Docker container name for --docker-fallback (uses docker run --rm if omitted).",
    )
    select_parser.add_argument(
        "--include-unindexed-diffs",
        action="store_true",
        help="Load local .diff/.patch files discovered in --dataset even when not indexed in patch_info_pkl.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        settings = load_settings(args.config, env_path=args.env_file)
        if args.command == "check-config":
            print(json.dumps(configuration_status(settings), ensure_ascii=False, indent=2))
            return 0
        if args.command == "doctor":
            report = run_doctor(settings, mode=args.mode, check_api=args.check_api)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["overall_ok"] else 1
        if args.command == "select-dataset":
            loader = FurinaDataLoader.from_settings(
                settings,
                enable_docker_fallback=args.docker_fallback,
                container_name=args.docker_container,
            )
            load_result, packs = load_intent_packs_for_dataset(
                loader,
                source=args.dataset,
                target_tier=args.tier,
                projects=args.projects,
                bug_ids=args.bugs,
                targets=args.patches,
                file_paths=args.files,
                method_names=args.methods,
                limit=args.limit,
                require_tests=args.require_tests,
                strict=args.strict,
                include_unindexed_diffs=(True if args.include_unindexed_diffs else None),
            )
            payload = {
                "summary": load_result.summary(),
                "target_tier": args.tier,
                "selected_intent_packs": [
                    {
                        "patch_id": pack.patch_id,
                        "bug_id": pack.bug_id,
                        "project": pack.project,
                        "tier": pack.tier,
                        "methods": [m.method_name for m in pack.target_methods],
                        "reasons": pack.tier_decision.reasons if pack.tier_decision else [],
                    }
                    for pack in packs
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
    except (ConfigError, DataLoadError, PatchBlindViolationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
