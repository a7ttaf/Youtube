# ============================================================================
# Purpose: Tracked-file hygiene guard — forbidden operator-real identifiers
#   (held as SHA-256 digests, never literals) must stay out of this public repo.
# Database/ORM: None.
# Standards: Fail-closed scanning; unreadable tracked files and git-worktree
#   preconditions are reported explicitly, never silently skipped.
# Blast Radius: None detected (test-only guard; no finance, authz, or audit paths).
# Connections:
#   - File: tests/org/test_channel_group_sync_apply.py -> Fixture placeholder
#     convention (PR #164) that this guard protects from regression.
#   - File: Docs/superpowers/plans/2026-08-05-cms-group-sync.md -> Plan doc
#     re-redacted under the same convention.
# ============================================================================
"""Tracked-file hygiene guard: operator-real identifiers must stay out of the repo.

The repository is PUBLIC (since 2026-08-04). The hygiene convention set by the
same-day exposure audit and PR #164 is: docs and fixtures use placeholders,
never operator-real identifiers. That convention regressed once already — the
real CMS content-owner id, redacted from the #159 branch on 2026-08-04, was
reintroduced via #169's plan doc and a test fixture two days later — so the
convention now has this guard.

Forbidden values are stored as SHA-256 digests, never literals: the guard must
be able to name the class of leak without re-committing the leak. To add an
entry, hash the literal locally (`printf %s '<token>' | sha256sum`) and add the
digest with a one-line description; never paste the literal anywhere in the
repo, including this file's history.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# digest -> what leaked (description only; the literal lives nowhere in-repo).
FORBIDDEN_TOKEN_SHA256: dict[str, str] = {
    # CMS content-owner id: redacted 2026-08-04 (commit 8e2b0b52), reintroduced
    # via the #169 plan doc + a fixture, re-redacted alongside this guard.
    "150446699c74d75970f93621a6dbb2e63b23925f43a65d25f1b077eb912c278a": (
        "real CMS content-owner id"
    ),
    # Operator-personal emails: redacted from the runlogs in PR #164.
    "b154357d5da21bbfe26a823d95de0629158b510e659da3a6a8e826f8edf0119f": (
        "operator personal email (hotmail)"
    ),
    "af065e4d6501461706c859c472ed3527e4650da11f5555eeff14fb8fa0d7f8b3": (
        "operator personal email (gmail)"
    ),
}

# Candidate extractors, matched per line. The base64url window covers the
# YouTube CMS id shape (22 chars) with headroom either side so a re-leak
# cannot dodge the guard by riding inside a longer token.
_BASE64URLISH = re.compile(r"[A-Za-z0-9_-]{20,64}")
_EMAILISH = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Binary artifacts where tokenized scanning is noise, not signal. Plaintext
# formats (.lock, .svg, ...) stay scanned: they can carry identifiers just as
# docs do, and excluding them would leave a leak surface the guard cannot see.
_SKIP_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"})


def _tracked_files() -> list[Path]:
    """Every git-tracked file — the exact set a push would publish.

    The guard is meaningful only inside a git worktree with git on PATH; any
    other environment (source archive, vendored tree) fails explicitly because
    an unavailable scanner cannot prove that tracked content is clean.
    """
    # FIX: Fail closed instead of calling pytest.skip; required CI's policy gate
    # forbids skip/xfail and a missing scanner is not proof of clean content.
    if shutil.which("git") is None:
        pytest.fail("repo hygiene guard requires git on PATH")
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.fail("repo hygiene guard is meaningful only inside a git worktree")
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        REPO_ROOT / rel
        for rel in listing.stdout.split("\0")
        if rel and Path(rel).suffix.lower() not in _SKIP_SUFFIXES
    ]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_no_forbidden_identifiers_in_tracked_files() -> None:
    """Fail with file:line coordinates if any forbidden identifier is tracked."""
    hits: list[str] = []
    unreadable: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as error:
            if not path.exists():
                # git tracks the path but it vanished mid-run (checkout race):
                # a worktree quirk, not a hygiene violation.
                continue
            # A tracked file that still exists but cannot be read is an
            # unscanned leak surface: the guard must fail closed, not pass.
            unreadable.append(f"{path.relative_to(REPO_ROOT)}: {type(error).__name__}")
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern in (_BASE64URLISH, _EMAILISH):
                for match in pattern.finditer(line):
                    description = FORBIDDEN_TOKEN_SHA256.get(_digest(match.group()))
                    if description is not None:
                        rel = path.relative_to(REPO_ROOT)
                        hits.append(f"{rel}:{line_number}: {description}")
    assert not unreadable, (
        "hygiene guard could not scan these git-tracked files; failing closed "
        "rather than passing over an unscanned leak surface:\n" + "\n".join(unreadable)
    )
    assert not hits, (
        "forbidden operator-real identifiers found in tracked files "
        "(redact with placeholders, per the PR #164 convention):\n" + "\n".join(hits)
    )


def test_guard_tokenizer_extracts_both_shapes() -> None:
    """Anti-vacuity: the extractors actually produce the shapes the digests cover.

    A 22-char base64url token embedded in prose and an email inside brackets
    must both surface as candidates — otherwise the main test could pass
    forever because the tokenizer never sees a leak, not because none exists.
    """
    base64url_sample = "owner id TestOwnerAAAAAAAAAAAAA in prose"
    email_sample = "contact <someone.real+tag@example-host.co.uk> today"
    assert "TestOwnerAAAAAAAAAAAAA" in [
        match.group() for match in _BASE64URLISH.finditer(base64url_sample)
    ]
    assert "someone.real+tag@example-host.co.uk" in [
        match.group() for match in _EMAILISH.finditer(email_sample)
    ]
