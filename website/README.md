# BlitheBot Landing Page

Static HTML/CSS/JS landing page — no build step, no dependencies, no npm.
Open `index.html` directly in a browser, or deploy the `/website` folder to Vercel.

---

## Deploy to Vercel

### Option A — Vercel Dashboard (recommended)

1. Push this repo to GitHub if it isn't already
2. Go to [vercel.com](https://vercel.com) → **New Project** → import your repo
3. In the **Configure Project** screen:
   - **Root Directory**: set to `website`
   - **Framework Preset**: Other (no framework)
   - **Build Command**: leave blank
   - **Output Directory**: leave blank (or set to `.`)
4. Click **Deploy**

Vercel auto-detects the static HTML and serves it instantly.

### Option B — Vercel CLI

```bash
npm i -g vercel     # install once
cd website          # enter the website folder
vercel              # follow the prompts
```

When prompted:
- Framework? → **Other**
- Build command? → leave blank (press Enter)
- Output directory? → `.` (current folder)

---

## Connect a Custom Domain

1. In Vercel → Project → **Settings** → **Domains**
2. Add your domain (e.g. `blithebot.com`)
3. Update your DNS registrar:
   - Add a **CNAME** record: `www` → `cname.vercel-dns.com`
   - For the apex (`@`): add an **A** record pointing to Vercel's IP (shown in dashboard)
4. Vercel provisions HTTPS automatically via Let's Encrypt — no config needed

---

## What to Update After Launch

### Stats bar numbers (`index.html` → `.stats-bar`)
Replace the four hard-coded values once you have live paper trading data:
- `19` — concurrent async loops (accurate now, update if loops change)
- `15` — independent trade gates (accurate now)
- `11` — signal sources active (update as sources go live/offline)
- `1,215` — parameter combos tested (update after each discovery engine run)

### Adding a performance section
Insert a new `<section>` between the stats bar and `#research` section.
Suggested content: cumulative P&L chart (embed a Streamlit chart iframe or a static PNG),
win rate, Sharpe ratio, and drawdown — pulled from `signal_outcomes` in PostgreSQL.

### Roadmap updates
- When paper trading phase ends: change Q2 2026 `status-badge` text from `ACTIVE` to `COMPLETE`
- When live trading begins: add `ACTIVE` badge to Summer 2026 item
- Update footer note from `"Paper trading · Summer 2026 live launch"` to reflect current status

### Contact info
Replace `gamerdiamondknight@gmail.com` with a dedicated business email before sharing widely.

---

## Local Development

No server required — just open the file:

```bash
# macOS / Linux
open website/index.html

# Windows
start website/index.html
```

For live-reload during editing, use VS Code's Live Server extension or:

```bash
npx serve website
```
