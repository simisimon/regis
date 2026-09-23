"""Save commits matching both message keywords and file patterns as JSON Lines."""

from fnmatch import fnmatchcase
import json
from pathlib import Path

from pydriller import Repository

file_patterns = [
    "pom.xml",
    "Dockerfile*",
    "*compose*.yml",
    "*compose*.yaml",
    "application*.properties",
    "application*.yml",
    "application*.yaml",
]
keywords = ["config", "error", "misconfig", "fix", "bug", "issue"]

repo_url = "https://github.com/sqshq/PiggyMetrics"
output_file = Path(__file__).resolve().parents[1] / "data" / "PiggyMetrics_config_commits.jsonl"


def matches_file(modified_file, patterns):
    """Match both paths so deleted and renamed configuration files are included.

    Patterns without a slash match filenames at any depth; patterns with a slash
    match repository-relative paths. Wildcards can span directory separators.
    """
    return any(
        fnmatchcase(path if "/" in pattern else path.rsplit("/", 1)[-1], pattern)
        for path in (modified_file.old_path, modified_file.new_path)
        if path is not None
        for pattern in patterns
    )


def matching_commits(repository, patterns, message_keywords):
    """Yield matching commits and their matching files, once per commit.

    Keywords are case-insensitive literal substrings; file patterns are
    case-sensitive wildcards. Both criteria must match. PyDriller may return no
    modified files for merge commits, so those commits cannot match the filter.
    """
    normalized_keywords = [word.casefold() for word in message_keywords if word.strip()]
    for commit in Repository(repository).traverse_commits():
        if not any(word in commit.msg.casefold() for word in normalized_keywords):
            continue
        matching_files = [
            modified_file
            for modified_file in commit.modified_files
            if matches_file(modified_file, patterns)
        ]
        if matching_files:
            yield commit, matching_files


def save_commits(repository, patterns, message_keywords, destination):
    """Write one JSON object per commit, including diffs for all changed files.

    Each file records its old/new paths, change type, diff, and whether it matched
    a configuration pattern. Missing paths (additions/deletions) become JSON null.
    previous_commit_hash is the first Git parent, not the previous matching
    commit; it is null for root commits. parent_hashes includes every parent.
    The destination is replaced on each run; completed records are flushed so
    they remain readable if mining is interrupted.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8") as output:
        for commit, matching_files in matching_commits(repository, patterns, message_keywords):
            matching_paths = {(file.old_path, file.new_path) for file in matching_files}
            parents = commit.parents
            record = {
                "repository": str(repository),
                "commit_hash": commit.hash,
                "previous_commit_hash": parents[0] if parents else None,
                "parent_hashes": parents,
                "message": commit.msg,
                "files": [
                    {
                        "old_path": file.old_path,
                        "new_path": file.new_path,
                        "change_type": file.change_type.name,
                        "matches_config_pattern": (file.old_path, file.new_path) in matching_paths,
                        "diff": file.diff,
                    }
                    for file in commit.modified_files
                ],
            }
            output.write(json.dumps(record, ensure_ascii=True) + "\n")
            output.flush()
            count += 1
    return count


if __name__ == "__main__":
    count = save_commits(repo_url, file_patterns, keywords, output_file)
    print(f"Saved {count} matching commits to {output_file}")
