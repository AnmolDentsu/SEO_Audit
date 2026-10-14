#!/usr/bin/env python3
"""Apply the P0/P1 dynamic anchor-range UI/report wording patch safely.

This script intentionally edits only range-specific text/logic in the *current*
project files, so newer crawler, sitemap, Brand Scope removal, Section/FAQ,
and pagination changes are preserved.

Core benchmark computation itself lives in app/services/benchmarks.py and should
be replaced with the companion complete benchmarks.py file before running this
script.

Files edited:
- frontend/src/App.jsx
- backend/app/services/opportunities.py
- backend/app/services/export.py

Behavior after patch:
- P0 and P1 remain stable benchmark URL roles.
- For each keyword, lower(P0 density, P1 density) = Target Min.
- For each keyword, higher(P0 density, P1 density) = Target Max.
- No INVALID RANGE UI/state is presented merely because P1 > P0.
- Opportunity/export wording no longer says P1 is always Min or P0 is always Max.

The script is idempotent and makes timestamped backups before writing.
"""
from __future__ import annotations

import argparse
import re
import shutil
from datetime import datetime
from pathlib import Path


DYNAMIC_HELP = (
    "CLIENT is evaluated; P0 and P1 are benchmark anchors. "
    "For each keyword, the lower anchor density becomes Target Min and the higher "
    "anchor density becomes Target Max; OTHER expands the benchmark pool."
)


def _replace(text: str, old: str, new: str) -> tuple[str, int]:
    count = text.count(old)
    if count:
        text = text.replace(old, new)
    return text, count


def _sub(text: str, pattern: str, repl: str, *, flags: int = 0) -> tuple[str, int]:
    return re.subn(pattern, repl, text, flags=flags)


def patch_app(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    help_variants = [
        "CLIENT is evaluated; P1 = Target Min; P0 = Target Max; OTHER expands the benchmark pool.",
        "CLIENT is evaluated; P1 = Target Min; P0 = Target Max.",
    ]
    for old in help_variants:
        text, n = _replace(text, old, DYNAMIC_HELP)
        if n:
            notes.append(f"updated Page Analysis benchmark help text ({n})")

    # Remove runtime INVALID RANGE detection. Dynamic ordering in benchmarks.py
    # means this state no longer exists for two available anchors.
    text, n = _sub(
        text,
        r"(?m)^\s*const invalidRange = phase1\.some\([^\n]*\)\s*\n",
        "",
    )
    if n:
        notes.append(f"removed invalidRange state calculation ({n})")

    # Support the formatting used by the current React component.
    invalid_badge_patterns = [
        r"\{invalidRange\s*\?\s*<Badge tone=\"failed\">INVALID RANGE</Badge>\s*:\s*anyReady\s*\?\s*<Badge tone=\"completed\">RANGE READY</Badge>\s*:\s*<Badge>RANGE UNAVAILABLE</Badge>\}",
        r"\{invalidRange\s*\?\s*<Badge tone='failed'>INVALID RANGE</Badge>\s*:\s*anyReady\s*\?\s*<Badge tone='completed'>RANGE READY</Badge>\s*:\s*<Badge>RANGE UNAVAILABLE</Badge>\}",
    ]
    for pattern in invalid_badge_patterns:
        text, n = _sub(
            text,
            pattern,
            "{anyReady ? <Badge tone=\"completed\">RANGE READY</Badge> : <Badge>RANGE UNAVAILABLE</Badge>}",
            flags=re.S,
        )
        if n:
            notes.append(f"removed INVALID RANGE readiness badge ({n})")
            break

    # Current Comparison Matrix used raw P1 then P0 values in the columns named
    # min/max. Display the already-computed dynamic target_min/target_max instead.
    replacements = [
        (
            "fmt(x.p1?.density_pct), fmt(x.p0?.density_pct),",
            "benchmark.target_min == null ? '—' : fmt(benchmark.target_min), benchmark.target_max == null ? '—' : fmt(benchmark.target_max),",
        ),
        (
            'fmt(x.p1?.density_pct), fmt(x.p0?.density_pct),',
            "benchmark.target_min == null ? '—' : fmt(benchmark.target_min), benchmark.target_max == null ? '—' : fmt(benchmark.target_max),",
        ),
    ]
    for old, new in replacements:
        text, n = _replace(text, old, new)
        if n:
            notes.append(f"made Comparison Matrix min/max values dynamic ({n})")
            break

    simple_replacements = {
        "'Target Min P1 %'": "'Target Min %'",
        "'Target Max P0 %'": "'Target Max %'",
        '"Target Min P1 %"': '"Target Min %"',
        '"Target Max P0 %"': '"Target Max %"',
        "'P1 Min'": "'Target Min'",
        "'P0 Max'": "'Target Max'",
        '"P1 Min"': '"Target Min"',
        '"P0 Max"': '"Target Max"',
    }
    for old, new in simple_replacements.items():
        text, n = _replace(text, old, new)
        if n:
            notes.append(f"renamed {old} -> {new} ({n})")

    forbidden = [
        "INVALID RANGE",
        "Target Min P1 %",
        "Target Max P0 %",
        "P1 = Target Min",
        "P0 = Target Max",
    ]
    active = [x for x in forbidden if x in text]
    if active:
        raise RuntimeError(
            "App.jsx still contains fixed/invalid range UI tokens after patch: "
            + ", ".join(active)
        )

    # Strictly ensure the comparison matrix is displaying the dynamic backend range.
    if "benchmark.target_min" not in text or "benchmark.target_max" not in text:
        raise RuntimeError(
            "Could not confirm dynamic target_min/target_max rendering in App.jsx. "
            "No file was written."
        )

    return text, notes


def patch_opportunities(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    text, n = _sub(
        text,
        r"(?m)^\s*range_valid = benchmark\.get\(\"valid_range\"\)\s*\n",
        "",
    )
    if n:
        notes.append(f"removed obsolete range_valid local ({n})")

    # Delete the obsolete invalid-range opportunity block and turn the following
    # `elif target_min...` into the normal range check.
    pattern = (
        r"(?ms)^        if target_min is not None and target_max is not None and range_valid is False:\n"
        r".*?"
        r"^        elif target_min is not None and target_max is not None:\n"
    )
    text, n = _sub(
        text,
        pattern,
        "        if target_min is not None and target_max is not None:\n",
    )
    if n:
        notes.append(f"removed invalid_benchmark_range opportunity branch ({n})")

    replacements = {
        '"target_min_from_p1"': '"target_min"',
        '"target_max_from_p0"': '"target_max"',
        '"below_p1_target"': '"below_anchor_range"',
        '"above_p0_target"': '"above_anchor_range"',
        "Client density below P1 benchmark:": "Client density below benchmark range:",
        "Client density above P0 benchmark:": "Client density above benchmark range:",
        "versus P1-derived Target Min ": "versus dynamically assigned Target Min ",
        "versus P0-derived Target Max ": "versus dynamically assigned Target Max ",
    }
    for old, new in replacements.items():
        text, c = _replace(text, old, new)
        if c:
            notes.append(f"updated opportunity wording/key {old!r} ({c})")

    forbidden = [
        "invalid_benchmark_range",
        "P1/P0 benchmark range is inverted",
        "below_p1_target",
        "above_p0_target",
        "P1-derived Target Min",
        "P0-derived Target Max",
        "range_valid = benchmark.get",
    ]
    active = [x for x in forbidden if x in text]
    if active:
        raise RuntimeError(
            "opportunities.py still contains retired fixed-range logic: "
            + ", ".join(active)
        )
    return text, notes


def patch_export(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    replacements = {
        '"Target Min Source (P1)"': '"Target Min Source"',
        '"Target Max Source (P0)"': '"Target Max Source"',
        '"CLIENT": "Client - compare against P1/P0 target range"': '"CLIENT": "Client - compare against the ordered P0/P1 anchor range"',
        '"P0": "P0 - Target Max source"': '"P0": "P0 - benchmark anchor; lower/higher role is assigned per keyword"',
        '"P1": "P1 - Target Min source"': '"P1": "P1 - benchmark anchor; lower/higher role is assigned per keyword"',
        '"P1 density -> Target Min; P0 density -> Target Max; CLIENT is evaluated; OTHER contributes distribution context"': '"P0/P1 are benchmark anchors; lower density -> Target Min and higher density -> Target Max per keyword; CLIENT is evaluated; OTHER contributes distribution context"',
    }
    for old, new in replacements.items():
        text, c = _replace(text, old, new)
        if c:
            notes.append(f"updated export wording {old!r} ({c})")

    # Multi-line comparison-matrix range validation block.
    text, n = _sub(
        text,
        r'''(?ms)            "Target Range Validation": \(\n\s*"Target Unavailable" if b\.get\("target_min"\) is None or b\.get\("target_max"\) is None\n\s*else "Valid" if b\.get\("valid_range"\) else "Invalid Target Range \(P1 > P0\)"\n\s*\),''',
        '''            "Target Range Assignment": (\n                "Unavailable" if b.get("target_min") is None or b.get("target_max") is None\n                else "Lower P0/P1 density = Min; higher P0/P1 density = Max"\n            ),''',
    )
    if n:
        notes.append(f"replaced comparison export range validation ({n})")

    # Compact one-line detailed-results form used in some current versions.
    old_line = '"Target Range Validation": "Target Unavailable" if b.get("target_min") is None or b.get("target_max") is None else "Valid" if b.get("valid_range") else "Invalid Target Range (P1 > P0)",'
    new_line = '"Target Range Assignment": "Unavailable" if b.get("target_min") is None or b.get("target_max") is None else "Lower P0/P1 density = Min; higher P0/P1 density = Max",'
    text, n = _replace(text, old_line, new_line)
    if n:
        notes.append(f"replaced detailed export range validation ({n})")

    # If a later version formats the validation slightly differently, at least
    # retire the misleading error wording. Dynamic benchmarks always return a
    # valid ordered range when both anchors exist.
    text, n = _replace(
        text,
        '"Invalid Target Range (P1 > P0)"',
        '"Lower P0/P1 density = Min; higher P0/P1 density = Max"',
    )
    if n:
        notes.append(f"removed remaining invalid-range export wording ({n})")

    forbidden = [
        "Target Min Source (P1)",
        "Target Max Source (P0)",
        "P0 - Target Max source",
        "P1 - Target Min source",
        "Invalid Target Range (P1 > P0)",
        "P1 density -> Target Min; P0 density -> Target Max",
    ]
    active = [x for x in forbidden if x in text]
    if active:
        raise RuntimeError(
            "export.py still contains retired fixed-range wording after patch: "
            + ", ".join(active)
        )
    return text, notes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch Page Analysis UI/opportunity/export wording for dynamic P0/P1 ranges."
    )
    parser.add_argument(
        "--project-root",
        default="..",
        help="Project root containing backend/ and frontend/. Default '..' when run from backend/.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show which files would change without writing.",
    )
    args = parser.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    targets = [
        (root / "frontend/src/App.jsx", patch_app),
        (root / "backend/app/services/opportunities.py", patch_opportunities),
        (root / "backend/app/services/export.py", patch_export),
    ]

    missing = [str(path) for path, _ in targets if not path.exists()]
    if missing:
        raise SystemExit("Missing required file(s):\n - " + "\n - ".join(missing))

    planned: list[tuple[Path, str, str, list[str]]] = []
    try:
        for path, patcher in targets:
            original = path.read_text(encoding="utf-8")
            updated, notes = patcher(original)
            planned.append((path, original, updated, notes))
    except Exception as exc:
        print("Dynamic anchor-range patch validation FAILED.")
        print("No files were written.")
        print(f"Reason: {exc}")
        return 2

    print("Dynamic anchor-range patch validation passed.")
    for path, original, updated, notes in planned:
        state = "CHANGE" if updated != original else "unchanged/already patched"
        print(f"\n[{state}] {path}")
        for note in notes:
            print(f"  - {note}")

    if args.dry_run:
        print("\nDry run only: no files written.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    changed = [(p, o, u, n) for p, o, u, n in planned if u != o]
    for path, original, updated, _ in changed:
        backup = path.with_name(path.name + f".before-dynamic-range-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(updated, encoding="utf-8")
        print(f"WROTE  : {path}")
        print(f"BACKUP : {backup}")

    if not changed:
        print("\nNo writes were necessary; files are already patched.")
    else:
        print(f"\nCommitted {len(changed)} file change(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
