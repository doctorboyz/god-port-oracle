#!/usr/bin/env python3
"""Pre-deploy SQL and scope validation — called by pre-deploy-check.sh"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

errors = 0


def check_sql_placeholders():
    """Validate INSERT statements have matching column/value counts."""
    global errors
    db_file = ROOT / "metty" / "core" / "db.py"
    if not db_file.exists():
        print("  ⚠ db.py not found, skipping SQL check")
        return

    content = db_file.read_text()

    # Find INSERT INTO ... (columns) VALUES (?, ?, ...) patterns
    # Use greedy match for VALUES content to capture multi-line and function calls
    # Skip INSERTs that use f-strings or tuple substitution
    inserts = list(re.finditer(
        r'INSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\((.+?)\)\s*\"\"\",\s*\(',
        content, re.DOTALL,
    ))

    for m in inserts:
        table = m.group(1)
        cols_text = m.group(2)
        vals_text = m.group(3)

        # Skip dynamic INSERTs (f-strings, column variables)
        if "{" in vals_text or "columns" in vals_text or "values" in vals_text.lower():
            print(f"  ⊘ {table}: dynamic INSERT (skipped)")
            continue

        # Count columns
        cols = [c.strip() for c in cols_text.split(",") if c.strip()]
        n_cols = len(cols)

        # Count ? placeholders
        q_marks = vals_text.count("?")

        # Count function calls like datetime('now') as single values
        func_calls = len(re.findall(r"\w+\([^)]*\)", vals_text))

        # Remove function calls, then count remaining literals
        vals_no_funcs = re.sub(r"\w+\([^)]*\)", "FUNC_CALL", vals_text)
        literals = len(re.findall(r"\b(?:\d+|None|True|False|NULL)\b|'[^']*'", vals_no_funcs))

        n_vals = q_marks + func_calls + literals

        if n_cols != n_vals:
            print(f"  ✗ {table}: {n_cols} columns but {n_vals} values ({q_marks}? + {literals} literals)")
            print(f"    Columns: {cols}")
            print(f"    VALUES: {vals_text.strip()[:200]}")
            errors += 1
        else:
            print(f"  ✓ {table}: {n_cols} columns = {n_vals} values ({q_marks}? + {literals} literals)")

    # Special check: all INSERT INTO live_trades
    live_inserts = list(re.finditer(
        r"INSERT\s+INTO\s+live_trades\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)",
        content, re.DOTALL,
    ))
    for m in live_inserts:
        cols_text = m.group(1)
        vals_text = m.group(2)
        if "{" in vals_text:
            print("  ⊘ live_trades INSERT: dynamic (skipped)")
            continue

        cols = [c.strip() for c in cols_text.split(",") if c.strip()]
        q = vals_text.count("?")
        fcs = len(re.findall(r"\w+\([^)]*\)", vals_text))
        vals_no_funcs = re.sub(r"\w+\([^)]*\)", "FUNC_CALL", vals_text)
        lits = len(re.findall(r"\b(?:\d+|None|True|False|NULL)\b|'[^']*'", vals_no_funcs))
        total = q + fcs + lits

        if len(cols) != total:
            print(f"  ✗ live_trades INSERT: {len(cols)} columns vs {total} values")
            errors += 1
        else:
            print(f"  ✓ live_trades INSERT: {len(cols)} columns = {total} values ({q}? + {lits} literals)")


def check_scope():
    """Check that key variables used in run_once() are defined there."""
    global errors
    vars_to_check = ["h4_trend", "d1_trend", "session", "regime"]

    trader_files = [
        ROOT / "metty" / "execution" / "live_trader.py",
        ROOT / "metty" / "execution" / "scalp_trader.py",
        ROOT / "metty" / "execution" / "m5_scalp_trader.py",
    ]

    for filepath in trader_files:
        if not filepath.exists():
            continue
        source = filepath.read_text()
        tree = ast.parse(source)

        # Find run_once method
        run_once = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_once":
                run_once = node
                break

        if run_once is None:
            continue

        # Collect all variables assigned in run_once
        assigned = set()
        for node in ast.walk(run_once):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)
                    elif isinstance(target, ast.Tuple):
                        for elt in target.elts:
                            if isinstance(elt, ast.Name):
                                assigned.add(elt.id)
            elif isinstance(node, ast.AugAssign):
                if isinstance(node.target, ast.Name):
                    assigned.add(node.target.id)
            elif isinstance(node, ast.For):
                if isinstance(node.target, ast.Name):
                    assigned.add(node.target.id)
                elif isinstance(node.target, ast.Tuple):
                    for elt in node.target.elts:
                        if isinstance(elt, ast.Name):
                            assigned.add(elt.id)

        # Collect self.X attributes available
        self_attrs = set()
        for node in ast.walk(run_once):
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == "self":
                    self_attrs.add(node.attr)

        # Check each variable
        for var in vars_to_check:
            # Is this variable referenced in run_once?
            used = False
            for node in ast.walk(run_once):
                if isinstance(node, ast.Name) and node.id == var:
                    used = True
                    break
                if isinstance(node, ast.keyword) and node.arg == var:
                    if isinstance(node.value, ast.Name) and node.value.id == var:
                        used = True
                        break

            if not used:
                continue  # Variable not used in this method, skip

            if var in assigned:
                print(f"  ✓ {filepath.name}: {var} defined in run_once()")
            elif var in self_attrs:
                print(f"  ✓ {filepath.name}: {var} available via self.{var}")
            else:
                # Check if self.X = var is assigned somewhere
                found_self_assign = False
                for node in ast.walk(run_once):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if (isinstance(target, ast.Attribute)
                                    and isinstance(target.value, ast.Name)
                                    and target.value.id == "self"
                                    and target.attr == var):
                                found_self_assign = True

                if found_self_assign:
                    print(f"  ✓ {filepath.name}: {var} stored in self.{var}")
                else:
                    print(f"  ✗ {filepath.name}: {var} used in run_once() but NOT defined in scope")
                    print(f"    → Add \"{var} = ...\" assignment or use self.{var}")
                    errors += 1


if __name__ == "__main__":
    print("[3/5] SQL placeholder validation...")
    check_sql_placeholders()

    print("[4/5] Scope check (variable definitions)...")
    check_scope()

    if errors > 0:
        print(f"\n{errors} check(s) failed")
        sys.exit(1)
    else:
        print("\nSQL + scope checks passed")
        sys.exit(0)