#!/usr/bin/env python3
"""
ynab_sync.py — pull your YNAB budget over the API and regenerate the
Ledgerlens dashboard on your live numbers. No manual exports.

Usage
-----
    export YNAB_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    python ynab_sync.py

    # options
    python ynab_sync.py --budget last-used     # or a specific budget id
    python ynab_sync.py --months 24            # history window (default 18)
    python ynab_sync.py --out my-dashboard.html

Get a token: YNAB → Account Settings → Developer Settings → New Token.
It is full-access and revocable; keep it in the env var, not in code.

The script writes:
  ledgerlens.html   the dashboard, ready to open in a browser
  data.json         the computed dataset (handy for debugging / other tools)
  .ynab_raw.json    the last raw budget pulled (cache)
"""
import os, sys, json, argparse, urllib.request, urllib.error, re, shutil
from datetime import date
import analytics

API = "https://api.ynab.com/v1"
MILLI = 1000.0

LIABILITY_TYPES = {"creditCard", "lineOfCredit", "otherLiability", "mortgage",
                   "autoLoan", "studentLoan", "personalLoan", "medicalDebt", "otherDebt"}
INVEST_HINTS = ("invest", "isa", "pension", "stock", "brokerage", "vanguard", "fund", "crypto")
BALANCE_ADJUST_PAYEES = {"Starting Balance", "Manual Balance Adjustment",
                         "Reconciliation Balance Adjustment"}


def api_get(path, token):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}",
                                                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"YNAB API error {e.code} on {path}:\n{body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Could not reach api.ynab.com ({e.reason}). "
                         f"Check your internet connection / firewall.")


def classify_account(a):
    t = a.get("type", "")
    name = (a.get("name") or "").lower()
    is_liab = t in LIABILITY_TYPES
    is_inv = (not a.get("on_budget", True) and t == "otherAsset") or \
             any(h in name for h in INVEST_HINTS)
    if is_liab:
        is_inv = False
    return is_liab, is_inv


def normalise(budget, hist_months):
    cur = budget.get("currency_format") or {}
    currency = cur.get("currency_symbol") or "£"

    accounts = []
    keep_ids = set()
    for a in budget.get("accounts", []):
        if a.get("deleted") or a.get("closed"):
            continue
        is_liab, is_inv = classify_account(a)
        accounts.append({"name": a["name"], "balance": a["balance"] / MILLI,
                         "on_budget": a.get("on_budget", True),
                         "is_liability": is_liab, "is_investment": is_inv,
                         "type": a.get("type", ""),
                         "last_reconciled_at": a.get("last_reconciled_at"),
                         "direct_import_linked": a.get("direct_import_linked", False),
                         "direct_import_in_error": a.get("direct_import_in_error", False)})
        keep_ids.add(a["id"])
    acct_name = {a["id"]: a["name"] for a in budget.get("accounts", [])}
    # The full-budget endpoint returns transactions with category_id / payee_id
    # but NOT the resolved *_name fields, so resolve them from these lookups.
    cat_name = {c["id"]: c["name"] for c in budget.get("categories", [])}
    pay_name = {p["id"]: p["name"] for p in budget.get("payees", [])}
    def rcat(o):        return o.get("category_name") or cat_name.get(o.get("category_id")) or ""
    def rpay(o, fb=""): return o.get("payee_name") or pay_name.get(o.get("payee_id")) or fb

    # index subtransactions by parent
    subs_by_parent = {}
    for s in budget.get("subtransactions", []):
        if s.get("deleted"):
            continue
        subs_by_parent.setdefault(s["transaction_id"], []).append(s)

    def is_income(cat_name, payee_name, amount, transfer_id):
        if transfer_id:
            return False
        if payee_name in BALANCE_ADJUST_PAYEES:
            return False
        if amount <= 0:
            return False
        return (cat_name or "").startswith("Inflow")

    txns = []
    for t in budget.get("transactions", []):
        if t.get("deleted"):
            continue
        if t["account_id"] not in keep_ids:
            continue
        parent_payee = rpay(t)
        parent_subs = subs_by_parent.get(t["id"])
        if parent_subs:                      # split: emit legs per subtransaction
            for s in parent_subs:
                amt = s["amount"] / MILLI
                scat = rcat(s); spayee = rpay(s, parent_payee)
                txns.append({
                    "date": t["date"], "account": acct_name.get(t["account_id"], "?"),
                    "payee": spayee, "category": scat, "amount": amt,
                    "is_income": is_income(scat, spayee, amt, s.get("transfer_account_id")),
                    "is_transfer": bool(s.get("transfer_account_id")),
                    "is_adjustment": spayee in BALANCE_ADJUST_PAYEES or scat == "Pot correction" or "pot transfer" in spayee.lower(),
                    "memo": s.get("memo") or t.get("memo") or "",
                    "cleared": t.get("cleared") or "uncleared",
                    "approved": bool(t.get("approved", False)),
                    "flag_color": t.get("flag_color"),
                })
        else:
            amt = t["amount"] / MILLI
            tcat = rcat(t)
            txns.append({
                "date": t["date"], "account": acct_name.get(t["account_id"], "?"),
                "payee": parent_payee, "category": tcat, "amount": amt,
                "is_income": is_income(tcat, parent_payee, amt, t.get("transfer_account_id")),
                "is_transfer": bool(t.get("transfer_account_id")),
                "is_adjustment": parent_payee in BALANCE_ADJUST_PAYEES or tcat == "Pot correction" or "pot transfer" in parent_payee.lower(),
                "memo": t.get("memo") or "",
                "cleared": t.get("cleared") or "uncleared",
                "approved": bool(t.get("approved", False)),
                "flag_color": t.get("flag_color"),
            })

    months = sorted((m for m in budget.get("months", []) if not m.get("deleted")),
                    key=lambda m: m.get("month", ""))
    current_key = date.today().strftime("%Y-%m")
    eligible = [m for m in months if (m.get("month") or "")[:7] <= current_key]
    current_month = eligible[-1] if eligible else (months[-1] if months else {})
    assigned = {}; category_balances = {}; moved_out = {}
    for c in current_month.get("categories", []):
        if c.get("deleted") or c.get("hidden"):
            continue
        raw_budgeted = c.get("budgeted", 0) / MILLI
        assigned[c["name"]] = max(0.0, raw_budgeted)
        category_balances[c["name"]] = c.get("balance", 0) / MILLI
        moved_out[c["name"]] = max(0.0, -raw_budgeted)
    latest = current_month
    dashboard_meta = {
        "plan": budget.get("name", "YNAB plan").upper(),
        "ready": latest.get("to_be_budgeted", 0) / MILLI,
        "age": latest.get("age_of_money"),
    }
    return currency, accounts, txns, assigned, moved_out, category_balances, dashboard_meta


# Keep the design assets beside the sync script so a fresh GitHub clone is
# self-contained. Older installations may still have the template in
# Downloads, so retain that location as a backwards-compatible fallback.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DESIGN_DIR = r"C:\Users\Kane\Downloads\YNAB Data Visualization Mockups"
PROJECT_TEMPLATE = os.path.join(PROJECT_DIR, "YNAB Visualiser.dc.html")
PROJECT_SUPPORT = os.path.join(PROJECT_DIR, "support.js")
DESIGN_TEMPLATE = PROJECT_TEMPLATE if os.path.exists(PROJECT_TEMPLATE) else os.path.join(DESIGN_DIR, "YNAB Visualiser.dc.html")
DESIGN_SUPPORT = PROJECT_SUPPORT if os.path.exists(PROJECT_SUPPORT) else os.path.join(DESIGN_DIR, "support.js")


def _pad_left(values, length, default=0):
    """Keep real observations intact, extending only the left edge for the template's fixed grids."""
    values = list(values)
    if not values:
        values = [default]
    return [values[0]] * max(0, length - len(values)) + values[-length:]


def _template_model(data):
    """Shape the source template's original mock model from the live analytics dataset."""
    meta = data.get("meta", {})
    rows = data.get("networth", [])
    current_nw = float(data.get("kpi", {}).get("net_worth", 0))
    nw_actual = [float(row.get("net", current_nw)) for row in rows] or [current_nw]
    # Do not fabricate a flat pre-history when the YNAB plan has only recently
    # started. The chart will show the genuine points and the explicit forecast.
    nw = nw_actual

    if rows:
        first_month = date.fromisoformat(rows[0]["m"] + "-01")
    else:
        first_month = date.today()
    months = []
    first_template_month = analytics._add_month(first_month, -(36 - min(36, len(rows))))
    for index in range(36):
        month = analytics._add_month(first_template_month, index)
        months.append({"m": month.strftime("%b"), "y": month.year,
                       "short": month.strftime("%b %y").upper()})

    accounts = data.get("accounts_snapshot", [])
    liabilities_now = sum(float(a.get("balance", 0)) for a in accounts
                          if a.get("is_liability"))
    liab = [liabilities_now] * len(nw)
    assets = [value - liabilities_now for value in nw]
    expected = data.get("projection", {}).get("expected", {}).get("series", [])
    proj = [nw[-1]] + [float(value) for value in expected[1:7]]
    proj = _pad_left(proj, 7, nw[-1])[-7:]

    bva = {row["cat"]: row for row in data.get("bva", [])}
    totals = {row["cat"]: row for row in data.get("cat_totals", [])}
    cats = []
    currency = meta.get("currency", "£")
    for name in data.get("cat_names", []):
        history = [max(0, float(row.get(name, 0))) for row in data.get("monthly_cat", [])]
        history = _pad_left(history, 18, 0)[-18:]
        budgeted = max(0.0, float(bva.get(name, {}).get("assigned", totals.get(name, {}).get("avg", 0))))
        moved_out = max(0.0, float(bva.get(name, {}).get("moved_out", 0)))
        balance = float(bva.get(name, {}).get("balance", budgeted - history[-1]))
        cats.append({"name": name, "budgeted": budgeted, "spent": history[-1], "balance": balance, "hist": history,
                     "moved_out": moved_out})

    flow_rows = data.get("monthly_flow", [])
    flow = [{"i": float(row.get("income", 0)), "o": float(row.get("outflow", 0)),
             "n": float(row.get("net", 0)), "label": row.get("label", "")}
            for row in flow_rows]
    if not flow:
        flow = [{"i": 0, "o": 0, "n": 0, "label": "CURRENT"}]

    def account_row(account):
        name = account["name"]
        history = [float(row.get(name, account.get("balance", 0))) for row in rows]
        return {"name": name, "type": (account.get("type") or "ACCOUNT").upper(),
                "bal": float(account.get("balance", 0)), "s": _pad_left(history, 12, account.get("balance", 0))[-12:],
                "recon": account.get("last_reconciled_at")}

    budget_accounts = [account_row(a) for a in accounts if a.get("on_budget", True)]
    tracking_accounts = [account_row(a) for a in accounts if not a.get("on_budget", True)]
    comp = [{"name": a["name"], "v": float(a.get("balance", 0)), "c": color}
            for a, color in zip(accounts, ["#8A8B88", "#3FA37A", "#5C5E5C", "#3E4140", "#454846", "#2C2F2D", "#262928"])]

    avg_spend = float(data.get("kpi", {}).get("avg_spend", 0))
    liquid = sum(float(a.get("balance", 0)) for a in accounts
                 if a.get("on_budget", True) and not a.get("is_liability"))
    savings_rate = float(data.get("kpi", {}).get("savings_rate", 0))
    last12 = nw[-12:]
    reconstructed_points = sum(1 for row in rows if "(MTD)" not in str(row.get("label", "")))
    complete_flow = [f for f in flow if "(MTD)" not in str(f.get("label", ""))]
    sr_series = [f["n"] / f["i"] * 100 if f["i"] else 0 for f in complete_flow[-12:]]
    runway_series = [liquid / f["o"] if f["o"] else 0 for f in complete_flow[-12:]]
    kpi_defs = [
        ["NET WORTH", current_nw, f"{len(rows)} MONTHS OF HISTORY", last12, "#3FA37A", "#E8E7E3"],
        ["READY TO ASSIGN", float(meta.get("ready", 0)), "CURRENT PLAN MONTH", last12, "#2C2F2D", "#656866"],
        ["AGE OF MONEY", str(meta.get("age") or "—"), "DAYS", sr_series, "#656866", "#E8E7E3"],
        ["SAVINGS RATE", f"{savings_rate:.1f}%", "TRAILING COMPLETE MONTHS", sr_series, "#3FA37A", "#E8E7E3"],
        ["RUNWAY", f"{(liquid / avg_spend if avg_spend else 0):.1f}", "MONTHS OF BURN", runway_series, "#656866", "#E8E7E3"],
    ]
    last_flow = flow[-1]
    previous_flow = flow[-2] if len(flow) > 1 else last_flow
    transaction_groups = {}
    for txn in data.get("transaction_details", []):
        key = str(txn.get("date", ""))[:7]
        if not key:
            continue
        transaction_groups.setdefault(key, []).append(txn)
    transaction_months = []
    for key in sorted(transaction_groups, reverse=True):
        month_rows = transaction_groups[key]
        try:
            month_label = date.fromisoformat(key + "-01").strftime("%b %y").upper()
        except ValueError:
            month_label = key.upper()
        transaction_months.append({
            "key": key, "label": month_label, "count": len(month_rows),
            "total": round(sum(float(row.get("amount", 0)) for row in month_rows
                               if row.get("type") not in {"TRANSFER", "ADJUSTMENT"}), 2),
            "rows": month_rows,
        })
    return {
        "months": months, "nw": nw, "liab": liab, "assets": assets, "proj": proj,
        "nwSubtitle": f"RECONSTRUCTED MONTH-END · {reconstructed_points} OBSERVATIONS · CURRENT BALANCES ANCHOR",
        "cats": cats, "flow": flow, "accounts": {"budget": budget_accounts, "tracking": tracking_accounts},
        "comp": comp, "liquid": liquid, "kpiDefs": kpi_defs,
        "runwayRows": [["Liquid buffer", liquid, "#D2D1CD"], ["Average monthly burn", avg_spend, "#D2D1CD"],
                       ["Runway", f"{(liquid / avg_spend if avg_spend else 0):.1f} mo", "#3FA37A"], ["Target (6 mo)", "Met" if liquid >= avg_spend * 6 else "Building", "#656866"]],
        "spendPrev": previous_flow["o"], "savingsRate": savings_rate, "age": meta.get("age") or "—",
        "mobilePeriod": date.today().strftime("%b %Y").upper(),
        "ready": float(meta.get("ready", 0)), "netWorthDelta": current_nw - (nw[-2] if len(nw) > 1 else current_nw),
        "planName": meta.get("plan", "YNAB PLAN"), "currentPeriod": last_flow["label"] or meta.get("last_month", "CURRENT"),
        "periodDetail": "LIVE YNAB DATA", "syncLabel": "SYNCED FROM YNAB",
        "mobileKpis": [["SAVINGS RATE", f"{savings_rate:.1f}%", "#E8E7E3"],
                       ["AGE OF MONEY", f"{meta.get('age') or '—'}d", "#E8E7E3"],
                       ["READY TO ASSIGN", float(meta.get("ready", 0)), "#656866"],
                       ["RUNWAY", f"{(liquid / avg_spend if avg_spend else 0):.1f} mo", "#3FA37A"]],
        "recentTxns": data.get("recent_transactions", []),
        "transactionDetails": data.get("transaction_details", []),
        "transactionMonths": transaction_months,
        "txnHistoryNote": f"{len(data.get('transaction_details', []))} TRANSACTIONS · FULL SYNCED HISTORY",
    }


def _replace_between(text, start, end, replacement):
    a = text.index(start)
    b = text.index(end, a)
    return text[:a] + replacement + text[b:]


def _live_template_html(data):
    if not os.path.exists(DESIGN_TEMPLATE):
        raise SystemExit(f"Design template not found: {DESIGN_TEMPLATE}")
    html = open(DESIGN_TEMPLATE, encoding="utf-8").read()
    html = re.sub(r"\n<!--[^>]*MOBILE[^>]*-->.*?(?=\n<!--[^>]*NOTES[^>]*-->)", "\n", html, count=1, flags=re.DOTALL)
    html = re.sub(r"\n<!--[^>]*NOTES[^>]*-->.*?(?=\n</div>\n\n</x-dc>)", "\n", html, count=1, flags=re.DOTALL)
    payload = json.dumps(_template_model(data), ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("<x-dc>", f"<script>window.__YNAB_TEMPLATE_DATA__={payload};</script>\n<x-dc>", 1)
    html = html.replace("  build() {", "  build() {\n    if (window.__YNAB_TEMPLATE_DATA__) return window.__YNAB_TEMPLATE_DATA__;", 1)
    html = html.replace("const n = S.range, hist = d.nw.slice(36 - n), histL = d.liab.slice(36 - n), histA = d.assets.slice(36 - n), mo = d.months.slice(36 - n);", "const n = Math.min(S.range, d.nw.length), hist = d.nw.slice(-n), histL = d.liab.slice(-n), histA = d.assets.slice(-n), mo = d.months.slice(-n);")
    html = html.replace("const last12 = d.nw.slice(24);", "const last12 = d.nw.slice(-12);")
    html = html.replace("const flow12 = d.flow.slice(12);", "const completeFlow = d.flow.filter(f => !String(f.label || '').includes('(MTD)')); const flow12 = completeFlow.slice(-12);")
    html = html.replace("const runway = flow12.map(f => 16442 / f.o);", "const runway = flow12.map(f => d.liquid / (f.o || 1));")
    html = html.replace("const pct = Math.min(100, c.spent / c.budgeted * 100);\n      const rem = c.budgeted - c.spent;", "const pct = c.budgeted > 0 ? Math.min(100, c.spent / c.budgeted * 100) : 0;\n      const rem = Number.isFinite(c.balance) ? c.balance : c.budgeted - c.spent;")
    html = html.replace("const on = c.name === sel.name, pct = Math.min(100, c.spent / c.budgeted * 100), rem = c.budgeted - c.spent;", "const on = c.name === sel.name, pct = c.budgeted > 0 ? Math.min(100, c.spent / c.budgeted * 100) : 0, rem = Number.isFinite(c.balance) ? c.balance : c.budgeted - c.spent;")
    html = html.replace("const pct = Math.min(100, c.spent / c.budgeted * 100), rem = c.budgeted - c.spent;", "const pct = c.budgeted > 0 ? Math.min(100, c.spent / c.budgeted * 100) : 0, rem = Number.isFinite(c.balance) ? c.balance : c.budgeted - c.spent;")
    html = _replace_between(html, "    const kpiDefs = [", "    const kpis =", "    const kpiDefs = d.kpiDefs;\n")
    html = html.replace("label, value, sub, scol, vcol, bl: i ? HAIR : '0', spark: sp(series, 68, 18)", "label, value: typeof value === 'number' ? this.m(value) : value, sub, scol, vcol, bl: i ? HAIR : '0', spark: sp(series, 68, 18)")
    html = _replace_between(html, "    const runwayRows = [", "\n\n    // ── spending", "    const runwayRows = d.runwayRows.map(([k, v, c]) => ({ k, v: typeof v === 'number' ? this.m(v) : v, c }));")
    html = html.replace("const sel = d.cats.find(c => c.name === S.cat) || d.cats[1];", "const sel = d.cats.find(c => c.name === S.cat) || d.cats[0];\n    const selTxns = (d.transactionDetails || []).filter(t => t.category === sel.name).slice(0, 12).map(t => ({ date: t.date, payee: t.payee, account: t.account, memo: t.memo || '', amount: this.m(t.amount, 2), col: t.amount < 0 ? '#D2D1CD' : ACCENT }));\n    const selIndex = Math.max(0, d.cats.findIndex(c => c.name === sel.name));\n    const txnTop = ((P.spendingLayout ?? 'chart') === 'chart' ? 350 : 42) + selIndex * 40 + 'px';\n    const transactionMonths = (d.transactionMonths || []).map(m => ({ key: m.key, label: m.label, count: m.count, total: this.m(m.total, 2), isOpen: S.txnMonth === m.key, bg: S.txnMonth === m.key ? '#0F1110' : 'transparent', chevron: S.txnMonth === m.key ? '−' : '+', toggle: () => this.setState({ txnMonth: S.txnMonth === m.key ? null : m.key }), rows: (m.rows || []).map(t => ({ date: t.date, payee: t.payee, account: t.account, category: t.category, memo: t.memo || t.type || '', amount: this.m(t.amount, 2), col: t.amount < 0 ? '#D2D1CD' : ACCENT })) }));")
    html = html.replace("const catMonths = d.months.slice(18);", "const catMonths = d.months.slice(-18);")
    html = html.replace("catMonths[17].short", "catMonths[catMonths.length - 1].short")
    html = html.replace("spendPrev: this.m(2384)", "spendPrev: this.m(d.spendPrev)")
    html = html.replace("((i + .5) * (1000 / 24))", "((i + .5) * (1000 / d.flow.length))")
    html = html.replace("d.flow[23].label", "d.flow[d.flow.length - 1].label")
    html = html.replace("concat([{ t: 'FEB 27' }])", "concat([{ t: 'PROJECTED' }])")
    html = html.replace("d.nw[35]", "d.nw[d.nw.length - 1]")
    html = html.replace("this.m(3118)", "this.m(d.netWorthDelta)")
    html = html.replace("      nwGrid, nwYLabels, nwXLabels, projLeft,", "      nwGrid, nwYLabels, nwXLabels, projLeft, nwSubtitle: d.nwSubtitle,")
    html = html.replace("      selCatSub: this.m(sel.budgeted - sel.spent) + ' left of ' + this.m(sel.budgeted),", "      selCatSub: this.m(Number.isFinite(sel.balance) ? sel.balance : sel.budgeted - sel.spent) + ' left of ' + this.m(sel.budgeted),\n      selCatMoved: this.m(sel.moved_out || 0), selTxns, hasSelTxns: selTxns.length > 0,\n      selTxnsNote: selTxns.length ? selTxns.length + ' MATCHING TRANSACTIONS' : 'NO TRANSACTIONS IN SYNC WINDOW',\n      showTxnDrawer: !!S.txnOpen, closeTxnDrawer: () => this.setState({ txnOpen: false }),")
    html = html.replace("showTxnDrawer: !!S.txnOpen,", "showTxnDrawer: !!S.txnOpen, openTxnDrawer: () => this.setState({ txnOpen: true }), txnTop, transactionMonths, txnHistoryNote: d.txnHistoryNote,")
    html = _replace_between(html, "    const comp = [", "    const posTotal =", "    const comp = d.comp;\n")
    html = _replace_between(html, "    const mobileKpis = [", "    const mobileCats =", "    const mobileKpis = d.mobileKpis.map(([label, value, vcol], i) => ({ label, value: typeof value === 'number' ? this.m(value, 2) : value, vcol, bl: i % 2 ? HAIR : '0' }));\n")
    html = _replace_between(html, "    const txns = [", "\n\n    const notes =", "    const txns = d.recentTxns.map(t => ({ payee: t.payee, meta: t.date.toUpperCase() + ' · ' + t.account.toUpperCase(), amt: this.m(t.amount, 2) }));")
    html = html.replace("Numbers are illustrative", "Live YNAB data")
    html = html.replace("Balances, categories and payees are plausible placeholders. Swap in a real export and the layout holds — every figure is derived, none hard-positioned.", "Balances, categories and payees are calculated from the latest local YNAB sync. Nothing in this dashboard writes back to your budget.")
    html = html.replace(">PERSONAL<", ">{{ planName }}<")
    html = html.replace(">AUG 2026<", ">{{ currentPeriod }}<")
    html = html.replace(">DAY 9 / 31<", ">{{ periodDetail }}<")
    html = html.replace("SYNCED 09:14", "{{ syncLabel }}")
    # The hosted dashboard is rebuilt by the scheduled Cloudflare/GitHub sync.
    # Reloading on a timer means an already-open tab picks up that deployment
    # without exposing the YNAB token to the browser.
    refresh_script = """
<script>
(() => {
  const refreshMs = 5 * 60 * 1000;
  window.setTimeout(() => {
    const next = new URL(window.location.href);
    next.searchParams.set('_refresh', Date.now().toString());
    window.location.replace(next.toString());
  }, refreshMs);
})();
</script>
"""
    if "_refresh" not in html and "</body>" in html:
        html = html.replace("</body>", refresh_script + "\n</body>")
    return html


def render(data, out_path):
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if not os.path.exists(DESIGN_SUPPORT):
        raise SystemExit(f"Template runtime not found: {DESIGN_SUPPORT}")
    runtime_out = os.path.join(os.path.dirname(out_path), "support.js")
    if os.path.abspath(DESIGN_SUPPORT) != os.path.abspath(runtime_out):
        shutil.copyfile(DESIGN_SUPPORT, runtime_out)
    open(out_path, "w", encoding="utf-8").write(_live_template_html(data))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", default="last-used")
    ap.add_argument("--months", type=int, default=36)
    ap.add_argument("--out", default="ynab-ledger-dashboard.html")
    ap.add_argument("--token", default=os.environ.get("YNAB_TOKEN"))
    ap.add_argument("--from-cache", action="store_true",
                    help="Regenerate from .ynab_raw.json without contacting YNAB.")
    args = ap.parse_args()
    if args.from_cache:
        try:
            budget = json.load(open(".ynab_raw.json", encoding="utf-8"))
        except FileNotFoundError:
            raise SystemExit("No .ynab_raw.json cache exists yet. Run a live sync first.")
        print("Rebuilding from the local YNAB cache...")
    else:
        if not args.token:
            raise SystemExit("Set YNAB_TOKEN or use --from-cache to rebuild from the last sync.")
        print("Fetching budget from YNAB...")
        resp = api_get(f"/budgets/{args.budget}", args.token)
        budget = resp["data"]["budget"]
        json.dump(budget, open(".ynab_raw.json", "w", encoding="utf-8"))
        print(f"  {len(budget.get('accounts',[]))} accounts, "
              f"{len(budget.get('transactions',[]))} transactions pulled")

    # build the history month list the analytics will use, to know which months
    # to average 'assigned' over
    from analytics import _month_iter, _month_start, _add_month
    today = date.today()
    last_complete = _add_month(_month_start(today), -1)
    hist_months = _month_iter(_add_month(last_complete, -(args.months - 1)), last_complete)

    currency, accounts, txns, assigned, moved_out, category_balances, dashboard_meta = normalise(budget, hist_months)
    analytics.set_assigned(assigned)
    data = analytics.build_data(txns, accounts, currency=currency,
                                today=today, hist_n=args.months,
                                dashboard_meta=dashboard_meta, budget_movements=moved_out,
                                category_balances=category_balances)

    json.dump(data, open("data.json", "w"))
    render(data, args.out)
    k = data["kpi"]
    print(f"Done: {args.out} written")
    print(f"  net worth {currency}{k['net_worth']:,.0f} · "
          f"avg spend {currency}{k['avg_spend']:,.0f}/mo · "
          f"savings rate {k['savings_rate']}%")
    print(f"  open {args.out} in your browser.")


if __name__ == "__main__":
    main()
