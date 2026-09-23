#!/usr/bin/env python3
"""Clone projects temporarily and export configuration-related commits as JSONL."""

import argparse
import csv
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


DATA_DIR = Path(__file__).resolve().parent / "data"

# Edit these lists to select commits. Either list can be empty, but not both.
CONFIG_FILE_PATTERNS = [
    "pom.xml",
    "Dockerfile*",
    "*compose*.yml",
    "*compose*.yaml",
    "application*.properties",
    "application*.yml",
    "application*.yaml",
    ".env*",
]

CONFIG_KEYWORDS = [
    "config",
    "port",
    "environment",
]


def git(*args, cwd=None):
    """Run Git without a shell or interactive credential prompts."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8", errors="replace")


def matches_path(path, patterns):
    # Patterns without a slash match basenames at any depth. Otherwise match
    # repository-relative paths; fnmatch wildcards can span directory separators.
    return any(
        fnmatch.fnmatchcase(path if "/" in pattern else path.rsplit("/", 1)[-1], pattern)
        for pattern in patterns
    )


def extract(repo, revision_args, patterns, keywords):
    """Yield each matching commit once, including both reasons when applicable."""
    log = git(
        "log", "-z", "--format=%H%x00%P%x00%aI%x00%B",
        *revision_args, "--", cwd=repo,
    )
    fields = log.split("\0")
    if fields[-1] == "":
        fields.pop()
    if len(fields) % 4:
        raise RuntimeError("Unexpected Git log record format")
    for index in range(0, len(fields), 4):
        sha, parent_text, authored_at, message = fields[index:index + 4]
        parents = parent_text.split()
        # Compare merges against their first parent. Treat renames as deletion
        # plus addition so both old and new configuration paths are checked.
        revisions = [parents[0], sha] if parents else [sha]
        changed = git(
            "diff-tree", "--root", "--no-commit-id", "--name-only", "-z",
            "-r", "--no-renames", *revisions, "--", cwd=repo,
        )
        changed_files = sorted(set(filter(None, changed.split("\0"))))
        matched_files = [p for p in changed_files if matches_path(p, patterns)]
        matched_keywords = [k for k in keywords if k.casefold() in message.casefold()]
        if matched_files or matched_keywords:
            yield {
                "commit": sha,
                "parents": parents,
                "authored_at": authored_at,
                "message": message,
                "match_reasons": (["file"] if matched_files else [])
                + (["message"] if matched_keywords else []),
                "matched_files": matched_files,
                "matched_keywords": matched_keywords,
                "changed_files": changed_files,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path,
                        default=DATA_DIR / "microservice_projects_filtered.csv")
    parser.add_argument("--output", type=Path,
                        default=DATA_DIR / "config_commits.jsonl")
    parser.add_argument("--history", choices=["all", "head", "dataset"], default="all",
                        help="All cloned branches/tags (default), default branch, or ancestors "
                        "of the CSV commit_hash.")
    parser.add_argument("--temp-dir", type=Path,
                        help="Parent directory for temporary clones (default: system temp).")
    args = parser.parse_args()
    if not CONFIG_FILE_PATTERNS and not CONFIG_KEYWORDS:
        parser.error("Add at least one entry to CONFIG_FILE_PATTERNS or CONFIG_KEYWORDS.")
    if any(not isinstance(item, str) or not item.strip()
           for item in CONFIG_FILE_PATTERNS + CONFIG_KEYWORDS):
        parser.error("File patterns and keywords must be non-empty strings.")
    if args.input.resolve() == args.output.resolve():
        parser.error("Input and output must be different files.")

    csv.field_size_limit(10**8)
    with args.input.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source, delimiter=";")
        required = {"URL"} | ({"commit_hash"} if args.history == "dataset" else set())
        missing = required - set(reader.fieldnames or [])
        if missing:
            parser.error(f"Missing CSV columns: {', '.join(sorted(missing))}")
        projects = list(reader)

    failures = []
    total = 0
    # Exclusive creation protects previous extraction results from overwriting.
    with args.output.open("x", encoding="utf-8") as output:
        for index, project in enumerate(projects, 1):
            url = (project.get("URL") or "").strip()
            print(f"[{index}/{len(projects)}] {url}", file=sys.stderr, flush=True)
            try:
                if not url or url.startswith("-"):
                    raise ValueError("Missing or invalid repository URL")
                with tempfile.TemporaryDirectory(prefix="config-commits-", dir=args.temp_dir) as tmp:
                    repo = Path(tmp) / "repository.git"
                    git("clone", "--bare", "--", url, str(repo))
                    if args.history == "all":
                        revisions = ["--all"]
                    elif args.history == "head":
                        revisions = ["HEAD"]
                    else:
                        endpoint = (project.get("commit_hash") or "").strip()
                        if not endpoint or endpoint.startswith("-"):
                            raise ValueError("Missing or invalid dataset commit_hash")
                        revisions = [git("rev-parse", "--verify", "--end-of-options",
                                         endpoint + "^{commit}", cwd=repo).strip()]
                    count = 0
                    for record in extract(repo, revisions, CONFIG_FILE_PATTERNS, CONFIG_KEYWORDS):
                        record.update(repository=url, project=project.get("Identifier", ""),
                                      history=args.history)
                        output.write(json.dumps(record, ensure_ascii=True) + "\n")
                        output.flush()
                        count += 1
                        total += 1
                    print(f"  {count} matching commits; temporary clone removed on exit.",
                          file=sys.stderr, flush=True)
            except (RuntimeError, ValueError, OSError) as exc:
                failures.append(url)
                print(f"  ERROR: {exc}", file=sys.stderr, flush=True)
    print(f"Saved {total} commits to {args.output}; {len(failures)} repositories failed.")
    if failures:
        print("Failed repositories: " + ", ".join(failures), file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted; temporary clone cleaned up. Partial output retained.", file=sys.stderr)
        sys.exit(130)
