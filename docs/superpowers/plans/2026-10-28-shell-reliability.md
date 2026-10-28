# Shell Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Improve the existing Shell transfer tools for older Linux machines without changing the Python implementation.

**Architecture:** Retain two Bash entry points and sequential transfer. Isolate upload scratch files and download resume state; publish manifests and restored files only after success.

**Tech Stack:** Bash 4.2, curl 7.29, GNU coreutils, awk, sed.

**Spec:** `docs/superpowers/specs/2026-10-28-shell-reliability-design.md`

## Global Constraints

- Baseline: Bash 4.2 and curl 7.29 on CentOS 7/8, Rocky Linux and Ubuntu.
- Do not modify `file-transfer.py`.
- No parallel transfer scheduler or Python runtime dependency.

## Review Focus

- Existing manifest and target survive network and checksum failures.
- Names with spaces, leading dashes and legacy Windows paths are handled safely.
- CRLF, malformed manifests and Python metadata are handled before network access.
- Interrupted downloads reuse only verified chunks, with isolated state and locking.
- Empty files, ambiguous upload responses and exhausted retries have explicit results.

## Task 1: Reliable upload

**Files:** `file-push.sh`, `tests/file-transfer-test.sh`.
**Interface:** `file-push.sh FILE [--url URL] [--auth VALUE] [--manifest PATH] [--chunk-size SIZE] [--retries N] [--expires SECONDS]`.

- [x] Replace function-level transfer stubs with executable curl fixtures; add assertions for old manifest preservation after HTTP failure, non-fixed upload domains, retries, spaces and leading dashes, empty files and isolated scratch files.
- [x] Run the new tests against the old script and confirm meaningful failures.
- [x] Implement argument validation, environment fallback, private splitting, finite retries and atomic manifest publication, preserving source file checksum consistency.
- [x] Run upload regressions and Bash syntax checks.

## Task 2: Reliable download

**Files:** `file-pull.sh`, `tests/file-transfer-test.sh`.
**Interface:** `file-pull.sh [MANIFEST] [--output PATH] [--auth VALUE] [--retries N]`.

- [x] Add assertions for nonzero failure exit, existing target preservation, verified chunk reuse, corrupt chunk redownload, final checksum mismatch, CRLF, Python metadata, malformed manifests, empty files and legacy basename handling.
- [x] Run against the old script and confirm expected failures.
- [x] Implement full manifest validation, isolated locked state, verified chunk reuse, ordered assembly and atomic destination replacement; remove sysctl changes and insecure TLS defaults.
- [x] Run the complete Shell suite and syntax checks.

## Task 3: Documentation and final verification

**Files:** `README.md`, the two Shell scripts and regression suite.

- [x] Document dependencies, options, defaults, legacy environment fallback, resume behavior, one-time links and migration examples.
- [x] Run `bash tests/file-transfer-test.sh` and `python -m unittest discover -s tests -p "test_*.py" -v`; use a local HTTP fixture if a suitable runtime is available.
- [x] Review `git diff --check` and confirm `git diff -- file-transfer.py` is empty; report any runtime or distribution verification limitations.
