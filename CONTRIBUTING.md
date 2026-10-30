# Contributing

Thanks for helping. vault-graph is small on purpose, so a few rules keep it that way.

## Ground rules

- **Standard library only.** No `pip install`, no `requirements.txt`. If you need something, write the few lines it takes.
- **Python 3.10+.** Do not use features from newer versions.
- **`tools/graph.html` stays one file** with no external scripts, fonts, stylesheets or network requests. It must work offline from the local server.
- **Never commit a real vault, real notes, logs or secrets.** Use `example-vault/` (made-up content) for anything you need to show. `graph/` output is gitignored.
- Keep the user model simple: "drop the `tools/` folder into your vault".

## Run the tests

```
python tools/heal.py --selftest
python tools/vault.py selftest
```

To run them against the demo vault, set the root:

```
# macOS / Linux
VAULT_ROOT=example-vault python tools/heal.py --selftest
VAULT_ROOT=example-vault python tools/vault.py selftest

# Windows PowerShell
$env:VAULT_ROOT = "example-vault"; python tools/heal.py --selftest; python tools/vault.py selftest
```

The self-tests build their own throwaway vaults in a temp folder, so they never change your files.

## Try the UI

```
python example-vault/demo.py
```

It opens `http://127.0.0.1:8766/` on the demo vault. Edit `tools/graph.html` and the open page reloads itself within 2 seconds. Useful hashes: `#cat=agent` (legend filter) and `#task=1` (open a task).

Do not run `setup` against your real home folder while testing. Point `HOME` (and `USERPROFILE` on Windows) at a temp folder first, or use `setup --dry-run`.

## Pull request checklist

- [ ] Both self-tests pass.
- [ ] No new dependencies, and `graph.html` makes no external requests.
- [ ] README (and `vault-graph.example.json`) updated if behavior or config changed.
- [ ] No personal data: no real names, paths, emails, tokens or notes in code, docs, screenshots or the demo vault.
- [ ] `python tools/heal.py --check` reports no secrets.

Small, focused PRs are easiest to review. For a bigger change, open an issue first so we can agree on the shape.
