"""
analytics.py — turn a normalised list of transactions + accounts into the
data.json that the Ledgerlens dashboard reads.

This is the SAME analytics used to build the sample dashboard, generalised to
take real inputs. It is deliberately source-agnostic: `ynab_sync.py` feeds it
data pulled from the YNAB API, but you could feed it a CSV export too.

Normalised input contract
-------------------------
txns: list of dicts, one per (sub)transaction leg:
    {
      "date": "YYYY-MM-DD",
      "account": "<account name>",
      "payee": "<payee name or ''>",
      "category": "<category name or ''>",   # '' for income / transfers
      "amount": <float>,      # in currency units, +inflow / -outflow
      "is_income": <bool>,    # inflow to budget (salary etc.)
      "is_transfer": <bool>,  # movement between your own accounts
    }
accounts: list of dicts, one per account (current state):
    {
      "name": "<account name>",
      "balance": <float>,          # current balance, liabilities negative
      "on_budget": <bool>,
      "is_liability": <bool>,
      "is_investment": <bool>,     # tracking/investment asset
    }
currency: symbol string, e.g. "£"
today: datetime.date (used to anchor forecasts/projections)
hist_n: how many months of history to include (default 18)
"""
from collections import defaultdict
from datetime import date, timedelta
import math


def _mkey(d):  return d.strftime("%Y-%m")
def _mlabel(d): return d.strftime("%b %y")

def _month_start(d): return date(d.year, d.month, 1)
def _add_month(d, n=1):
    y, m = d.year, d.month - 1 + n
    return date(y + m // 12, m % 12 + 1, 1)
def _month_end(d):
    return _add_month(_month_start(d)) - timedelta(days=1)
def _month_iter(a, b):
    out, cur = [], _month_start(a)
    while cur <= b:
        out.append(cur); cur = _add_month(cur)
    return out


def build_data(txns, accounts, currency="£", today=None, hist_n=18,
               low_threshold=3000.0, save_return=(0.06, 0.09, 0.02),
               dashboard_meta=None, budget_movements=None, category_balances=None):
    if today is None:
        raise ValueError("pass today=date.today() from the caller")
    for t in txns:
        t["_d"] = date.fromisoformat(t["date"])

    # ---- span: up to hist_n complete months, but never before data exists ----
    cur_m = _month_start(today)
    last_complete = _add_month(cur_m, -1)                  # last fully-elapsed month
    has_current = any(t["_d"] >= cur_m for t in txns)      # any spend/income this month?
    mtd_key = _mkey(cur_m) if has_current else None
    view_last = cur_m if has_current else last_complete
    first = _add_month(view_last, -(hist_n - 1))
    earliest = min((t["_d"] for t in txns), default=first)
    if _month_start(earliest) > first:                    # don't fabricate pre-budget history
        first = _month_start(earliest)
    # view_months INCLUDE the current partial (month-to-date) month → live trends/heatmap/totals
    hist_months = _month_iter(first, view_last) or [cur_m]
    # complete months EXCLUDE the partial month → used for averages, movers, budget-vs-actual, gating
    comp_months = [m for m in hist_months if m <= last_complete] or hist_months[:1]
    hm_keys = [_mkey(m) for m in hist_months]
    comp_keys = [_mkey(m) for m in comp_months]
    lastk = comp_keys[-1]                                  # "last month" = last COMPLETE month
    heat_start = hist_months[max(0, len(hist_months) - 12)]

    # ---- spending / income aggregates ----
    spend_mc   = defaultdict(lambda: defaultdict(float))  # month -> cat -> spend
    spend_payee= defaultdict(float)
    spend_cat  = defaultdict(float)
    spend_mtot = defaultdict(float)
    income_m   = defaultdict(float)
    daily      = defaultdict(float)
    for t in txns:
        k = _mkey(t["_d"])
        if t["is_transfer"] or t.get("is_adjustment"):   # transfers & balance bookkeeping
            continue
        if t["is_income"]:
            income_m[k] += t["amount"]; continue
        cat = t["category"] or "Uncategorised"
        if cat.startswith("Inflow"):                     # inflow-category outflows aren't spend
            continue
        amt = -t["amount"]           # positive spend
        if amt <= 0:                 # refunds / negatives ignored for spend views
            continue
        spend_mc[k][cat] += amt
        spend_payee[t["payee"] or "(no payee)"] += amt
        spend_cat[cat] += amt
        spend_mtot[k] += amt
        if t["_d"] >= heat_start and t["_d"] <= today:   # live: through today
            daily[t["_d"].isoformat()] += amt

    cat_names = sorted(spend_cat, key=lambda c: -spend_cat[c])
    n_hist = len(comp_months)

    # ---- net worth reconstruction (month-end) ----
    # month_end_balance(acct, m) = current_balance - sum(tx.amount after month end)
    tx_by_acct = defaultdict(list)
    for t in txns:
        tx_by_acct[t["account"]].append(t)
    # choose up to 5 headline accounts for the balance chart (largest |balance|)
    headline = sorted(accounts, key=lambda a: -abs(a["balance"]))[:5]
    headline_names = [a["name"] for a in headline]

    networth = []
    for m in hist_months:
        me = min(_month_end(m), today)          # current month reconstructs to today
        total = 0.0
        row = {"m": _mkey(m), "label": _mlabel(m) + (" (MTD)" if _mkey(m) == mtd_key else "")}
        for a in accounts:
            after = sum(t["amount"] for t in tx_by_acct.get(a["name"], []) if t["_d"] > me)
            bal = a["balance"] - after
            total += bal
            if a["name"] in headline_names:
                row[a["name"]] = round(bal, 2)
        row["net"] = round(total, 2)
        networth.append(row)

    # headline net worth = today's actual balances (not the month-end reconstruction)
    cur_nw = round(sum(a["balance"] for a in accounts), 2)
    nw_12ago = networth[-13]["net"] if len(networth) >= 13 else networth[0]["net"]
    # account snapshot (all accounts, current) for the low-history balance view
    accounts_snapshot = sorted(
        [{"name": a["name"], "balance": round(a["balance"], 2),
          "is_liability": a["is_liability"], "is_investment": a["is_investment"],
          "on_budget": a.get("on_budget", True),
          "type": a.get("type", ""),
          "last_reconciled_at": a.get("last_reconciled_at"),
          "direct_import_linked": a.get("direct_import_linked", False),
          "direct_import_in_error": a.get("direct_import_in_error", False)}
         for a in accounts], key=lambda x: -x["balance"])

    # ---- trailing averages ----
    def avg_last(dic, n):
        ks = comp_keys[-n:]                                       # complete months only
        return sum(dic.get(k, 0) for k in ks) / max(1, len(ks))
    avg_spend = avg_last(spend_mtot, 3)
    avg_inc   = avg_last(income_m, 3)
    savings_rate = (avg_inc - avg_spend) / avg_inc * 100 if avg_inc else 0

    # ---- monthly stacked (last 12, current month flagged month-to-date) ----
    trend = hist_months[-12:]
    monthly_cat = []
    for m in trend:
        k = _mkey(m)
        lbl = _mlabel(m) + (" (MTD)" if k == mtd_key else "")
        r = {"m": k, "label": lbl, "total": round(spend_mtot.get(k, 0), 2), "mtd": k == mtd_key}
        for c in cat_names: r[c] = round(spend_mc[k].get(c, 0), 2)
        monthly_cat.append(r)

    top_payees = [{"payee": p, "amount": round(v, 2)}
                  for p, v in sorted(spend_payee.items(), key=lambda x: -x[1])[:12]]
    cat_totals = [{"cat": c, "amount": round(spend_cat[c], 2), "avg": round(spend_cat[c] / n_hist, 2)}
                  for c in cat_names]

    # ---- drift: last complete month vs prior 3-mo avg ----
    prev3 = comp_keys[-4:-1]
    drift = []
    for c in cat_names:
        last = spend_mc[lastk].get(c, 0)
        base = sum(spend_mc[k].get(c, 0) for k in prev3) / max(1, len(prev3))
        drift.append({"cat": c, "last": round(last, 2), "base": round(base, 2),
                      "delta": round(last - base, 2),
                      "pct": round((last - base) / base * 100 if base else 0, 1)})
    drift.sort(key=lambda x: -abs(x["delta"]))

    # ---- budget vs actual (complete months only; assigned from YNAB) ----
    bva = []
    for c in cat_names[:12]:
        ks6 = comp_keys[-6:]
        act = sum(spend_mc[k].get(c, 0) for k in ks6) / max(1, len(ks6))
        assigned = max(0.0, _ASSIGNED.get(c, round(act)))  # unassignments must not become negative budgets
        moved_out = max(0.0, float((budget_movements or {}).get(c, 0)))
        balance = float((category_balances or {}).get(c, assigned - act))
        bva.append({"cat": c, "assigned": round(assigned, 2), "moved_out": round(moved_out, 2), "balance": round(balance, 2), "actual": round(act, 2),
                    "var": round(assigned - act, 2),
                    "pct": round(act / assigned * 100 if assigned else 0, 0)})

    # ---- cash-flow forecast (liquid on-budget balance) ----
    liquid_now = sum(a["balance"] for a in accounts if a["on_budget"] and not a["is_liability"])
    base_out = avg_spend      # all spend leaves liquid; sweeps to tracking handled separately
    fc = []; cur = liquid_now; cumvar = 0.0
    fc_months = _month_iter(_month_start(today), _add_month(_month_start(today), 5))
    for m in fc_months:
        out = base_out
        if m.month == 12: out *= 1.32
        if m.month in (7, 8): out *= 1.18
        cur = cur + avg_inc - out
        cumvar += (out * 0.13) ** 2
        spread = math.sqrt(cumvar)
        fc.append({"m": _mkey(m), "label": _mlabel(m), "proj": round(cur, 2),
                   "lo": round(cur - spread, 2), "hi": round(cur + spread, 2),
                   "flag": (cur - spread) < low_threshold})

    # ---- money-flow sankey (trailing 12 months) ----
    ks12 = hm_keys[-12:]
    inc12 = sum(income_m.get(k, 0) for k in ks12)
    cat12 = {c: sum(spend_mc[k].get(c, 0) for k in ks12) for c in cat_names}
    spend12 = sum(cat12.values())
    # 'saved' = net inflow to investment/tracking accounts over the window (via transfers)
    inv_names = {a["name"] for a in accounts if a["is_investment"]}
    save_names = {a["name"] for a in accounts
                  if not a["is_liability"] and not a["is_investment"]
                  and a["name"].lower().find("saving") >= 0}
    inv_flow = _net_transfer_in(txns, inv_names, ks12)
    save_flow = _net_transfer_in(txns, save_names, ks12)
    buffer12 = max(0, inc12 - spend12 - inv_flow - save_flow)
    cs = sorted(cat12.items(), key=lambda x: -x[1]); top6 = cs[:6]; other = sum(v for _, v in cs[6:])
    uses = [{"name": c, "value": round(v, 2), "kind": "spend"} for c, v in top6]
    if other > 0: uses.append({"name": "Other spending", "value": round(other, 2), "kind": "spend"})
    if inv_flow > 0: uses.append({"name": "Investments", "value": round(inv_flow, 2), "kind": "save"})
    if save_flow > 0: uses.append({"name": "Savings", "value": round(save_flow, 2), "kind": "save"})
    if buffer12 > 0: uses.append({"name": "Kept in cash", "value": round(buffer12, 2), "kind": "save"})
    # income sources: top 2 income payees
    inc_by_payee = defaultdict(float)
    for t in txns:
        if t["is_income"] and _mkey(t["_d"]) in ks12:
            inc_by_payee[t["payee"] or "Income"] += t["amount"]
    src_sorted = sorted(inc_by_payee.items(), key=lambda x: -x[1])
    sources = [{"name": p if len(p) < 22 else p[:20] + "…", "value": round(v, 2)} for p, v in src_sorted[:2]]
    if len(src_sorted) > 2:
        sources.append({"name": "Other income", "value": round(sum(v for _, v in src_sorted[2:]), 2)})
    if not sources:
        sources = [{"name": "Income", "value": round(inc12, 2)}]
    if has_current:
        sk_period = "Live · to " + today.strftime("%d %b")
    else:
        nmo = len(comp_months)
        sk_period = ("This month · " + _mlabel(hist_months[-1])) if nmo <= 1 else f"Trailing {min(12, nmo)} months"
    sankey = {"sources": sources, "hub": {"name": "Money in", "value": round(inc12, 2)},
              "uses": uses, "period": sk_period}

    # ---- net-worth projection (24 months, 3 scenarios) ----
    inv_now = sum(a["balance"] for a in accounts if a["is_investment"])
    noninv_now = cur_nw - inv_now
    monthly_surplus = avg_inc - avg_spend
    # rough split: whatever flows to investments monthly (from sankey) else 1/3 of surplus
    inv_contrib = (inv_flow / 12) if inv_flow > 0 else max(0, monthly_surplus / 3)
    cash_contrib = monthly_surplus - inv_contrib
    pmonths = _month_iter(_month_start(today), _add_month(_month_start(today), 23))
    projection = {"months": [_mlabel(m) for m in pmonths],
                  "start_label": _mlabel(hist_months[-1]), "start": round(cur_nw, 2),
                  "current_invest": round(inv_now, 2), "monthly_surplus": round(monthly_surplus, 2)}
    for key, (ret, smul) in zip(("expected", "optimistic", "pessimistic"),
                                ((save_return[0], 1.00), (save_return[1], 1.15), (save_return[2], 0.70))):
        inv = inv_now; noninv = noninv_now; series = []
        ai = inv_contrib * smul; ac = cash_contrib * smul
        for _ in pmonths:
            inv = inv * (1 + ret / 12) + ai
            noninv = noninv + ac
            series.append(round(inv + noninv, 2))
        projection[key] = {"label": {"expected": "Expected", "optimistic": "Optimistic",
                                     "pessimistic": "Cautious"}[key],
                           "series": series, "end": series[-1]}

    # ---- daily heat scale ----
    dv = sorted(v for v in daily.values() if v > 0)
    def pc(a, p): return a[min(len(a) - 1, int(p * len(a)))] if a else 0
    through = f"{today.day} {today.strftime('%b %Y')}"   # portable (no %-d; Windows-safe)
    heat_scale = {"p50": round(pc(dv, .5), 2), "p90": round(pc(dv, .9), 2),
                  "max": round(max(dv), 2) if dv else 0,
                  "avg": round(sum(dv) / len(dv), 2) if dv else 0,
                  "start": heat_start.isoformat(), "end": _mkey(hist_months[-1]),
                  "end_date": today.isoformat()}

    span = f"{_mlabel(hist_months[0])} – {through}" if has_current else \
           f"{_mlabel(hist_months[0])} – {_mlabel(hist_months[-1])}"
    monthly_flow = []
    for m in hist_months:
        key = _mkey(m)
        income = round(income_m.get(key, 0), 2)
        outflow = round(spend_mtot.get(key, 0), 2)
        monthly_flow.append({"m": key,
                             "label": _mlabel(m) + (" (MTD)" if key == mtd_key else ""),
                             "income": income, "outflow": outflow,
                             "net": round(income - outflow, 2)})

    return {
        "meta": {"currency": currency, "generated": "live",
                 "last_month": _mlabel(hist_months[-1]),
                 "last_complete": _mlabel(last_complete),
                 "through": through, "has_current": has_current,
                 "span": span,
                 "accounts": headline_names, "low_threshold": low_threshold,
                 "n_history": len(comp_months), "first_month": _mlabel(hist_months[0]),
                 **(dashboard_meta or {})},
        "accounts_snapshot": accounts_snapshot,
        "kpi": {"net_worth": round(cur_nw, 2), "nw_change_12m": round(cur_nw - nw_12ago, 2),
                "nw_change_pct": round((cur_nw - nw_12ago) / nw_12ago * 100, 1) if nw_12ago > 100 else 0,
                "avg_spend": round(avg_spend, 2), "avg_income": round(avg_inc, 2),
                "savings_rate": round(savings_rate, 1)},
        "networth": networth, "monthly_flow": monthly_flow,
        "cat_names": cat_names, "monthly_cat": monthly_cat,
        "top_payees": top_payees, "cat_totals": cat_totals, "drift": drift, "bva": bva,
        "forecast": fc, "forecast_start": {"label": _mlabel(hist_months[-1]), "bal": round(liquid_now, 2)},
        "daily": [{"d": k, "v": round(v, 2)} for k, v in sorted(daily.items())],
        "heat_scale": heat_scale, "sankey": sankey, "projection": projection,
        "recent_transactions": [
            {"date": t["date"], "payee": t.get("payee") or "(no payee)",
             "account": t.get("account") or "Account", "amount": round(t["amount"], 2)}
            for t in sorted((t for t in txns if not t["is_transfer"] and not t.get("is_adjustment")),
                            key=lambda t: t["date"], reverse=True)[:6]
        ],
        "transaction_details": [
            {"date": t["date"], "payee": t.get("payee") or "(no payee)",
             "account": t.get("account") or "Account", "category": t.get("category") or "Uncategorised",
             "amount": round(t["amount"], 2), "memo": t.get("memo") or "",
             "cleared": t.get("cleared") or "uncleared", "approved": bool(t.get("approved", False)),
             "flag": t.get("flag_color") or "", "type": "TRANSFER" if t.get("is_transfer") else ("ADJUSTMENT" if t.get("is_adjustment") else ("INCOME" if t.get("is_income") else "SPENDING"))}
            for t in sorted(txns, key=lambda t: t["date"], reverse=True)
        ],
        "n_txns": sum(1 for t in txns if not t["is_transfer"]),
    }


# assigned-budget lookup, optionally populated by the caller before build_data()
_ASSIGNED = {}
def set_assigned(mapping):
    """Give build_data access to per-category assigned amounts (avg over recent months)."""
    _ASSIGNED.clear(); _ASSIGNED.update(mapping)


def _net_transfer_in(txns, acct_names, month_keys):
    """Net money moved INTO the given accounts via transfers over month_keys."""
    if not acct_names: return 0.0
    total = 0.0
    for t in txns:
        if not t["is_transfer"]: continue
        if t["date"][:7] not in month_keys: continue
        if t["account"] in acct_names:
            total += t["amount"]          # +inflow to the tracked account
    return max(0.0, round(total, 2))
