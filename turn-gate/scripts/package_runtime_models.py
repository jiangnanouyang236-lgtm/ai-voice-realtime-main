#!/usr/bin/env python3
"""Create a verified Turn Gate runtime-model archive for bare-metal deployment."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
DEFAULT_OUTPUT = ROOT / "dist" / "turn-gate-runtime-models.tar.gz"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_runtime_models.py")],
        check=True,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    members = (
        "smart-turn-v3.2",
        "livekit-eou-v0.4.1-intl",
        "model-lock.json",
        "README.md",
    )
    with tarfile.open(output, "w:gz") as archive:
        for member in members:
            archive.add(MODELS_DIR / member, arcname=member)
    print(f"Created {output}")
    print(f"SHA256 {sha256(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
