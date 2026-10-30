---
name: v2-migration-plan
description: "Plan for moving clients from /v1 to /v2 by Q1"
metadata:
  type: reference
---

# v2 migration plan

1. Ship /v2 behind a flag (ACME-140).
2. Rotate tokens with the new scheme, see [[auth-token-rotation]].
3. Split the orders table, see [[db-migration-order]] and ACME-150.
4. Announce it in the docs, see [[docs-ia-rework]].
5. Turn off /v1 writes after 12 months ([[api-versioning-policy]]).
