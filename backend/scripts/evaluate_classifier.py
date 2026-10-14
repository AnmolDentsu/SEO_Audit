#!/usr/bin/env python3
"""Evaluate heuristic/AI classifier confidence against analyst-labelled CSV data.

Expected CSV columns (case-insensitive aliases supported):
    actual,predicted,confidence

Examples:
    PRODUCT,PRODUCT,0.97
    CONTENT,OTHER,0.64

This script does not retrain or modify the production rules. It measures what the
current confidence means on a real labelled set so thresholds can be calibrated
with evidence rather than intuition.
"""
from __future__ import annotations

import argparse
import csv
import math
from collections import Counter, defaultdict
from pathlib import Path


ALIASES = {
    "actual": {"actual", "actual_label", "gold", "gold_label", "expected", "label"},
    "predicted": {"predicted", "predicted_label", "prediction", "model_label"},
    "confidence": {"confidence", "score", "probability", "confidence_score"},
}


def _norm_header(value: str) -> str:
    return "_".join(str(value or "").strip().lower().replace("-", " ").split())


def _resolve_headers(fieldnames: list[str]) -> dict[str, str]:
    normalized = {_norm_header(x): x for x in fieldnames}
    result: dict[str, str] = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            if _norm_header(alias) in normalized:
                result[canonical] = normalized[_norm_header(alias)]
                break
    missing = [x for x in ALIASES if x not in result]
    if missing:
        raise ValueError(
            "CSV is missing required column(s): " + ", ".join(missing)
            + ". Expected actual,predicted,confidence (aliases are accepted)."
        )
    return result


def _safe_div(n: float, d: float) -> float:
    return n / d if d else 0.0


def _fmt(value: float) -> str:
    return f"{value * 100:6.2f}%"


def _calibration_bin(confidence: float) -> str:
    edges = [0.0, 0.6, 0.7, 0.8, 0.9, 1.0000001]
    labels = ["<0.60", "0.60-0.69", "0.70-0.79", "0.80-0.89", "0.90-1.00"]
    for i in range(len(labels)):
        if edges[i] <= confidence < edges[i + 1]:
            return labels[i]
    return "out-of-range"


def load_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = _resolve_headers(reader.fieldnames or [])
        rows = []
        for line_no, raw in enumerate(reader, start=2):
            actual = str(raw.get(headers["actual"]) or "").strip().upper()
            predicted = str(raw.get(headers["predicted"]) or "").strip().upper()
            conf_text = str(raw.get(headers["confidence"]) or "").strip()
            if not actual or not predicted or not conf_text:
                continue
            try:
                confidence = float(conf_text)
            except ValueError as exc:
                raise ValueError(f"Row {line_no}: invalid confidence {conf_text!r}") from exc
            if confidence > 1 and confidence <= 100:
                confidence /= 100.0
            if not 0 <= confidence <= 1:
                raise ValueError(f"Row {line_no}: confidence must be 0..1 or 0..100")
            rows.append({"actual": actual, "predicted": predicted, "confidence": confidence})
    return rows


def evaluate(rows: list[dict], min_confidence: float = 0.0) -> None:
    rows = [r for r in rows if r["confidence"] >= min_confidence]
    if not rows:
        raise ValueError("No rows remain after confidence filtering.")

    labels = sorted({r["actual"] for r in rows} | {r["predicted"] for r in rows})
    actual_counts = Counter(r["actual"] for r in rows)
    predicted_counts = Counter(r["predicted"] for r in rows)
    correct_counts = Counter(r["actual"] for r in rows if r["actual"] == r["predicted"])

    metrics = []
    for label in labels:
        tp = correct_counts[label]
        fp = predicted_counts[label] - tp
        fn = actual_counts[label] - tp
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        f1 = _safe_div(2 * precision * recall, precision + recall)
        metrics.append((label, precision, recall, f1, actual_counts[label]))

    total = len(rows)
    correct = sum(1 for r in rows if r["actual"] == r["predicted"])
    accuracy = correct / total
    macro_f1 = sum(x[3] for x in metrics) / len(metrics)
    weighted_f1 = sum(x[3] * x[4] for x in metrics) / total

    print(f"Rows evaluated: {total}")
    print(f"Minimum confidence: {min_confidence:.2f}")
    print(f"Accuracy: {_fmt(accuracy)}")
    print(f"Macro F1: {_fmt(macro_f1)}")
    print(f"Weighted F1: {_fmt(weighted_f1)}")
    print()
    print(f"{'LABEL':28} {'PRECISION':>10} {'RECALL':>10} {'F1':>10} {'SUPPORT':>9}")
    print("-" * 74)
    for label, precision, recall, f1, support in metrics:
        print(f"{label[:28]:28} {_fmt(precision):>10} {_fmt(recall):>10} {_fmt(f1):>10} {support:9d}")

    bins: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        bins[_calibration_bin(row["confidence"])].append(row)

    print("\nConfidence calibration (heuristic confidence vs observed correctness)")
    print(f"{'BIN':12} {'N':>7} {'AVG CONF':>12} {'ACCURACY':>12} {'GAP':>12}")
    print("-" * 59)
    for label in ["<0.60", "0.60-0.69", "0.70-0.79", "0.80-0.89", "0.90-1.00"]:
        group = bins.get(label, [])
        if not group:
            continue
        avg_conf = sum(x["confidence"] for x in group) / len(group)
        bin_acc = sum(x["actual"] == x["predicted"] for x in group) / len(group)
        print(f"{label:12} {len(group):7d} {_fmt(avg_conf):>12} {_fmt(bin_acc):>12} {_fmt(avg_conf-bin_acc):>12}")

    high = [r for r in rows if r["confidence"] >= 0.90]
    if high:
        high_precision = sum(r["actual"] == r["predicted"] for r in high) / len(high)
        print(f"\n>=0.90-confidence observed accuracy: {_fmt(high_precision)} ({len(high)} rows)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate classifier precision/recall/F1 and confidence calibration.")
    parser.add_argument("csv_file", type=Path, help="Analyst-labelled CSV with actual,predicted,confidence")
    parser.add_argument("--min-confidence", type=float, default=0.0, help="Evaluate only rows at/above this confidence (0..1)")
    args = parser.parse_args()
    if not 0 <= args.min_confidence <= 1:
        parser.error("--min-confidence must be between 0 and 1")
    rows = load_rows(args.csv_file)
    evaluate(rows, args.min_confidence)


if __name__ == "__main__":
    main()
