"""Self-healing indexes for a Markdown memory vault.

    python tools/heal.py                  heal: rebuild indexes, fix what it can, report the rest
    python tools/heal.py --check          no writes; exit 1 if anything needs healing or a secret is found
    python tools/heal.py --install-hook   run --check before every git commit (local pre-commit hook)
    python tools/heal.py --selftest       prove the healing logic on a throwaway vault

The vault root is the folder above tools/, or the VAULT_ROOT environment variable.

What it heals:
  * HOME.md, <global>/_index.md and every <repos>/<repo>/_index.md: the block between
    AUTO markers is rebuilt from the notes on disk, so indexes never go stale.
  * A repo folder without _index.md gets one.
  * A note in a */memory/ folder without front-matter gets name + description.
  * A [[wikilink]] whose target was renamed is repointed when exactly one note matches.
What it only reports: wikilinks it cannot resolve, markdown links to missing files,
duplicate note names, and secrets (tokens, API keys). Secrets also fail --check.
Standard library only, Python 3.10+.
"""
import datetime as dt
import json
import os
import re
import sys
import tempfile

ROOT = os.path.abspath(os.environ.get("VAULT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
START, END = "<!-- AUTO:START -->", "<!-- AUTO:END -->"
SKIP_DIRS = {".git", ".obsidian", "__pycache__", "node_modules"}
TEXT_EXT = (".md", ".txt", ".json", ".jsonl", ".py", ".bat", ".ps1", ".sh", ".csv", ".tsv", ".html", ".js", ".yaml", ".yml")
# Token shapes. The [x] brackets keep these prefixes out of plain-text greps of this file; the regex is the same.
SECRET = re.compile(r"gh[p]_[A-Za-z0-9]{30,}|gh[o]_[A-Za-z0-9]{30,}|github_pa[t]_[A-Za-z0-9_]{40,}|"
                    r"np[m]_[A-Za-z0-9]{30,}|sk-an[t]-[A-Za-z0-9_-]{20,}|sk-pro[j]-[A-Za-z0-9_-]{20,}|"
                    r"ATAT[T][A-Za-z0-9_=-]{20,}|AKI[A][0-9A-Z]{16}|xox[bp]-[A-Za-z0-9-]{20,}|"
                    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
                    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{43}=")  # bare 32-byte base64 key (e.g. an ed25519 seed)
# code spans and blocks hold example links, so link checks skip them
CODE = re.compile(r"(```.*?```|`[^`\n]+`)", re.S)
WIKILINK = re.compile(r"\[\[([^\]|#]+)(#[^\]|]*)?(\|[^\]]*)?\]\]")
MDLINK = re.compile(r"(?<!\\)\]\((?!https?://|mailto:|#)([^)\s]+?\.md)(#[^)]*)?\)")

# ---------------- config (vault-graph.json at the vault root; every key optional) ----------------
DEFAULTS = {
    "port": 8765,                      # local UI server; env VAULT_GRAPH_PORT overrides
    "board": "_global/now.md",         # the task board file
    "repos_dir": "repos",              # one folder per project/repo under here
    "global_dir": "_global",           # notes that apply everywhere
    "entity_patterns": [r"\b[A-Z][A-Z0-9]+-\d+\b",                                  # issue keys: ABC-123
                        r"(?<=github\.com/)[A-Za-z0-9-]+/[A-Za-z0-9._-]*[A-Za-z0-9]"],  # github.com/owner/repo
    "entity_ignore": ["UTF-8", "UTF-16", "SHA-1", "SHA-256", "SHA-512", "ISO-8601"],
    "auto_memory": {"import": True, "strip_prefixes": [], "map": {}, "unmapped": "_inbox", "skip": []},
    "hook": {"matches": 3},
    "launcher": {"permission_mode": "default", "cwd": "", "wmux": ""},
    "plan_usage": False,
    "local_usage": True,
    "idle_shutdown": 1800,
    "daily": [],
}


def load_config(root):
    """DEFAULTS, overridden by <root>/vault-graph.json. Nested dicts merge one level deep."""
    cfg = json.loads(json.dumps(DEFAULTS))
    p = os.path.join(root, "vault-graph.json")
    try:
        with open(p, encoding="utf-8") as fh:
            user = json.load(fh)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as e:
        print(f"vault-graph.json ignored ({e})", file=sys.stderr)
        return cfg
    for k, v in user.items():
        if k.startswith("_"):
            continue  # "_comment" keys
        if isinstance(cfg.get(k), dict) and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def outside_code(text, fn):
    parts = CODE.split(text)
    return "".join(fn(x) if i % 2 == 0 else x for i, x in enumerate(parts))


def norm(s):
    return re.sub(r"[\s_]+", "-", s.strip().lower())


def walk(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for f in sorted(files):
            yield os.path.join(d, f)


def name_of(r):
    """A note's name: its file name without .md; for a skill (<name>/SKILL.md) the folder name."""
    s = os.path.splitext(os.path.basename(r))[0]
    return os.path.basename(os.path.dirname(r)) if s == "SKILL" and os.path.dirname(r) else s


def rel(p, root):
    return os.path.relpath(p, root).replace(os.sep, "/")


def read(p):
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"')
    return meta


def summary(text, limit=110):
    meta = frontmatter(text)
    if meta.get("description"):
        s = meta["description"]
    else:
        body = text.split("\n---", 1)[1] if text.startswith("---") and "\n---" in text[3:] else text
        s = next((l.strip() for l in body.splitlines()
                  if l.strip() and not l.lstrip().startswith(("#", "---", "<!--", "|", "```", ">"))), "")
    s = re.sub(r"[*`\[\]]", "", s)
    return (s[:limit - 1] + "…") if len(s) > limit else s


class Vault:
    def __init__(self, root, cfg=None):
        self.root = root
        cfg = cfg or load_config(root)
        self.repos, self.glob = cfg["repos_dir"].strip("/"), cfg["global_dir"].strip("/")
        self.notes = {}       # rel path -> text (md only)
        self.files = []       # all rel paths
        for p in walk(root):
            r = rel(p, root)
            self.files.append(r)
            if r.endswith(".md"):
                self.notes[r] = read(p)
        self.by_stem = {}
        for r in self.notes:
            self.by_stem.setdefault(name_of(r).lower(), []).append(r)
        self.changes, self.problems, self.secrets = [], [], []

    # ---------- writes (collected, applied by commit()) ----------
    def put(self, r, text):
        if self.notes.get(r) != text:
            self.notes[r] = text
            self.changes.append(r)

    def commit(self):
        for r in sorted(set(self.changes)):
            p = os.path.join(self.root, r)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self.notes[r])

    # ---------- healing steps ----------
    def heal_frontmatter(self):
        for r, t in list(self.notes.items()):
            if "/memory/" in "/" + r and not os.path.basename(r).startswith("_") and not t.startswith("---"):
                stem = os.path.splitext(os.path.basename(r))[0]
                desc = summary(t).replace('"', "'")
                self.put(r, f'---\nname: {stem}\ndescription: "{desc}"\nmetadata:\n  type: project\n---\n\n{t}')

    def heal_wikilinks(self):
        stems_norm = {}
        for s, paths in self.by_stem.items():
            stems_norm.setdefault(norm(s), []).extend(paths)
        for r, t in list(self.notes.items()):

            def fix(m):
                target = m.group(1).strip()
                base = os.path.basename(target).lower()
                if base in self.by_stem:
                    return m.group(0)
                cands = stems_norm.get(norm(base), [])
                if len(cands) == 1:
                    new = name_of(cands[0])
                    return "[[" + new + (m.group(2) or "") + (m.group(3) or "") + "]]"
                self.problems.append(f"broken link [[{target}]] in {r}")
                return m.group(0)

            self.put(r, outside_code(t, lambda x: WIKILINK.sub(fix, x)))

    def check_mdlinks(self):
        for r, t in self.notes.items():
            for m in MDLINK.finditer("".join(CODE.split(t)[0::2])):
                target = os.path.normpath(os.path.join(os.path.dirname(r), m.group(1))).replace(os.sep, "/")
                if target not in self.notes and target != "HOME.md" and not target.startswith("graph/"):
                    self.problems.append(f"missing file {m.group(1)} linked from {r}")

    def check_duplicates(self):
        for s, paths in self.by_stem.items():
            real = [p for p in paths if not os.path.basename(p).startswith("_")]
            if len(real) > 1 and s not in ("readme", "index", "agent", "status", "skill"):  # SKILL.md is a fixed name
                self.problems.append(f"duplicate note name '{s}': " + ", ".join(real))

    def scan_secrets(self):
        for r in self.files:
            if r.lower().endswith(TEXT_EXT):
                text = self.notes.get(r) if r in self.notes else read(os.path.join(self.root, r))
                if SECRET.search(text):
                    self.secrets.append(r)

    # ---------- index building ----------
    def listing(self, folder, index_dir):
        """Notes under `folder`, linked relative to the index file's folder `index_dir`."""
        rows = []
        prefix = folder.rstrip("/") + "/"
        for r in sorted(self.notes):
            if r.startswith(prefix) and not os.path.basename(r).startswith("_index"):
                name = r[len(prefix):]
                link = os.path.relpath(r, index_dir).replace(os.sep, "/")
                rows.append(f"- [{name}]({link}): {summary(self.notes[r])}")
        others = [r for r in self.files if r.startswith(prefix) and not r.endswith(".md")]
        if others:
            rows.append(f"- _plus {len(others)} other file(s): " + ", ".join(sorted({os.path.splitext(o)[1] or o for o in others})) + "_")
        return rows

    def replace_auto(self, r, block, default_header):
        text = self.notes.get(r)
        if text is None or START not in text or END not in text:
            if text is None:
                self.changes.append(r)
            text = (text.rstrip() + "\n\n" if text else default_header) + f"{START}\n{END}\n"
        new = text[:text.index(START) + len(START)] + "\n" + block.rstrip() + "\n" + text[text.index(END):]
        self.put(r, new)

    def repo_folders(self):
        base = os.path.join(self.root, self.repos)
        return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))) if os.path.isdir(base) else []

    def build_repo_indexes(self):
        for repo in self.repo_folders():
            lines = ["_Generated by `python tools/heal.py`. Edit the text above the markers, not below._", ""]
            for sub, title in (("memory", "Memory (what Claude learned)"), ("work", "Work (plans, runbooks, reports)"),
                               ("agents", "Agents")):
                rows = self.listing(f"{self.repos}/{repo}/{sub}", f"{self.repos}/{repo}")
                lines += [f"### {title}", ""] + (rows or ["- _none yet_"]) + [""]
            up = "../" * (self.repos.count("/") + 2)
            header = (f"# {repo}\n\n- **Repository:** _add link_\n- **Local clone:** _add path_\n- **What it is:** _one line_\n\n"
                      f"Back to [HOME]({up}HOME.md)\n\n")
            self.replace_auto(f"{self.repos}/{repo}/_index.md", "\n".join(lines), header)

    def build_global_index(self):
        base = os.path.join(self.root, self.glob)
        if not os.path.isdir(base):
            return
        lines = ["_Generated by `python tools/heal.py`._", ""]
        for sub in sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))):
            rows = self.listing(f"{self.glob}/{sub}", self.glob)
            lines += [f"### {sub}", ""] + (rows or ["- _empty_"]) + [""]
        up = "../" * (self.glob.count("/") + 1)
        self.replace_auto(f"{self.glob}/_index.md", "\n".join(lines),
                          f"# Global (applies to every repo)\n\nBack to [HOME]({up}HOME.md)\n\n")

    def build_home(self, today):
        rows = ["| Repo | Memory | Work | Agents |", "|---|---|---|---|"]
        for repo in self.repo_folders():
            c = {s: sum(1 for r in self.notes if r.startswith(f"{self.repos}/{repo}/{s}/")) for s in ("memory", "work", "agents")}
            rows.append(f"| [{repo}]({self.repos}/{repo}/_index.md) | {c['memory']} | {c['work']} | {c['agents']} |")
        g = os.path.join(self.root, self.glob)
        areas = sorted(d for d in os.listdir(g) if os.path.isdir(os.path.join(g, d))) if os.path.isdir(g) else []
        n_notes = sum(1 for r in self.notes if r != "HOME.md" and not os.path.basename(r).startswith("_index"))
        health = "healthy" if not self.problems and not self.secrets else (
            f"{len(self.problems)} problem(s), {len(self.secrets)} secret(s), see `python tools/heal.py`")
        block = "\n".join([
            f"_Rebuilt by `python tools/heal.py` on {today}. {n_notes} notes. Status: **{health}**._", "",
            "### Repos", "", *rows, "",
            "### Global", "", f"[Global index]({self.glob}/_index.md): " + (", ".join(areas) or "_empty_"),
        ])
        self.replace_auto("HOME.md", block, "# HOME\n\n")

    def run(self, write, today):
        self.heal_frontmatter()
        self.heal_wikilinks()
        self.build_repo_indexes()
        self.build_global_index()
        self.check_mdlinks()
        self.check_duplicates()
        self.scan_secrets()
        self.build_home(today)
        if write:
            self.commit()
        return self


def report(v, write):
    verb = "healed" if write else "would heal"
    print(f"{verb}: {len(set(v.changes))} file(s)" + ("" if not v.changes else ": " + ", ".join(sorted(set(v.changes))[:12])))
    for p in v.problems:
        print("  problem:", p)
    for s in v.secrets:
        print("  SECRET FOUND (remove it before committing):", s)
    print("status:", "healthy" if not v.problems and not v.secrets else "needs attention")


def install_hook(root):
    """Local git pre-commit hook: refuse the commit while heal.py --check fails. Returns False without .git."""
    hooks = os.path.join(root, ".git", "hooks")
    if not os.path.isdir(hooks):
        print("no .git folder in the vault: skipped the pre-commit check (run `git init` first if you want it)")
        return False
    path = os.path.join(hooks, "pre-commit")
    script = os.path.relpath(os.path.abspath(__file__), root).replace(os.sep, "/")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'#!/bin/sh\npython "{script}" --check || {{ echo "Run: python {script}, then commit again."; exit 1; }}\n')
    os.chmod(path, 0o755)
    print("installed", path)
    return True


def selftest():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "repos", "demo", "memory"))
        os.makedirs(os.path.join(d, "_global", "memory"))
        with open(os.path.join(d, "repos", "demo", "memory", "release-flow.md"), "w", encoding="utf-8") as fh:
            fh.write("How we release ABC-123. See [[Release_Flow]] and [[missing-note]].\n")
        with open(os.path.join(d, "_global", "memory", "rules.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: rules\ndescription: house rules\n---\nkey " + "gh" + "p_" + "a" * 36 + "\n")
        v = Vault(d).run(write=True, today="2000-01-01")
        note = read(os.path.join(d, "repos", "demo", "memory", "release-flow.md"))
        assert note.startswith("---\nname: release-flow"), "front-matter not added"
        assert "[[release-flow]]" in note, "renamed link not repointed"
        assert any("missing-note" in p for p in v.problems), "unresolvable link not reported"
        assert v.secrets == ["_global/memory/rules.md"], "secret not detected"
        assert os.path.exists(os.path.join(d, "repos", "demo", "_index.md")), "repo index not created"
        assert "[demo](repos/demo/_index.md)" in read(os.path.join(d, "HOME.md")), "HOME map not built"
        again = Vault(d).run(write=False, today="2000-01-01")
        assert not again.changes, "second run should change nothing (idempotent)"
        # custom folder names from vault-graph.json
        with open(os.path.join(d, "vault-graph.json"), "w", encoding="utf-8") as fh:
            json.dump({"repos_dir": "projects", "_comment": "ignored"}, fh)
        os.makedirs(os.path.join(d, "projects", "acme-api", "memory"))
        Vault(d).run(write=True, today="2000-01-01")
        assert os.path.exists(os.path.join(d, "projects", "acme-api", "_index.md")), "repos_dir from config ignored"
    print("selftest passed")


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    elif "--install-hook" in args:
        install_hook(ROOT)
    else:
        check = "--check" in args
        v = Vault(ROOT).run(write=not check, today=dt.date.today().isoformat())
        report(v, write=not check)
        if check and (v.changes or v.secrets):
            sys.exit(1)
