---
name: scope-verification-sql-placeholders
description: When changing variable references, verify the new variable exists in the same scope. SQL INSERT placeholders must be counted against column names.
metadata:
  type: learning
  source: rrr: god-port-oracle
  date: 2026-06-04
---

# Scope Verification + SQL Placeholder Counting

## Context
Fixed two production-only bugs: (1) `h4_trend` NameError — changed `self._last_h4_trend` to `h4_trend` but `h4_trend` was only defined in `_generate_signal()`, not in `run_once()` where it was referenced. (2) INSERT INTO live_trades had 30 column names but only 29 values (28 `?` + 1 literal).

## Lesson 1: Scope Verification
When changing a variable reference from `self._last_h4_trend` to `h4_trend`, you must verify that `h4_trend` is defined in the **same scope** where it's used. Python methods have separate local scopes — a variable defined in `_generate_signal()` is not accessible in `run_once()`.

**Checklist**: After any find-and-replace fix, trace the variable definition path:
1. Where is the variable defined?
2. Where is it used?
3. Are definition and usage in the same scope?

## Lesson 2: SQL Placeholder Counting
INSERT INTO table (col1, col2, ... colN) VALUES (?, ?, ..., literal, ?, ...)
Must have exactly N total values (count of `?` + count of literals = count of columns).

**Prevention**: Write a test that calls the INSERT function with all parameters and verifies the row was written correctly. Or add a linter that counts `?` vs column names.

## Related
- [[ml-train-serve-skew]] — previous session's ML parameter bugs
- [[decorator-import-side-effects]] — scope-related issues