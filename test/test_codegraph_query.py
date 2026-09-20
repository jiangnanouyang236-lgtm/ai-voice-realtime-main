from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import codegraph_query


def _insert_node(
    conn: sqlite3.Connection,
    node_id: str,
    kind: str,
    name: str,
    qualified_name: str,
    file_path: str,
    start_line: int,
) -> None:
    conn.execute(
        """
        INSERT INTO nodes (
            id, kind, name, qualified_name, file_path, language,
            start_line, end_line, start_column, end_column, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'python', ?, ?, 1, 1, 1)
        """,
        (node_id, kind, name, qualified_name, file_path, start_line, start_line + 2),
    )


class CodegraphQueryWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "codegraph.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()
        self._seed_data()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                language TEXT NOT NULL,
                size INTEGER NOT NULL,
                modified_at INTEGER NOT NULL,
                indexed_at INTEGER NOT NULL,
                node_count INTEGER DEFAULT 0,
                errors TEXT
            );
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                language TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                start_column INTEGER NOT NULL,
                end_column INTEGER NOT NULL,
                docstring TEXT,
                signature TEXT,
                visibility TEXT,
                is_exported INTEGER DEFAULT 0,
                is_async INTEGER DEFAULT 0,
                is_static INTEGER DEFAULT 0,
                is_abstract INTEGER DEFAULT 0,
                decorators TEXT,
                type_parameters TEXT,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                metadata TEXT,
                line INTEGER,
                col INTEGER,
                provenance TEXT
            );
            CREATE TABLE unresolved_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_node_id TEXT NOT NULL,
                reference_name TEXT NOT NULL,
                reference_kind TEXT NOT NULL,
                line INTEGER NOT NULL,
                col INTEGER NOT NULL,
                candidates TEXT,
                file_path TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'unknown'
            );
            """
        )

    def _seed_data(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO files (path, content_hash, language, size, modified_at, indexed_at, node_count, errors)
            VALUES (?, 'hash', ?, 100, 10, 20, ?, NULL)
            """,
            [
                ("src/app.py", "python", 2),
                ("rust_client/src/protocol.rs", "rust", 1),
            ],
        )
        _insert_node(self.conn, "caller", "function", "caller", "caller", "src/app.py", 10)
        _insert_node(self.conn, "target", "function", "handle_text", "handle_text", "src/app.py", 20)
        _insert_node(self.conn, "callee", "function", "send_message", "send_message", "src/app.py", 40)
        self.conn.execute(
            "INSERT INTO edges (source, target, kind, line, col) VALUES ('caller', 'target', 'calls', 12, 1)"
        )
        self.conn.execute(
            "INSERT INTO edges (source, target, kind, line, col) VALUES ('target', 'callee', 'calls', 25, 1)"
        )
        self.conn.execute(
            """
            INSERT INTO unresolved_refs (
                from_node_id, reference_name, reference_kind, line, col, file_path, language
            )
            VALUES ('target', 'missing_symbol', 'call', 26, 1, 'src/app.py', 'python')
            """
        )
        self.conn.commit()

    def _capture(self, func, args: argparse.Namespace) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            func(self.conn, args)
        return output.getvalue()

    def test_health_reports_index_coverage_and_unresolved_refs(self) -> None:
        output = self._capture(
            codegraph_query.health,
            argparse.Namespace(db=self.db_path),
        )

        self.assertIn("Files indexed: 2", output)
        self.assertIn("Total nodes: 3", output)
        self.assertIn("Unresolved refs: 1", output)
        self.assertIn("python", output)
        self.assertIn("rust", output)

    def test_inspect_combines_symbol_location_callers_and_callees(self) -> None:
        output = self._capture(
            codegraph_query.inspect,
            argparse.Namespace(symbol="handle_text", limit=20),
        )

        self.assertIn("Symbol matches", output)
        self.assertIn("handle_text", output)
        self.assertIn("Callers", output)
        self.assertIn("caller", output)
        self.assertIn("Callees", output)
        self.assertIn("send_message", output)

    def test_changed_lists_symbols_for_changed_files(self) -> None:
        output = self._capture(
            codegraph_query.changed,
            argparse.Namespace(paths=["src/app.py"], limit=20),
        )

        self.assertIn("src/app.py", output)
        self.assertIn("handle_text", output)
        self.assertNotIn("rust_client/src/protocol.rs", output)

    def test_review_outputs_compact_impact_checklist(self) -> None:
        output = self._capture(
            codegraph_query.review,
            argparse.Namespace(symbol="handle_text", limit=20),
        )

        self.assertIn("Review target", output)
        self.assertIn("Impact surface", output)
        self.assertIn("caller", output)
        self.assertIn("send_message", output)
        self.assertIn("Checklist", output)


if __name__ == "__main__":
    unittest.main()
