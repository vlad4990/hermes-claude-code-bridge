import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "cc-bridge", "scripts"))
sys.path.insert(0, HERE)

import ccb_screen as S  # noqa: E402
import fixtures_screens as F  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_trust(self):
        scr = S.parse_screen(F.TRUST)
        self.assertEqual(scr["kind"], "trust")
        self.assertEqual(scr["cursor"], 1)
        self.assertTrue(scr["answerable"])
        labels = [o["label"] for o in scr["options"]]
        self.assertEqual(labels, ["No, exit", "Yes, I trust this folder"])
        self.assertEqual(scr["options"][0]["index"], None)  # unnumbered rows

    def test_idle_prompt(self):
        scr = S.parse_screen(F.IDLE_PROMPT)
        self.assertEqual(scr["kind"], "prompt")
        self.assertFalse(scr["answerable"])

    def test_working(self):
        scr = S.parse_screen(F.WORKING)
        self.assertEqual(scr["kind"], "working")

    def test_question_single(self):
        scr = S.parse_screen(F.QUESTION_SINGLE)
        self.assertEqual(scr["kind"], "question")
        self.assertEqual(scr["question"], "Which editor do you use?")
        self.assertEqual(scr["title"], "Editor")
        self.assertFalse(scr["multi_select"])
        self.assertEqual(scr["cursor"], 1)
        opts = scr["options"]
        self.assertEqual([o["index"] for o in opts], [1, 2, 3, 4, 5])
        self.assertEqual(opts[1]["label"], "emacs")
        self.assertEqual(opts[1]["description"], "Extensible text editor with powerful features")
        self.assertEqual(opts[3]["special"], "free_text")
        self.assertEqual(opts[4]["special"], "chat")

    def test_question_multi_tab1(self):
        scr = S.parse_screen(F.QUESTION_MULTI_TAB1)
        self.assertEqual(scr["kind"], "question")
        self.assertEqual([t["label"] for t in scr["tabs"]], ["Color", "Fruit"])
        self.assertEqual(scr["title"], "Color")
        self.assertEqual(scr["question"], "Which color do you prefer?")
        self.assertFalse(scr["multi_select"])

    def test_question_multi_tab2(self):
        scr = S.parse_screen(F.QUESTION_MULTI_TAB2)
        self.assertTrue(scr["multi_select"])
        self.assertEqual(scr["title"], "Fruit")
        self.assertEqual(scr["tabs"][0]["done"], True)
        opts = scr["options"]
        self.assertEqual([o["label"] for o in opts], ["Apple", "Pear", "Plum", "Type something", "Submit", "Chat about this"])
        self.assertEqual(opts[0]["checked"], False)
        self.assertEqual(opts[4]["special"], "submit")
        self.assertEqual(opts[4]["row"], 5)

    def test_question_multi_checked(self):
        scr = S.parse_screen(F.QUESTION_MULTI_CHECKED)
        self.assertEqual([o["checked"] for o in scr["options"][:3]], [True, False, True])

    def test_submit_cursor(self):
        scr = S.parse_screen(F.QUESTION_SUBMIT_CURSOR)
        sub = [o for o in scr["options"] if o["special"] == "submit"][0]
        self.assertTrue(sub["cursor"])
        self.assertEqual(scr["cursor"], sub["row"])

    def test_review(self):
        scr = S.parse_screen(F.QUESTION_REVIEW)
        self.assertEqual(scr["kind"], "question_review")
        self.assertEqual([o["label"] for o in scr["options"]], ["Submit answers", "Cancel"])
        self.assertIn("→ Green", scr["detail"])

    def test_permission(self):
        scr = S.parse_screen(F.PERMISSION_BASH)
        self.assertEqual(scr["kind"], "permission")
        self.assertEqual(scr["title"], "Bash command")
        self.assertEqual(scr["detail"], ["echo hello > hello.txt", "Create hello.txt file with hello content"])
        opts = scr["options"]
        self.assertEqual(len(opts), 3)
        self.assertTrue(opts[1]["label"].startswith("Yes, and always allow access to"))
        self.assertTrue(opts[1]["label"].endswith("from this project"))  # wrapped line joined
        self.assertEqual(opts[2]["label"], "No")
        self.assertEqual(scr["cursor"], 1)


class ReplyTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(S.normalize_reply("2"), {"options": [2]})
        self.assertEqual(S.normalize_reply("1, 3"), {"options": [1, 3]})
        self.assertEqual(S.normalize_reply("1,3"), {"options": [1, 3]})
        self.assertEqual(S.normalize_reply("yes"), {"decision": "allow"})
        self.assertEqual(S.normalize_reply("Да"), {"decision": "allow"})
        self.assertEqual(S.normalize_reply("always"), {"decision": "always"})
        self.assertEqual(S.normalize_reply("нет"), {"decision": "deny"})
        self.assertEqual(S.normalize_reply("use postgres"), {"text": "use postgres"})


class PlanTests(unittest.TestCase):
    def keys(self, plan):
        return [k.get("key") or ("text:" + k["text"]) for k in plan]

    def test_trust_yes(self):
        plan = S.plan_keys(S.parse_screen(F.TRUST), {"decision": "allow"})
        self.assertEqual(self.keys(plan), ["Down", "Enter"])

    def test_permission_allow_deny_always(self):
        scr = S.parse_screen(F.PERMISSION_BASH)
        self.assertEqual(self.keys(S.plan_keys(scr, {"decision": "allow"})), ["Enter"])
        self.assertEqual(self.keys(S.plan_keys(scr, {"decision": "always"})), ["Down", "Enter"])
        self.assertEqual(self.keys(S.plan_keys(scr, {"decision": "deny"})), ["Down", "Down", "Enter"])
        self.assertEqual(self.keys(S.plan_keys(scr, {"options": [3]})), ["Down", "Down", "Enter"])

    def test_single_question(self):
        scr = S.parse_screen(F.QUESTION_SINGLE)
        self.assertEqual(self.keys(S.plan_keys(scr, {"options": [2]})), ["2"])
        with self.assertRaises(ValueError):
            S.plan_keys(scr, {"options": [9]})

    def test_free_text(self):
        scr = S.parse_screen(F.QUESTION_SINGLE)
        self.assertEqual(self.keys(S.plan_keys(scr, {"text": "autumn"})),
                         ["Down", "Down", "Down", "text:autumn", "Enter"])

    def test_multi_select(self):
        scr = S.parse_screen(F.QUESTION_MULTI_TAB2)
        # toggle 1 and 3, then move from row 3 to the Submit row (row 5) and confirm
        self.assertEqual(self.keys(S.plan_keys(scr, {"options": [1, 3]})), ["1", "3", "Down", "Down", "Enter"])

    def test_multi_select_already_checked(self):
        scr = S.parse_screen(F.QUESTION_MULTI_CHECKED)
        # nothing to toggle; cursor is on row 1 -> 4 downs to Submit
        self.assertEqual(self.keys(S.plan_keys(scr, {"options": [1, 3]})), ["Down"] * 4 + ["Enter"])

    def test_review_submit(self):
        scr = S.parse_screen(F.QUESTION_REVIEW)
        self.assertEqual(self.keys(S.plan_keys(scr, {})), ["Enter"])
        self.assertEqual(self.keys(S.plan_keys(scr, {"decision": "deny"})), ["Down", "Enter"])

    def test_not_answerable(self):
        with self.assertRaises(ValueError):
            S.plan_keys(S.parse_screen(F.IDLE_PROMPT), {"options": [1]})


if __name__ == "__main__":
    unittest.main()
