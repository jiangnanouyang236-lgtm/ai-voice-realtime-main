from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.check_repo_hygiene import REQUIRED_FILES, is_forbidden, scan


ROOT = Path(__file__).resolve().parents[1]


def test_generated_and_secret_paths_are_forbidden() -> None:
    assert is_forbidden(".env")
    assert is_forbidden("tmp/eval.json")
    assert is_forbidden("rust_client/target/debug/app")
    assert is_forbidden("reports/eval.json")


def test_source_and_examples_are_allowed() -> None:
    assert is_forbidden("llm/llm_grpc_server.py") is None
    assert is_forbidden(".env.example") is None
    assert is_forbidden("deploy/vllm/llm/.env.llm.example") is None


def test_current_tracked_tree_respects_hygiene_contract() -> None:
    assert scan(ROOT) == []


def _hygiene_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for relative in REQUIRED_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == ".ai/harness.json":
            path.write_text(json.dumps({"secret_env": ["WEBSEARCH_API_KEY", "SECOND_API_KEY"]}), encoding="utf-8")
        else:
            path.write_text("test\n", encoding="utf-8")
    (tmp_path / "deploy").mkdir()
    (tmp_path / ".env.example").write_text("WEBSEARCH_API_KEY=\nSECOND_API_KEY=\n", encoding="utf-8")
    (tmp_path / "deploy" / "env.v3.compose.example").write_text(
        "WEBSEARCH_API_KEY=\nSECOND_API_KEY=\n", encoding="utf-8"
    )
    return tmp_path


def test_index_mode_requires_contract_files_to_be_staged(tmp_path: Path) -> None:
    root = _hygiene_repo(tmp_path)
    assert scan(root) == []
    assert any("required repository contract is missing" in item for item in scan(root, index=True))
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    assert scan(root, index=True) == []
    (root / "AGENTS.md").unlink()
    assert any("AGENTS.md" in item for item in scan(root))
    assert scan(root, index=True) == []


def test_template_reference_must_be_the_entire_value(tmp_path: Path) -> None:
    root = _hygiene_repo(tmp_path)
    (root / ".env.example").write_text(
        "WEBSEARCH_API_KEY=${PUBLIC_REF}hardcoded-secret\nSECOND_API_KEY=real-token\n",
        encoding="utf-8",
    )
    problems = scan(root)
    assert any("WEBSEARCH_API_KEY" in item for item in problems)
    assert any("SECOND_API_KEY" in item for item in problems)
