#!/usr/bin/env python3
"""
AST-based static checker for dual_log payload contract.

Finds calls to .dual_log(...) that either omit the keyword argument `payload`,
pass `payload=None`, or pass an empty dict literal ({} or dict()).

Usage: python scripts/check_log_contract.py --path . --exclude deprecated,.venv,tests
Exits with code 0 when no violations are found, 2 when violations exist.
"""

from __future__ import annotations

import ast
import os
import sys
import argparse
from typing import List, Tuple


class DualLogVisitor(ast.NodeVisitor):
    def __init__(self, filename: str, source: str):
        self.filename = filename
        self.source_lines = source.splitlines()
        self.violations: List[Tuple[int, str]] = []  # (lineno, kind)

    def visit_Call(self, node: ast.Call) -> None:
        # Determine callee name: attribute.attr or Name.id
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id

        if name == "dual_log":
            kw_names = [kw.arg for kw in node.keywords if kw.arg is not None]
            if "payload" not in kw_names:
                self.violations.append((node.lineno, "MISSING_PAYLOAD"))
            else:
                # Find the payload keyword and inspect its value
                for kw in node.keywords:
                    if kw.arg == "payload":
                        val = kw.value
                        # payload=None
                        if isinstance(val, ast.Constant) and val.value is None:
                            self.violations.append((node.lineno, "PAYLOAD_NONE"))
                        # payload = {}
                        elif isinstance(val, ast.Dict):
                            # empty dict literal
                            if len(val.keys) == 0:
                                self.violations.append((node.lineno, "PAYLOAD_EMPTY_DICT"))
                        # payload = dict()
                        elif isinstance(val, ast.Call):
                            if isinstance(val.func, ast.Name) and val.func.id == "dict" and len(val.args) == 0 and len(val.keywords) == 0:
                                self.violations.append((node.lineno, "PAYLOAD_EMPTY_DICT_CALL"))
                        # other complex expressions are not flagged here
        # Continue traversal
        self.generic_visit(node)


def find_py_files(root: str, excludes: List[str]) -> List[str]:
    py_files: List[str] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip excluded path segments
        parts = set(os.path.normpath(dirpath).split(os.sep))
        if any(ex in parts for ex in excludes):
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                py_files.append(full)
    return py_files


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check dual_log payload contract across Python files.")
    parser.add_argument("--path", default=".", help="Root path to scan (default: current directory)")
    parser.add_argument("--exclude", default="deprecated,.venv,venv,tests", help="Comma-separated directory name segments to exclude")
    args = parser.parse_args(argv)

    excludes = [p for p in (args.exclude or "").split(",") if p]
    files = find_py_files(args.path, excludes)

    total_violations = 0
    violations_map = {}

    for f in sorted(files):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                src = fh.read()
        except Exception as e:
            # Could not read - skip but report to stderr
            print(f"SKIP {f}: cannot read file: {e}", file=sys.stderr)
            continue
        try:
            tree = ast.parse(src, filename=f)
        except SyntaxError as e:
            print(f"SKIP {f}: syntax error: {e}", file=sys.stderr)
            continue

        visitor = DualLogVisitor(f, src)
        visitor.visit(tree)
        if visitor.violations:
            violations_map[f] = visitor.violations
            total_violations += len(visitor.violations)

    if total_violations == 0:
        print("No dual_log payload violations found.")
        return 0

    # Print detailed report
    print(f"Found {total_violations} dual_log payload contract violation(s):\n")
    for f, items in violations_map.items():
        print(f"{f}: {len(items)} violation(s)")
        try:
            with open(f, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:
            lines = []
        for lineno, kind in items:
            snippet = ""
            if 1 <= lineno <= len(lines):
                snippet = lines[lineno - 1].rstrip("\n")
            print(f"  Line {lineno}: {kind}\n    {snippet}")
        print("")

    print("Run the checker again after addressing the reported call sites.")
    return 2


if __name__ == '__main__':
    rc = main()
    sys.exit(rc)
