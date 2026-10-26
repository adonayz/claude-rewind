import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_rewind as cr

SOURCE = "11111111-1111-4111-8111-111111111111"


def entry(kind, uuid=None, parent=None, **fields):
    data = {"type": kind, "sessionId": SOURCE, "timestamp": "2026-01-01T00:00:00.000Z", **fields}
    if uuid:
        data.update(uuid=uuid, parentUuid=parent, isSidechain=fields.get("isSidechain", False))
    return data


def typed(uuid, parent, text):
    return entry("user", uuid, parent, origin={"kind": "human"}, promptSource="typed",
                 message={"role": "user", "content": text})


def usage(total):
    return {"role": "assistant", "content": [], "usage": {"input_tokens": total - 10, "output_tokens": 10}}


# One session in file order: a tool call with a progress link, generated user
# messages, a subagent entry, a prompt abandoned by an in-session rewind, then a
# compaction followed by one more prompt.
ENTRIES = [
    entry("permission-mode", permissionMode="default"),
    typed("u1", None, "first prompt") | {"cwd": "/work/app"},
    entry("assistant", "a1", "u1", message=usage(160)),
    entry("system", "s1", "a1", subtype="turn_duration"),
    {"type": "ai-title", "aiTitle": "Generated title", "sessionId": SOURCE},
    typed("u2", "s1", "second prompt"),
    entry("assistant", "a2", "u2", teamName="team", slug="slug", message={"role": "assistant", "content": []}),
    entry("user", "tr2", "a2", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}),
    entry("progress", "p1", "tr2"),
    entry("assistant", "a3", "p1", message=usage(500)),
    {"type": "file-history-snapshot", "messageId": "u2", "snapshot": {}},
    {"type": "content-replacement", "sessionId": SOURCE, "replacements": [{"toolUseId": "t", "content": "c"}]},
    entry("user", "tn", "a3", origin={"kind": "task-notification"}, message={"role": "user", "content": "<task-notification>done</task-notification>"}),
    entry("user", "meta", "tn", isMeta=True, message={"role": "user", "content": "meta"}),
    entry("assistant", "sc", None, isSidechain=True, message=usage(5)),
    typed("u3", "a3", "abandoned prompt"),
    entry("assistant", "a4", "u3", message={"role": "assistant", "content": []}),
    typed("u3b", "a3", "redo prompt"),
    entry("assistant", "a5", "u3b", message=usage(900)),
    entry("system", "cb", None, subtype="compact_boundary", logicalParentUuid="a5", compactMetadata={"postTokens": 40}),
    entry("user", "sum", "cb", isCompactSummary=True, message={"role": "user", "content": "summary"}),
    typed("u4", "sum", "after compaction"),
    entry("assistant", "a6", "u4", message=usage(60)),
    {"type": "custom-title", "customTitle": "My session", "sessionId": SOURCE},
]


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class RewindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "-work-app" / f"{SOURCE}.jsonl"
        self.path.parent.mkdir()
        self.write(ENTRIES)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, entries):
        self.path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries))

    def prompt(self, text):
        return next(p for p in cr.list_prompts(self.path) if p.text == text)

    def test_lists_typed_prompts_with_context_size(self):
        prompts = cr.list_prompts(self.path)
        self.assertEqual([p.text for p in prompts], ["second prompt", "abandoned prompt", "redo prompt", "after compaction"])
        self.assertEqual([p.context_tokens for p in prompts], [160, 500, 500, 40])
        self.assertEqual(prompts[2].parent, "a3")

    def test_reads_title_and_working_directory(self):
        session = cr.read_session(self.path)
        self.assertEqual((session.title, session.cwd), ("My session", "/work/app"))
        self.write(ENTRIES[:-1])
        self.assertEqual(cr.read_session(self.path).title, "Generated title")
        self.write([e for e in ENTRIES if "aiTitle" not in e and "customTitle" not in e])
        self.assertEqual(cr.read_session(self.path).title, "first prompt")

    def test_finds_sessions_by_id_or_path(self):
        self.assertEqual(cr.find_session(SOURCE, self.root), self.path)
        self.assertEqual(cr.find_session(str(self.path)), self.path)
        self.assertEqual([s.path for s in cr.list_sessions(self.root)], [self.path])
        with self.assertRaises(SystemExit):
            cr.find_session("missing", self.root)

    def test_rewinds_across_a_compaction(self):
        original = self.path.read_bytes()
        new_id = cr.rewind(self.path, self.prompt("after compaction"), "My session (rewind)")
        fork = self.path.with_name(f"{new_id}.jsonl")
        lines = read_lines(fork)
        conversation = lines[:-2]

        self.assertEqual(self.path.read_bytes(), original)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(fork.stat().st_mode), 0o600)
        self.assertEqual([e["forkedFrom"]["messageUuid"] for e in conversation],
                         ["u1", "a1", "s1", "u2", "a2", "tr2", "a3", "tn", "meta", "u3", "a4", "u3b", "a5", "cb", "sum"])
        self.assertTrue(all(e["sessionId"] == new_id for e in lines))
        new = {e["forkedFrom"]["messageUuid"]: e for e in conversation}
        self.assertEqual(len({e["uuid"] for e in conversation} | set(new)), 2 * len(conversation))
        self.assertEqual(new["a3"]["parentUuid"], new["tr2"]["uuid"])  # past the progress link
        self.assertIsNone(new["cb"]["parentUuid"])
        self.assertEqual(new["cb"]["logicalParentUuid"], new["a5"]["uuid"])
        self.assertNotIn("teamName", new["a2"])
        self.assertNotIn("slug", new["a2"])
        self.assertNotEqual(new["sum"]["timestamp"], "2026-01-01T00:00:00.000Z")
        self.assertEqual(lines[-2]["type"], "content-replacement")
        self.assertEqual(lines[-1], lines[-1] | {"type": "custom-title", "customTitle": "My session (rewind)"})

    def test_cut_follows_the_prompt_parent_not_the_line_above(self):
        new_id = cr.rewind(self.path, self.prompt("redo prompt"), "t")
        lines = read_lines(self.path.with_name(f"{new_id}.jsonl"))
        self.assertEqual([e["forkedFrom"]["messageUuid"] for e in lines[:-1]], ["u1", "a1", "s1", "u2", "a2", "tr2", "a3"])

    def test_missing_parent_leaves_no_file(self):
        prompt = self.prompt("redo prompt")
        prompt.parent = "gone"
        with self.assertRaises(SystemExit):
            cr.rewind(self.path, prompt, "t")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_cli_creates_a_named_session(self):
        picks = iter([1])  # prompts are listed newest first: index 1 is "redo prompt"
        with mock.patch.object(cr.shutil, "which", return_value="/usr/bin/fzf"), \
                mock.patch.object(cr, "_pick", side_effect=lambda *a, **k: next(picks)), \
                mock.patch("sys.stdout"):
            self.assertEqual(cr.main([str(self.path), "--no-launch", "--name", "Named"]), 0)
        (fork,) = [p for p in self.path.parent.iterdir() if p != self.path]
        self.assertEqual(read_lines(fork)[-1]["customTitle"], "Named")


if __name__ == "__main__":
    unittest.main()
