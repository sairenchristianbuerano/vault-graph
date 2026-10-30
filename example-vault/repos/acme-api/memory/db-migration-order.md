---
name: db-migration-order
description: "Run schema migrations before deploying the API, never in the same step"
metadata:
  type: project
---

Schema migrations run first, then the API deploy, never in the same pipeline step. ACME-150 is the next big one (orders table split).

**How to apply:** check [[v2-migration-plan]] for the order, and ask before touching production.
