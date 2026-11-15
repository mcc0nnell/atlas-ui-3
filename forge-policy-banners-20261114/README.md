# PR #1175 — theme policy and classification-banner visual evidence

These screenshots were captured in headless Chromium from an **offline,
network-disabled container** using the built Atlas UI 3 CSS and the actual
`ComplianceBanner`, `Header`, and `RetroControls` React components.
Classification configuration is **synthetic CUI demonstration data**; no real
classified or protected content is present.

PR source head: `52269c908e7f89a3c2070e1bdcb6d333f28ed37c`
(local Forge review candidate; do not infer approval or merge).

| Theme | Screenshot |
|---|---|
| Dark | [dark-proof.png](dark-proof.png) |
| Windows XP Luna | [windows-xp-luna-proof.png](windows-xp-luna-proof.png) |
| DOS CGA | [dos-cga-proof.png](dos-cga-proof.png) |
| Vaporwave | [vaporwave-proof.png](vaporwave-proof.png) |
| Maximalist 90s Banner | [maximalist-banners-proof.png](maximalist-banners-proof.png) |

## Checks and scope

- Local full frontend Vitest at exact source head: **2,124/2,124 passed**
  across 156 test files.
- Pinned Rizzer Forge profile passed, backend pinned pytest: **22 passed,
  2 skipped**.
- CSS generator consistency check and scoped ESLint passed.
- Browser contrast checker: **55/55 themes**, **39 rendered UI regions**,
  **98,609 measured text pairs**, **0 measured text pairs below 4.5:1**.
  **909 gradient cases unmeasurable**, **556 disabled-control pairs exempt**.
  Unmeasurable does **not** mean accessible or passed WCAG.
- The five screenshots demonstrate visually independent banner colors, label
  and pattern under representative dark, light, saturated and decorative UI
  themes. They do not replace manual accessibility/security review.
- Exact screenshot SHA-256 hashes are in [manifest.json](manifest.json).
  Full browser measurement JSON is in
  [contrast-55-with-banner.json](contrast-55-with-banner.json).

The screenshots were generated from representative local components, **not**
from a production deployment or a live conversation. The theme catalog's
original generated CSS and vendor demo assets were not modified by the four
Forge commits.
