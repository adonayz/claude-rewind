# claude-rewind

Reopen a Claude Code session at any earlier prompt, including prompts from before a compaction.

When a long Claude Code session compacts, `claude --resume` and the rewind menu (`Esc Esc`) only reach back to the last compaction. Everything before it is still in the session file on disk, but Claude Code gives you no way back to it ([anthropics/claude-code#27242](https://github.com/anthropics/claude-code/issues/27242)).

`claude-rewind` lists every prompt you typed in a session, across all of its compactions, and creates a new session that ends just before the prompt you pick. Then it opens that session in Claude Code. The original session is never modified.

## Install

Needs Python 3.9 or newer and [fzf](https://github.com/junegunn/fzf).

```sh
uv tool install claude-rewind
# or
pipx install claude-rewind
```

## Usage

```sh
claude-rewind                   # pick a session, then a prompt
claude-rewind <session-id>      # pick a prompt in this session (an ID or a .jsonl path)
claude-rewind --name "My title" # title for the new session
claude-rewind --no-launch       # create the session and print how to open it
```

Both lists show the newest entries first; type to search. Each prompt row shows when you sent it and the context size Claude Code reported at that point.

The new session is titled after the original with ` (rewind)` appended, unless you pass `--name`. It is saved right away and appears in `claude --resume` like any other session.

Sessions are read from `~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects` when that variable is set.

## How it works

The copy follows `fork_session` in Anthropic's [Agent SDK](https://github.com/anthropics/claude-agent-sdk-python). It keeps the main conversation up to the parent of the prompt you picked, gives every entry a new ID and the new session ID, and writes `<new-id>.jsonl` next to the original. Cutting at the prompt's parent leaves out branches you abandoned with an earlier rewind. The file is streamed, so a 300 MB session takes about a second.

## Limitations

- Claude Code opens any session at its most recent compaction. If the prompt you pick comes after one or more compactions, the new session opens at the last of those, with the earlier part shown as its summary.
- If the context size at the point you pick is close to your auto-compact limit, Claude Code compacts again soon after you continue.
- Code checkpoints are not copied, so `/rewind` in the new session restores the conversation but not files from before the copy.
- Session files are an internal Claude Code format that can change between releases. Tested with Claude Code 2.1.310 on Linux.

## License

MIT
