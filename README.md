### Data

*microservice_projects_all.csv*: dataset from "A Dataset of Microservices-based Open-Source Projects"

### Extract configuration-related commits

Requires Python 3 and Git; no Python packages are needed. First run
`python3 filter_projects.py` to generate the relevant project list. Edit
`CONFIG_FILE_PATTERNS` and `CONFIG_KEYWORDS` at the top of
`extract_config_commits.py` to choose your file patterns and message keywords,
then run:

```bash
python3 extract_config_commits.py
```

The lists initially contain Maven, Docker, Compose, Spring, and `.env` patterns
and the keywords `config`, `port`, and `environment`. A commit
matches if **any file pattern OR any keyword** matches. Patterns are case-sensitive;
patterns without `/` match filenames at any directory depth. Patterns with `/`
match repository-relative paths, and wildcards can span directory separators.
Keywords are case-insensitive literal substrings of the full commit message,
including its body (for example, `config` also matches `configuration`). Either
list can be empty to disable that criterion, but at least one entry is required
across the two lists.

By default the script reads `data/microservice_projects_filtered.csv` and creates
`data/config_commits.jsonl`. Use `--input` and `--output` to change these paths;
the output must not already exist. Each JSON line includes the repository, commit
hash, parents, author timestamp, full message, changed files, matched files and
keywords, and match reasons. Commits satisfying both criteria appear once per
repository. This exports metadata, not patches or file contents.

`--history all` (default) scans history reachable from all cloned branches and
tags. Use `--history head` for the default branch's history, or `--history dataset`
to scan the CSV's `commit_hash` and its ancestors. Dataset hashes must be available
in the clone; unavailable hashes are reported as failures. Merge changes are
compared with the first parent, root commits with the empty tree. Additions,
deletions, and both paths of renames are considered.

Repositories are cloned sequentially as full bare clones into temporary
directories. Each clone is removed after scanning, including on errors and normal
Ctrl+C interruption. `--temp-dir` selects an existing parent directory for clones.
Forced termination such as SIGKILL cannot guarantee cleanup. Full histories can
require substantial download time and temporary disk space.

Repository failures are reported to stderr; scanning continues and exits with
status 1 if any repository failed. Existing results are retained, including any
partial results from a failed scan. Exit status 130 indicates Ctrl+C interruption.
