# CLAUDE.md — BlitheBot Website Rules

## Stack
- Pure HTML/CSS/JS — no framework, no build step
- Deployed to Vercel, root directory = /website
- Files: index.html, styles.css, script.js, 
  plus sub-pages: architecture.html, performance.html,
  discovery.html, dashboard.html

## Design Philosophy — Anti-AI Slop Rules
- NO gradient text (no bg-clip-text, no text-transparent)
- NO pill/badge shapes (no rounded-full on text elements)
- NO illustrations floating to the right of hero text
- NO centered hero layouts — left-align headlines always
- NO more than 2 animations per page total
- NO generic purple (#7c3aed is acceptable, 
  #8b5cf6 generic — avoid)
- USE asymmetry intentionally — not everything centered
- USE restraint — remove sections rather than adding more

## Spacing — 8pt Grid (strict)
Every padding, margin, and gap must be a multiple of 8:
8px, 16px, 24px, 32px, 40px, 48px, 64px, 80px, 96px
Never use: 12px, 20px, 28px, 52px, 74px or any rem 
value that does not resolve to a multiple of 8.
Before writing any spacing value, verify it is on-grid.

## Color System
--accent: #7c3aed (primary purple)
--accent-light: #a78bfa (soft lavender)  
--ticker: #c4b5fd bold (ticker symbols in terminal)
--bracket: #9d7dea (log bracket labels)
--log-val: #8b95a1 (log body text — WCAG AA compliant)
--buy: #ffffff (buy signals and profit)
--sell: #ec4899 (sell signals and loss)
--status: #4ade80 (active/pass status)
--bg: #0a0a0f (background)
--surface: #0f0f18 (card surface)
--body: #9d92c4 (body text — WCAG AA compliant)
--muted: #8b82b8 (muted text — WCAG AA compliant)
Timestamps in terminal: #6b7280 minimum

## WCAG AA Contrast Requirements
All text must meet 4.5:1 contrast on #0a0a0f background.
Verified passing colors: #ffffff, #c4b5fd, #9d7dea, 
#8b95a1, #9d92c4, #8b82b8, #4ade80, #ec4899
NEVER use: #4b5563, #6b5fa0, #7c6fa0 for readable text

## Typography Rules
- JetBrains Mono: all terminal/log content, stats, 
  labels, nav, buttons, monospace elements
- Inter: body paragraphs, descriptions, about text
- H1 must always be larger than H2 at every breakpoint
- No more than 4 font sizes per page section
- No arbitrary sizes like text-[9px] text-[11px] text-[13px]
  Use the defined scale: 10px, 12px, 14px, 16px, 24px, 
  32px, 48px, 64px, 96px

## Animation Rules (maximum 2 per page)
Allowed:
  1. Terminal log cycling feed (setTimeout)
  2. Scroll reveal (.reveal → .reveal.visible via 
     IntersectionObserver)
Not allowed:
  - Canvas particle engines
  - Multiple RAF loops
  - Shimmer/sweep effects
  - Bounce animations
  - Hover scale transforms
CSS transitions on buttons/links (color, opacity 0.2s) 
are micro-interactions, not animations — allowed freely.

## Terminal Log Color System (exact, no deviations)
[Startup] [SWING] [ORDER] [EXIT] [P&L] brackets: #9d7dea
COST JPM SPY BRK.B PG V ticker symbols: #c4b5fd bold 700
Log body text (values, descriptions): #8b95a1
↑ BUY and positive P&L amounts: #ffffff
↓ SELL and negative P&L amounts: #ec4899  
Timestamps: #6b7280
Blinking cursor: #a78bfa
PASS/OK status: #4ade80
BLOCKED/VETOED status: #ec4899

## Pre-Ship Checklist
Before pushing any change, verify:
1. All spacing values are multiples of 8
2. No more than 2 animations active
3. No pill badges on text elements
4. Hero headline is left-aligned
5. All body text colors pass WCAG AA (4.5:1)
6. Typography hierarchy: h1 > h2 > h3 at all breakpoints

## Section Content Rules
- Performance page: never show fake stats. 
  Use — for any metric not yet accumulated.
- Dashboard page: "DASHBOARD OFFLINE" when Railway inactive
- All dollar amounts in performance: paper trading only,
  always labeled clearly
- Never write marketing copy like "Empower your trading"
  Use direct functional descriptions of what the bot does

Push commit message format:
"fix/feat/refactor: [what changed] — [which file]"
