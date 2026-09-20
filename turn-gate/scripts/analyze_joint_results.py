#!/usr/bin/env python3
"""Compare aligned Smart Turn and LiveKit EOU decisions without production integration."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def load(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"duplicate id in {path}")
    return result


def summarize(truth: list[bool], predictions: list[bool]) -> dict[str, float | int]:
    tp = sum(t and p for t, p in zip(truth, predictions))
    fp = sum(not t and p for t, p in zip(truth, predictions))
    tn = sum(not t and not p for t, p in zip(truth, predictions))
    fn = sum(t and not p for t, p in zip(truth, predictions))
    return {
        "accuracy": (tp + tn) / len(truth),
        "false_end": fp,
        "false_end_rate": fp / (fp + tn) if fp + tn else 0.0,
        "missed_end": fn,
        "missed_end_rate": fn / (tp + fn) if tp + fn else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smart", type=Path, required=True)
    parser.add_argument("--eou", type=Path, required=True)
    parser.add_argument("--llm", type=Path)
    parser.add_argument("--smart-threshold", type=float, default=0.5)
    parser.add_argument("--eou-threshold", type=float, default=0.0066)
    args = parser.parse_args()

    smart = load(args.smart)
    eou = load(args.eou)
    if set(smart) != set(eou):
        missing_smart = sorted(set(eou) - set(smart))
        missing_eou = sorted(set(smart) - set(eou))
        raise ValueError(f"unaligned ids: missing_smart={missing_smart} missing_eou={missing_eou}")

    ids = list(smart)
    truth = [smart[item]["ground_truth"] == "END" for item in ids]
    if any(smart[item]["ground_truth"] != eou[item]["ground_truth"] for item in ids):
        raise ValueError("ground_truth mismatch")
    smart_end = [float(smart[item]["probability"]) > args.smart_threshold for item in ids]
    eou_end = [float(eou[item]["probability"]) >= args.eou_threshold for item in ids]
    policies = {
        "smart_only": smart_end,
        "eou_only": eou_end,
        "both_end": [left and right for left, right in zip(smart_end, eou_end)],
        "either_end": [left or right for left, right in zip(smart_end, eou_end)],
    }
    if args.llm:
        llm = load(args.llm)
        if set(llm) != set(smart):
            raise ValueError("LLM result ids are not aligned")
        llm_end = []
        for item in ids:
            prediction = llm[item]["prediction"]
            if prediction not in {"END", "CONTINUE"}:
                raise ValueError(f"invalid LLM prediction for {item}: {prediction}")
            llm_end.append(prediction == "END")
        policies.update(
            {
                "llm_only": llm_end,
                "eou_llm_both": [left and right for left, right in zip(eou_end, llm_end)],
                "all_three_end": [
                    first and second and third
                    for first, second, third in zip(smart_end, eou_end, llm_end)
                ],
                "majority_2_of_3": [
                    sum((first, second, third)) >= 2
                    for first, second, third in zip(smart_end, eou_end, llm_end)
                ],
            }
        )
    print("policy,accuracy,false_end,false_end_rate,missed_end,missed_end_rate")
    for name, prediction in policies.items():
        result = summarize(truth, prediction)
        print(
            f"{name},{result['accuracy']:.4f},{result['false_end']},"
            f"{result['false_end_rate']:.4f},{result['missed_end']},"
            f"{result['missed_end_rate']:.4f}"
        )

    smart_correct = [actual == predicted for actual, predicted in zip(truth, smart_end)]
    eou_correct = [actual == predicted for actual, predicted in zip(truth, eou_end)]
    print("overlap,count")
    print(f"both_correct,{sum(a and b for a, b in zip(smart_correct, eou_correct))}")
    print(f"smart_only_correct,{sum(a and not b for a, b in zip(smart_correct, eou_correct))}")
    print(f"eou_only_correct,{sum(not a and b for a, b in zip(smart_correct, eou_correct))}")
    print(f"both_wrong,{sum(not a and not b for a, b in zip(smart_correct, eou_correct))}")
    print(
        "both_false_end,"
        f"{sum((not t) and s and e for t, s, e in zip(truth, smart_end, eou_end))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
