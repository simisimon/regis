#!/usr/bin/env python3
"""Run a LangGraph agent that edits a repository to fulfil a task.

The agent receives a repository path and a natural-language task, explores the
repository with its tools to gather the context it needs, and applies the
changes itself. All tools are confined to the repository root: paths are
resolved and rejected if they escape it, and commands run with the repository
as their working directory.
"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, ToolRuntime, tools_condition

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")
COMMAND_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 30000
MAX_LISTED_FILES = 2000

# Directories that hold build output or dependencies rather than repository
# sources; listing them buries the configuration files the agent is looking for.
IGNORED_DIRECTORIES = {
    ".git",
    ".idea",
    ".gradle",
    ".mvn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
}

INSTRUCTIONS = """\
You are a configuration agent working inside a single Git repository.

You are given a configruation task to perform on the repository. Nothing about 
the repository is known to you in advance, so collect the context yourself 
before you change anything. To gather context, you can use the following tools:

- list_files(subdirectory="."): List the files in a repository subdirectory.
- read_file(path, start_line=1, line_count=800): Read a text file from the repository, with line numbers.
- write_file(path, content): Write a text file
- run_command(command): Run a shell command with the repository as the working directory.

Only after gathering the necessary context apply perform the given task using write_file. 
write_file replaces the whole file, so pass the complete new content, keeping everything 
you were not asked to change byte for byte, including indentation, comments and the trailing newline.

Finish with a short report: which files you changed and what you changed in each.
"""

TASK = """Change the port of my auth-service from 5000 to 5555."""

@dataclass
class RepositoryContext:
    """The repository every tool call is confined to."""

    root: Path

    def __post_init__(self):
        """Canonicalise the root so containment checks compare like with like.

        Tool paths are resolved before they are compared against the root, so a
        root that still contains a symlink (on macOS /var, for example) would
        never match and every path would be rejected as an escape.
        """
        self.root = Path(self.root).resolve()


def resolve_within_root(runtime: ToolRuntime[RepositoryContext], path: str) -> Path:
    """Resolve a repository-relative path, refusing paths outside the repository."""
    root = runtime.context.root
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path {path!r} is outside the repository.")
    return candidate


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Cap tool output so a single large file cannot fill the context window."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


@tool
def list_files(runtime: ToolRuntime[RepositoryContext], subdirectory: str = ".") -> str:
    """List the files in the repository, recursively.

    Args:
        subdirectory: Repository-relative directory to list. Defaults to the
            repository root. Build output and dependency directories such as
            .git, target, build and node_modules are skipped.
    """
    directory = resolve_within_root(runtime, subdirectory)
    if not directory.is_dir():
        return f"Error: {subdirectory!r} is not a directory."

    paths = []
    for current, subdirectories, filenames in os.walk(directory):
        subdirectories[:] = sorted(
            name for name in subdirectories if name not in IGNORED_DIRECTORIES
        )
        for filename in sorted(filenames):
            paths.append(
                str(Path(current, filename).relative_to(runtime.context.root))
            )
            if len(paths) >= MAX_LISTED_FILES:
                paths.append(f"... [truncated at {MAX_LISTED_FILES} files]")
                return "\n".join(paths)

    return "\n".join(paths) if paths else f"No files under {subdirectory!r}."


@tool
def read_file(
    runtime: ToolRuntime[RepositoryContext],
    path: str,
    start_line: int = 1,
    line_count: int = 800,
) -> str:
    """Read a text file from the repository, with line numbers.

    Args:
        path: Repository-relative path of the file to read.
        start_line: First line to return, counting from 1.
        line_count: How many lines to return from start_line.
    """
    file_path = resolve_within_root(runtime, path)
    if not file_path.is_file():
        return f"Error: {path!r} is not a file."

    content = file_path.read_bytes()
    if b"\x00" in content:
        return f"Error: {path!r} appears to be a binary file."

    lines = content.decode("utf-8", errors="replace").splitlines()
    selected = lines[max(start_line - 1, 0) : max(start_line - 1, 0) + line_count]
    numbered = "\n".join(
        f"{number:>6}\t{line}"
        for number, line in enumerate(selected, start=max(start_line, 1))
    )
    if start_line - 1 + line_count < len(lines):
        numbered += f"\n... [{len(lines)} lines total, continue at {start_line + line_count}]"
    return truncate(numbered)


@tool
def write_file(runtime: ToolRuntime[RepositoryContext], path: str, content: str) -> str:
    """Write a text file in the repository, replacing it if it exists.

    Args:
        path: Repository-relative path of the file to write.
        content: The complete new content of the file. This replaces the file
            entirely, so include every line that should be kept, unchanged.
    """
    file_path = resolve_within_root(runtime, path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    existed = file_path.is_file()
    file_path.write_text(content, encoding="utf-8")
    action = "Overwrote" if existed else "Created"
    return f"{action} {path} ({len(content.encode('utf-8'))} bytes)."


@tool
def run_command(runtime: ToolRuntime[RepositoryContext], command: str) -> str:
    """Run a shell command with the repository as the working directory.

    Use this to search the repository (grep, find), inspect Git history, or run
    the project's tests and build.

    Args:
        command: The shell command to run. It times out after
            120 seconds; its exit code, stdout and stderr are returned.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=runtime.context.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {COMMAND_TIMEOUT_SECONDS} seconds."

    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    report = [f"exit code: {result.returncode}"]
    if stdout.strip():
        report.append(f"stdout:\n{stdout}")
    if stderr.strip():
        report.append(f"stderr:\n{stderr}")
    if not stdout.strip() and not stderr.strip():
        report.append("(no output)")
    return truncate("\n".join(report))


TOOLS = [list_files, read_file, write_file, run_command]


class AgentState(TypedDict):
    """The conversation between the model and its tools."""

    messages: Annotated[list[AnyMessage], add_messages]


def build_agent(model=DEFAULT_MODEL):
    """Create the agent graph: the model calls tools until it answers without one."""
    llm = ChatOllama(model=model, temperature=0).bind_tools(TOOLS)

    def call_model(state: AgentState) -> dict:
        response = llm.invoke([SystemMessage(INSTRUCTIONS), *state["messages"]])
        return {"messages": [response]}

    graph = StateGraph(AgentState, context_schema=RepositoryContext)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def perform_task(repository, task=TASK, model=DEFAULT_MODEL, max_turns=60):
    """Run the agent against a repository and return its final report."""
    root = Path(repository).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{repository} is not a directory.")

    agent = build_agent(model)
    prompt = f"Repository: {root}\n\nTask: {task}"
    # Each turn is a model step followed by a tool step, hence two supersteps.
    result = agent.invoke(
        {"messages": [HumanMessage(prompt)]},
        context=RepositoryContext(root=root),
        config={"recursion_limit": 2 * max_turns + 1},
    )
    return result["messages"][-1].content


def main():
    parser = argparse.ArgumentParser(
        description="Perform a task on a repository with a LangGraph agent."
    )
    parser.add_argument("repository", help="Path to the repository to work in.")
    parser.add_argument(
        "task",
        nargs="?",
        default=TASK,
        help='The task, for example "change the config server port from 8888 to 9999".',
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model to run the agent on.")
    parser.add_argument(
        "--max-turns",
        type=int,
        default=60,
        help="Maximum agent turns before aborting.",
    )
    arguments = parser.parse_args()

    print(
        perform_task(
            arguments.repository, arguments.task, arguments.model, arguments.max_turns
        )
    )


if __name__ == "__main__":
    main()
