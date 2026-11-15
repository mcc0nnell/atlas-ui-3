# Atlas UI 3 PR #1175 — final admin theme-policy review evidence

Exact application PR head: 5a711cf99dbb2f3bc18c42ea9f3017a058f96f8f

These two 1280×1000 screenshots were captured with offline, network-disabled
headless Chromium, using the genuine Atlas UI 3 ThemeProvider, ThemePicker,
RetroWindow and production-built CSS, not a hand-drawn reproduction. The
visible review-only panel calls out synthetic browser preferences and the
injected administrator policy. It is NOT a screenshot of a production Atlas
deployment.

## Scenarios

1. [Restricted gallery — 3 permitted themes](01-restricted-gallery.png)
   Server-stamped policy: dark, light, windows-xp-luna. Saved theme: light.
   Browser DOM asserted exactly 3 theme radios in that order and an active
   rendered Light theme.
2. [Previously saved theme revoked — safe fallback](02-revoked-theme-fallback.png)
   Server-stamped policy: dark, light. Saved theme: windows-xp-luna.
   Browser DOM asserted exactly 2 theme radios; the persisted choice remains
   windows-xp-luna, but the effective rendered theme is Light. Browser JS
   error sink remained empty.

Both scenarios use synthetic data and run with no network access.

## Review issues addressed by the two new Forge commits

- Compose exposes FEATURE_RETRO_THEMES_ENABLED and ATLAS_ALLOWED_THEMES,
  preserving defaults false and light,dark. A real docker compose config
  invocation with explicit overrides verified true + the requested allowlist.
- Vite now calls loadEnv(mode, frontend root) before deciding whether retro
  styles are compiled in. A real build with frontend/.env.local set to false
  emitted ZERO retro stylesheets; the normal build emitted all 53.
- An already-settled parser-blocking bootstrap CSS link that failed is now
  detected immediately; a dynamic pending link still waits on events.
- SPA response carries Cache-Control: no-store for / and the catchall route.

## Test evidence

- Full frontend Vitest on the exact same 12 changed source file bytes:
  2,127/2,127 tests passed across 156 test files.
- Backend tests: 25 passed, 0 skipped when built SPA assets were present.
- Scoped changed-file ESLint passed; generated-source check passed.
- Four separate security-sensitive negative tests added in the scoped
  implementation, including Vite flag precedence and CSS loading behavior.
- Two pinned Rizzer Forge missions generated passing source/mission-bound
  receipts, human review pending, no production admission.
- The two commits touched no original theme palette or generated chrome CSS.

The fixture used for screenshots is a review-only modification of
frontend/scripts/contrast-fixture/fixture.jsx; that separate diff is in
[policy-proof-fixture.patch.gz](policy-proof-fixture.patch.gz). It is intentionally
not included in the Atlas PR application code. The local source generator
used to build standalone proof pages is also provided for audit.

This evidence demonstrates representative UI behavior. It is not a security
certification or complete accessibility audit.
