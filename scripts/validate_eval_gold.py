#!/usr/bin/env python3
"""Validate independent acceptance Gold provenance and schema without calling a model."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GOLD_DIR = ROOT / "data" / "eval_gold"
ALLOWED_ROUTER_EXPECTED = {
    "1",
    "2:robot",
    "2:task",
    "2:utils",
    "2:websearch",
    "2:vision",
    "2:complex",
    "exit",
}
FORBIDDEN_PROVENANCE_MARKERS = (
    "auto_correct",
    "auto-correct",
    "corrector",
    "tested_model",
    "model_prediction",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_jsonl(path: Path, problems: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        problems.append(f"missing file: {path.relative_to(ROOT)}")
        return rows
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"{path.relative_to(ROOT)}:{line_no}: invalid JSON: {exc}")
            continue
        if not isinstance(row, dict):
            problems.append(f"{path.relative_to(ROOT)}:{line_no}: row must be an object")
            continue
        row["_line"] = line_no
        rows.append(row)
    return rows


def validate_gold(gold_dir: Path = GOLD_DIR) -> dict[str, Any]:
    problems: list[str] = []
    manifest_path = gold_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "acceptance_ready": False, "problems": [f"invalid manifest: {exc}"]}

    required_manifest = {
        "schema_version": "eval-gold-manifest/v1",
        "authoring_method": "independent_rule_authored",
        "tested_model_output_used": False,
        "candidate_policy": "full_candidate_set",
        "argument_policy": "exact_complete_match",
    }
    for key, expected in required_manifest.items():
        if manifest.get(key) != expected:
            problems.append(f"manifest.{key} must be {expected!r}")
    for key in ("gold_revision", "created_at", "author", "review_status"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            problems.append(f"manifest.{key} must be a non-empty string")

    router_rows = _load_jsonl(gold_dir / "4b_router.jsonl", problems)
    tool_rows = _load_jsonl(gold_dir / "9b_tool.jsonl", problems)
    vision_rows = _load_jsonl(gold_dir / "vision" / "cases.jsonl", problems)
    ids: set[str] = set()

    def common_check(row: dict[str, Any], file_name: str, *, text_key: str = "text") -> None:
        line = row.get("_line", "?")
        for key in ("id", text_key, "rationale"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                problems.append(f"{file_name}:{line}: {key} must be non-empty")
        case_id = row.get("id")
        if isinstance(case_id, str):
            if case_id in ids:
                problems.append(f"{file_name}:{line}: duplicate id {case_id}")
            ids.add(case_id)
        provenance_text = json.dumps(row, ensure_ascii=False).lower()
        if any(marker in provenance_text for marker in FORBIDDEN_PROVENANCE_MARKERS):
            problems.append(f"{file_name}:{line}: forbidden model-derived provenance marker")

    router_categories: set[str] = set()
    for row in router_rows:
        common_check(row, "4b_router.jsonl")
        expected = row.get("expected")
        if expected not in ALLOWED_ROUTER_EXPECTED:
            problems.append(f"4b_router.jsonl:{row.get('_line')}: invalid expected={expected!r}")
        elif isinstance(expected, str):
            router_categories.add(expected)
        history = row.get("history")
        if history is not None:
            if not isinstance(history, list) or not history or len(history) > 10:
                problems.append(
                    f"4b_router.jsonl:{row.get('_line')}: history must contain 1 to 10 messages"
                )
            elif any(
                not isinstance(message, dict)
                or message.get("role") not in {"user", "assistant"}
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
                for message in history
            ):
                problems.append(
                    f"4b_router.jsonl:{row.get('_line')}: history messages require user/assistant role and non-empty content"
                )

    tool_categories: set[str] = set()
    for row in tool_rows:
        common_check(row, "9b_tool.jsonl")
        expected_tool = row.get("expected_tool")
        if not isinstance(expected_tool, str) or not expected_tool.strip():
            problems.append(f"9b_tool.jsonl:{row.get('_line')}: expected_tool must be non-empty")
        elif expected_tool in {"any", "*"}:
            problems.append(f"9b_tool.jsonl:{row.get('_line')}: ambiguous expected_tool is forbidden")
        else:
            tool_categories.add(expected_tool)
        if not isinstance(row.get("expected_args"), dict):
            problems.append(f"9b_tool.jsonl:{row.get('_line')}: expected_args must be an object")

    missing_router = ALLOWED_ROUTER_EXPECTED - router_categories
    if missing_router:
        problems.append(f"router Gold missing categories: {sorted(missing_router)}")
    if len(tool_categories) < 10 or "no_tool" not in tool_categories:
        problems.append("tool Gold must cover at least 10 tool categories including no_tool")

    allowed_image_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    for row in vision_rows:
        common_check(row, "vision/cases.jsonl", text_key="prompt")
        line = row.get("_line", "?")
        image_path = row.get("image_path")
        if not isinstance(image_path, str) or not image_path.strip():
            problems.append(f"vision/cases.jsonl:{line}: image_path must be non-empty")
            continue
        resolved_image = (gold_dir / image_path).resolve()
        if not resolved_image.is_relative_to(gold_dir.resolve()):
            problems.append(f"vision/cases.jsonl:{line}: image_path escapes Gold directory")
        elif resolved_image.suffix.lower() not in allowed_image_suffixes:
            problems.append(f"vision/cases.jsonl:{line}: unsupported image suffix")
        elif not resolved_image.is_file():
            problems.append(f"vision/cases.jsonl:{line}: image file is missing")
        elif row.get("image_sha256") != _sha256(resolved_image):
            problems.append(f"vision/cases.jsonl:{line}: image_sha256 mismatch")
        groups = row.get("required_fact_groups")
        if not isinstance(groups, list) or not groups or any(
            not isinstance(group, list)
            or not group
            or any(not isinstance(term, str) or not term.strip() for term in group)
            for group in groups
        ):
            problems.append(f"vision/cases.jsonl:{line}: required_fact_groups must contain term lists")
        forbidden = row.get("forbidden_terms")
        if not isinstance(forbidden, list) or any(not isinstance(term, str) for term in forbidden):
            problems.append(f"vision/cases.jsonl:{line}: forbidden_terms must be a string list")

    acceptance_ready = manifest.get("review_status") == "owner_approved"
    return {
        "schema_version": "eval-gold-validation/v1",
        "valid": not problems,
        "acceptance_ready": acceptance_ready and not problems,
        "review_status": manifest.get("review_status"),
        "gold_revision": manifest.get("gold_revision"),
        "manifest_sha256": _sha256(manifest_path),
        "router_sha256": _sha256(gold_dir / "4b_router.jsonl") if (gold_dir / "4b_router.jsonl").is_file() else "",
        "tool_sha256": _sha256(gold_dir / "9b_tool.jsonl") if (gold_dir / "9b_tool.jsonl").is_file() else "",
        "vision_sha256": _sha256(gold_dir / "vision" / "cases.jsonl") if (gold_dir / "vision" / "cases.jsonl").is_file() else "",
        "router_cases": len(router_rows),
        "tool_cases": len(tool_rows),
        "vision_cases": len(vision_rows),
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验独立 acceptance Gold")
    parser.add_argument("--require-owner-approved", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = validate_gold()
    output = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(output)
    if args.output:
        path = Path(args.output)
        resolved = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
        if not resolved.is_relative_to((ROOT / "reports").resolve()):
            print("ERROR: output 必须位于 reports/")
            return 2
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(output + "\n", encoding="utf-8")
    if not report["valid"]:
        return 1
    if args.require_owner_approved and not report["acceptance_ready"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
