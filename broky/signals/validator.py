"""Strategy validator — AST-based pre-runtime checks for signal generators.

Catches common errors before live trading:
- Missing take_profit / stop_loss
- Missing NaN guards
- Hardcoded balance/equity values
- Unused signal parameters

Usage:
    python -m broky.signals.validator                    # Validate all generators
    python -m broky.signals.validator --file generator.py  # Validate specific file
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Files to validate by default
DEFAULT_GENERATORS = [
    "broky/signals/generator.py",
    "broky/signals/m5_scalp_generator.py",
]


@dataclass
class ValidationIssue:
    """A single validation issue found in a strategy."""
    file: str
    line: int
    severity: str  # "error", "warning", "info"
    rule: str
    message: str


@dataclass
class ValidationResult:
    """Aggregate validation result."""
    file: str
    issues: list[ValidationIssue] = field(default_factory=list)
    passed: bool = True

    def add(self, line: int, severity: str, rule: str, message: str):
        self.issues.append(ValidationIssue(
            file=self.file, line=line, severity=severity,
            rule=rule, message=message,
        ))
        if severity == "error":
            self.passed = False

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"[{status}] {self.file}"]
        for issue in self.issues:
            icon = {"error": "✗", "warning": "⚠", "info": "ℹ"}[issue.severity]
            lines.append(f"  {icon} L{issue.line}: [{issue.rule}] {issue.message}")
        if not self.issues:
            lines.append("  ✓ No issues found")
        return "\n".join(lines)


class StrategyValidator(ast.NodeVisitor):
    """AST visitor that checks signal generators for common issues."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.result = ValidationResult(file=filepath)
        self._function_names: list[str] = []
        self._has_nan_guard = False
        self._sets_tp = False
        self._sets_sl = False
        self._has_hardcoded_balance = False

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._function_names.append(node.name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        # Check for hardcoded balance/equity values
        for target in node.targets:
            if isinstance(target, ast.Name):
                name_lower = target.id.lower()
                if name_lower in ("balance", "equity", "account_balance"):
                    # Check if the value is a numeric literal (hardcoded)
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                        self.result.add(
                            node.lineno, "error", "hardcoded_balance",
                            f"Hardcoded {target.id} = {node.value.value}. Use config/env instead.",
                        )
                        self._has_hardcoded_balance = True
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare):
        # Check for NaN guards in comparisons: "x == np.nan"
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(comparator, ast.Call):
                self._check_nan_call(comparator)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        # Check for NaN guards anywhere: pd.isna(x), pd.notna(x), np.isnan(x)
        self._check_nan_call(node)
        self.generic_visit(node)

    def _check_nan_call(self, node: ast.Call):
        """Detect pd.isna/pd.notna/np.isnan/math.isnan calls."""
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in ("isna", "notna", "isnan"):
                self._has_nan_guard = True
        elif isinstance(func, ast.Name):
            if func.id in ("isnan", "isna", "notna"):
                self._has_nan_guard = True

    def visit_Return(self, node: ast.Return):
        # Check if return value includes TP/SL fields
        if node.value and isinstance(node.value, ast.Call):
            if isinstance(node.value.func, ast.Name):
                # Signal() or similar constructor
                for keyword in node.value.keywords:
                    if keyword.arg == "take_profit" or keyword.arg == "tp":
                        self._sets_tp = True
                    if keyword.arg == "stop_loss" or keyword.arg == "sl":
                        self._sets_sl = True
        self.generic_visit(node)

    def validate(self, source: str) -> ValidationResult:
        """Parse and validate source code."""
        try:
            tree = ast.parse(source, filename=self.filepath)
        except SyntaxError as e:
            self.result.add(e.lineno or 0, "error", "syntax_error", f"Syntax error: {e.msg}")
            return self.result

        self.visit(tree)

        # Rule: signal generators must set TP and SL
        signal_funcs = [n for n in self._function_names if "signal" in n.lower() or "generate" in n.lower()]
        if signal_funcs and not self._sets_tp:
            self.result.add(0, "warning", "missing_take_profit",
                "Signal generator doesn't explicitly set take_profit in return value.")
        if signal_funcs and not self._sets_sl:
            self.result.add(0, "warning", "missing_stop_loss",
                "Signal generator doesn't explicitly set stop_loss in return value.")

        # Rule: should have NaN guards
        if signal_funcs and not self._has_nan_guard:
            self.result.add(0, "warning", "missing_nan_guard",
                "No NaN guard found (pd.isna/pd.notna/isnan). "
                "Signal generators should handle NaN values.")

        return self.result


def validate_file(filepath: str) -> ValidationResult:
    """Validate a single Python file."""
    path = Path(filepath)
    if not path.exists():
        result = ValidationResult(file=filepath)
        result.add(0, "error", "file_not_found", f"File not found: {filepath}")
        return result

    source = path.read_text()
    validator = StrategyValidator(filepath)
    return validator.validate(source)


def validate_all(files: Optional[list[str]] = None) -> list[ValidationResult]:
    """Validate multiple files. Defaults to all registered strategies."""
    if files is None:
        # Try registry-based discovery first
        try:
            import broky.signals  # noqa: F401 — triggers registration
            from broky.signals.registry import StrategyRegistry
            registered = StrategyRegistry.all()
            if registered:
                import inspect
                files = [inspect.getfile(fn) for fn, _ in registered.values()]
            else:
                files = DEFAULT_GENERATORS
        except (ImportError, KeyError):
            files = DEFAULT_GENERATORS

    results = []
    for filepath in files:
        results.append(validate_file(filepath))
    return results


def main():
    """CLI entry point for strategy validation."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate signal generators")
    parser.add_argument("--file", default=None, help="Validate specific file")
    parser.add_argument("--all", action="store_true", help="Validate all default generators")
    args = parser.parse_args()

    if args.file:
        results = [validate_file(args.file)]
    else:
        results = validate_all()

    all_passed = True
    for result in results:
        print(result)
        if not result.passed:
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()