#!/usr/bin/env python3
"""Replay static Turn Gate decisions through candidate/fallback timing profiles.

This is an offline state-machine experiment.  It combines existing aligned
Smart Turn / LiveKit EOU probabilities with each case's observed silence before
speech resumes.  It does not run VAD, models, WebRTC, or production services.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class TimingProfile:
    candidate_silence_ms: int = 300
    min_commit_silence_ms: int = 600
    active_deadline_ms: int = 300
    failure_fallback_ms: int = 1000
    hard_timeout_ms: int = 1600
    asr_ms: int = 150
    smart_ms: int = 50
    eou_ms: int = 20
    control_ms: int = 30

    def __post_init__(self) -> None:
        positive = {
            name: value
            for name, value in asdict(self).items()
            if value <= 0
        }
        if positive:
            raise ValueError(f"timings must be positive: {positive}")
        if self.failure_fallback_ms <= self.candidate_silence_ms:
            raise ValueError("failure fallback must exceed candidate silence")
        if self.min_commit_silence_ms < self.candidate_silence_ms:
            raise ValueError("minimum commit silence must not precede candidate silence")
        if self.min_commit_silence_ms > self.failure_fallback_ms:
            raise ValueError("minimum commit silence must not exceed failure fallback")
        if self.hard_timeout_ms < self.failure_fallback_ms:
            raise ValueError("hard timeout must not precede failure fallback")

    @property
    def semantic_path_ms(self) -> int:
        return max(self.asr_ms, self.smart_ms) + self.eou_ms + self.control_ms

    @property
    def early_commit_ms(self) -> int:
        return max(
            self.min_commit_silence_ms,
            self.candidate_silence_ms + self.semantic_path_ms,
        )

    @property
    def active_deadline_met(self) -> bool:
        return self.semantic_path_ms <= self.active_deadline_ms


@dataclass(frozen=True)
class CaseDecision:
    case_id: str
    ground_truth: str
    action: str
    commit_ms: int | None
    resume_ms: int | None
    outcome: str


def simulate_case(
    *,
    case_id: str,
    ground_truth: str,
    resume_ms: int,
    both_end: bool,
    profile: TimingProfile,
    models_available: bool = True,
) -> CaseDecision:
    if ground_truth not in {"END", "CONTINUE"}:
        raise ValueError(f"invalid ground truth for {case_id}: {ground_truth}")

    if not models_available or not profile.active_deadline_met:
        action = "failure_fallback"
        commit_ms = profile.failure_fallback_ms
    elif both_end:
        action = "early_commit"
        commit_ms = profile.early_commit_ms
    else:
        action = "hard_timeout"
        commit_ms = profile.hard_timeout_ms

    if ground_truth == "END":
        return CaseDecision(case_id, ground_truth, action, commit_ms, None, "correct_end")
    if commit_ms < resume_ms:
        outcome = "false_end"
    elif commit_ms == resume_ms:
        outcome = "boundary_risk"
    else:
        outcome = "safe_continue"
    return CaseDecision(case_id, ground_truth, action, commit_ms, resume_ms, outcome)


def percentile(values: list[int], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(decisions: Iterable[CaseDecision]) -> dict[str, Any]:
    rows = list(decisions)
    continue_rows = [row for row in rows if row.ground_truth == "CONTINUE"]
    end_rows = [row for row in rows if row.ground_truth == "END"]
    false_end = sum(row.outcome == "false_end" for row in continue_rows)
    boundary_risk = sum(row.outcome == "boundary_risk" for row in continue_rows)
    end_latencies = [row.commit_ms for row in end_rows if row.commit_ms is not None]
    return {
        "cases": len(rows),
        "continue_cases": len(continue_rows),
        "end_cases": len(end_rows),
        "false_end": false_end,
        "boundary_risk": boundary_risk,
        "conservative_false_end": false_end + boundary_risk,
        "false_end_rate": round(false_end / len(continue_rows), 6) if continue_rows else 0.0,
        "conservative_false_end_rate": (
            round((false_end + boundary_risk) / len(continue_rows), 6)
            if continue_rows
            else 0.0
        ),
        "early_commit_end": sum(
            row.action == "early_commit" for row in end_rows
        ),
        "failure_fallback_end": sum(
            row.action == "failure_fallback" for row in end_rows
        ),
        "hard_timeout_end": sum(row.action == "hard_timeout" for row in end_rows),
        "end_latency_ms": {
            "mean": round(sum(end_latencies) / len(end_latencies), 3)
            if end_latencies
            else 0.0,
            "p50": round(percentile(end_latencies, 0.50), 3),
            "p95": round(percentile(end_latencies, 0.95), 3),
            "max": max(end_latencies, default=0),
        },
    }


def simulate_fixed_timeout_baseline(
    *, cases: dict[str, dict[str, Any]], timeout_ms: int
) -> dict[str, Any]:
    decisions = []
    for case_id, case in cases.items():
        truth = str(case["ground_truth"])
        resume_ms = int(case["trailing_silence_ms"])
        if truth == "END":
            outcome = "correct_end"
            reported_resume_ms = None
        elif timeout_ms < resume_ms:
            outcome = "false_end"
            reported_resume_ms = resume_ms
        elif timeout_ms == resume_ms:
            outcome = "boundary_risk"
            reported_resume_ms = resume_ms
        else:
            outcome = "safe_continue"
            reported_resume_ms = resume_ms
        decisions.append(
            CaseDecision(
                case_id=case_id,
                ground_truth=truth,
                action="fixed_timeout",
                commit_ms=timeout_ms,
                resume_ms=reported_resume_ms,
                outcome=outcome,
            )
        )
    return {
        "timeout_ms": timeout_ms,
        "summary": summarize(decisions),
        "false_end_ids": [row.case_id for row in decisions if row.outcome == "false_end"],
        "boundary_risk_ids": [
            row.case_id for row in decisions if row.outcome == "boundary_risk"
        ],
    }


def load_csv(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"duplicate id in {path}")
    return result


def load_cases(path: Path) -> dict[str, dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = {str(row["id"]): row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"duplicate id in {path}")
    return result


def run_profile(
    *,
    cases: dict[str, dict[str, Any]],
    smart: dict[str, dict[str, str]],
    eou: dict[str, dict[str, str]],
    profile: TimingProfile,
    smart_threshold: float,
    eou_threshold: float,
    models_available: bool,
) -> dict[str, Any]:
    if set(smart) != set(eou) or set(smart) != set(cases):
        raise ValueError("case, Smart Turn, and EOU ids must be aligned")
    decisions = []
    for case_id, smart_row in smart.items():
        if smart_row["ground_truth"] != eou[case_id]["ground_truth"]:
            raise ValueError(f"ground truth mismatch for {case_id}")
        both_end = (
            float(smart_row["probability"]) > smart_threshold
            and float(eou[case_id]["probability"]) >= eou_threshold
        )
        decisions.append(
            simulate_case(
                case_id=case_id,
                ground_truth=smart_row["ground_truth"],
                resume_ms=int(cases[case_id]["trailing_silence_ms"]),
                both_end=both_end,
                profile=profile,
                models_available=models_available,
            )
        )
    return {
        "smart_threshold": smart_threshold,
        "eou_threshold": eou_threshold,
        "models_available": models_available,
        "summary": summarize(decisions),
        "false_end_ids": [row.case_id for row in decisions if row.outcome == "false_end"],
        "boundary_risk_ids": [
            row.case_id for row in decisions if row.outcome == "boundary_risk"
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--smart", type=Path, required=True)
    parser.add_argument("--eou", type=Path, required=True)
    parser.add_argument("--smart-threshold", type=float, default=0.5)
    parser.add_argument(
        "--eou-thresholds",
        default="0.0066,0.03,0.1,0.2,0.3",
        help="Comma-separated thresholds; non-official values are same-set exploration only.",
    )
    parser.add_argument("--candidate-ms", type=int, default=300)
    parser.add_argument("--min-commit-silence-ms", type=int, default=600)
    parser.add_argument("--active-deadline-ms", type=int, default=300)
    parser.add_argument("--failure-fallback-ms", type=int, default=1000)
    parser.add_argument("--hard-timeout-ms", type=int, default=1600)
    parser.add_argument("--asr-ms", type=int, default=150)
    parser.add_argument("--smart-ms", type=int, default=50)
    parser.add_argument("--eou-ms", type=int, default=20)
    parser.add_argument("--control-ms", type=int, default=30)
    parser.add_argument("--models-unavailable", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile = TimingProfile(
        candidate_silence_ms=args.candidate_ms,
        min_commit_silence_ms=args.min_commit_silence_ms,
        active_deadline_ms=args.active_deadline_ms,
        failure_fallback_ms=args.failure_fallback_ms,
        hard_timeout_ms=args.hard_timeout_ms,
        asr_ms=args.asr_ms,
        smart_ms=args.smart_ms,
        eou_ms=args.eou_ms,
        control_ms=args.control_ms,
    )
    cases = load_cases(args.cases)
    smart = load_csv(args.smart)
    eou = load_csv(args.eou)
    thresholds = [float(item.strip()) for item in args.eou_thresholds.split(",") if item.strip()]
    output = {
        "schema_version": "turn-gate-timing-simulation/v1",
        "evidence_boundary": (
            "offline replay of existing synthetic model outputs; no live VAD, WebRTC, "
            "Gateway, model inference, or hardware"
        ),
        "timing_profile": {
            **asdict(profile),
            "semantic_path_ms": profile.semantic_path_ms,
            "early_commit_ms": profile.early_commit_ms,
            "active_deadline_met": profile.active_deadline_met,
        },
        "baselines": {
            "vad_only_500": simulate_fixed_timeout_baseline(cases=cases, timeout_ms=500),
            "models_unavailable_fallback": simulate_fixed_timeout_baseline(
                cases=cases, timeout_ms=profile.failure_fallback_ms
            ),
        },
        "profiles": [
            run_profile(
                cases=cases,
                smart=smart,
                eou=eou,
                profile=profile,
                smart_threshold=args.smart_threshold,
                eou_threshold=threshold,
                models_available=not args.models_unavailable,
            )
            for threshold in thresholds
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
