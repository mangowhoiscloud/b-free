"""FR-2 dependency inventory (T16): static detection + rule-table classification, no LLM (NFR-5)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .detector import SCHEMA_VERSION, Finding, InventoryReport, mask, rules_table, scan
from .rules import RULES, RULES_VERSION, Tier

ROOT_ENV = "BRAVO_INVENTORY_ROOT"

__all__ = [
    "RULES",
    "RULES_VERSION",
    "ROOT_ENV",
    "SCHEMA_VERSION",
    "Finding",
    "InventoryReport",
    "Tier",
    "main_inventory",
    "mask",
    "rules_table",
    "scan",
]


def main_inventory(argv: list[str] | None = None) -> int:
    """``bravo-collector inventory [--path P] [--label L] [--rules]``. JSON on stdout, errors on stderr."""
    parser = argparse.ArgumentParser(prog="bravo-collector inventory", description="FR-2 static dependency inventory")
    parser.add_argument("--path", help=f"app repository to scan (default: ${ROOT_ENV})")
    parser.add_argument("--label", help="name of the root in the output (default: directory name)")
    parser.add_argument("--rules", action="store_true", help="print the rule table instead of scanning")
    args = parser.parse_args(argv)
    if args.rules:
        json.dump({"rules_version": RULES_VERSION, "rules": rules_table()}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    path = args.path or os.environ.get(ROOT_ENV, "").strip()
    if not path:
        print(f"inventory: no path; pass --path or set {ROOT_ENV}", file=sys.stderr)
        return 2
    try:
        report = scan(Path(path), label=args.label)
    except NotADirectoryError as exc:
        print(f"inventory: {exc}", file=sys.stderr)
        return 2
    json.dump(report.model_dump(mode="json"), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0
