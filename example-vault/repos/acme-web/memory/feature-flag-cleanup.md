---
name: feature-flag-cleanup
description: "Flags older than 90 days get removed; the flag report lists them"
metadata:
  type: project
---

Feature flags older than 90 days are removed. The flag report lists them. Eleven are overdue, tracked in ACME-149. The new checkout ships behind `checkout_v2`, see [[checkout-redesign-plan]].
