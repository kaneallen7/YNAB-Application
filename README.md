# Ledgerlens — YNAB sync

Pull your YNAB budget over the API and regenerate the Ledgerlens dashboard on
your **live numbers**. Run it whenever you want fresh figures — no manual CSV
exports.

It runs on *your* machine, so your YNAB token and personal data stay local.

---

## 1. Get a YNAB token

YNAB → **Account Settings → Developer Settings → New Token**. Copy it.

It's a *personal access token*: full read/write to your budget, and revocable
in one click on that same page. Keep it in an environment variable, never in a
file you might share.

## 2. Run it

Python 3.8+ is all you need — no third-party packages (uses the standard
library).

```bash
cd ledgerlens_sync
export YNAB_TOKEN=your_token_here        # Windows: set YNAB_TOKEN=your_token_here
python3 ynab_sync.py
```

Open the resulting **`ynab-ledger-dashboard.html`** in your browser. Done.

### Options

```
--budget last-used     which budget (default: your most recently used)
--months 24            months of history to analyse (default 36)
--out my-dash.html     output filename
--token xxx            token inline instead of the env var
```

## 3. Keep it fresh automatically

**macOS / Linux (cron)** — refresh every morning at 7am:

```cron
0 7 * * *  cd /path/to/ledgerlens_sync && YNAB_TOKEN=xxx /usr/bin/python3 ynab_sync.py
```

**Windows** — Task Scheduler → new task → action:
`python3 C:\path\to\ledgerlens_sync\ynab_sync.py`, with `YNAB_TOKEN` set in the
task's environment.

YNAB allows 200 requests/hour; one run is a single request, so any sane
schedule is fine.

---

## What it writes

| file | purpose |
|---|---|
| `ynab-ledger-dashboard.html` | the dashboard, ready to open |
| `data.json` | the computed dataset (debugging / feeding other tools) |
| `.ynab_raw.json` | cache of the last raw budget pulled |

## Preview without a token (demo)

```bash
python3 make_mock.py        # writes a fake YNAB payload
python3 -c "import json,ynab_sync,analytics;from datetime import date; \
 b=json.load(open('.ynab_mock.json'))['data']['budget']; \
 from analytics import _month_iter,_month_start,_add_month; \
 t=date.today(); lc=_add_month(_month_start(t),-1); h=_month_iter(_add_month(lc,-17),lc); \
 c,a,x,g=ynab_sync.normalise(b,h); analytics.set_assigned(g); \
 d=analytics.build_data(x,a,currency=c,today=t); ynab_sync.render(d,'demo.html')"
# open demo.html
```

---

## How your accounts are classified (and how to adjust)

The dashboard needs to know which accounts are **liabilities** and which are
**investments**. The script guesses:

- **Liabilities**: YNAB account types `creditCard`, `mortgage`, `lineOfCredit`,
  loans, etc.
- **Investments**: off-budget tracking accounts of type `otherAsset`, *or* any
  account whose name contains `invest`, `isa`, `pension`, `stock`, `brokerage`,
  `vanguard`, `fund`, `crypto`.

If something lands in the wrong bucket, edit the `INVEST_HINTS` /
`LIABILITY_TYPES` lists near the top of `ynab_sync.py`. (When this becomes a
real app these become a one-time settings screen.)

Other reasonable assumptions baked in, all easy to change later:

- **Net worth history** is reconstructed by walking transactions back from
  today's balances (YNAB doesn't store historical balances). Closed accounts
  are excluded.
- **Income** = inflows categorised `Inflow: …` (Starting Balance and manual
  adjustments are excluded so they don't look like salary).
- **Projections** assume your recent 3-month savings rate continues, with
  investment returns of 6% (expected) / 9% / 2%. Tune in `analytics.py`.

## First run

Because I built this against YNAB's documented API shapes but couldn't test it
against a live token, the first run is the real test. If anything looks off —
a miscategorised account, an odd number — send me the terminal output (and the
top of `data.json`) and I'll adjust the mapping.

---

## Current dashboard refresh

The active dashboard is `ynab-ledger-dashboard.html`. It is a self-contained
file with calculated display data embedded in it; it does **not** contain a
YNAB personal access token.

Set `YNAB_TOKEN` in the environment, then run:

```powershell
python ynab_sync.py
```

That pulls the selected YNAB budget, refreshes the local cache, regenerates
`data.json`, and updates `ynab-ledger-dashboard.html` in place. Open that HTML
file in a browser after the command completes.

To rebuild the dashboard from the latest local cache without contacting YNAB:

```powershell
python ynab_sync.py --from-cache
```

Keep `.ynab_raw.json` and `data.json` private: both contain personal financial
information. Do not upload them to a public repository or website.

## GitHub

The repository contains the source code and project-local design assets.
Generated dashboards and YNAB data are excluded by `.gitignore`. Keep
`YNAB_TOKEN` in your local environment and never commit it.

## Where this goes next

This is the **personal-token** path. If Ledgerlens ever becomes a hosted app
other people log into, the same `analytics.py` stays exactly as-is; only the
fetch layer changes — swap the token call for a YNAB **OAuth** flow
(“Connect YNAB” button → redirect → access/refresh tokens) and add incremental
**delta sync** via `last_knowledge_of_server` so refreshes only pull what
changed.
