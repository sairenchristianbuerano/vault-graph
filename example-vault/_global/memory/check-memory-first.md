---
name: check-memory-first
description: "Before any task, search the vault and read the board"
metadata:
  type: feedback
---

Before starting work, read the notes the prompt hook injected, run `python tools/vault.py search "<words>"`, and check the board for open tasks.

**Why:** most mistakes repeat something we already learned.

**How to apply:** search first, then act. Rules that apply everywhere: [[writing-style]], [[pr-flow]], [[secrets-never-in-notes]].
