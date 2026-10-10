"""Pre-publish safety check: refuse to ship anything large, private or machine-specific.

Run before every push, and certainly before making the repo public:

    python scripts/check_repo_safe.py
    python scripts/check_repo_safe.py --history    # also scan every past commit

It inspects the files git would actually track (so .gitignore is respected) and
fails on oversized files, weights, images, absolute local paths, emails,
hostnames, IP addresses and credential-shaped strings.

This is a backstop, not a guarantee. It cannot know that a piece of prose is
sensitive. Read your own diff before publishing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 1 * 1024 * 1024  # nothing first-party here should approach 1 MB

BANNED_SUFFIXES = {
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif",
    ".heic", ".avif", ".psd", ".cr2", ".dng", ".arw", ".nef", ".raw",
    ".mp4", ".webm", ".mov",
    ".db", ".sqlite", ".sqlite3", ".log", ".key", ".pem", ".pfx", ".p12",
}

# Whole filenames that carry no suffix and so slip past BANNED_SUFFIXES.
BANNED_NAMES = {".env", ".npmrc", ".netrc", "_netrc", "id_rsa", "id_ed25519"}

# Images deliberately committed as documentation art.
ALLOWED_IMAGE_DIRS = ("docs/img/",)

PATTERNS = [
    # Local paths. Both separators, trailing slash optional.
    ("Windows user path", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9._-]+", re.I)),
    ("UNC path", re.compile(r"\\\\[A-Za-z0-9._-]+\\[A-Za-z0-9._$-]+")),
    ("Unix home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")),
    # Identity. Local part must start alphanumeric, or a diff line like
    # "+@decorator.something.here" reads as an address.
    ("email address", re.compile(
        r"\b[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}\b")),
    ("Windows SID", re.compile(r"\bS-1-5-21(?:-\d+){3,}\b")),
    ("machine name", re.compile(r"\b(?:DESKTOP|LAPTOP|WIN)-[A-Z0-9]{7}\b")),
    ("GPU UUID", re.compile(r"\bGPU-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.I)),
    # Network
    ("IPv4 address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Credentials
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("HuggingFace token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # Quoted OR bare assignment - a real .env uses KEY=value with no quotes.
    ("secret assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|credential)\b\s*[:=]\s*"
        r"(?:['\"][^'\"\n]{8,}['\"]|[^\s'\"#][^\s'\"#]{7,})")),
]

# Literal values that are meant to appear: doc placeholders, loopback, and the
# repo owner's own public identity. Add your own here rather than weakening a
# pattern - and keep it to things you are happy to see on a public page.
ALLOWED = [
    "you@example.com",
    "noreply@anthropic.com",
    # GitHub's per-account noreply form, which is what commits here are authored as
    "@users.noreply.github.com",
    "C:\\Users\\<you>",
    "C:\\Users\\you",
    "127.0.0.1",
    "0.0.0.0",
]

# Loopback and version-like strings the IPv4 rule would otherwise trip on.
IP_OK = re.compile(r"^(?:127\.|0\.0\.0\.0$|255\.|10\.0\.0\.0$)")


def normalize(s: str) -> str:
    """Collapse escaped backslash runs so a source literal matches its parsed form."""
    return re.sub(r"\\+", "\\\\", s.lower())


ALLOWED_NORM = [normalize(a) for a in ALLOWED]


def is_allowed(hit: str, label: str) -> bool:
    h = normalize(hit)
    for a in ALLOWED_NORM:
        # An entry beginning with '@' allows a whole mail domain.
        if a.startswith("@") and h.endswith(a):
            return True
        # Exact, or a prefix that ends at a separator. A placeholder ending in
        # "you" must therefore not whitelist a real username starting "you".
        if h == a:
            return True
        if h.startswith(a) and len(h) > len(a) and not (h[len(a)].isalnum() or h[len(a)] == "_"):
            return True
    if label == "IPv4 address" and IP_OK.match(hit):
        return True
    return False


def tracked_files(repo: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    return [repo / line for line in out if line.strip()]


def scan_text(text: str, where: str, problems: list[str]) -> None:
    for label, rx in PATTERNS:
        for m in rx.finditer(text):
            hit = m.group(0)
            if is_allowed(hit, label):
                continue
            line = text[:m.start()].count("\n") + 1
            problems.append(f"{where}:{line}: {label}: {hit[:120]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", action="store_true",
                    help="also scan every commit's diff (catches things removed but not purged)")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    problems: list[str] = []
    checked = 0

    for path in tracked_files(repo):
        if not path.is_file():
            continue
        rel = path.relative_to(repo).as_posix()
        checked += 1

        size = path.stat().st_size
        if size > MAX_BYTES:
            problems.append(f"{rel}: {size/1024/1024:.1f} MB exceeds the {MAX_BYTES//1024//1024} MB limit")

        doc_art = rel.startswith(ALLOWED_IMAGE_DIRS)
        if (path.suffix.lower() in BANNED_SUFFIXES or path.name.lower() in BANNED_NAMES) and not doc_art:
            problems.append(f"{rel}: '{path.name}' is a type that must never be tracked")
            continue
        if doc_art:
            continue  # binary documentation art: size-checked above, not text-scanned

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            problems.append(f"{rel}: not valid UTF-8 text - is this a binary that slipped through?")
            continue

        scan_text(text, rel, problems)

    if args.history:
        print("scanning commit history...")
        diff = subprocess.run(["git", "log", "-p", "--all", "--no-color"],
                              cwd=repo, capture_output=True, text=True, errors="replace").stdout
        hist: list[str] = []
        scan_text(diff, "<history>", hist)
        # History lines are noisy; dedupe by the finding itself.
        seen = set()
        for h in hist:
            key = h.split(": ", 1)[-1]
            if key not in seen:
                seen.add(key)
                problems.append(h.replace("<history>:", "<history> line "))

    print(f"checked {checked} tracked file(s)" + (" + full history" if args.history else "") + "\n")
    if problems:
        print("NOT SAFE TO PUBLISH:\n")
        for p in problems:
            print(f"  {p}")
        print(f"\n{len(problems)} problem(s). Fix these before pushing.")
        return 1

    print("Clean: no large files, no weights, no images, no local paths, no secrets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
