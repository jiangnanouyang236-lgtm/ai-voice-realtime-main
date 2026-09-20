#!/usr/bin/env python3
"""Verify local Turn Gate runtime models against model-lock.json."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
LOCK_PATH = MODELS_DIR / "model-lock.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, expected_size: int, expected_sha256: str) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"missing: {path}"]
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        errors.append(f"size mismatch: {path} expected={expected_size} actual={actual_size}")
    actual_sha256 = sha256(path)
    if actual_sha256 != expected_sha256:
        errors.append(
            f"sha256 mismatch: {path} expected={expected_sha256} actual={actual_sha256}"
        )
    if not errors:
        print(f"PASS {path.relative_to(ROOT)} size={actual_size} sha256={actual_sha256}")
    return errors


def main() -> int:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []
    for model in lock["models"].values():
        model_dir = MODELS_DIR / model["local_dir"]
        errors.extend(
            verify_file(
                model_dir / model["onnx_file"],
                int(model["onnx_size_bytes"]),
                model["onnx_sha256"],
            )
        )
        tokenizer_sha = model.get("tokenizer_json_sha256")
        if tokenizer_sha:
            tokenizer = model_dir / "tokenizer.json"
            if not tokenizer.is_file():
                errors.append(f"missing: {tokenizer}")
            elif sha256(tokenizer) != tokenizer_sha:
                errors.append(f"sha256 mismatch: {tokenizer}")
            else:
                print(
                    f"PASS {tokenizer.relative_to(ROOT)} size={tokenizer.stat().st_size} "
                    f"sha256={tokenizer_sha}"
                )
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print("Turn Gate runtime models verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
