"""Hook-path tests for ccb.py: run the real CLI as subprocesses against a throwaway config.

No tmux session exists for the fake task, so payload tails are empty and `ccb answer`
takes the hook path (pending.source == "hook" and the hook pid is alive)."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CCB = HERE.parent / "skills" / "cc-bridge" / "scripts" / "ccb.py"
sys.path.insert(0, str(CCB.parent))


def ask_event(questions):
    return {"session_id": "s1", "transcript_path": "", "cwd": "/tmp", "permission_mode": "default",
            "hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion",
            "tool_input": {"questions": questions}}


Q_COLOR = {"question": "Which color?", "header": "Color", "multiSelect": False,
           "options": [{"label": "Red", "description": "warm"}, {"label": "Green", "description": "cool"}]}
Q_FRUIT = {"question": "Which fruits?", "header": "Fruit", "multiSelect": True,
           "options": [{"label": "Apple", "description": ""}, {"label": "Pear", "description": ""}, {"label": "Plum", "description": ""}]}
PERM_BASH = {"session_id": "s1", "transcript_path": "", "cwd": "/tmp", "permission_mode": "default",
             "hook_event_name": "PermissionRequest", "tool_name": "Bash",
             "tool_input": {"command": "echo hi > x.txt", "description": "write"},
             "permission_suggestions": [{"type": "addDirectories", "directories": ["/tmp"], "destination": "session"}]}


class HookHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccb-test-"))
        self.cfg = self.tmp / "config.json"
        self.cfg.write_text(json.dumps({
            "sinks": ["inbox"],
            "state_dir": str(self.tmp / "sessions"),
            "answers_dir": str(self.tmp / "answers"),
            "inbox_dir": str(self.tmp / "inbox"),
            "raw_dir": str(self.tmp / "raw"),
            "answer_timeout": 8,
            "answer_poll": 0.1,
        }))
        self.env = dict(os.environ, CCB_CONFIG=str(self.cfg), CCB_TASK="t1")

    def run_ccb(self, *args, stdin=None, env=None, timeout=30):
        return subprocess.run([sys.executable, str(CCB), *args], input=stdin, text=True,
                              capture_output=True, env=env or self.env, timeout=timeout)

    def hook_async(self, event):
        proc = subprocess.Popen([sys.executable, str(CCB), "hook"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)
        proc.stdin.write(json.dumps(event)); proc.stdin.close(); proc.stdin = None
        return proc

    def wait_pending(self):
        """Wait until the hook has both written `pending` and published the ask event."""
        for _ in range(200):
            st = self.state()
            if st and st.get("pending") and st["pending"]["id"] in (st.get("notified") or {}):
                return st
            time.sleep(0.05)
        self.fail("hook never wrote a pending request")

    def state(self):
        p = self.tmp / "sessions" / "t1.json"
        return json.loads(p.read_text()) if p.exists() else None

    def inbox(self):
        d = self.tmp / "inbox"
        return [json.loads(f.read_text()) for f in sorted(d.glob("*.json"))] if d.exists() else []

    def decision(self, proc):
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        return json.loads(out)["hookSpecificOutput"] if out.strip() else None


class QuestionFlow(HookHarness):
    def test_two_questions_answered(self):
        proc = self.hook_async(ask_event([Q_COLOR, Q_FRUIT]))
        st = self.wait_pending()
        self.assertEqual(st["status"], "awaiting_input")
        self.assertEqual(len(st["pending"]["questions"]), 2)
        events = self.inbox()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual((ev["event"], ev["kind"], ev["schema"]), ("ask", "question", 1))
        self.assertEqual(ev["request_id"], st["pending"]["id"])
        self.assertIn("1. Red — warm", ev["text"])
        self.assertIn("choose several", ev["text"])
        self.assertIn("--q 1", ev["answer_command"])

        r = self.run_ccb("answer", "t1", "--q", "1", "2", "--q", "2", "1,3")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.decision(proc)
        self.assertEqual(d["hookEventName"], "PermissionRequest")
        self.assertEqual(d["decision"]["behavior"], "allow")
        self.assertEqual(d["decision"]["updatedInput"]["answers"],
                         {"Which color?": "Green", "Which fruits?": "Apple, Plum"})
        self.assertEqual(d["decision"]["updatedInput"]["questions"], [Q_COLOR, Q_FRUIT])
        st = self.state()
        self.assertIsNone(st["pending"])
        self.assertEqual(st["status"], "running")

    def test_pipe_syntax_and_free_text(self):
        proc = self.hook_async(ask_event([Q_COLOR, Q_FRUIT]))
        self.wait_pending()
        r = self.run_ccb("answer", "t1", "purple | 2")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.decision(proc)
        self.assertEqual(d["decision"]["updatedInput"]["answers"], {"Which color?": "purple", "Which fruits?": "Pear"})

    def test_validation_rejects_bad_option(self):
        proc = self.hook_async(ask_event([Q_COLOR]))
        self.wait_pending()
        r = self.run_ccb("answer", "t1", "7")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("options 1..2", r.stderr)
        r = self.run_ccb("answer", "t1", "1,2")
        self.assertIn("single-select", r.stderr)
        r = self.run_ccb("answer", "t1", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.decision(proc)["decision"]["updatedInput"]["answers"], {"Which color?": "Green"})

    def test_release_returns_no_decision(self):
        proc = self.hook_async(ask_event([Q_COLOR]))
        self.wait_pending()
        r = self.run_ccb("answer", "t1", "--release")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.decision(proc))

    def test_timeout_returns_no_decision_and_keeps_pending(self):
        self.cfg.write_text(json.dumps({**json.loads(self.cfg.read_text()), "answer_timeout": 1}))
        proc = self.hook_async(ask_event([Q_COLOR]))
        self.wait_pending()
        self.assertIsNone(self.decision(proc))
        self.assertEqual(self.state()["status"], "awaiting_input")  # dialog is now in the terminal

    def test_tui_answer_clears_pending_and_unblocks_hook(self):
        proc = self.hook_async(ask_event([Q_COLOR]))
        self.wait_pending()
        # the human answered in the terminal -> PostToolUse fires
        r = self.run_ccb("hook", stdin=json.dumps({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
                                                    "session_id": "s1", "tool_response": {}}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.decision(proc))
        self.assertEqual(self.state()["status"], "running")


class PermissionFlow(HookHarness):
    def test_allow(self):
        proc = self.hook_async(PERM_BASH)
        st = self.wait_pending()
        self.assertEqual(st["status"], "awaiting_permission")
        ev = self.inbox()[0]
        self.assertEqual(ev["kind"], "permission")
        self.assertEqual(ev["permission"]["summary"], "echo hi > x.txt")
        self.assertIn("1. Allow", ev["text"])
        r = self.run_ccb("answer", "t1", "yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.decision(proc)["decision"], {"behavior": "allow"})

    def test_always_echoes_suggestions(self):
        proc = self.hook_async(PERM_BASH)
        self.wait_pending()
        self.run_ccb("answer", "t1", "2")
        d = self.decision(proc)["decision"]
        self.assertEqual(d["behavior"], "allow")
        self.assertEqual(d["updatedPermissions"], PERM_BASH["permission_suggestions"])

    def test_deny_with_message(self):
        proc = self.hook_async(PERM_BASH)
        self.wait_pending()
        r = self.run_ccb("answer", "t1", "нет", "--message", "not on prod")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.decision(proc)["decision"], {"behavior": "deny", "message": "not on prod"})

    def test_bad_reply_rejected(self):
        proc = self.hook_async(PERM_BASH)
        self.wait_pending()
        r = self.run_ccb("answer", "t1", "maybe later")
        self.assertNotEqual(r.returncode, 0)
        self.run_ccb("answer", "t1", "--release")
        proc.communicate(timeout=10)


class StopAndMisc(HookHarness):
    def stop(self, text, extra=None):
        ev = {"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False,
              "last_assistant_message": text, "transcript_path": ""}
        ev.update(extra or {})
        return self.run_ccb("hook", stdin=json.dumps(ev))

    def test_stop_notifies_once_per_message(self):
        self.assertEqual(self.stop("All set. Anything else?").returncode, 0)
        self.assertEqual(self.stop("All set. Anything else?").returncode, 0)
        events = self.inbox()
        self.assertEqual([e["kind"] for e in events], ["idle"])
        self.assertIn("All set", events[0]["text"])
        self.assertEqual(self.state()["status"], "idle")

    def test_stop_hook_active_is_silent(self):
        self.stop("continuing", {"stop_hook_active": True})
        self.assertEqual(self.inbox(), [])

    def test_markers(self):
        self.stop("round finished\n[[CCB:ROUND:2]]")
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.state()["status"], "running")
        self.stop("merged\n[[CCB:DONE]]")
        self.assertEqual([e["kind"] for e in self.inbox()], ["done"])
        self.assertEqual(self.state()["marker"], "DONE")
        self.assertNotIn("[[CCB", self.state()["last_message"])

    def test_markers_policy_silences_plain_stops(self):
        self.cfg.write_text(json.dumps({**json.loads(self.cfg.read_text()), "stop_policy": "markers"}))
        self.stop("some progress text")
        self.assertEqual(self.inbox(), [])
        self.stop("Which database should I use?")
        self.assertEqual([e["kind"] for e in self.inbox()], ["idle"])

    def test_session_end(self):
        r = self.run_ccb("hook", stdin=json.dumps({"hook_event_name": "SessionEnd", "session_id": "s1", "reason": "other"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([e["kind"] for e in self.inbox()], ["stopped"])

    def test_manual_session_ignored(self):
        env = dict(self.env); env.pop("CCB_TASK")
        r = self.run_ccb("hook", stdin=json.dumps(PERM_BASH), env=env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIsNone(self.state())

    def test_no_sink_does_not_block(self):
        self.cfg.write_text(json.dumps({**json.loads(self.cfg.read_text()), "sinks": ["command"], "command_sink": "exit 3"}))
        t0 = time.time()
        r = self.run_ccb("hook", stdin=json.dumps(PERM_BASH))
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))
        self.assertLess(time.time() - t0, 5)

    def test_command_sink_receives_payload(self):
        out = self.tmp / "sink.json"
        self.cfg.write_text(json.dumps({**json.loads(self.cfg.read_text()), "sinks": ["command"],
                                        "command_sink": f"cat > {out}"}))
        self.stop("hello from claude")
        self.assertEqual(json.loads(out.read_text())["kind"], "idle")


if __name__ == "__main__":
    unittest.main()
