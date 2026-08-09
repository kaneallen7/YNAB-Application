"""Generate a YNAB-shaped .ynab_raw.json for testing the pipeline offline.
Mimics the real BudgetDetail: milliunits, splits, transfers, Inflow income,
tracking (investment) account, and monthly assigned budgets."""
import json, random
from datetime import date, timedelta
random.seed(11)
M = 1000

def mkey(d): return d.strftime("%Y-%m")
def months(a, b):
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(date(y, m, 1)); m += 1
        if m == 13: m = 1; y += 1
    return out

hist = months(date(2025, 5, 1), date(2026, 7, 1))  # 15 months

ACC = {
    "acc-check": ("Everyday Checking", "checking", True,  3200),
    "acc-save":  ("Savings",           "savings",  True,  8500),
    "acc-cc":    ("Amex",              "creditCard", True, -640),
    "acc-inv":   ("Vanguard ISA",      "otherAsset", False, 15200),
}
CATS = {"Rent":1450,"Groceries":520,"Dining Out":240,"Transport":160,
        "Utilities":180,"Shopping":200,"Health":130,"Travel":250}
PAYEES = {"Rent":"Oakfield Lettings","Groceries":"Tesco","Dining Out":"Dishoom",
          "Transport":"TfL","Utilities":"Octopus Energy","Shopping":"Amazon",
          "Health":"PureGym","Travel":"British Airways"}

txns, subs = [], []
tid = 0
def add(d, acc, payee, cat, amount_units, transfer=None, split=None):
    global tid; tid += 1
    t = {"id": f"t{tid}", "date": d.isoformat(), "account_id": acc,
         "account_name": ACC[acc][0], "payee_name": payee,
         "category_name": cat, "amount": int(round(amount_units*M)),
         "transfer_account_id": transfer, "cleared": "cleared", "deleted": False}
    txns.append(t); return t["id"]

# starting balances
for aid,(name,typ,onb,bal) in ACC.items():
    add(hist[0].replace(day=1), aid, "Starting Balance", "Inflow: Ready to Assign", bal)

for d in hist:
    # salary inflow
    add(d.replace(day=28), "acc-check", "Northwind Ltd", "Inflow: Ready to Assign", 4520)
    if d.month in (6,12):
        add(d.replace(day=20), "acc-check", "Northwind Ltd (Bonus)", "Inflow: Ready to Assign", 900)
    # rent from checking
    add(d.replace(day=1), "acc-check", PAYEES["Rent"], "Rent", -CATS["Rent"])
    # utilities from checking
    add(d.replace(day=15), "acc-check", PAYEES["Utilities"], "Utilities",
        -round(CATS["Utilities"]*random.uniform(.85,1.3)))
    # cc spending across categories
    for cat in ["Groceries","Dining Out","Transport","Shopping","Health"]:
        seas = 1.4 if (cat=="Dining Out" and d.month==12) else 1.0
        for _ in range(random.randint(2,5)):
            add(d.replace(day=random.randint(2,27)), "acc-cc", PAYEES[cat], cat,
                -round(CATS[cat]/3*random.uniform(.4,1.4)*seas))
    # travel (lumpy) on cc
    if random.random()<0.4 or d.month in (7,8,12):
        add(d.replace(day=random.randint(2,27)), "acc-cc", PAYEES["Travel"], "Travel",
            -round(CATS["Travel"]*random.uniform(.8,3.0)))
    # a SPLIT transaction: Amazon split Shopping + Health
    pid = add(d.replace(day=random.randint(2,27)), "acc-cc", "Amazon", "Split (Multiple)", -85)
    for scat,amt in [("Shopping",-55),("Health",-30)]:
        subs.append({"id":f"s{pid}-{scat}","transaction_id":pid,"amount":int(amt*M),
                     "payee_name":"Amazon","category_name":scat,"transfer_account_id":None,"deleted":False})
    # transfer to savings & investments
    add(d.replace(day=2), "acc-save", "Transfer : Everyday Checking", None, 350, transfer="acc-check")
    add(d.replace(day=2), "acc-check", "Transfer : Savings", None, -350, transfer="acc-save")
    add(d.replace(day=2), "acc-inv", "Transfer : Everyday Checking", None, 500, transfer="acc-check")
    add(d.replace(day=2), "acc-check", "Transfer : Vanguard ISA", None, -500, transfer="acc-inv")
    # investment growth as a balance adjustment (not income)
    add(d.replace(day=28), "acc-inv", "Manual Balance Adjustment", "Inflow: Ready to Assign",
        round(random.uniform(-100,350)))

# recompute current balances = start + sum of that account's txns
bal = {}
for aid in ACC: bal[aid]=0
for t in txns:
    bal[t["account_id"]] += t["amount"]
accounts=[]
for aid,(name,typ,onb,_) in ACC.items():
    accounts.append({"id":aid,"name":name,"type":typ,"on_budget":onb,
                     "closed":False,"deleted":False,"balance":bal[aid]})

# months[] with assigned + activity per category
months_arr=[]
for d in hist:
    cats=[]
    for c,base in CATS.items():
        act=sum(t["amount"] for t in txns if t["category_name"]==c and t["date"][:7]==mkey(d))
        # plus split subs
        act+=sum(s["amount"] for s in subs if s["category_name"]==c
                 and any(t["id"]==s["transaction_id"] and t["date"][:7]==mkey(d) for t in txns))
        cats.append({"id":f"c-{c}","name":c,"budgeted":int(base*M),"activity":act,
                     "balance":0,"hidden":False,"deleted":False})
    months_arr.append({"month":d.isoformat(),"categories":cats})

budget={"currency_format":{"currency_symbol":"£","iso_code":"GBP","decimal_digits":2},
        "accounts":accounts,"transactions":txns,"subtransactions":subs,
        "months":months_arr,"categories":[],"category_groups":[],"payees":[]}
json.dump({"data":{"budget":budget,"server_knowledge":1}}, open(".ynab_mock.json","w"))
print("mock written:",len(txns),"txns,",len(subs),"subs,",len(accounts),"accounts")
