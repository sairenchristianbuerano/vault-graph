---
name: flaky-integration-tests
description: "Three integration tests fail about 1 run in 20 because of a shared test database"
metadata:
  type: project
---

`test_refund_flow`, `test_webhook_retry` and `test_bulk_export` share one test database and fail about 1 run in 20. Quarantine them with the `flaky` marker until ACME-127 gives each test its own schema.
