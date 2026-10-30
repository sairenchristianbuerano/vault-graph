---
name: checkout-redesign-plan
description: "Checkout redesign: new address form, same payment step, behind a flag"
metadata:
  type: reference
---

# Checkout redesign

- New address form with autocomplete (ACME-158).
- Payment step unchanged until ACME-160.
- Ship behind `checkout_v2`, see [[feature-flag-cleanup]].
- Colors from [[design-tokens]]; check against [[accessibility-audit]].
