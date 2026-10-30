---
name: secrets-never-in-notes
description: "Never paste tokens or keys into a note; heal.py blocks the commit"
metadata:
  type: feedback
---

Tokens, API keys, passwords and `.env` values never go into the vault. `python tools/heal.py --check` fails on them and the pre-commit hook blocks the commit.

Rotation itself is described in [[auth-token-rotation]].
