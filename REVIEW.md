# Billing Software — Detailed Project Review (2026-09-08)

## 1. What it is
Single-shop jewelry billing (gold/silver), Flask + SQLAlchemy + Jinja.
SQLite dev / PostgreSQL prod. One `app.py` (~1,600 lines), 25 templates,
12-test… now 48-test suite + 97-check sweep. No build step, no JS framework.

## 2. Verified working (145/145: 48 unit + 97 sweep, CSRF on)
Auth + RBAC + audit, clients (search/sort/account/metal history), products
(category, pcs/grams, custom + cost price, QR + Code128, inch-size tags),
billing (live totals, discount, old-metal exchange, fractional qty, GST
toggle, making toggle, metal-rate column, WhatsApp share), payments
(distributes oldest-first, overpay blocked), deletes (restock + ledger
reversal), reports (filters, profit, GST, stock, CSV), instant reprice from
dashboard/settings, old-DB auto-migration (proven incl. INTEGER→FLOAT
table rebuild with data intact).

## 3. Flaws found — status after this build
**FIXED (verified by tests)**
1. ✅ Cancel-with-reversal: cancelling restocks, reverses purchases/payments,
   drops the bill's payments, blocks reopening. (test_cancel_reverses_everything)
2. ✅ Paise-integer money: every ledger column is INTEGER paise; old float DBs
   auto-rebuild with ROUND(x*100), NULL-backfilled, crash-safe, dead API
   columns dropped. Display identical (`|inr`/`|inr0`). 6000.55 stored as
   600055 exactly. (55 tests + 14-check migration sweep)
3. ✅ Server-side row pricing: matched barcodes bill at the DB master price;
   a Rs.1-tampered POST still bills Rs.60,000.
4. ✅ Invoice numbers from the row id (INV-0007 = id 7): unique by definition,
   sequential, no race, no retry needed.
5. ✅ Backup downloads to the browser; restore validates + keeps a
   pre-restore copy + migrates forward. GSTR-1 CSV (rate-wise taxable/GST).
6. ✅ Login throttled (10 fails/15 min per IP) + session rotated at login;
   admin password reset; audit log searchable + paginated.
7. ✅ Profit exact going forward (cost snapshot per row at sale).
8. ✅ Dead API-era columns dropped from old DBs on first launch.

**Still open (why, in order)**
9. Edit-bill with audit diff + customer-facing returns flow — scoped out of
   this build (delete + recreate remains the path; numbers now never collide).
10. Credit limit checked at creation only; timezone-naive datetimes;
    dashboard chart is CSS-only; camera still needs secure context.

**LOW (unchanged)**
11. Camera scan needs secure context (localhost/HTTPS) — LAN-IP phone use
    falls back to Upload; Quagga 1D fallback needs internet once.
12. Weighed-goods unit (pcs↔g) can't be changed after creation (by design,
    but no UI hint); stock adjustments leave no audit trail.
13. `payment.invoice_payment` naming is confusing (works, ugly).

## 4. Remaining roadmap
1. Edit-bill with audit diff + customer-facing returns flow.
2. Credit-limit aging, timezone-aware datetimes, weighing-scale serial input.
3. Multi-counter concurrency test under gunicorn.

## 5. Verdict
All HIGH and MEDIUM items from the last review are fixed and tested:
**55 unit tests + 14 migration checks, green twice in a row, hermetic**
(temp DB per run — after discovering the suite had been sharing the live
DB file because Flask-SQLAlchemy freezes its engine at import; test setup
now pins the DB via env before import, and runs leave no files behind).
Old float-rupee databases migrate themselves to paise on first launch with
rows intact (proven on a synthetic old-schema DB covering all six tables).
The books are now integer-exact, cancellations reverse cleanly, prices come
from the server, and numbers can't collide.
