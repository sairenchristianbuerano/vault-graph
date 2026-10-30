---
name: auth-token-rotation
description: "Service tokens rotate every 30 days; the old token stays valid for 24 h"
metadata:
  type: project
---

Service tokens rotate every 30 days. The old token stays valid for 24 hours so running jobs do not fail. Tracked in ACME-142, code in https://github.com/acme/api.

**Why:** a leaked token in March stayed valid for weeks.

**How to apply:** never hardcode a token; read it from the secret store. See [[secrets-never-in-notes]] and [[api-versioning-policy]].
