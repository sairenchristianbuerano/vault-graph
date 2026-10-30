---
name: design-tokens
description: "Colors and spacing come from tokens.json; never hardcode hex values"
metadata:
  type: project
---

All colors and spacing come from `tokens.json`, generated from the design file. Never hardcode a hex value. New color tokens are pending with design (ACME-155).

The [[accessibility-audit]] found two token pairs below 4.5:1 contrast.
