---
name: login-redirect-bug
description: "Safari loops on /login when the session cookie is SameSite=None without Secure"
metadata:
  type: project
---

Safari loops between `/login` and `/account` when the session cookie is `SameSite=None` without `Secure`. Tracked in ACME-133. The token side is in [[auth-token-rotation]].
