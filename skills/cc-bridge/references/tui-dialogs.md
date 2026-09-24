# Claude Code TUI dialogs (fallback path)

Captured with `tmux capture-pane -p -J` from Claude Code **2.1.281** on 2026-09-24. These are
the fixtures behind `scripts/ccb_screen.py` and `tests/test_ccb_screen.py`. The bridge only
drives the TUI when the hook path is unavailable (hook not installed, timed out, released with
`ccb answer --release`, or a session started outside `ccb`). Re-capture when a Claude Code
release changes a dialog and update both the fixture and the parser.

## Trust prompt (first start in a folder)

```
 Quick safety check: Is this a project you created or one you trust? (Like your own code, …
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
```

Rows carry no numbers; the cursor starts on **No, exit**. `ccb start` moves to the row whose
label contains "trust" (Down) and presses Enter.

## Permission

```
 Bash command
   echo hello > hello.txt
   Create hello.txt file with hello content
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /very/long/path/that/wraps/onto/the/next
      line from this project
   3. No
 Esc to cancel · Tab to amend
```

`title` = first line after the rule (`Bash command`), `detail` = the lines up to
"Do you want to proceed?", options are numbered, wrapped labels are re-joined. Keys: the
parser moves the cursor with Down/Up from its current row to the target and presses Enter
(`yes` → row 1, `always` → the row containing "always"/"don't ask", `no` → the row starting
with "No").

## AskUserQuestion — single select

```
 ☐ Editor
Which editor do you use?
❯ 1. vim
     Modal text editor with keyboard shortcuts
  2. emacs
     Extensible text editor with powerful features
  3. nano
     Simple, beginner-friendly text editor
  4. Type something.
────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
```

The tabs line (`☐ Editor`) marks the dialog start; everything above it (including the human's
own numbered prompt in the scrollback) is ignored. Pressing a digit **selects and advances**.
Free text: move to "Type something" (Down ×3 from row 1), type, Enter.

## AskUserQuestion — several questions, multi-select

```
←  ☒ Color  ☐ Fruit  ✔ Submit  →
Which fruits do you like?
❯ 1. [ ] Apple
         A crisp, sweet fruit
  2. [ ] Pear
         A juicy, mild fruit
  3. [✔] Plum
         A soft, tart fruit
  4. [ ] Type something
     Submit
────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel
```

`☒` = answered tab, `☐` = pending tab. Digits **toggle** checkboxes and leave the cursor on the
toggled row; the unnumbered `Submit` row is reached with Down and confirmed with Enter. After
the last question a review screen appears:

```
Review your answers
 ● Which color do you prefer?
   → Green
 ● Which fruits do you like?
   → Apple, Plum
Ready to submit your answers?
❯ 1. Submit answers
  2. Cancel
```

`ccb answer` submits it automatically (Enter on row 1). A single question skips the review.

## Idle prompt and working

```
❯ Try "create a util logging.py that..."
────────────────────────────────────────────────────────────────────────
  ⏸ manual mode on · ? for shortcuts · ← for agents
```

Idle: the footer says `? for shortcuts`. Working: the footer says `esc to interrupt` and a
spinner line like `✻ Swirling… (3s · ↓ 207 tokens · thinking)` is visible.

## While a PermissionRequest hook is running

The dialog is drawn immediately; nothing indicates the hook. A human can answer at the
keyboard; the hook then exits without a decision (see `hook-events.md`).

## Parser output

`ccb screen <name> --json` prints:

```json
{"kind": "question", "title": "Fruit", "question": "Which fruits do you like?",
 "detail": [], "multi_select": true, "cursor": 1,
 "tabs": [{"label": "Color", "done": true}, {"label": "Fruit", "done": false}],
 "options": [{"row": 1, "index": 1, "label": "Apple", "description": "A crisp, sweet fruit", "checked": false, "cursor": true, "special": null}, …,
             {"row": 5, "index": null, "label": "Submit", "description": "", "checked": null, "cursor": false, "special": "submit"}],
 "footer": "Enter to select · Tab/Arrow keys to navigate · Esc to cancel", "answerable": true}
```

`row` is the cursor position in the list (special rows included); `index` is the digit shown.
