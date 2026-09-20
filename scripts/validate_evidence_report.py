#!/usr/bin/env python3
"""Validate an evidence report against the repository Harness policy.

The validator deliberately distinguishes implementation, local integration,
live services, and physical hardware. It does not run probes or grant
authorization; it only checks that a claimed level is backed by explicit,
hash-bound evidence produced by an authorized run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_STATUSES = {"PASS", "FAIL", "NOT_RUN"}
ALLOWED_KINDS = {
    "static",
    "unit",
    "local_integration",
    "hybrid_integration",
    "live_service",
    "hardware",
}
ALLOWED_SCOPES = {"mock", "synthetic", "localhost", "lan", "external", "hardware"}
ALLOWED_SOURCES = {"command", "log", "runtime_status", "artifact", "manual_observation"}


class EvidenceValidationError(ValueError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceValidationError(f"JSON root must be an object: {path}")
    return value


def _require_non_empty_string(value: Any, field: str, problems: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{field} must be a non-empty string")
        return ""
    return value.strip()


def _require_bool(mapping: Mapping[str, Any], field: str, problems: list[str]) -> bool:
    value = mapping.get(field)
    if not isinstance(value, bool):
        problems.append(f"{field} must be true or false")
        return False
    return value


def _resolve_artifact(report_path: Path, artifact: str, root: Path) -> Path:
    candidate = Path(artifact)
    if candidate.is_absolute():
        return candidate.resolve()
    root_candidate = (root / candidate).resolve()
    report_candidate = (report_path.parent / candidate).resolve()
    if root_candidate.is_file() or not report_candidate.is_file():
        return root_candidate
    return report_candidate


def _commit_exists(root: Path, commit: str) -> bool:
    if not commit:
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _valid_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_hashed_artifact(
    item: Mapping[str, Any],
    *,
    report_path: Path,
    root: Path,
    prefix: str,
    problems: list[str],
) -> None:
    artifact = _require_non_empty_string(item.get("artifact"), f"{prefix}.artifact", problems)
    sha256 = _require_non_empty_string(item.get("sha256"), f"{prefix}.sha256", problems).lower()
    if not artifact or not sha256:
        return
    if not SHA256_RE.fullmatch(sha256):
        problems.append(f"{prefix}.sha256 must be 64 lowercase hex characters")
        return
    path = _resolve_artifact(report_path, artifact, root)
    if not path.is_file():
        problems.append(f"{prefix}.artifact does not exist: {artifact}")
        return
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != sha256:
        problems.append(f"{prefix}.sha256 does not match artifact: {artifact}")


def _validate_environment_artifact(
    binding: Any,
    *,
    report: Mapping[str, Any],
    expected_endpoint_ids: set[str],
    observed_endpoint_keys: set[tuple[str, str]],
    report_path: Path,
    root: Path,
    problems: list[str],
) -> None:
    if not isinstance(binding, dict):
        problems.append("environment_artifact must be an object")
        return
    _validate_hashed_artifact(
        binding,
        report_path=report_path,
        root=root,
        prefix="environment_artifact",
        problems=problems,
    )
    artifact = binding.get("artifact")
    if not isinstance(artifact, str) or not artifact.strip():
        return
    path = _resolve_artifact(report_path, artifact, root)
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"environment_artifact is not valid JSON: {exc}")
        return
    if not isinstance(payload, dict):
        problems.append("environment_artifact JSON root must be an object")
        return
    if payload.get("schema_version") != "ai-voice-environment/v1":
        problems.append("environment_artifact has unsupported schema_version")
    if payload.get("dirty") is not False:
        problems.append("environment_artifact.dirty must be false")
    for field in ("commit", "capability", "claimed_level"):
        if payload.get(field) != report.get(field):
            problems.append(f"environment_artifact.{field} must match report.{field}")
    profile = payload.get("profile")
    if not isinstance(profile, str) or not profile.strip():
        problems.append("environment_artifact.profile must be a non-empty string")
    captured_at = payload.get("captured_at")
    if not isinstance(captured_at, str) or not _valid_timestamp(captured_at):
        problems.append("environment_artifact.captured_at must be an ISO-8601 timestamp with timezone")

    artifact_endpoints = payload.get("endpoints")
    artifact_endpoint_keys: set[tuple[str, str]] = set()
    if not isinstance(artifact_endpoints, list):
        problems.append("environment_artifact.endpoints must be an array")
        return
    for index, endpoint in enumerate(artifact_endpoints):
        if not isinstance(endpoint, dict):
            problems.append(f"environment_artifact.endpoints[{index}] must be an object")
            continue
        component = endpoint.get("component")
        scope = endpoint.get("scope")
        if not isinstance(component, str) or not component.strip():
            problems.append(f"environment_artifact.endpoints[{index}].component is invalid")
            continue
        if scope not in ALLOWED_SCOPES:
            problems.append(f"environment_artifact.endpoints[{index}].scope is invalid")
            continue
        artifact_endpoint_keys.add((component, scope))
    artifact_endpoint_ids = {component for component, _scope in artifact_endpoint_keys}
    if artifact_endpoint_ids != expected_endpoint_ids:
        problems.append(
            "environment_artifact endpoint IDs must exactly match Harness policy: "
            f"expected {sorted(expected_endpoint_ids)}, found {sorted(artifact_endpoint_ids)}"
        )
    if artifact_endpoint_keys != observed_endpoint_keys:
        problems.append("observed_endpoints must exactly match environment_artifact.endpoints")


def _merge_policy(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key == "required_assertions":
            current = dict(merged.get(key, {}))
            current.update(value if isinstance(value, dict) else {})
            merged[key] = current
        elif key in {
            "required_components",
            "forbidden_endpoint_scopes",
            "allowed_observed_endpoint_scopes",
            "forbidden_observed_endpoint_scopes",
            "required_any_observed_endpoint_scopes",
        }:
            current = list(merged.get(key, []))
            for item in value if isinstance(value, list) else []:
                if item not in current:
                    current.append(item)
            merged[key] = current
        else:
            merged[key] = value
    return merged


def validate_report(
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    report_path: Path,
    root: Path = ROOT,
) -> list[str]:
    problems: list[str] = []
    policy = manifest.get("evidence_policy")
    if not isinstance(policy, dict):
        return ["Harness manifest does not define evidence_policy"]
    schema_version = policy.get("schema_version")
    if report.get("schema_version") != schema_version:
        problems.append(f"schema_version must be {schema_version!r}")

    _require_non_empty_string(report.get("objective"), "objective", problems)
    commit = _require_non_empty_string(report.get("commit"), "commit", problems).lower()
    if commit and not COMMIT_RE.fullmatch(commit):
        problems.append("commit must be a 7-40 character lowercase Git SHA")
    elif commit and not _commit_exists(root, commit):
        problems.append(f"commit does not exist in this repository: {commit}")

    capability = _require_non_empty_string(report.get("capability"), "capability", problems)
    capabilities = policy.get("capabilities", {})
    if capability and capability not in capabilities:
        problems.append(f"capability is not declared by Harness: {capability}")

    claimed_level = _require_non_empty_string(report.get("claimed_level"), "claimed_level", problems)
    levels = policy.get("levels", {})
    if claimed_level and claimed_level not in levels:
        problems.append(f"claimed_level is not declared by Harness: {claimed_level}")

    endpoint_scope = _require_non_empty_string(report.get("endpoint_scope"), "endpoint_scope", problems)
    if endpoint_scope and endpoint_scope not in ALLOWED_SCOPES:
        problems.append(f"endpoint_scope must be one of {sorted(ALLOWED_SCOPES)}")

    observed_endpoints = report.get("observed_endpoints")
    observed_endpoint_scopes: set[str] = set()
    observed_endpoint_keys: set[tuple[str, str]] = set()
    if observed_endpoints is None:
        observed_endpoints = []
    elif not isinstance(observed_endpoints, list):
        problems.append("observed_endpoints must be an array (empty is allowed)")
        observed_endpoints = []
    for index, raw_endpoint in enumerate(observed_endpoints):
        prefix = f"observed_endpoints[{index}]"
        if not isinstance(raw_endpoint, dict):
            problems.append(f"{prefix} must be an object")
            continue
        component = _require_non_empty_string(raw_endpoint.get("component"), f"{prefix}.component", problems)
        scope = _require_non_empty_string(raw_endpoint.get("scope"), f"{prefix}.scope", problems)
        if scope and scope not in ALLOWED_SCOPES:
            problems.append(f"{prefix}.scope must be one of {sorted(ALLOWED_SCOPES)}")
            continue
        key = (component, scope)
        if key in observed_endpoint_keys:
            problems.append(f"{prefix} duplicates component/scope {key!r}")
        observed_endpoint_keys.add(key)
        if scope:
            observed_endpoint_scopes.add(scope)

    authorization = report.get("authorization")
    if not isinstance(authorization, dict):
        problems.append("authorization must be an object")
        authorization = {}
    connectivity_authorized = _require_bool(authorization, "connectivity", problems)
    external_write_authorized = _require_bool(authorization, "external_write", problems)
    hardware_authorized = _require_bool(authorization, "hardware_action", problems)
    authorization_reference = authorization.get("reference", "")
    if not isinstance(authorization_reference, str):
        problems.append("authorization.reference must be a string")
        authorization_reference = ""
    if (connectivity_authorized or external_write_authorized or hardware_authorized) and not authorization_reference.strip():
        problems.append("authorized operations require a non-empty authorization.reference")

    assertions = report.get("assertions")
    if not isinstance(assertions, dict):
        problems.append("assertions must be an object")
        assertions = {}
    runtime_snapshot = _require_bool(assertions, "runtime_snapshot", problems)
    trace_ids = assertions.get("trace_ids")
    if not isinstance(trace_ids, list) or not all(isinstance(item, str) and item.strip() for item in trace_ids):
        problems.append("assertions.trace_ids must be an array of non-empty strings (empty is allowed)")
        trace_ids = []
    elif len(set(trace_ids)) != len(trace_ids):
        problems.append("assertions.trace_ids must not contain duplicates")
    bot_bindings = assertions.get("bot_bindings")
    if not isinstance(bot_bindings, list) or not all(isinstance(item, str) and item.strip() for item in bot_bindings):
        problems.append("assertions.bot_bindings must be an array of non-empty strings (empty is allowed)")
        bot_bindings = []
    elif len(set(bot_bindings)) != len(bot_bindings):
        problems.append("assertions.bot_bindings must not contain duplicates")

    unknowns = report.get("unknowns")
    if not isinstance(unknowns, list) or not all(isinstance(item, str) and item.strip() for item in unknowns):
        problems.append("unknowns must be an array of non-empty strings (empty is allowed)")

    evidence = report.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        problems.append("evidence must be a non-empty array")
        evidence = []

    pass_kinds: set[str] = set()
    pass_items: list[Mapping[str, Any]] = []
    evidence_ids: set[str] = set()
    for index, raw_item in enumerate(evidence):
        prefix = f"evidence[{index}]"
        if not isinstance(raw_item, dict):
            problems.append(f"{prefix} must be an object")
            continue
        item = raw_item
        evidence_id = _require_non_empty_string(item.get("id"), f"{prefix}.id", problems)
        if evidence_id in evidence_ids:
            problems.append(f"{prefix}.id is duplicated: {evidence_id}")
        evidence_ids.add(evidence_id)
        status = _require_non_empty_string(item.get("status"), f"{prefix}.status", problems)
        if status and status not in ALLOWED_STATUSES:
            problems.append(f"{prefix}.status must be one of {sorted(ALLOWED_STATUSES)}")
        kind = _require_non_empty_string(item.get("kind"), f"{prefix}.kind", problems)
        if kind and kind not in ALLOWED_KINDS:
            problems.append(f"{prefix}.kind must be one of {sorted(ALLOWED_KINDS)}")
        _require_non_empty_string(item.get("component"), f"{prefix}.component", problems)
        source = _require_non_empty_string(item.get("source"), f"{prefix}.source", problems)
        if source and source not in ALLOWED_SOURCES:
            problems.append(f"{prefix}.source must be one of {sorted(ALLOWED_SOURCES)}")
        observed_at = _require_non_empty_string(item.get("observed_at"), f"{prefix}.observed_at", problems)
        if observed_at and not _valid_timestamp(observed_at):
            problems.append(f"{prefix}.observed_at must be an ISO-8601 timestamp with timezone")
        _require_non_empty_string(item.get("summary"), f"{prefix}.summary", problems)
        for linkage_field in ("trace_ids", "bot_bindings"):
            linkage_values = item.get(linkage_field)
            if linkage_values is None:
                continue
            if not isinstance(linkage_values, list) or not all(
                isinstance(value, str) and value.strip() for value in linkage_values
            ):
                problems.append(f"{prefix}.{linkage_field} must be an array of non-empty strings")
            elif len(set(linkage_values)) != len(linkage_values):
                problems.append(f"{prefix}.{linkage_field} must not contain duplicates")
        if status == "PASS":
            pass_kinds.add(kind)
            pass_items.append(item)

    if claimed_level in levels:
        level_policy = dict(levels[claimed_level])
        capability_policy = capabilities.get(capability, {}) if capability in capabilities else {}
        if isinstance(capability_policy, dict):
            override = capability_policy.get(claimed_level, {})
            if isinstance(override, dict):
                level_policy = _merge_policy(level_policy, override)

        required_any = set(level_policy.get("required_any_kinds", []))
        if required_any and not pass_kinds.intersection(required_any):
            problems.append(f"{claimed_level} requires at least one PASS evidence kind from {sorted(required_any)}")
        required_all = set(level_policy.get("required_all_kinds", []))
        missing_kinds = required_all - pass_kinds
        if missing_kinds:
            problems.append(f"{claimed_level} is missing PASS evidence kinds: {sorted(missing_kinds)}")

        forbidden_scopes = set(level_policy.get("forbidden_endpoint_scopes", []))
        if endpoint_scope in forbidden_scopes:
            problems.append(f"{claimed_level} cannot use endpoint_scope={endpoint_scope}")
        if level_policy.get("require_observed_endpoints") and not observed_endpoints:
            problems.append(f"{claimed_level} requires non-empty observed_endpoints")
        allowed_observed_scopes = set(level_policy.get("allowed_observed_endpoint_scopes", []))
        disallowed_observed_scopes = observed_endpoint_scopes - allowed_observed_scopes
        if allowed_observed_scopes and disallowed_observed_scopes:
            problems.append(
                f"{claimed_level} observed endpoint scopes must be within "
                f"{sorted(allowed_observed_scopes)}; found {sorted(disallowed_observed_scopes)}"
            )
        forbidden_observed_scopes = set(level_policy.get("forbidden_observed_endpoint_scopes", []))
        present_forbidden_observed_scopes = observed_endpoint_scopes & forbidden_observed_scopes
        if present_forbidden_observed_scopes:
            problems.append(
                f"{claimed_level} cannot use observed endpoint scopes "
                f"{sorted(present_forbidden_observed_scopes)}"
            )
        required_any_observed_scopes = set(
            level_policy.get("required_any_observed_endpoint_scopes", [])
        )
        if required_any_observed_scopes and not (
            observed_endpoint_scopes & required_any_observed_scopes
        ):
            problems.append(
                f"{claimed_level} requires at least one observed endpoint scope from "
                f"{sorted(required_any_observed_scopes)}"
            )
        if level_policy.get("require_environment_artifact"):
            _validate_environment_artifact(
                report.get("environment_artifact"),
                report=report,
                expected_endpoint_ids=set(level_policy.get("environment_endpoint_ids", [])),
                observed_endpoint_keys=observed_endpoint_keys,
                report_path=report_path,
                root=root,
                problems=problems,
            )
        if level_policy.get("require_connectivity_authorization") and not connectivity_authorized:
            problems.append(f"{claimed_level} requires authorization.connectivity=true")
        if level_policy.get("require_external_write_authorization") and not external_write_authorized:
            problems.append(f"{claimed_level} requires authorization.external_write=true")
        if level_policy.get("require_hardware_authorization") and not hardware_authorized:
            problems.append(f"{claimed_level} requires authorization.hardware_action=true")
        if level_policy.get("require_runtime_snapshot") and not runtime_snapshot:
            problems.append(f"{claimed_level} requires assertions.runtime_snapshot=true")

        required_components = set(level_policy.get("required_components", []))
        qualifying_items = [item for item in pass_items if item.get("kind") in required_all]
        qualifying_components = {str(item.get("component") or "") for item in qualifying_items}
        missing_components = required_components - qualifying_components
        if missing_components:
            problems.append(f"{claimed_level} is missing PASS components: {sorted(missing_components)}")

        required_assertions = level_policy.get("required_assertions", {})
        if not isinstance(required_assertions, dict):
            problems.append(f"Harness policy for {capability}/{claimed_level} has invalid required_assertions")
            required_assertions = {}
        else:
            for key, expected in required_assertions.items():
                if key == "min_bot_bindings":
                    if len(bot_bindings) < int(expected):
                        problems.append(f"assertions.bot_bindings must contain at least {expected} unique bindings")
                elif key == "min_trace_ids":
                    if len(trace_ids) < int(expected):
                        problems.append(f"assertions.trace_ids must contain at least {expected} unique trace IDs")
                elif assertions.get(key) != expected:
                    problems.append(f"assertions.{key} must be {expected!r}")

        if assertions.get("same_trace") is True and not trace_ids:
            problems.append("assertions.same_trace=true requires non-empty assertions.trace_ids")

        if assertions.get("same_trace") is True and required_components:
            component_trace_sets: list[set[str]] = []
            for component in sorted(required_components):
                observed_trace_ids = {
                    trace_id
                    for item in qualifying_items
                    if item.get("component") == component
                    for trace_id in item.get("trace_ids", [])
                    if isinstance(trace_id, str) and trace_id.strip()
                }
                if not observed_trace_ids:
                    problems.append(
                        f"PASS evidence for required component {component!r} must declare trace_ids"
                    )
                component_trace_sets.append(observed_trace_ids)
            shared_trace_ids = set.intersection(*component_trace_sets) if component_trace_sets else set()
            if trace_ids and not set(trace_ids).issubset(shared_trace_ids):
                problems.append(
                    "assertions.trace_ids must be present in every required component's PASS evidence"
                )
            minimum_trace_ids = int(required_assertions.get("min_trace_ids", 1))
            if len(shared_trace_ids) < minimum_trace_ids:
                problems.append(
                    f"required components must share at least {minimum_trace_ids} trace IDs"
                )

        minimum_bot_bindings = int(required_assertions.get("min_bot_bindings", 0))
        if minimum_bot_bindings and required_components and bot_bindings:
            for component in sorted(required_components):
                observed_bot_bindings = {
                    binding
                    for item in qualifying_items
                    if item.get("component") == component
                    for binding in item.get("bot_bindings", [])
                    if isinstance(binding, str) and binding.strip()
                }
                if not set(bot_bindings).issubset(observed_bot_bindings):
                    problems.append(
                        f"PASS evidence for required component {component!r} must declare every asserted bot binding"
                    )

        if level_policy.get("require_hashed_artifacts"):
            for index, item in enumerate(qualifying_items):
                _validate_hashed_artifact(
                    item,
                    report_path=report_path,
                    root=root,
                    prefix=f"required_pass_evidence[{index}]",
                    problems=problems,
                )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Evidence report JSON to validate")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        manifest = _load_json(root / ".ai" / "harness.json")
        report_path = args.report.resolve()
        report = _load_json(report_path)
        problems = validate_report(report, manifest, report_path=report_path, root=root)
    except EvidenceValidationError as exc:
        print(f"Evidence report validation failed:\n- {exc}")
        return 2
    if problems:
        print("Evidence report validation failed:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"Evidence report valid: {report['capability']} {report['claimed_level']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
