#!/usr/bin/env python3
"""Fail the build if a credential looks committed.

This exists because of one specific hazard: an n8n workflow exported after
a token was typed directly into an HTTP node carries that token in the JSON.
The workflow files here reference $env instead, and this check keeps it that
way after someone edits a workflow in the UI and re-exports it.

The patterns are prefix-based on purpose. Matching "anything that looks like
a long random string" would flag every hash in the repo and get switched off
within a week.

Run locally:  python scripts/check_no_secrets.py
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", ".idea", ".vscode",
}
SKIP_FILES = {"check_no_secrets.py"}
TEXT_SUFFIXES = {
    ".py", ".json", ".yml", ".yaml", ".md", ".toml", ".txt",
    ".env", ".example", ".sh", ".ts", ".js", "",
}

PATTERNS = [
    ("HubSpot private app token", re.compile(r"\bpat-[a-z0-9]{2,4}-[A-Za-z0-9-]{20,}")),
    ("HubSpot legacy API key", re.compile(r"\bhapikey=[A-Za-z0-9-]{20,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9/]{20,}")),
    ("Discord webhook", re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d{17,}/[\w-]{60,}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "Authorization: Bearer with a literal value",
        # An expression such as {{ $env.X }} is fine; a literal is not.
        re.compile(r"[Bb]earer\s+(?!\{\{)(?!\$)[A-Za-z0-9._-]{24,}"),
    ),
]


def tracked_files() -> list[Path] | None:
    """Ask git what is actually tracked.

    Scanning the working tree is the wrong question. A real `.env` holding a
    real token is correct and expected -- it is gitignored. Flagging it
    teaches the reader to ignore this script, which is how the one genuine
    finding later gets waved through. What matters is what git would publish.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [ROOT / name for name in result.stdout.split("\0") if name]


def iter_files():
    candidates = tracked_files()
    if candidates is None:
        # Not a git checkout (a release tarball, say). Fall back to walking
        # the tree, minus the directories that never hold source.
        candidates = [p for p in ROOT.rglob("*") if p.is_file()]

    for path in candidates:
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path


def main() -> int:
    findings: list[str] = []
    scanned = 0

    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS:
                match = pattern.search(line)
                if match:
                    shown = match.group(0)[:12]
                    findings.append(
                        f"{path.relative_to(ROOT)}:{line_number}: {label} "
                        f"(starts '{shown}...')"
                    )

    if findings:
        print("Possible committed credentials:\n", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nRotate anything real, remove it from the file, and use an "
            "environment variable instead.",
            file=sys.stderr,
        )
        return 1

    print(f"No credential patterns found in {scanned} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
