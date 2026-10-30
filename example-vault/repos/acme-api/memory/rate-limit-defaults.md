---
name: rate-limit-defaults
description: "Default rate limit is 600 requests per minute per key"
metadata:
  type: project
---

Default limit: 600 requests per minute per API key, burst 100. Raised from 300 in ACME-118 after the dashboard started polling.

The web app's polling is described in [[build-cache-gotcha]] (it hit the limit during local dev).
