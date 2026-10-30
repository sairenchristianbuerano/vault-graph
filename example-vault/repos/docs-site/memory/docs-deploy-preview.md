---
name: docs-deploy-preview
description: "Every docs PR gets a preview URL; it broke when the base path changed"
metadata:
  type: project
---

Each docs PR builds a preview at a per-branch URL. It broke when the base path changed to `/docs`; fixed by reading the base path from the environment (ACME-146). Docs follow the light path in [[pr-flow]].
