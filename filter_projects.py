#!/usr/bin/env python3
"""Filter projects with Maven, Spring, and Docker Compose evidence.

Docker usage is inferred from Docker Compose; a Dockerfile is not required.
Additional technologies are allowed. Uses only the Python standard library.
"""

import argparse
import ast
import csv
import re
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent / "data"
MAVEN_PATH = re.compile(r"'path':\s*'[^']*/pom\.xml'")
COMPOSE_PATH = re.compile(
    r"'path':\s*'[^']*/(?:docker-)?compose(?:[.-][^'/]+)?\.ya?ml'",
    re.IGNORECASE,
)


def matches(project):
    """Return whether the row contains evidence of all required technologies."""
    try:
        servers = ast.literal_eval(project["servers"] or "[]")
    except (ValueError, SyntaxError):
        servers = []
    return bool(
        isinstance(servers, list)
        and "spring" in servers
        and MAVEN_PATH.search(project["dockers_raw"] or "")
        and COMPOSE_PATH.search(project["structure_raw"] or "")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, default=DATA_DIR / "microservice_projects_all.csv"
    )
    parser.add_argument(
        "--output", type=Path, default=DATA_DIR / "microservice_projects_filtered.csv"
    )
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Input and output must be different files.")

    # Some rows contain large serialized metadata fields.
    csv.field_size_limit(10**8)
    with args.input.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source, delimiter=";")
        columns = reader.fieldnames or []
        missing = {"servers", "dockers_raw", "structure_raw"} - set(columns)
        if missing:
            parser.error(f"Missing required columns: {', '.join(sorted(missing))}")
        projects = list(reader)

    filtered = [project for project in projects if matches(project)]
    with args.output.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=columns, delimiter=";")
        writer.writeheader()
        writer.writerows(filtered)

    print(f"Found {len(filtered)} of {len(projects)} projects. Saved to {args.output}")


if __name__ == "__main__":
    main()
