---
name: api-versioning-policy
description: "Breaking changes only in a new /v{n}; the old version stays for 12 months"
metadata:
  type: project
---

Breaking changes go into a new `/v{n}` prefix. The previous version stays for 12 months with a `Deprecation` header. The v2 plan is in [[v2-migration-plan]]; the reference docs come from [[api-reference-generation]].
