#!/usr/bin/env python3
"""Small read-only helpers for the local CodeGraph SQLite index."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path


DEFAULT_DB = Path(".codegraph/codegraph.db")


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"CodeGraph database not found: {db_path}")
    # The CodeGraph DB can use WAL mode. In this read-only helper, immutable=1
    # avoids SQLite trying to create or inspect sidecar files through a read-only
    # URI. Refresh the index with `codegraph sync` before relying on new edits.
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def print_rows(rows: list[sqlite3.Row], columns: list[str]) -> None:
    if not rows:
        print("(no results)")
        return
    widths = {
        col: max(len(col), *(len(str(row[col] if row[col] is not None else "")) for row in rows))
        for col in columns
    }
    print("  ".join(col.ljust(widths[col]) for col in columns))
    print("  ".join("-" * widths[col] for col in columns))
    for row in rows:
        print("  ".join(str(row[col] if row[col] is not None else "").ljust(widths[col]) for col in columns))


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def summary(conn: sqlite3.Connection, _args: argparse.Namespace) -> None:
    print("Files by language")
    rows = conn.execute(
        """
        SELECT language, COUNT(*) AS files, COALESCE(SUM(node_count), 0) AS nodes
        FROM files
        GROUP BY language
        ORDER BY files DESC, language
        """
    ).fetchall()
    print_rows(rows, ["language", "files", "nodes"])

    print("\nNode kinds")
    rows = conn.execute(
        """
        SELECT kind, COUNT(*) AS count
        FROM nodes
        GROUP BY kind
        ORDER BY count DESC, kind
        LIMIT 20
        """
    ).fetchall()
    print_rows(rows, ["kind", "count"])

    print("\nEdge kinds")
    rows = conn.execute(
        """
        SELECT kind, COUNT(*) AS count
        FROM edges
        GROUP BY kind
        ORDER BY count DESC, kind
        """
    ).fetchall()
    print_rows(rows, ["kind", "count"])

    unresolved = conn.execute("SELECT COUNT(*) AS count FROM unresolved_refs").fetchone()["count"]
    print(f"\nUnresolved refs: {unresolved}")


def health(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    files = scalar(conn, "SELECT COUNT(*) FROM files")
    nodes = scalar(conn, "SELECT COUNT(*) FROM nodes")
    edges = scalar(conn, "SELECT COUNT(*) FROM edges")
    unresolved = scalar(conn, "SELECT COUNT(*) FROM unresolved_refs")
    stale_files = scalar(conn, "SELECT COUNT(*) FROM files WHERE modified_at > indexed_at")
    db_path = getattr(args, "db", DEFAULT_DB)

    print("CodeGraph health")
    print(f"Database: {db_path}")
    print(f"Files indexed: {files}")
    print(f"Total nodes: {nodes}")
    print(f"Total edges: {edges}")
    print(f"Unresolved refs: {unresolved}")
    print(f"Files newer than index: {stale_files}")

    rows = conn.execute(
        """
        SELECT language, COUNT(*) AS files, COALESCE(SUM(node_count), 0) AS nodes
        FROM files
        GROUP BY language
        ORDER BY files DESC, language
        """
    ).fetchall()
    print("\nLanguages")
    print_rows(rows, ["language", "files", "nodes"])


def search(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = conn.execute(
        """
        SELECT n.kind, n.qualified_name, n.file_path, n.start_line, n.end_line
        FROM nodes_fts f
        JOIN nodes n ON f.id = n.id
        WHERE nodes_fts MATCH ?
        ORDER BY n.file_path, n.start_line
        LIMIT ?
        """,
        (args.query, args.limit),
    ).fetchall()
    print_rows(rows, ["kind", "qualified_name", "file_path", "start_line", "end_line"])


def resolve_nodes(conn: sqlite3.Connection, symbol: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, kind, name, qualified_name, file_path, start_line, end_line
        FROM nodes
        WHERE qualified_name = ?
           OR name = ?
           OR qualified_name LIKE ?
        ORDER BY
            CASE WHEN qualified_name = ? THEN 0 WHEN name = ? THEN 1 ELSE 2 END,
            file_path,
            start_line
        LIMIT ?
        """,
        (symbol, symbol, f"%{symbol}%", symbol, symbol, limit),
    ).fetchall()


def pick_node(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row:
    matches = resolve_nodes(conn, symbol, 20)
    if not matches:
        raise SystemExit(f"Symbol not found: {symbol}")
    if len(matches) > 1:
        print("Multiple matches; using the first one. Refine the symbol if needed.", file=sys.stderr)
        print_rows(matches, ["kind", "qualified_name", "file_path", "start_line", "end_line"])
        print("", file=sys.stderr)
    return matches[0]


def query_callers(conn: sqlite3.Connection, node_id: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT DISTINCT s.kind, s.qualified_name, s.file_path, s.start_line, e.line AS call_line
        FROM edges e
        JOIN nodes s ON e.source = s.id
        WHERE e.target = ?
          AND e.kind IN ('calls', 'references')
        ORDER BY s.file_path, call_line, s.start_line
        LIMIT ?
        """,
        (node_id, limit),
    ).fetchall()


def callers(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    node = pick_node(conn, args.symbol)
    rows = query_callers(conn, node["id"], args.limit)
    print(f"Target: {node['qualified_name']} ({node['file_path']}:{node['start_line']})")
    print_rows(rows, ["kind", "qualified_name", "file_path", "start_line", "call_line"])


def query_callees(conn: sqlite3.Connection, node_id: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT DISTINCT e.kind, t.qualified_name, t.file_path, t.start_line, e.line AS edge_line
        FROM edges e
        JOIN nodes t ON e.target = t.id
        WHERE e.source = ?
          AND e.kind IN ('calls', 'references', 'instantiates')
        ORDER BY e.kind, t.file_path, edge_line, t.start_line
        LIMIT ?
        """,
        (node_id, limit),
    ).fetchall()


def callees(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    node = pick_node(conn, args.symbol)
    rows = query_callees(conn, node["id"], args.limit)
    print(f"Source: {node['qualified_name']} ({node['file_path']}:{node['start_line']})")
    print_rows(rows, ["kind", "qualified_name", "file_path", "start_line", "edge_line"])


def inspect(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    matches = resolve_nodes(conn, args.symbol, args.limit)
    if not matches:
        raise SystemExit(f"Symbol not found: {args.symbol}")

    print("Symbol matches")
    print_rows(matches, ["kind", "qualified_name", "file_path", "start_line", "end_line"])

    node = matches[0]
    print(f"\nSelected: {node['qualified_name']} ({node['file_path']}:{node['start_line']})")

    print("\nCallers")
    print_rows(
        query_callers(conn, node["id"], args.limit),
        ["kind", "qualified_name", "file_path", "start_line", "call_line"],
    )

    print("\nCallees")
    print_rows(
        query_callees(conn, node["id"], args.limit),
        ["kind", "qualified_name", "file_path", "start_line", "edge_line"],
    )


def hotspots(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = conn.execute(
        """
        SELECT n.kind, n.qualified_name, n.file_path, n.start_line, COUNT(e.id) AS outgoing
        FROM nodes n
        LEFT JOIN edges e ON e.source = n.id AND e.kind = 'calls'
        WHERE n.kind IN ('function', 'method')
        GROUP BY n.id
        ORDER BY outgoing DESC, n.file_path, n.start_line
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print_rows(rows, ["kind", "qualified_name", "file_path", "start_line", "outgoing"])


def file_symbols(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = conn.execute(
        """
        SELECT kind, qualified_name, start_line, end_line
        FROM nodes
        WHERE file_path = ?
        ORDER BY start_line, end_line
        LIMIT ?
        """,
        (args.path, args.limit),
    ).fetchall()
    print_rows(rows, ["kind", "qualified_name", "start_line", "end_line"])


def git_changed_paths() -> list[str]:
    commands = [
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    paths: list[str] = []
    for command in commands:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        paths.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(set(paths))


def changed(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    paths = sorted({path.strip().removeprefix("./") for path in (args.paths or git_changed_paths()) if path.strip()})
    if not paths:
        print("No changed files found.")
        return

    print("Changed indexed symbols")
    for path in paths:
        rows = conn.execute(
            """
            SELECT kind, qualified_name, start_line, end_line
            FROM nodes
            WHERE file_path = ?
            ORDER BY start_line, end_line
            LIMIT ?
            """,
            (path, args.limit),
        ).fetchall()
        if not rows:
            continue
        print(f"\n{path}")
        print_rows(rows, ["kind", "qualified_name", "start_line", "end_line"])


def review(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    node = pick_node(conn, args.symbol)
    callers_rows = query_callers(conn, node["id"], args.limit)
    callees_rows = query_callees(conn, node["id"], args.limit)

    print(f"Review target: {node['qualified_name']} ({node['file_path']}:{node['start_line']})")
    print("\nImpact surface")
    print(f"Callers: {len(callers_rows)}")
    print(f"Callees/references: {len(callees_rows)}")

    print("\nCallers")
    print_rows(callers_rows, ["kind", "qualified_name", "file_path", "start_line", "call_line"])

    print("\nCallees")
    print_rows(callees_rows, ["kind", "qualified_name", "file_path", "start_line", "edge_line"])

    print("\nChecklist")
    print("- Verify each caller still passes the expected inputs and state.")
    print("- Verify callees still receive valid arguments and errors are handled.")
    print("- Use rg for config keys, log text, environment variables, and prompt strings.")
    print("- Re-run tests or compile checks that cover the touched files.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to CodeGraph SQLite DB")
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary_parser = subparsers.add_parser("summary", help="Show index coverage summary")
    summary_parser.set_defaults(func=summary)

    health_parser = subparsers.add_parser("health", help="Show practical index health checks")
    health_parser.set_defaults(func=health)

    search_parser = subparsers.add_parser("search", help="Full-text search symbols")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=30)
    search_parser.set_defaults(func=search)

    callers_parser = subparsers.add_parser("callers", help="Show callers/references of a symbol")
    callers_parser.add_argument("symbol")
    callers_parser.add_argument("--limit", type=int, default=50)
    callers_parser.set_defaults(func=callers)

    callees_parser = subparsers.add_parser("callees", help="Show calls/references made by a symbol")
    callees_parser.add_argument("symbol")
    callees_parser.add_argument("--limit", type=int, default=50)
    callees_parser.set_defaults(func=callees)

    inspect_parser = subparsers.add_parser("inspect", help="Show matches, callers, and callees for a symbol")
    inspect_parser.add_argument("symbol")
    inspect_parser.add_argument("--limit", type=int, default=30)
    inspect_parser.set_defaults(func=inspect)

    changed_parser = subparsers.add_parser("changed", help="Show indexed symbols in changed files")
    changed_parser.add_argument("paths", nargs="*", help="Optional file paths; defaults to git changed files")
    changed_parser.add_argument("--limit", type=int, default=100)
    changed_parser.set_defaults(func=changed)

    review_parser = subparsers.add_parser("review", help="Show a compact review checklist for a symbol")
    review_parser.add_argument("symbol")
    review_parser.add_argument("--limit", type=int, default=30)
    review_parser.set_defaults(func=review)

    hotspots_parser = subparsers.add_parser("hotspots", help="Show functions/methods with many outgoing calls")
    hotspots_parser.add_argument("--limit", type=int, default=20)
    hotspots_parser.set_defaults(func=hotspots)

    file_parser = subparsers.add_parser("file", help="Show symbols in a file")
    file_parser.add_argument("path")
    file_parser.add_argument("--limit", type=int, default=100)
    file_parser.set_defaults(func=file_symbols)

    args = parser.parse_args()
    with connect(args.db) as conn:
        args.func(conn, args)


if __name__ == "__main__":
    main()
