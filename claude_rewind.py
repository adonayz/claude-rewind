"""Reopen a Claude Code session at any earlier prompt, including before a compaction.

Claude Code resumes a session from its last compaction and its rewind menu only
reaches prompts after it, although the whole conversation stays on disk. This
tool lists every prompt in a session and writes a new session that ends just
before the one you pick, then opens it with `claude --resume`. The original
session is never modified.

The copy follows `fork_session` in Anthropic's claude-agent-sdk-python: keep the
main conversation entries, give them fresh UUIDs and the new session ID, and
drop file-history snapshots (code checkpoints are not carried over).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.1.0"

# Entry types that make up the conversation chain; everything else (titles,
# file-history snapshots, API bookkeeping) is left out of the new session.
TRANSCRIPT_TYPES = frozenset({"user", "assistant", "attachment", "system", "progress"})
# Fields that tie an entry to the source session's team or subagents.
SOURCE_ONLY_KEYS = ("teamName", "agentName", "slug", "sourceToolAssistantUUID")
# User messages Claude Code generates itself rather than the person typing.
GENERATED_PREFIXES = (
    "<task-notification",
    "<local-command-",
    "<command-name",
    "<command-message",
    "<system-reminder",
    "<bash-input",
    "<bash-stdout",
    "<bash-stderr",
)
TITLE_SCAN_BYTES = 64 * 1024


@dataclass
class Session:
    path: Path
    title: str
    cwd: str | None
    modified: float
    size: int


@dataclass
class Prompt:
    line: int
    uuid: str
    parent: str
    timestamp: str
    text: str
    context_tokens: int | None


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _string_fields(text: str, key: str) -> list[str]:
    values = []
    for raw in re.findall(r'"%s":"((?:[^"\\]|\\.)*)"' % re.escape(key), text):
        try:
            value = json.loads(f'"{raw}"').strip()
        except ValueError:
            continue
        if value:
            values.append(value)
    return values


def prompt_text(entry: dict) -> str | None:
    """Text of a prompt the user typed, or None for any other entry."""
    if entry.get("type") != "user":
        return None
    if entry.get("isSidechain") or entry.get("isMeta") or entry.get("isCompactSummary"):
        return None
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text.strip() and any(b.get("type") == "image" for b in blocks):
            text = "[image]"
    else:
        return None
    text = text.strip()
    if not text or text.startswith(GENERATED_PREFIXES):
        return None
    return text


def read_session(path: Path) -> Session:
    """Title, working directory and file stats, read from the head and tail only."""
    stat = path.stat()
    with open(path, "rb") as f:
        head = f.read(TITLE_SCAN_BYTES).decode("utf-8", "replace")
        f.seek(max(0, stat.st_size - TITLE_SCAN_BYTES))
        tail = f.read().decode("utf-8", "replace")

    def last(text: str, key: str) -> str | None:
        values = _string_fields(text, key)
        return values[-1] if values else None

    title = (
        last(tail, "customTitle")
        or last(head, "customTitle")
        or last(tail, "aiTitle")
        or last(head, "aiTitle")
    )
    if not title:
        lines = head.splitlines()
        if stat.st_size > TITLE_SCAN_BYTES:
            lines = lines[:-1]  # may be cut off mid-entry
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            first = prompt_text(entry) if isinstance(entry, dict) else None
            if first:
                title = first[:80]
                break
    # Read directly: the first entry can be larger than the scanned head.
    cwds = _string_fields(head, "cwd")
    return Session(path, " ".join((title or "(untitled)").split()), cwds[0] if cwds else None,
                   stat.st_mtime, stat.st_size)


def list_sessions(root: Path | None = None) -> list[Session]:
    sessions = []
    for path in (root or projects_dir()).glob("*/*.jsonl"):
        try:
            if path.stat().st_size:
                sessions.append(read_session(path))
        except OSError:
            continue
    return sorted(sessions, key=lambda s: s.modified, reverse=True)


def find_session(ref: str, root: Path | None = None) -> Path:
    """Resolve a session ID or a path to a session file."""
    path = Path(ref).expanduser()
    if path.suffix == ".jsonl" and path.is_file():
        return path
    matches = list((root or projects_dir()).glob(f"*/{ref}.jsonl"))
    if not matches:
        raise SystemExit(f"claude-rewind: no session found for {ref!r}")
    return matches[0]


def list_prompts(path: Path) -> list[Prompt]:
    """Every typed prompt that has something before it, in file order.

    context_tokens is the context size Claude Code reported just before the
    prompt, so you can tell how close a rewound session starts to compaction.
    """
    prompts = []
    context = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for number, line in enumerate(f):
            # Cheap filter: only these lines matter, and parsing every line of
            # a large session is several times slower.
            if not ('"type":"user"' in line or '"type":"assistant"' in line or '"compact_boundary"' in line):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            if entry.get("subtype") == "compact_boundary":
                context = (entry.get("compactMetadata") or {}).get("postTokens")
                continue
            if entry.get("type") == "assistant":
                usage = (entry.get("message") or {}).get("usage")
                if isinstance(usage, dict):
                    context = sum(
                        usage.get(key) or 0
                        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
                    )
                continue
            text = prompt_text(entry)
            if text and isinstance(entry.get("uuid"), str) and isinstance(entry.get("parentUuid"), str):
                prompts.append(Prompt(number, entry["uuid"], entry["parentUuid"], entry.get("timestamp", ""), text, context))
    return prompts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rewind(path: Path, prompt: Prompt, title: str) -> str:
    """Write a new session holding the conversation up to just before `prompt`.

    The cut is the prompt's parent entry rather than the line above it, so a
    branch abandoned by an earlier in-session rewind is not carried along.
    Returns the new session ID.
    """
    source_id = path.stem
    new_id = str(uuid.uuid4())
    destination = path.with_name(f"{new_id}.jsonl")
    new_uuids: dict[str, str] = {}
    progress_parents: dict[str, str | None] = {}
    replacements: list = []
    previous = None
    found = False

    def parent_of(old: str | None) -> str | None:
        # Progress entries are chain links Claude Code does not need; point
        # past them, as the SDK does.
        while old in progress_parents:
            old = progress_parents[old]
        return new_uuids.get(old) if old else None

    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out, open(path, encoding="utf-8", errors="replace") as source:

            def write(entry: dict) -> None:
                out.write(json.dumps(entry, separators=(",", ":")) + "\n")

            for line in source:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                if kind == "content-replacement" and entry.get("sessionId") == source_id:
                    replacements.extend(entry.get("replacements") or [])
                    continue
                old = entry.get("uuid")
                if kind not in TRANSCRIPT_TYPES or not isinstance(old, str) or entry.get("isSidechain"):
                    continue
                new_uuids[old] = str(uuid.uuid4())
                if kind == "progress":
                    progress_parents[old] = entry.get("parentUuid")
                else:
                    if previous is not None:
                        write(previous)
                    logical = entry.get("logicalParentUuid")
                    previous = {
                        **entry,
                        "uuid": new_uuids[old],
                        "parentUuid": parent_of(entry.get("parentUuid")),
                        "logicalParentUuid": new_uuids.get(logical) if logical else logical,
                        "sessionId": new_id,
                        "isSidechain": False,
                        "forkedFrom": {"sessionId": source_id, "messageUuid": old},
                    }
                    for key in SOURCE_ONLY_KEYS:
                        previous.pop(key, None)
                if old == prompt.parent:
                    found = True
                    break
            if not found or previous is None:
                raise SystemExit("claude-rewind: the session changed; pick the prompt again")
            # Claude Code resumes from the newest entry.
            previous["timestamp"] = _now()
            write(previous)
            if replacements:
                write({"type": "content-replacement", "sessionId": new_id, "replacements": replacements,
                       "uuid": str(uuid.uuid4()), "timestamp": _now()})
            write({"type": "custom-title", "sessionId": new_id, "customTitle": title,
                   "uuid": str(uuid.uuid4()), "timestamp": _now()})
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return new_id


def _local_time(timestamp: str) -> str:
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp[:16]
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _size(count: float) -> str:
    for unit in ("B", "KB", "MB"):
        if count < 1024:
            return f"{count:.0f} {unit}"
        count /= 1024
    return f"{count:.1f} GB"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _pick(rows: list[str], header: str, preview: bool = False) -> int | None:
    """Let the user pick a row with fzf; returns its index or None if cancelled."""
    command = ["fzf", "--delimiter=\t", "--with-nth=2..", "--layout=reverse", "--tiebreak=index",
               "--no-multi", f"--header={header}"]
    if preview:
        command += ["--preview=printf '%s\\n' {4}", "--preview-window=down,30%,wrap"]
    result = subprocess.run(command, input="\n".join(f"{i}\t{row}" for i, row in enumerate(rows)),
                            stdout=subprocess.PIPE, text=True)
    if result.returncode in (1, 130):  # no match, or cancelled
        return None
    if result.returncode != 0:
        raise SystemExit(f"claude-rewind: fzf exited with status {result.returncode}")
    return int(result.stdout.split("\t", 1)[0])


def _launch(session_id: str, cwd: str | None) -> int:
    claude = shutil.which("claude")
    directory = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    command = [claude or "claude", "--resume", session_id]
    if not claude:
        print(f"Open it with: cd {directory} && claude --resume {session_id}")
        return 0
    if os.name == "nt":
        return subprocess.call(command, cwd=directory)
    os.chdir(directory)
    os.execv(claude, command)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-rewind",
        description="Reopen a Claude Code session at any earlier prompt, including prompts from "
        "before a compaction. Writes a new session; the original is never modified.",
    )
    parser.add_argument("session", nargs="?", help="session ID or .jsonl path (default: pick from a list)")
    parser.add_argument("-n", "--name", help="title for the new session (default: original title + ' (rewind)')")
    parser.add_argument("--no-launch", action="store_true", help="create the session and print how to open it")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if not shutil.which("fzf"):
        parser.error("fzf is required for picking (https://github.com/junegunn/fzf)")

    if args.session:
        session = read_session(find_session(args.session))
    else:
        sessions = list_sessions()
        if not sessions:
            parser.error(f"no Claude Code sessions in {projects_dir()}")
        home = str(Path.home())
        rows = [
            "\t".join((
                datetime.fromtimestamp(s.modified).strftime("%Y-%m-%d %H:%M"),
                f"{_size(s.size):>7}",
                (s.cwd or "?").replace(home, "~", 1),
                s.title,
            ))
            for s in sessions
        ]
        index = _pick(rows, "Pick a session (newest first, type to search)")
        if index is None:
            return 130
        session = sessions[index]

    prompts = list_prompts(session.path)
    if not prompts:
        parser.error(f"{session.title!r} has no earlier prompt to rewind to")
    prompts.reverse()
    rows = [
        "\t".join((
            _local_time(p.timestamp),
            f"{p.context_tokens // 1000}k ctx" if p.context_tokens else "  ? ctx",
            _one_line(p.text),
        ))
        for p in prompts
    ]
    index = _pick(rows, f"{session.title}\nPick a prompt: the new session ends just before it", preview=True)
    if index is None:
        return 130
    prompt = prompts[index]

    title = (args.name or f"{session.title} (rewind)").strip()
    new_id = rewind(session.path, prompt, title)
    print(f'Created "{title}" ({new_id}), ending before your prompt from {_local_time(prompt.timestamp)}.')
    if args.no_launch:
        print(f"Open it with: cd {session.cwd or '.'} && claude --resume {new_id}")
        return 0
    return _launch(new_id, session.cwd)


if __name__ == "__main__":
    sys.exit(main())
