---
name: build-cache-gotcha
description: "Clear .cache/build when switching branches with different lockfiles"
metadata:
  type: project
---

The build cache keys on file paths, not the lockfile. Switching branches with different dependencies gives odd runtime errors. Clear `.cache/build` first.

Also: the dev server polls the API every 2 s, which used to hit [[rate-limit-defaults]]. Code: https://github.com/acme/web.
