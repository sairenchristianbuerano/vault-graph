---
name: incident-2026-09-api-latency
description: "Incident report: p95 latency spike from a missing index"
metadata:
  type: reference
---

# Incident: API latency spike

p95 went from 180 ms to 2.4 s for 40 minutes. Cause: a missing index on `orders.customer_id` after a migration. Fix in ACME-131.

Lessons: [[db-migration-order]], and the alert threshold next to [[rate-limit-defaults]] was too loose.
