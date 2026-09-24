"""Real Claude Code 2.1.281 screens captured with `tmux capture-pane -p` (120 cols) on 2026-09-24.
Also documented in skills/cc-bridge/references/tui-dialogs.md. Keep byte-for-byte when adding."""

TRUST = """
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:
 /private/tmp/claude-501/lab/project
 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source
 project, or work from your team). If not, take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""

IDLE_PROMPT = """
 ▐▛███▛█   Claude Code v2.1.281
▝▜██████▀  Haiku 4.5 · Claude Team
  ▝▝ ▝▝    /…/scratchpad/lab/project · /rc
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ Try "create a util logging.py that..."
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏸ manual mode on · ? for shortcuts · ← for agents
"""

WORKING = """
❯ Use AskUserQuestion: ask which season I like, options winter / spring / summer, header "Season". Then tell me what I
  answered.
⏺ User answered Claude's questions:
  ⎿  · Which season do you like? → autumn
✻ Swirling… (3s · ↓ 207 tokens · thinking)
  ⎿  Tip: Use /focus to see just your prompt, a one-line summary of the work, and the response
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ 
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏸ manual mode on · esc to interrupt · ← for agents
"""

QUESTION_MULTI_TAB1 = """
❯ Use the AskUserQuestion tool to ask me two things in one call: (1) which color I prefer, options red / green / blue,
  single select, header "Color"; (2) which fruits I like, options apple / pear / plum, multiSelect true, header
  "Fruit". Do nothing else, do not read files.
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
←  ☐ Color  ☐ Fruit  ✔ Submit  →
Which color do you prefer?
❯ 1. Red
     A warm, vibrant color
  2. Green
     A cool, natural color
  3. Blue
     A calm, serene color
  4. Type something.
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

QUESTION_MULTI_TAB2 = """
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
←  ☒ Color  ☐ Fruit  ✔ Submit  →
Which fruits do you like?
❯ 1. [ ] Apple
         A crisp, sweet fruit
  2. [ ] Pear
         A juicy, mild fruit
  3. [ ] Plum
         A soft, tart fruit
  4. [ ] Type something
     Submit
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

QUESTION_MULTI_CHECKED = """
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
←  ☒ Color  ☒ Fruit  ✔ Submit  →
Which fruits do you like?
❯ 1. [✔] Apple
         A crisp, sweet fruit
  2. [ ] Pear
         A juicy, mild fruit
  3. [✔] Plum
         A soft, tart fruit
  4. [ ] Type something
     Submit
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

QUESTION_SUBMIT_CURSOR = """
         A juicy, mild fruit
  3. [✔] Plum
         A soft, tart fruit
  4. [ ] Type something
❯    Submit
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · Tab/Arrow keys to navigate · ctrl+g to edit in Vim · Esc to cancel
"""

QUESTION_REVIEW = """
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
←  ☒ Color  ☒ Fruit  ✔ Submit  →
Review your answers
 ● Which color do you prefer?
   → Green
 ● Which fruits do you like?
   → Apple, Plum
Ready to submit your answers?
❯ 1. Submit answers
  2. Cancel
Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

QUESTION_SINGLE = """
❯ Use AskUserQuestion once more: ask which editor I use, options vim / emacs / nano, header "Editor". Then tell me what I
  answered.
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 ☐ Editor
Which editor do you use?
❯ 1. vim
     Modal text editor with keyboard shortcuts
  2. emacs
     Extensible text editor with powerful features
  3. nano
     Simple, beginner-friendly text editor
  4. Type something.
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""

PERMISSION_BASH = """
❯ Run the shell command echo hello > hello.txt using the Bash tool. Do nothing else.
⏺ Creating hello.txt file with hello content
  ⎿  $ echo hello > hello.txt
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Bash command
   echo hello > hello.txt
   Create hello.txt file with hello content
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /private/tmp/claude-501/-Users-hermes-Projects-hermes-claude-code-bridge/11507c24-
      aa81-4104-a8eb-ae3b3aacdbf2/scratchpad/lab/project from this project
   3. No
 Esc to cancel · Tab to amend
"""
