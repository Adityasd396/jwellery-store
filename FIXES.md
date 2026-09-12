# What was fixed (vs original billing-software.zip)

## P0 — security & correctness
- `login_required` on ALL routes (was missing on view/edit client, all invoice
  view/delete/pay, all product view/edit/delete, reports). Verified by
  `test_auth_required`.
- CSRF via Flask-WTF on all HTML forms (11 templates patched); API/scan/health
  exempt. Verified: POST without token → 400.
- `config.py`: SQLite no longer gets `pool_size/max_overflow` (crashed);
  Postgres keeps pooling. `DATABASE_URL` supports `postgres://`. `.env.example`
  added. Default `SECRET_KEY` warns loudly.
- `debug=True` removed; security headers (`nosniff`, `DENY`, referrer),
  404/500 handlers with rollback, session cookie flags, 8h session lifetime.
- Price cache `PRICE_CACHE_TTL` (1800s): homepage no longer fires 2 API calls +
  mass product rewrite per hit (suite went 64s → 17s). `update_all_product_prices`
  only on explicit POST `/api/update-prices` or settings-driven manual mode.
- Invoice creation is one atomic transaction: single commit, rollback on error,
  stock checked BEFORE decrement (was commit-in-loop, negative stock possible),
  invoice numbers skip collisions via unique check.
- `InvoiceItem.product_id` now recorded → delete invoice restores stock,
  reverses `client.total_purchases/payments/balance`, deletes its payments
  (was orphan + corrupt ledger). Verified by test.
- Delete product blocked when used in invoices; delete client blocked with
  invoices (new route). Overpayment blocked; payment method allow-listed
  (cash/UPI/bank_transfer/card); partial amount clamped to 0..total.
- `validate_positive` wired into settings/client/product/payment paths.
- Password min-length 8, duplicate phone/email checks, credit-limit enforced
  (was ignored despite column existing).
- QR generation deduped into `generate_qr_data()`; UTC via `now(timezone.utc)`;
  audit logging on every mutation (was login/export only).

## P1 — billing correctness
- Metal price parser accepts `price`/`rate`/`price_gram`/list payloads, clear
  fallback to manual → fallback constants (6000/80) with warning log.
- `calc_line_total()` single source: gold % of value, silver Rs/10g × weight;
  `general` items never attract making/GST. Zero-GST general invoice tested.
- GST: 3% iff any metal line, else 0% (same rule, now computed from flushed
  items instead of empty relationship).
- Backup: sqlite file-copy with instance-path resolution; Postgres returns a
  `pg_dump` warning instead of crashing. Added CSV invoice export,
  `/healthz`, date-filtered reports (`?days=`).

## P2 — scale & hygiene
- Pagination + search (`?q=&page=`) on clients/products/invoices (was `.all()`).
- `requirements.txt` pinned to real deps + Flask-WTF/pytest/gunicorn;
  `Dockerfile`, `init_db.py` (8-char admin `admin123456`), expanded `tests.py`
  (12 tests, all passing). Removed committed `__pycache__`, empty migrations
  cruft, stray `billing.db` from zip.

## Run
```
uv venv .venv && uv pip install --python .venv/Scripts/python.exe -r requirements.txt
cp .env.example .env   # set SECRET_KEY, DATABASE_URL
.venv/Scripts/python init_db.py
.venv/Scripts/python app.py   # or: gunicorn -w 4 -b 0.0.0.0:5000 app:app
.venv/Scripts/python tests.py
```
Default admin after `init_db.py`: `admin / admin123456` — change immediately.
