# Deep Test Report — billing-fixed

Date: 2026-09-07. Env: Windows 11, Python 3.14 venv
(Flask 3.1, SQLAlchemy 2.0, Flask-WTF CSRF **enabled** in sweep, disabled in
unit suite via `WTF_CSRF_ENABLED=False` as is standard for Flask tests).

## Result: 60 / 60 PASS, 0 FAIL

| Layer | Cases | Result |
|---|---|---|
| Unit suite `tests.py` (12 original + 9 new) | 21 | 21 pass (~17s) |
| Adversarial sweep `sweep.py` (CSRF on, logged-out + logged-in) | 39 | 39 pass |

## What the 21 unit tests cover
Auth (login, full logged-out matrix), no-API-storm homepage, duplicate-phone
guard, product pricing from manual rates, gold GST invoice (63,000 + 3% =
64,890) with stock decrement + auto-paid, insufficient-stock rejection with
zero side effects, delete restores stock + reverses ledger, product-in-invoice
delete guard, overpayment rejection, general-item 0% GST, mixed gold+general
GST (63,200 + 3% = 65,096), silver making math (3,200 + 600 = 3,800 + 3% =
3,914), pay-remaining FIFO → paid + zero balance, QR/Code128 helpers
(data-URI PNG, None on empty), product page (code text + barcode image, no
stale `making_charges_percent`), tag page (5 inch sizes, code, print CSS,
`?w=&h=` preset), invoice page (client search, camera scanner, defaults,
general option, old positional JS gone), exports, health, security headers.

## What the 39 sweep checks cover
20 anonymous route checks (GET → 302 login; POST → 400 CSRF / 302 — denied
either way), CSRF login, CSRF-protected product + invoice creation, exact
total 64,890, overpayment blocked, scan hit/miss, delete → double-delete 404
(no 500), exports, search/pagination params, reports filter, audit/users
pages, health.

## Bugs found during testing: none in app code
- One harness artifact: an ad-hoc inline script without `db.drop_all()`
  hit a stale `instance/billing.db` (UNIQUE user.email). Fixed in harness,
  not an app bug. Suite + sweep both start from a clean DB.
- Known non-bugs (by design): anon POSTs answer 400 (CSRF fires before the
  login redirect) vs 302 on exempt routes — both deny; `/products` runs one
  extra unpaginated query for the gold/silver weight totals.

## Not covered (need a browser/device, not scriptable here)
Live camera scan on a real phone/HTTPS host; printed-paper ruler check of
1.5×0.5 / 2×1 / 3×2 tags; metals.live API with a real key (suite uses manual
prices; fallback path asserted).

Run: `python tests.py` · `python sweep.py` (sweep is harness-only, not shipped).
