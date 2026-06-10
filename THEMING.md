# Theming & look guide — "Night Reader"

The interface uses a small set of **design tokens** (CSS variables) defined in
`web/src/index.css`. Dark is the default; light is a derived override. Changing the
whole look is mostly editing these variables — every screen reads from them.

> **Easiest option:** tell the assistant what you want ("warmer gold," "make light
> mode the default," "softer corners") and it'll adjust the tokens. This guide is for
> doing it yourself.

## Where it lives

| File | Controls |
|---|---|
| `web/src/index.css` | **All tokens** (colors, fonts, radii) + reusable classes (`.card`, `.btn`, `.pill`, `.reading`) |
| `web/index.html` | Pre-paint theme script + loading background |
| `web/src/components/ui.jsx` | Status-badge → pill mapping, cards, progress, modals |
| `web/src/components/ThemeToggle.jsx` | The light/dark switch (saved in the browser) |
| `web/src/components/*.jsx` | Each screen (they use the tokens, rarely raw colors) |

## The tokens (in `index.css`)

Dark values are under `:root`; light values under `[data-theme="light"]`. Edit a value
in **both** blocks to change that role in both modes.

| Token | Role | Dark | Light |
|---|---|---|---|
| `--page` | App background | `#181520` | `#FBF8F3` |
| `--surface` | Cards / panels | `#221E2A` | `#FFFFFF` |
| `--reading` | Chapter reading surface | `#1E1A26` | `#FFFDF9` |
| `--elevated` | Modals / popovers | `#2A2531` | `#FFFFFF` |
| `--border` / `--border-strong` | Hairlines | white 8% / 14% | black 8% / 14% |
| `--ink` / `--muted` / `--hint` | Text: primary / secondary / faint | `#ECE6DF` / `#B3AAB0` / `#837C88` | `#2A2622` / `#6E665E` / `#9B938A` |
| `--accent` | Candle-gold accent | `#D9A85C` | `#A86E22` |
| `--accent-hover` / `--accent-press` | Accent states | `#E6B86E` / `#C2924A` | `#B07A2E` / `#8F5C1A` |
| `--accent-ink` | Text on a gold button | `#1F1A0C` | `#FFFFFF` |
| `--b-*-bg` / `--b-*-tx` | Status pill colors | (5 semantic hues) | (light variants) |

**Accent rule:** gold is for buttons, progress fills, active states, links, and large
display type — **never small body text** (gold-on-dark fails contrast below ~18px). Use
`--ink` for reading text.

These map to Tailwind utilities via `@theme inline`: `bg-page`, `bg-surface`,
`text-ink`, `text-muted`, `text-hint`, `border-line`, `rounded-card`, `rounded-btn`,
`font-reading`, `font-ui`, `font-korean`.

## Fonts

Loaded at the top of `index.css`: **Spectral** (reading + titles), **Inter** (UI),
**Pretendard** (Korean source). Swap a family there and it changes everywhere that
uses `font-reading` / `font-ui` / `font-korean`.

## Common tweaks

- **Shift the accent** (warmer/cooler gold, or a different color entirely): change
  `--accent`, `--accent-hover`, `--accent-press`, `--accent-ink` in both theme blocks.
- **Make light the default:** in `index.html`, default to light unless saved theme is
  dark; or move the light values into `:root`.
- **Recolor a status badge:** edit the `--b-<status>-bg` / `--b-<status>-tx` pair.
- **Rounder/sharper:** change `--radius-card` (16px) and `--radius-btn` (10px).
- **Reading comfort:** the `.reading` block sets font-size (18px) and line-height
  (1.75); the column max-width is `68ch` in `ChapterReader.jsx`.

After editing, the dev server (http://localhost:5173) reloads instantly. For the
packaged app, rebuild with `python launch.py --rebuild`.
