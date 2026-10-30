#!/usr/bin/env python3
"""vault-graph: a live star map, task board and Claude Code memory hook for a folder of Markdown notes.

  python tools/vault.py [--root DIR] <command>

  sync                    import Claude Code auto-memory, heal links and indexes, rebuild the graph
  build                   rebuild graph/graph.json and graph/GRAPH_REPORT.md
  ui                      open the live graph in the browser (starts the local server if needed)
  serve                   run the local server in the foreground
  search "words" [-n 8]   ranked notes with the best matching line
  note <name> [--full]    one note: description, links, backlinks, entities
  related <name> [-n 10]  notes connected to it, with the reason
  path <a> <b>            shortest chain of links between two notes or entities (lit up in the UI)
  entity <ABC-123 | owner/repo>   every note that names it
  report                  print graph/GRAPH_REPORT.md
  task list | task add <section> "text" | task move "<words>" <section>
  ask <key> "what"        log something Claude had to ask the user; on the 2nd time, suggests a skill
  daily                   run the `daily` commands from vault-graph.json, keep their output for the hook
  setup [--dry-run] [--force]   install the Claude Code hooks (backs up ~/.claude/settings.json first)
  hook | activity <start|stop|idle>   Claude Code hook entry points (they read JSON on stdin)
  selftest

The vault root is the folder above tools/, or --root DIR, or the VAULT_ROOT environment variable.
Settings: optional vault-graph.json at the vault root (see vault-graph.example.json).
Exit codes: 0 answer, 3 ambiguous name (candidates printed), 4 no match.
Standard library only, Python 3.10+. Builds on tools/heal.py.
"""
import datetime as dt
import glob
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter, defaultdict, deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import heal  # noqa: E402

DEFAULT_ROOT = os.path.dirname(HERE)
GENERATED = ("HOME.md", "graph/")
SECTIONS = ("Ongoing", "Next", "Open", "Pending", "Done")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")
MAX_RESUME = 20_000_000  # bytes; bigger sessions start fresh with a pointer instead of --resume
TTL = {"agent": 900, "skill": 60}  # seconds; stale entries expire if a stop event is missed


def configure(root):
    """Point every path at this vault and load its vault-graph.json."""
    global ROOT, CFG, ENTITY, IGNORE, REPOS, BOARD, BOARD_REL, PORT, GRAPH, ACTIVITY, SESSION, WMUX_MAP, LOG, DAILY
    ROOT = os.path.abspath(root)
    CFG = heal.load_config(ROOT)
    ENTITY = [re.compile(p) for p in CFG["entity_patterns"]]
    IGNORE = set(CFG["entity_ignore"])
    REPOS = CFG["repos_dir"].strip("/")
    BOARD_REL = CFG["board"].replace("\\", "/")
    BOARD = os.path.join(ROOT, BOARD_REL)
    PORT = int(os.environ.get("VAULT_GRAPH_PORT") or CFG["port"])
    GRAPH = os.path.join(ROOT, "graph")
    ACTIVITY = os.path.join(GRAPH, "activity.json")
    SESSION = os.path.join(GRAPH, "session.json")          # last main-session id seen by a hook
    WMUX_MAP = os.path.join(GRAPH, "wmux-sessions.json")   # session id -> the wmux tab (pty) it runs in
    LOG = os.path.join(GRAPH, "activity-log.jsonl")
    DAILY = os.path.join(GRAPH, "daily.json")


configure(heal.ROOT)


def stem(r):
    return heal.name_of(r)


def repo_of(r):
    p = r.split("/")
    if r.startswith(REPOS + "/") and len(p) > REPOS.count("/") + 2:
        return p[REPOS.count("/") + 1]
    return p[0] if len(p) > 1 else "root"


def plain(t):
    return "".join(heal.CODE.split(t)[0::2])


def body_of(t):
    if t.startswith("---"):
        end = t.find("\n---", 3)
        if end > 0:
            return t[end + 4:]
    return t


def entities(text):
    return {m.group(0) for rx in ENTITY for m in rx.finditer(text)} - IGNORE


def load(root):
    """Notes a person or agent wrote. Generated files (HOME, _index, graph/) are left out."""
    notes = {}
    for p in heal.walk(root):
        r = heal.rel(p, root)
        if not r.endswith(".md") or r.startswith(GENERATED) or os.path.basename(r) == "_index.md":
            continue
        notes[r] = heal.read(p)
    return notes


# ---------------- graph ----------------
def build_graph(notes):
    by_stem = defaultdict(list)
    for r in notes:
        by_stem[stem(r).lower()].append(r)
    nodes, edges = {}, set()
    for r, t in notes.items():
        fm = t[:t.find("\n---", 3)] if t.startswith("---") else ""
        typ = (re.search(r"^\s*type:\s*(\S+)", fm, re.M) or [None, ""])[1]
        nodes[r] = {"kind": "note", "repo": repo_of(r), "type": typ, "desc": heal.summary(t)}
        txt = plain(t)
        for m in heal.WIKILINK.finditer(txt):
            c = by_stem.get(os.path.basename(m.group(1).strip()).lower(), [])
            if len(c) == 1 and c[0] != r:
                edges.add((r, c[0], "links"))
        for m in heal.MDLINK.finditer(txt):
            tgt = os.path.normpath(os.path.join(os.path.dirname(r), m.group(1))).replace(os.sep, "/")
            if tgt in notes and tgt != r:
                edges.add((r, tgt, "links"))
        for e in entities(txt):
            nodes.setdefault(e, {"kind": "entity"})
            edges.add((r, e, "mentions"))
    adj = defaultdict(dict)
    for a, b, k in edges:
        adj[a][b] = k
        adj[b][a] = "linked from" if k == "links" else "mentioned in"
    return nodes, sorted(edges), adj


def build(root, quiet=False, highlight=None):
    notes = load(root)
    nodes, edges, adj = build_graph(notes)
    out = os.path.join(root, "graph")
    os.makedirs(out, exist_ok=True)
    gj = os.path.join(out, "graph.json")
    try:
        with open(gj, encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = None
    if highlight is None:  # light up what is new since the last build
        if old is None:
            highlight = {"reason": "First build", "nodes": [], "edges": []}
        else:
            seen = {tuple(e) for e in old.get("edges", [])}
            new_e = [list(e) for e in edges if e not in seen]
            new_n = [n for n in nodes if n not in old.get("nodes", {})]
            if new_e or new_n:
                highlight = {"reason": f"New since last build: {len(new_e)} link(s), {len(new_n)} node(s)",
                             "nodes": sorted({x for e in new_e for x in e[:2]} | set(new_n)), "edges": new_e}
            else:
                highlight = old.get("highlight") or {"reason": "No changes", "nodes": [], "edges": []}
    with open(gj, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"built": dt.datetime.now().isoformat(timespec="seconds"), "highlight": highlight,
                   "nodes": nodes, "edges": edges}, fh, indent=1, sort_keys=True)
    note_ids = [n for n, v in nodes.items() if v["kind"] == "note"]
    link = lambda n: f"[{stem(n)}](../{n})"  # noqa: E731
    deg = {n: len(adj[n]) for n in note_ids}
    gods = sorted(note_ids, key=lambda n: (-deg[n], n))[:12]
    hubs = sorted((n for n, v in nodes.items() if v["kind"] == "entity"), key=lambda e: (-len(adj[e]), e))[:12]
    orphans = sorted(n for n in note_ids if "/memory/" in n and not any(k in ("links", "linked from") for k in adj[n].values()))
    bridges = sorted({(a, b) for a, b, k in edges if k == "links" and nodes[a]["repo"] != nodes[b]["repo"]})
    # plain-text mentions of another note's name that are not linked yet
    names = {stem(n).lower(): n for n in note_ids if "/memory/" in n and len(stem(n)) >= 12}
    unlinked = []
    for n in note_ids:
        low = plain(notes[n]).lower()
        for s, tgt in names.items():
            if tgt != n and tgt not in adj[n] and re.search(r"(?<![\w\[/-])" + re.escape(s) + r"(?![\w.-])", low):
                unlinked.append((n, tgt))
    by_repo = Counter(nodes[n]["repo"] for n in note_ids)
    lines = [
        "# Graph report",
        "",
        "Built by `python tools/vault.py build` (or `sync`). Generated, do not edit. Back to [HOME](../HOME.md)",
        "",
        f"**{len(note_ids)} notes, {len(nodes) - len(note_ids)} entities, "
        f"{sum(1 for e in edges if e[2] == 'links')} links, {sum(1 for e in edges if e[2] == 'mentions')} mentions.**",
        "",
        "## Notes per area", "", *[f"- {k}: {v}" for k, v in sorted(by_repo.items())], "",
        "## Most connected notes", "", *[f"- {link(n)}: {deg[n]} connections" for n in gods], "",
        "## Hub entities (issue keys and repos named in most notes)", "",
        *[f"- `{e}`: {len(adj[e])} notes" for e in hubs], "",
        "## Bridges (links between areas)", "",
        *([f"- {link(a)} → {link(b)}" for a, b in bridges[:25]] or ["- none"]), "",
        "## Orphan memory notes (no links in or out; consider linking)", "",
        *([f"- {link(n)}" for n in orphans] or ["- none"]), "",
        "## Unlinked mentions (the name appears as plain text; turn it into a wikilink)", "",
        *([f"- {link(a)} mentions {link(b)}" for a, b in unlinked[:40]] or ["- none"]), "",
    ]
    with open(os.path.join(out, "GRAPH_REPORT.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    if not quiet:
        print(f"graph: {len(note_ids)} notes, {len(nodes) - len(note_ids)} entities, {len(edges)} edges; "
              f"{len(orphans)} orphans, {len(unlinked)} unlinked mentions -> graph/GRAPH_REPORT.md")
    return nodes, edges, adj


# ---------------- name lookup ----------------
def resolve(name, nodes):
    if name in nodes:
        return name
    key = name.replace("\\", "/").lower().removesuffix(".md")
    exact = [n for n in nodes if n.lower().removesuffix(".md") == key or stem(n).lower() == key or n.lower() == key]
    if len(exact) == 1:
        return exact[0]
    cands = exact or [n for n in nodes if heal.norm(key) in heal.norm(stem(n))]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        print(f"no note or entity matches '{name}'. Try: vault.py search \"{name}\"")
        sys.exit(4)
    print(f"'{name}' is ambiguous; pick one:")
    for c in sorted(cands)[:12]:
        print("  ", c)
    sys.exit(3)


# ---------------- queries ----------------
def tokens(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def search(root, q, n=8):
    notes = load(root)
    terms = [w for w in dict.fromkeys(tokens(q)) if len(w) > 1]
    if not terms:
        sys.exit("empty query")
    docs = {r: (set(tokens(stem(r))), set(tokens(heal.summary(t))), Counter(tokens(body_of(t)))) for r, t in notes.items()}
    df = Counter(w for d in docs.values() for w in terms if w in d[0] or w in d[1] or w in d[2])
    idf = {w: math.log(1 + len(docs) / df[w]) for w in terms if df[w]}
    scored = []
    for r, (nm, ds, bd) in docs.items():
        s = sum(idf[w] * (3 * (w in nm) + 2 * (w in ds) + math.log1p(bd[w])) for w in idf)
        hit = sum(1 for w in idf if w in nm or w in ds or bd[w])
        if hit:
            scored.append((-(s * hit), r))
    if not scored:
        print(f"no match for: {q}")
        sys.exit(4)
    for i, (_, r) in enumerate(sorted(scored)[:n], 1):
        print(f"{i}. {r} - {heal.summary(notes[r])}")
        lines = notes[r].splitlines()
        skip = notes[r].count("\n", 0, len(notes[r]) - len(body_of(notes[r])))  # front-matter lines
        best = sorted((-sum(w in l.lower() for w in idf), j) for j, l in enumerate(lines)
                      if j >= skip and any(w in l.lower() for w in idf))
        for _, j in best[:1]:
            print(f"   L{j + 1}: {lines[j].strip()[:170]}")


def note(root, name, full=False):
    notes = load(root)
    nodes, edges, adj = build_graph(notes)
    r = resolve(name, nodes)
    if nodes[r]["kind"] == "entity":
        return entity(root, r)
    v = nodes[r]
    print(f"{r}\n  area: {v['repo']}  type: {v['type'] or '-'}\n  {v['desc']}")
    for label in ("links", "linked from", "mentions"):
        items = sorted(k for k, how in adj[r].items() if how == label)
        if items:
            print(f"  {label}: " + ", ".join(stem(i) if i in notes else i for i in items))
    if full:
        print("-" * 60)
        print(body_of(notes[r]).strip())
    else:
        heads = [l.strip() for l in body_of(notes[r]).splitlines() if l.startswith("#")]
        if heads:
            print("  sections: " + " | ".join(heads[:12]))
        print("  (add --full for the whole note)")


def related(root, name, n=10):
    nodes, edges, adj = build_graph(load(root))
    r = resolve(name, nodes)
    score, why = Counter(), defaultdict(list)
    for nb, how in adj[r].items():
        if nodes[nb]["kind"] == "note":
            score[nb] += 3
            why[nb].append(how)
        else:
            for nb2 in adj[nb]:
                if nb2 != r:
                    score[nb2] += 1
                    why[nb2].append(f"shares {nb}")
    if not score:
        print(f"{r}: no connections yet (see 'Orphan memory notes' in graph/GRAPH_REPORT.md)")
        sys.exit(4)
    for nb, s in score.most_common(n):
        print(f"- {nb}  ({', '.join(dict.fromkeys(why[nb]))[:120]})")


def path(root, a, b):
    nodes, edges, adj = build_graph(load(root))
    a, b = resolve(a, nodes), resolve(b, nodes)
    prev, q = {a: None}, deque([a])
    while q:
        x = q.popleft()
        if x == b:
            break
        for y in sorted(adj[x]):
            if y not in prev:
                prev[y] = x
                q.append(y)
    if b not in prev:
        print(f"no path between {a} and {b}")
        sys.exit(4)
    chain = [b]
    while prev[chain[-1]]:
        chain.append(prev[chain[-1]])
    chain.reverse()
    print(f"shortest path ({len(chain) - 1} hops):")
    for x, y in zip(chain, chain[1:]):
        print(f"  {x} --{adj[x][y]}--> {y}")
    build(root, quiet=True, highlight={"reason": f"Path: {stem(a)} -> {stem(b)} ({len(chain) - 1} hops)",
                                        "nodes": chain, "edges": [[x, y] for x, y in zip(chain, chain[1:])]})


def entity(root, name):
    notes = load(root)
    key = name.lower()
    hits = 0
    for r, t in sorted(notes.items()):
        lines = [(j, l) for j, l in enumerate(t.splitlines()) if key in l.lower()]
        if lines:
            hits += 1
            print(f"- {r} - {heal.summary(t)}")
            for j, l in lines[:2]:
                print(f"    L{j + 1}: {l.strip()[:170]}")
    if not hits:
        print(f"'{name}' is not mentioned")
        sys.exit(4)


# ---------------- sync: Claude Code auto-memory -> vault ----------------
def repo_for(project, am):
    """Claude project folder name -> vault folder, "_inbox", or None (skip).
    Folder names are the session's working directory with separators turned into '-',
    e.g. C--Users-alex-code-acme-api or -home-alex-code-acme-api."""
    p = re.sub(r"^(?:[A-Za-z]-)?-(?:Users|home)-[^-]+-", "", project)
    for pre in am.get("strip_prefixes", []):
        if p.startswith(pre):
            p = p[len(pre):]
            break
    if p in am.get("map", {}):
        return am["map"][p]
    how = am.get("unmapped", "_inbox")
    return None if how == "skip" else "_inbox" if how == "_inbox" else p


def import_auto_memory(root, cfg, home=None):
    """One-way: Claude Code's auto-memory folders -> vault, filed by project into <repos>/<repo>/memory/."""
    am = cfg["auto_memory"]
    if not am.get("import", True):
        print("auto-memory: import is off (vault-graph.json auto_memory.import)")
        return
    home = home or os.path.expanduser("~")
    vault = defaultdict(list)
    for r in load(root):
        vault[stem(r).lower()].append(r)
    skip = {s.lower() for s in am.get("skip", [])}  # names merged, renamed or deleted on purpose
    repos = cfg["repos_dir"].strip("/")
    new = upd = kept = 0
    for src in sorted(glob.glob(os.path.join(home, ".claude", "projects", "*", "memory", "*.md"))):
        s = stem(src)
        if s == "MEMORY" or s.lower() in skip:
            continue
        text = heal.read(src).replace("\r\n", "\n")
        dst = vault.get(s.lower(), [])
        if len(dst) > 1:
            print(f"  skip {s}: several vault notes share this name: {dst}")
            continue
        repo = repo_for(os.path.basename(os.path.dirname(os.path.dirname(src))), am)
        if repo is None and not dst:
            continue
        home_dir = os.path.join(root, "_inbox") if repo == "_inbox" else os.path.join(root, repos, repo or "_", "memory")
        target = os.path.join(root, dst[0]) if dst else os.path.join(home_dir, s + ".md")
        if os.path.exists(target):
            if heal.read(target).replace("\r\n", "\n").strip() == text.strip():
                continue
            if os.path.getmtime(src) <= os.path.getmtime(target):
                kept += 1
                continue
            upd += 1
        else:
            new += 1
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"  {'updated' if dst else 'new'}: {heal.rel(target, root)}")
    print(f"auto-memory: {new} new, {upd} updated, {kept} kept (vault copy newer)")
    inbox = glob.glob(os.path.join(root, "_inbox", "*.md"))
    if inbox:
        print(f"  {len(inbox)} note(s) in _inbox/ waiting to be filed under {repos}/<repo>/memory or {cfg['global_dir']}/memory")


def sync(root):
    import_auto_memory(root, CFG)
    v = heal.Vault(root, CFG).run(write=True, today=dt.date.today().isoformat())
    heal.report(v, write=True)
    build(root)


# ---------------- prompt hook ----------------
STOP = set("the a an and or but to of in on for with is are was be it this that these those i you we me my our "
           "your do does did can could should would will pls please make sure just also there here what how why "
           "when where which who get got have has had not no yes ok okay then than from about into out up all any "
           "every first thing doing before after now new use using want need let lets".split())


def hook():
    """UserPromptSubmit hook: search the vault for the prompt, hand Claude the top notes. Never blocks."""
    import contextlib
    import io
    try:
        data = json.load(sys.stdin)
        prompt = data.get("prompt", "")
        try:
            note_wmux_tab(data.get("session_id"))
        except OSError:
            pass
        words = [w for w in tokens(prompt) if len(w) > 2 and w not in STOP]
        if len(words) < 2:
            return
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                search(ROOT, " ".join(dict.fromkeys(words))[:300], int(CFG["hook"].get("matches", 3)))
            except SystemExit:
                pass
        hits = buf.getvalue().strip()
        ctx = (f"Check the memory vault first (vault: {ROOT}). Read {BOARD_REL} for open tasks, check "
               ".claude/skills before asking the user to do something by hand, and run "
               "`python tools/vault.py search|note|entity` for more.")
        if hits and not hits.startswith("no match"):
            ctx += "\nTop vault matches for this prompt:\n" + hits[:1500]
        if CFG["daily"]:
            today = time.strftime("%Y-%m-%d")
            d = read_daily()
            if d.get("date") != today:  # the scheduled run was missed (PC off): start it in the background
                write_daily({"date": today, "running": True, "ts": d.get("ts")})
                subprocess.Popen([sys.executable, os.path.abspath(__file__), "--root", ROOT, "daily"], cwd=ROOT,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            elif not d.get("running") and not d.get("shown"):  # show once a day, then stay quiet
                ctx += "\nTell the user first, in one or two lines: " + daily_text(d)
                write_daily(dict(d, shown=True))
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}))
    except Exception:  # noqa: BLE001
        pass  # a memory lookup must never block the prompt


# ---------------- agent activity (for the live graph) ----------------
def note_wmux_tab(session_id):
    """Hooks run inside the Claude process, so WMUX_PTY_ID says which wmux tab hosts this session."""
    pty = os.environ.get("WMUX_PTY_ID")
    if not (session_id and pty):
        return
    try:
        with open(WMUX_MAP, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        m = {}
    m[session_id] = {"pty": pty, "ts": time.time()}
    os.makedirs(GRAPH, exist_ok=True)
    with open(WMUX_MAP, "w", encoding="utf-8") as fh:
        json.dump(m, fh)


def wmux_tab_of(session_id):
    try:
        with open(WMUX_MAP, encoding="utf-8") as fh:
            return (json.load(fh).get(session_id) or {}).get("pty")
    except (OSError, ValueError):
        return None


def log_event(who, what, detail=""):
    """One line per event. Secrets redacted, details cut short, file kept to the last 500 lines."""
    line = json.dumps({"ts": round(time.time(), 2), "who": who, "what": what,
                       "detail": heal.SECRET.sub("[redacted]", str(detail)).replace("\n", " ")[:120]})
    os.makedirs(GRAPH, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if os.path.getsize(LOG) > 250_000:
        keep = heal.read(LOG).splitlines()[-500:]
        with open(LOG, "w", encoding="utf-8") as fh:
            fh.write("\n".join(keep) + "\n")


def read_log(n=80):
    try:
        lines = heal.read(LOG).splitlines()[-n:]
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def tool_detail(tool, inp):
    if tool in ("Agent", "Task"):
        return f"{inp.get('subagent_type', 'general-purpose')}: {inp.get('description', '')}"
    for key in ("description", "file_path", "notebook_path", "pattern", "skill", "url", "query", "command"):
        if inp.get(key):
            v = str(inp[key])
            return os.path.basename(v) if key.endswith("path") else v
    return ""


def read_activity():
    try:
        with open(ACTIVITY, encoding="utf-8") as fh:
            act = json.load(fh)
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in act.items() if now - v.get("since", 0) < TTL.get(v.get("kind"), 900)}


def activity(event):
    """Hook: PreToolUse -> log (and Agent/Skill -> running); PostToolUse Agent or SubagentStop -> done;
    Stop -> idle. Never blocks."""
    try:
        data = json.load(sys.stdin)
        tool, inp = data.get("tool_name", ""), data.get("tool_input") or {}
        who = data.get("agent_type") or data.get("subagent_type") or "main"
        os.makedirs(GRAPH, exist_ok=True)
        if data.get("session_id") and who == "main":  # `task add` stamps this session on new tasks
            with open(SESSION, "w", encoding="utf-8") as fh:
                json.dump({"id": data["session_id"], "cwd": data.get("cwd", ""), "ts": time.time()}, fh)
            note_wmux_tab(data["session_id"])
        if event == "start" and tool:
            short = tool.split("__")[-1] if tool.startswith("mcp__") else tool
            log_event(who, short, tool_detail(tool, inp))
        elif event == "idle":
            log_event("main", "idle", "turn finished, waiting for the user")
            return
        act = read_activity()
        if event == "start" and tool in ("Agent", "Task"):
            name = inp.get("subagent_type") or "general-purpose"
            act[name] = {"kind": "agent", "since": time.time(), "desc": (inp.get("description") or "")[:80]}
            log_event(name, "started", inp.get("description") or "")
        elif event == "start" and tool == "Skill":
            name = (inp.get("skill") or "").split(":")[-1]
            if name:
                act[name] = {"kind": "skill", "since": time.time(), "desc": "skill in use"}
        elif event == "stop":
            name = inp.get("subagent_type") or data.get("agent_type") or data.get("subagent_type")
            if tool in ("Agent", "Task") and inp.get("run_in_background"):
                return  # background agent keeps running; SubagentStop ends it
            if not name:  # SubagentStop without a name: end the oldest running agent
                agents = sorted((v["since"], k) for k, v in act.items() if v["kind"] == "agent")
                name = agents[0][1] if agents else None
            if name and name in act:
                act.pop(name, None)
                log_event(name, "done", "finished")
        else:
            return
        with open(ACTIVITY, "w", encoding="utf-8") as fh:
            json.dump(act, fh)
    except Exception:  # noqa: BLE001
        pass


# ---------------- task board ----------------
BOARD_TEMPLATE = "# Now: task board (updated {date})\n\n" + "".join(f"## {s}\n\n" for s in SECTIONS)


def today_short():
    return f"{dt.date.today():%d %b}".lstrip("0")


def read_board(path=None):
    """{section: [task, ...]} from the five headings; sub-bullets join their parent with ' · '."""
    board, cur = {k: [] for k in SECTIONS}, None
    try:
        lines = heal.read(path or BOARD).splitlines()
    except OSError:
        return board
    for l in lines:
        if l.startswith("## "):
            cur = l[3:].strip() if l[3:].strip() in SECTIONS else None
        elif cur and l.startswith("- "):
            board[cur].append(l[2:].strip())
        elif cur and l.startswith("  - ") and board[cur]:
            board[cur][-1] += " · " + l[4:].strip()
    return board


def _insert(lines, where, block):
    """Put `block` at the top of section `where`, adding the heading if it is missing."""
    if f"## {where}" not in lines:
        lines += ["", f"## {where}", ""]
    k = lines.index(f"## {where}") + 1
    while k < len(lines) and not lines[k].strip():
        k += 1
    lines[k:k] = block


def _write_board(path, lines):
    text = "\n".join(lines).rstrip("\n") + "\n"
    text = re.sub(r"# Now: task board \(updated [^)]*\)", f"# Now: task board (updated {today_short()} {dt.date.today():%Y})", text)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def task(cmd, args, path=None):
    """task add <section> "text" | task move "<words>" <section> | task list"""
    path = path or BOARD
    if cmd == "list":
        for k, v in read_board(path).items():
            print(f"{k} ({len(v)})")
            for t in v:
                print("  -", t[:140])
        return
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(BOARD_TEMPLATE.format(date=today_short()))
    lines = heal.read(path).splitlines()
    sec = {k.lower(): k for k in SECTIONS}
    if cmd == "add":
        where, item = sec.get((args[0] if args else "").lower()), " ".join(args[1:])
        if not where or not item:
            sys.exit("usage: task add <ongoing|next|open|pending|done> \"text\"")
        if where == "Done":
            item = f"{today_short()}: {item}"
        try:  # remember which Claude session added it, so "Do it in Claude" can resume that session
            with open(SESSION, encoding="utf-8") as fh:
                sid = json.load(fh)["id"]
            if "(s:" not in item and time.time() - os.path.getmtime(SESSION) < 3600:
                item += f" (s:{sid[:8]})"
        except (OSError, ValueError, KeyError):
            pass
        _insert(lines, where, [f"- {item}"])
    elif cmd == "move":
        where = sec.get(args[-1].lower()) if len(args) >= 2 else None
        words = args[0].lower() if args else ""
        hits = [i for i, l in enumerate(lines) if l.startswith("- ") and words in l.lower()]
        if not where or len(hits) != 1:
            sys.exit(f"need exactly one task matching '{words}' and a section; matched {len(hits)}")
        i = hits[0]
        j = i + 1
        while j < len(lines) and lines[j].startswith("  "):  # only the sub-items directly under it
            j += 1
        block = lines[i:j]
        del lines[i:j]
        if where == "Done":
            block[0] = f"- {today_short()}: " + block[0][2:]
        _insert(lines, where, block)
    else:
        sys.exit("task add|move|list")
    _write_board(path, lines)
    print("board updated:", {k: len(v) for k, v in read_board(path).items()})


def strip_tags(text):
    """'[85%] [done 30%] @you text (s:abcd1234)' -> 'text'."""
    t = re.sub(r"^\[\d{1,3}%\]\s*(\[done \d{1,3}%\]\s*)?", "", text)
    t = re.sub(r"\s*\(s:[0-9a-f]{8}\)", "", t)
    return re.sub(r"(^|\s)@you\b\s*", r"\1", t).strip()


def mark_done(sec, text, note, path=None):
    """Move this exact task (and its sub-items) to Done, tags stripped, with the user's note appended."""
    path = path or BOARD
    lines = heal.read(path).splitlines()
    text = text.split(" · ")[0]  # read_board joins sub-items onto the parent with " · "
    i = lines.index("- " + text)  # caller already checked it is on the board
    j = i + 1
    while j < len(lines) and lines[j].startswith("  "):
        j += 1
    subs = lines[i + 1:j]
    del lines[i:j]
    _insert(lines, "Done", [f"- {today_short()}: " + strip_tags(text) + (f" -- Note: {note}" if note else "")] + subs)
    _write_board(path, lines)


def task_prompt(sec, text):
    """Same wording as copyPrompt() in graph.html."""
    plain_text = re.sub(r"\[\[([^\]|]+)(\|[^\]]*)?\]\]", r"\1", strip_tags(text))
    return (f"Task from my board ({BOARD_REL}, {sec}): {plain_text} -- Check the memory vault first, then do this task. "
            "Ask me before anything public, outward-facing or irreversible, and update the board when it is done.")


# ---------------- Claude sessions and usage ----------------
def find_session(prefix):
    """(s:1a2b3c4d) -> {id, cwd, title, mtime, size, active, too_big, wmux} from ~/.claude/projects, or None."""
    if not re.fullmatch(r"[0-9a-f]{8}", prefix or ""):
        return None
    hits = glob.glob(os.path.join(PROJECTS, "*", prefix + "*.jsonl"))
    if not hits:
        return None
    f = max(hits, key=os.path.getmtime)
    info = {"id": os.path.basename(f)[:-6], "cwd": "", "title": "", "mtime": os.path.getmtime(f), "size": os.path.getsize(f)}
    # a huge transcript can't be reloaded well: start fresh with a pointer instead
    info["too_big"] = info["size"] > MAX_RESUME
    info["wmux"] = bool(wmux_tab_of(info["id"]))  # seen in a wmux tab; checked for real at send time
    with open(f, encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(400), fh):  # cwd and a title sit near the top; never read a whole transcript
            try:
                d = json.loads(line)
            except ValueError:
                continue
            info["cwd"] = info["cwd"] or d.get("cwd", "")
            if d.get("type") == "summary" and not info["title"]:
                info["title"] = d.get("summary", "")
            if d.get("type") == "user" and not info["title"]:
                c = (d.get("message") or {}).get("content")
                t = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                if t and not t.lstrip().startswith("<"):
                    info["title"] = " ".join(t.split())[:70]
            if info["cwd"] and info["title"]:
                break
    info["active"] = time.time() - info["mtime"] < 600  # written in the last 10 min: probably open right now
    return info


USAGE = {"files": {}, "seen": set(), "days": {}, "cache": {}, "scanned": 0, "plan": {}}
USAGE_EVERY = 300  # seconds


def scan_usage():
    """Add new assistant-message token usage from local Claude Code transcripts to per-day totals.
    Reads only bytes appended since the last scan; each message id counts once."""
    cutoff = time.time() - 8 * 86400
    for f in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):  # includes subagents/
        try:
            size = os.path.getsize(f)
            if os.path.getmtime(f) < cutoff and f not in USAGE["files"]:
                continue
            off = USAGE["files"].get(f, 0)
            if size <= off:
                continue
            with open(f, "rb") as fh:
                fh.seek(off)
                for raw in fh:
                    if b'"usage"' not in raw or not raw.endswith(b"\n"):
                        continue
                    try:
                        d = json.loads(raw)
                        m, ts = d.get("message") or {}, d.get("timestamp", "")
                        u = m.get("usage") or {}
                        key = (m.get("id"), d.get("requestId"))
                        if not u or key in USAGE["seen"] or not ts:
                            continue
                        USAGE["seen"].add(key)
                        day = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
                        USAGE["days"][day] = USAGE["days"].get(day, 0) + u.get("input_tokens", 0) + \
                            u.get("output_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                        USAGE["cache"][day] = USAGE["cache"].get(day, 0) + u.get("cache_read_input_tokens", 0)
                    except (ValueError, AttributeError):
                        continue
                USAGE["files"][f] = fh.tell()
        except OSError:
            continue
    USAGE["scanned"] = time.time()


def plan_usage():
    """OPT-IN ("plan_usage": true). Plan limits as Claude Code's /usage shows them: 5-hour and weekly %.
    Uses an undocumented endpoint with the local Claude Code sign-in. The token is read per call from
    ~/.claude/.credentials.json, sent only to api.anthropic.com, and never stored or logged.
    If the endpoint changes, the top bar falls back to local token counts."""
    try:
        with open(os.path.join(CLAUDE_DIR, ".credentials.json"), encoding="utf-8") as fh:
            tok = json.load(fh)["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20", "User-Agent": "vault-graph"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        return {k: {"pct": round(d[k]["utilization"]), "resets": d[k]["resets_at"]}
                for k in ("five_hour", "seven_day") if d.get(k) and d[k].get("utilization") is not None}
    except urllib.error.HTTPError as e:
        return {"error": "sign-in expired, open Claude Code once" if e.code == 401 else f"HTTP {e.code}"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"error": "plan usage unavailable"}


def usage_summary():
    today = dt.date.today()
    week = [(today - dt.timedelta(days=i)).isoformat() for i in range(7)]
    return {"day": USAGE["days"].get(week[0], 0), "week": sum(USAGE["days"].get(d, 0) for d in week),
            "day_cache": USAGE["cache"].get(week[0], 0), "week_cache": sum(USAGE["cache"].get(d, 0) for d in week),
            "scanned": USAGE["scanned"], "plan": USAGE.get("plan") or {}}


_SESS = {}


def board_sessions():
    """Session info for every (s:xxxxxxxx) on the board, for the page labels. 30 s cache; the page polls every 2 s."""
    ids = set(re.findall(r"\(s:([0-9a-f]{8})\)", json.dumps(read_board())))
    for i in ids:
        if i not in _SESS or time.time() - _SESS[i][0] > 30:
            _SESS[i] = (time.time(), find_session(i))
    return {i: _SESS[i][1] for i in ids if _SESS[i][1]}


# ---------------- "Do it in Claude" launcher ----------------
def wmux_exe():
    p = CFG["launcher"].get("wmux") or ""
    if p:
        return p if os.path.exists(p) else None
    local = os.environ.get("LOCALAPPDATA")
    if local and os.path.exists(os.path.join(local, "wmux", "bin", "wmux.cmd")):
        return os.path.join(local, "wmux", "bin", "wmux.cmd")
    return shutil.which("wmux")


def wmux(*args):
    """Run a wmux CLI command; output decoded as UTF-8 whatever the console code page is."""
    return subprocess.run([wmux_exe(), *args], capture_output=True, encoding="utf-8", errors="replace",
                          timeout=20, creationflags=NOWIN)


def claude_live_in(session_id):
    """The wmux tab id if that session's Claude is still running there, else None.
    Never types into a bare shell: a screen ending in a shell prompt means Claude has exited."""
    pty = session_id and wmux_tab_of(session_id)
    if not pty or not wmux_exe():
        return None
    try:
        r = wmux("read-screen", "--tail", "6", "--to", pty)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if r.returncode or not lines or re.match(r"^(PS [^>]*>|[^\s]*[$#%>])\s*$", lines[-1].strip()):
        return None
    return pty


def wmux_new_tab():
    """Open a wmux terminal tab; return its ptyId, or None when wmux is not running."""
    if not wmux_exe():
        return None
    try:
        m = re.search(r"surface-[0-9a-f-]+", wmux("new-surface").stdout)
        if not m:
            return None
        rows = json.loads(wmux("list-surfaces", "--json").stdout)
        return next((r["ptyId"] for r in rows if r["id"] == m[0] and r.get("ptyId")), None)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def permission_mode():
    m = CFG["launcher"].get("permission_mode") or "default"
    return m if m in PERMISSION_MODES else "default"


def launch_claude(prompt, session=None):
    """Interactive Claude on the task: resume its session when known (fork it if that session is open right now),
    in a wmux tab when wmux runs, else a new PowerShell window (Windows) or terminal (macOS/Linux).
    Returns a short note for the page."""
    pty = session and claude_live_in(session.get("id"))
    if pty:  # the session is open in a wmux tab: type the task in as its next message, no reload at all
        r = wmux("send", " ".join(prompt.split()), "--submit", "--to", pty)
        if r.returncode == 0 and "Error" not in r.stdout + r.stderr:
            return f"sent into live session {session['id'][:8]} (its wmux tab)"
        # wmux refuses to type while that tab shows a menu or approval prompt: say so, don't open a duplicate
        return f"NOT sent: session {session['id'][:8]}'s wmux tab is showing a prompt or menu; answer it there, then click again"
    flags, note = [], "new Claude session"
    if session and session.get("too_big"):
        prompt += (f" -- Background: this task came from Claude session {session['id']}, which is too long to reload;"
                   " the vault has its context.")
        note = f"new session (session {session['id'][:8]} is too long to reload)"
        session = {"cwd": session["cwd"]}  # keep its folder, drop --resume
    elif session:
        flags = ["--resume", session["id"]] + (["--fork-session"] if session["active"] else [])
        note = ("copy of session %s (it is open right now, so it was forked)" if session["active"]
                else "session %s, resumed") % session["id"][:8]
    flags = ["--permission-mode", permission_mode()] + flags
    cwd = session["cwd"] if session and os.path.isdir(session.get("cwd") or "") else (CFG["launcher"].get("cwd") or ROOT)
    # the prompt goes through a file so no shell ever parses task text
    os.makedirs(GRAPH, exist_ok=True)
    pfile = os.path.join(GRAPH, "last-task-prompt.txt")
    with open(pfile, "w", encoding="utf-8") as fh:
        fh.write(prompt.replace('"', "'") if os.name == "nt" else prompt)  # PowerShell 5.1 mangles embedded " in native args
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE"))}  # not a nested session
    if os.name == "nt":
        q = lambda s: s.replace("'", "''")  # noqa: E731 - PowerShell single-quote escape
        cmd = f"Set-Location -LiteralPath '{q(cwd)}'; claude {' '.join(flags)} (Get-Content -Raw -LiteralPath '{q(pfile)}')"
        pty = wmux_new_tab()
        if pty:
            time.sleep(1.5)  # let the shell print its prompt
            if "PS " in wmux("read-screen", "--tail", "3", "--to", pty).stdout:  # only type into a PowerShell prompt
                wmux("send", cmd, "--submit", "--to", pty)
                return note + ", in a new wmux tab"
        subprocess.Popen(["powershell", "-NoExit", "-NoProfile", "-Command", cmd], env=env,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return note + ", in a new PowerShell window"
    # macOS / Linux: best effort, open a terminal; otherwise hand back the command to paste
    cmd = f"cd {shlex.quote(cwd)} && claude {' '.join(flags)} \"$(cat {shlex.quote(pfile)})\""
    try:
        if sys.platform == "darwin":
            script = 'tell application "Terminal" to do script "%s"' % cmd.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'], env=env)
            return note + ", in a new Terminal window"
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            if shutil.which(term):
                sep = ["--"] if term == "gnome-terminal" else ["-e"]
                subprocess.Popen([term, *sep, "sh", "-c", cmd + "; exec ${SHELL:-sh}"], env=env, start_new_session=True)
                return note + f", in a new {term} window"
    except OSError:
        pass
    return f"{note}: no terminal found, run this yourself: {cmd}"


# ---------------- live graph UI ----------------
def serve(root, idle=None):
    """Tiny local server for tools/graph.html + graph/graph.json. Stops after `idle` seconds unused."""
    import http.server
    import secrets
    import threading
    idle = idle or int(CFG["idle_shutdown"])
    state = {"seen": 0.0, "used": time.time()}
    page = os.path.join(HERE, "graph.html")
    files = {"/": (page, "text/html; charset=utf-8"), "/graph.json": (os.path.join(GRAPH, "graph.json"), "application/json")}
    token = secrets.token_hex(16)  # baked into the page; other sites can't read it, so they can't launch Claude

    def error_log():
        import traceback
        os.makedirs(GRAPH, exist_ok=True)
        with open(os.path.join(GRAPH, "server-error.log"), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                self._post()
            except Exception:  # noqa: BLE001 - pythonw has no stderr; keep the traceback somewhere readable
                error_log()
                self.send_error(500)

        def _post(self):
            """POST /do {sec, text, note, action}: open Claude on that board task, or mark it done.
            Only exact tasks on the board are accepted."""
            ok_host = self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
            if self.path != "/do" or not ok_host or self.headers.get("X-Vault-Token") != token:
                return self.send_error(403)
            try:
                req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 20000)))
                sec, text = req["sec"], req["text"]
            except (ValueError, KeyError, TypeError):
                return self.send_error(400)
            if text not in read_board().get(sec, []):
                return self.send_error(404, "not a task on the board")
            user_note = " ".join(str(req.get("note") or "").split())[:2000]  # one line: it may land in the board
            if req.get("action") == "done":
                mark_done(sec, text, user_note)
                log_event("main", "Done", text[:80])
                return self._json({"note": "moved to Done"})
            m = re.search(r"\(s:([0-9a-f]{8})\)", text)
            prompt = task_prompt(sec, text) + (f" -- My note: {user_note}" if user_note else "")
            note = launch_claude(prompt, find_session(m[1]) if m else None)
            log_event("main", "Launch", note + ": " + text[:60])
            self._json({"note": note})

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            state["used"] = time.time()
            try:
                if p == "/status":
                    body, ctype = json.dumps({**state, "html": os.path.getmtime(page), "activity": read_activity(),
                                              "log": read_log(), "now": time.time(), "board": read_board(),
                                              "board_file": BOARD_REL, "sessions": board_sessions(),
                                              "usage": usage_summary()}).encode(), "application/json"
                elif p in files:
                    if p == "/graph.json":
                        state["seen"] = time.time()  # the page polls this, so it proves a tab is open
                    with open(files[p][0], "rb") as fh:
                        body, ctype = fh.read(), files[p][1]
                    if p == "/":
                        body = body.replace(b"__VAULT_TOKEN__", token.encode())
                else:
                    return self.send_error(404)
            except OSError:
                return self.send_error(404)
            except Exception:  # noqa: BLE001
                error_log()
                return self.send_error(500)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # localhost only
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def usage_loop():  # first pass reads a week of transcripts once; later passes only new bytes
        while True:
            if CFG.get("local_usage", True):
                scan_usage()
            if CFG.get("plan_usage") is True:
                USAGE["plan"] = plan_usage()
            time.sleep(USAGE_EVERY)
    threading.Thread(target=usage_loop, daemon=True).start()
    if sys.stdout:
        print(f"serving {root} at http://127.0.0.1:{PORT}/ (stops after {idle}s unused)")
    while time.time() - state["used"] < idle:
        time.sleep(15)
    srv.shutdown()


def ui_status():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=0.5) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def show_ui(root):
    """Not running -> start the server and open it. Open but stale -> the page refreshes itself within 2 s."""
    if os.environ.get("VAULT_NO_UI"):
        return
    url = f"http://127.0.0.1:{PORT}/"
    st = ui_status()
    if st is None:
        kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        env = dict(os.environ, VAULT_GRAPH_PORT=str(PORT))
        subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--root", root, "serve"], cwd=root, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        for _ in range(25):
            time.sleep(0.2)
            st = ui_status()
            if st:
                break
    if st is None:
        print(f"graph UI: server did not start (see graph/server-error.log, or is port {PORT} taken?)")
    elif time.time() - st["seen"] > 8:
        webbrowser.open(url)
        print("graph UI: opened", url)
    else:
        print("graph UI: the open tab refreshes within 2 s", url)


# ---------------- asks ----------------
def ask(root, key, what):
    """Log a request made to the user. The second time the same key appears, suggest a skill."""
    key = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    p = os.path.join(root, CFG["global_dir"], "asks-log.md")
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# Asks log\n\nEvery time Claude has to ask the user to do something by hand, it runs "
                     "`python tools/vault.py ask <key> \"<what>\"`. The second time a key shows up, Claude turns it "
                     "into a skill in `.claude/skills/<key>/SKILL.md`.\n\n"
                     "| Date | Key | What was asked |\n|---|---|---|\n")
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"| {dt.date.today().isoformat()} | {key} | {what.replace('|', '/')} |\n")
    n = sum(1 for l in heal.read(p).splitlines() if l.split("|")[2:3] == [f" {key} "])
    print(f"'{key}' asked {n} time(s)")
    if n >= 2 and not os.path.exists(os.path.join(root, ".claude", "skills", key, "SKILL.md")):
        print(f"REPEATED: write .claude/skills/{key}/SKILL.md so Claude does this itself, then run: python tools/vault.py setup")


# ---------------- setup ----------------
HOOKS = [("UserPromptSubmit", "", "hook"),              # top vault matches for every prompt
         ("PreToolUse", "", "activity start"),          # live graph: every tool call is logged
         ("PostToolUse", "Agent|Task", "activity stop"),  # a subagent finished
         ("SubagentStop", "", "activity stop"),
         ("Stop", "", "activity idle")]                 # the turn ended


def hook_command(root, arg):
    base = f'"{sys.executable}" -I "{os.path.abspath(__file__)}"'
    if os.path.abspath(root) != os.path.abspath(DEFAULT_ROOT):
        base += f' --root "{os.path.abspath(root)}"'
    return f"{base} {arg}".replace("\\", "/")


def install_hooks(home, root, dry=False):
    """Add the vault hooks to <home>/.claude/settings.json once each. Keeps every other setting.
    Backs the file up first and prints each entry it adds or changes. Returns True when it changed."""
    sp = os.path.join(home, ".claude", "settings.json")
    with open(sp, encoding="utf-8") if os.path.exists(sp) else open(os.devnull) as fh:
        raw = fh.read()
    cfg = json.loads(raw) if raw.strip() else {}
    changes = []
    for event, matcher, arg in HOOKS:
        lst = cfg.setdefault("hooks", {}).setdefault(event, [])
        want = {"matcher": matcher, "hooks": [{"type": "command", "command": hook_command(root, arg), "timeout": 5}]}
        mine = [e for e in lst if any("vault.py" in h.get("command", "") and h.get("command", "").endswith(" " + arg)
                                      for h in e.get("hooks", []))]
        if mine and mine[0] == want:
            continue
        if mine:
            lst[lst.index(mine[0])] = want
            changes.append(("update", event, want))
        else:
            lst.append(want)
            changes.append(("add", event, want))
    if not changes:
        print(f"Claude Code hooks: already installed in {sp}")
        return False
    for how, event, entry in changes:
        print(f"  {how} hooks.{event}: {json.dumps(entry)}")
    if dry:
        print(f"(dry run: {sp} not changed)")
        return False
    os.makedirs(os.path.dirname(sp), exist_ok=True)
    if os.path.exists(sp):
        bak = sp + time.strftime(".vault-graph-%Y%m%d-%H%M%S.bak")
        shutil.copyfile(sp, bak)
        print("backup:", bak)
    with open(sp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("saved", sp)
    return True


def setup(root, dry=False, force=False):
    home = os.path.expanduser("~")
    print(f"vault: {root}\nClaude Code config: {os.path.join(home, '.claude')}\n")
    print("1. Claude Code hooks")
    install_hooks(home, root, dry)
    print("\n2. Agents and skills from the vault (.claude/agents, .claude/skills)")
    found = False
    for src in sorted(glob.glob(os.path.join(root, ".claude", "agents", "*.md"))):
        found = True
        dst = os.path.join(home, ".claude", "agents", os.path.basename(src))
        _copy(src, dst, dry, force, "agent " + stem(src), shutil.copyfile)
    for sk in sorted(glob.glob(os.path.join(root, ".claude", "skills", "*", "SKILL.md"))):
        found = True
        src, name = os.path.dirname(sk), os.path.basename(os.path.dirname(sk))
        dst = os.path.join(home, ".claude", "skills", name)
        _copy(src, dst, dry, force, "skill " + name, lambda a, b: shutil.copytree(a, b, dirs_exist_ok=True))
    if not found:
        print("  none in this vault, nothing copied")
    print("\n3. Pointer in ~/.claude/CLAUDE.md")
    cm = os.path.join(home, ".claude", "CLAUDE.md")
    current = heal.read(cm) if os.path.exists(cm) else ""
    if root.lower() in current.lower():
        print("  already there")
    else:
        text = (f"\n\n## Memory vault\n\nMy long-term notes live in the vault at `{root}`. Before a task, check it: "
                f"the prompt hook adds the top matches; for more, run `python tools/vault.py search \"<words>\"` "
                f"there and read `{BOARD_REL}` for open tasks.\n")
        print("  append:" + text.replace("\n", "\n  | "))
        if not dry:
            os.makedirs(os.path.dirname(cm), exist_ok=True)
            with open(cm, "a", encoding="utf-8") as fh:
                fh.write(text)
    print("\n4. Git pre-commit secret check")
    if not dry:
        heal.install_hook(root)
    print("\nDone." + (" (dry run, nothing written)" if dry else " Next: python tools/vault.py sync"))


def _copy(src, dst, dry, force, label, fn):
    same = os.path.isfile(src) and os.path.isfile(dst) and heal.read(src) == heal.read(dst)
    if same:
        print(f"  {label}: up to date")
    elif os.path.exists(dst) and not force:
        print(f"  {label}: skipped, a different one is already installed (use --force to overwrite)")
    else:
        print(f"  {label}: {'would install' if dry else 'installed'} -> {dst}")
        if not dry:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            fn(src, dst)


# ---------------- daily checks ----------------
def read_daily():
    try:
        with open(DAILY, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_daily(d):
    os.makedirs(GRAPH, exist_ok=True)
    with open(DAILY, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)


def daily():
    """Run each `daily` command from vault-graph.json once; keep the last lines of its output.
    The prompt hook shows the result once a day."""
    out = {"date": time.strftime("%Y-%m-%d"), "ts": time.time(), "jobs": []}
    write_daily(dict(out, running=True))
    for job in CFG["daily"]:
        name = job.get("name") or str(job.get("cmd"))[:40]
        try:
            cmd = job["cmd"]
            cwd = os.path.join(ROOT, os.path.expanduser(job.get("cwd") or "."))
            r = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, capture_output=True,
                               timeout=int(job.get("timeout", 300)), creationflags=NOWIN,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            text = r.stdout.decode("utf-8", "replace") + (r.stderr.decode("utf-8", "replace") if r.returncode else "")
            lines = [ln.rstrip() for ln in re.sub(r"\x1b\[[0-9;]*m", "", text).splitlines() if ln.strip()]
            out["jobs"].append({"name": name, "ok": r.returncode == 0, "lines": [heal.SECRET.sub("[redacted]", ln)[:200] for ln in lines[-10:]]})
        except Exception as e:  # noqa: BLE001 - report it, never crash the scheduled run
            out["jobs"].append({"name": name, "ok": False, "lines": [str(e)[:200]]})
    write_daily(out)
    print(daily_text(out))


def daily_text(d):
    parts = [f"{j['name']} [{'ok' if j['ok'] else 'FAILED'}]: " + " | ".join(j["lines"]) for j in d.get("jobs", [])]
    return (f"Daily checks ({d.get('date')}): " + ("; ".join(parts) or "nothing configured"))[:1500]


# ---------------- selftest ----------------
def selftest():
    import contextlib
    import io
    with tempfile.TemporaryDirectory() as d:
        configure(d)

        def w(r, t):
            os.makedirs(os.path.dirname(os.path.join(d, r)), exist_ok=True)
            with open(os.path.join(d, r), "w", encoding="utf-8") as fh:
                fh.write(t)
        w("repos/acme-api/memory/token-rotation.md", "---\nname: token-rotation\ndescription: \"Rotate API tokens monthly\"\n"
          "metadata:\n  type: project\n---\nSee [[release-checklist]]. Ticket ABC-123, repo https://github.com/acme/api. UTF-8 only.\n")
        w("repos/acme-web/memory/release-checklist.md", "---\nname: release-checklist\ndescription: \"How releases ship\"\n---\n"
          "Tag then publish. ABC-123 too, code in `XYZ-9` is ignored.\n")
        w("_global/memory/style-guide-rules.md", "No em dashes. Mentions token-rotation in plain text.\n")
        nodes, edges, adj = build(d, quiet=True)
        assert ("repos/acme-api/memory/token-rotation.md", "repos/acme-web/memory/release-checklist.md", "links") in edges
        assert "ABC-123" in nodes and len(adj["ABC-123"]) == 2, "issue key not shared"
        assert "acme/api" in nodes, "github owner/repo not found"
        assert "UTF-8" not in nodes and "XYZ-9" not in nodes, "ignore list or code spans not respected"
        assert nodes["repos/acme-api/memory/token-rotation.md"]["repo"] == "acme-api", "area wrong"
        rep = heal.read(os.path.join(d, "graph", "GRAPH_REPORT.md"))
        assert "style-guide-rules](../_global/memory/style-guide-rules.md) mentions" in rep, "unlinked mention missed"
        assert resolve("release-checklist", nodes).startswith("repos/acme-web"), "resolve failed"
        # auto-memory import, filed by project via the config map
        with tempfile.TemporaryDirectory() as h:
            for proj, name in (("C--Users-alex-code-acme-api", "brand-new-fact"), ("-home-alex-code-misc", "loose-fact"),
                               ("C--Users-alex-code-private", "secret-fact")):
                mem = os.path.join(h, ".claude", "projects", proj, "memory")
                os.makedirs(mem)
                with open(os.path.join(mem, name + ".md"), "w", encoding="utf-8") as fh:
                    fh.write(f"---\nname: {name}\n---\nx\n")
            cfg = heal.load_config(d)
            cfg["auto_memory"].update(strip_prefixes=["code-"], map={"acme-api": "acme-api", "private": None})
            with contextlib.redirect_stdout(io.StringIO()):
                import_auto_memory(d, cfg, home=h)
            assert os.path.exists(os.path.join(d, "repos", "acme-api", "memory", "brand-new-fact.md")), "not filed by project"
            assert os.path.exists(os.path.join(d, "_inbox", "loose-fact.md")), "unmapped project not sent to _inbox"
            assert not glob.glob(os.path.join(d, "**", "secret-fact.md"), recursive=True), "skipped project imported"
            # setup's hook install: backs up, idempotent, keeps other settings
            os.makedirs(os.path.join(h, ".claude"), exist_ok=True)
            with open(os.path.join(h, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"theme": "dark", "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo hi"}]}]}}, fh)
            with contextlib.redirect_stdout(io.StringIO()):
                assert install_hooks(h, d) is True, "hooks not installed"
                assert install_hooks(h, d) is False, "second install should change nothing"
            with open(os.path.join(h, ".claude", "settings.json"), encoding="utf-8") as fh:
                s = json.load(fh)
            assert s["theme"] == "dark" and len(s["hooks"]["Stop"]) == 2, "other settings lost"
            assert len(glob.glob(os.path.join(h, ".claude", "settings.json.vault-graph-*.bak"))) == 1, "no backup"
        # task board: move takes only its own sub-items; mark done strips tags and keeps the note
        b = os.path.join(d, "board.md")
        with open(b, "w", encoding="utf-8") as fh:
            fh.write("# Now: task board (updated x)\n\n## Ongoing\n\n## Next\n\n- alpha\n\n## Open\n\n"
                     "- beta\n  - beta child\n- [80%] [done 50%] @you ship ACME-101 (s:0a1b2c3d)\n\n## Pending\n\n## Done\n\n")
        with contextlib.redirect_stdout(io.StringIO()):
            task("move", ["alpha", "done"], b)
            task("move", ["beta", "next"], b)
            task("add", ["pending", "wait for review"], b)
        bd = read_board(b)
        assert len(bd["Done"]) == 1 and "beta" not in bd["Done"][0], "move grabbed another task's sub-items"
        assert bd["Next"] == ["beta · beta child"], "move lost sub-items"
        assert bd["Pending"] == ["wait for review"], "add failed"
        mark_done("Open", "[80%] [done 50%] @you ship ACME-101 (s:0a1b2c3d)", "merged", b)
        bd = read_board(b)
        assert not bd["Open"] and bd["Done"][0].endswith(": ship ACME-101 -- Note: merged"), bd["Done"]
        assert "ship ACME-101 -- Check" in task_prompt("Open", "[80%] @you ship [[ACME-101]] (s:0a1b2c3d)"), "prompt"
        # daily: last lines of each command are kept
        CFG["daily"] = [{"name": "echo", "cmd": [sys.executable, "-c", "print('\\n'.join(map(str, range(15))))"]}]
        with contextlib.redirect_stdout(io.StringIO()):
            daily()
        dj = read_daily()["jobs"][0]
        assert dj["ok"] and dj["lines"] == [str(i) for i in range(5, 15)], dj
    configure(heal.ROOT)
    print("selftest passed")


def main(a):
    if sys.stdout:  # None under pythonw (scheduled runs)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    if "--root" in a:
        i = a.index("--root")
        configure(os.path.expanduser(a[i + 1]))
        a = a[:i] + a[i + 2:]
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = a[0], a[1:]
    flag = lambda f: f in rest  # noqa: E731
    num = int(rest[rest.index("-n") + 1]) if "-n" in rest else None
    args = [x for i, x in enumerate(rest) if not x.startswith("-") and not (i and rest[i - 1] == "-n")]
    need = {"note": 1, "related": 1, "entity": 1, "path": 2, "ask": 1}
    if len(args) < need.get(cmd, 0):
        sys.exit(f"'{cmd}' needs {need[cmd]} argument(s). Run: python tools/vault.py help")
    if cmd == "sync":
        sync(ROOT)
        show_ui(ROOT)
    elif cmd == "build":
        build(ROOT)
        show_ui(ROOT)
    elif cmd == "ui":
        if not os.path.exists(os.path.join(GRAPH, "graph.json")):
            build(ROOT)
        show_ui(ROOT)
    elif cmd == "hook":
        hook()
    elif cmd == "daily":
        daily()
    elif cmd == "task":
        task(rest[0] if rest else "list", rest[1:])
    elif cmd == "activity":
        activity(args[0] if args else "")
    elif cmd == "serve":
        serve(ROOT)
    elif cmd == "search":
        search(ROOT, " ".join(args), num or 8)
    elif cmd == "note":
        note(ROOT, args[0], flag("--full"))
    elif cmd == "related":
        related(ROOT, args[0], num or 10)
    elif cmd == "path":
        path(ROOT, args[0], args[1])
        show_ui(ROOT)
    elif cmd == "entity":
        entity(ROOT, args[0])
    elif cmd == "report":
        print(heal.read(os.path.join(GRAPH, "GRAPH_REPORT.md")))
    elif cmd == "ask":
        ask(ROOT, args[0], " ".join(args[1:]) or args[0])
    elif cmd == "setup":
        setup(ROOT, dry=flag("--dry-run"), force=flag("--force"))
    elif cmd == "selftest":
        selftest()
    else:
        sys.exit(f"unknown command '{cmd}'. Run: python tools/vault.py help")


if __name__ == "__main__":
    main(sys.argv[1:])
