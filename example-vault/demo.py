"""Try vault-graph on this made-up demo vault.

    python example-vault/demo.py

Writes a little fake agent activity (so the activity panel is not empty), heals the indexes,
builds the graph and opens the live UI on http://127.0.0.1:8766/ (port set in vault-graph.json).
Nothing outside example-vault/ is touched.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), "tools")
GRAPH = os.path.join(HERE, "graph")

# (seconds ago, who, what, detail)
EVENTS = [
    (300, "main", "Read", "now.md"),
    (290, "main", "Bash", "python tools/vault.py search \"token rotation\""),
    (282, "main", "Read", "auth-token-rotation.md"),
    (270, "main", "Read", "v2-migration-plan.md"),
    (255, "main", "Grep", "ACME-142"),
    (240, "main", "Agent", "vault-navigator: what do we know about ACME-150"),
    (240, "vault-navigator", "started", "what do we know about ACME-150"),
    (232, "vault-navigator", "Bash", "python tools/vault.py entity ACME-150"),
    (220, "vault-navigator", "done", "finished"),
    (200, "main", "Edit", "auth-token-rotation.md"),
    (180, "main", "Bash", "pytest tests/auth -q"),
    (150, "main", "Read", "db-migration-order.md"),
    (120, "main", "Skill", "changelog-writer"),
    (95, "main", "Agent", "release-helper: draft notes for Tuesday's train"),
    (95, "release-helper", "started", "draft notes for Tuesday's train"),
    (80, "release-helper", "Read", "release-train.md"),
    (60, "release-helper", "Bash", "gh pr list --state merged --limit 20"),
    (35, "release-helper", "Read", "writing-style.md"),
    (12, "main", "Edit", "now.md"),
    (4, "release-helper", "Write", "release-notes-draft.md"),
]


def main():
    os.makedirs(GRAPH, exist_ok=True)
    now = time.time()
    with open(os.path.join(GRAPH, "activity-log.jsonl"), "w", encoding="utf-8") as fh:
        for ago, who, what, detail in EVENTS:
            fh.write(json.dumps({"ts": round(now - ago, 2), "who": who, "what": what, "detail": detail}) + "\n")
    with open(os.path.join(GRAPH, "activity.json"), "w", encoding="utf-8") as fh:
        json.dump({"release-helper": {"kind": "agent", "since": now - 95, "desc": "draft notes for Tuesday's train"},
                   "changelog-writer": {"kind": "skill", "since": now - 5, "desc": "skill in use"}}, fh)
    env = dict(os.environ, VAULT_ROOT=HERE)
    subprocess.run([sys.executable, os.path.join(TOOLS, "heal.py")], env=env, check=True)
    subprocess.run([sys.executable, os.path.join(TOOLS, "vault.py"), "--root", HERE, "build"], env=env, check=True)


if __name__ == "__main__":
    main()
