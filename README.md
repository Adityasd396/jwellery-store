# Gold & Silver Billing — improved build

Flask billing app for a jewellery shop. Integer-paise money model, GST on metal lines,
live metal rates, product/stock, client ledger, reports and audit log.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # set SECRET_KEY, DATABASE_URL, SHOP_TZ
python init_db.py             # creates tables + first admin (safe: refuses to wipe data)
python app.py                 # http://127.0.0.1:5000
```

Docker:

```bash
docker build -t billing .
docker run -p 5000:5000 -e SECRET_KEY=... -v billing_data:/app/instance billing
```

## Environment

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | dev default | **Required in production** — boot fails if left at the dev value |
| `DATABASE_URL` | `sqlite:///instance/billing.db` | Postgres works too |
| `SHOP_TZ` | `Asia/Kolkata` | All timestamps stored in shop-local time |
| `FLASK_ENV` | `production` | Set to `development` to allow the dev key |
| `TRUST_PROXY` | `1` | Set `0` if not behind a reverse proxy |
| `LOGIN_MAX` / `LOGIN_WINDOW` | `10` / `600` | Login throttling |

`.env` is loaded automatically (real env vars win).

## What changed in this build

* **Bug fixes** — cancel-then-delete no longer reverses stock twice; client credit limit is
  stored in paise, not rupees; timestamps are shop-local instead of UTC; cancelled bills can't
  collect payments; invoice numbers are monotonic (never reused after a delete).
* **Reports** — cancelled bills excluded from sales/profit; gross and net profit shown
  separately with margin %.
* **UI** — one design system in `templates/base.html`: sticky header, stat cards, badges,
  toasts, mobile nav, print-friendly bills. Cancel/Delete actions are now reachable from the
  bills, client and product pages with confirm guards.
* **Ops** — no more Flask-Migrate (unused), `.dockerignore`, non-root container, single gunicorn
  worker (SQLite), health check at `/healthz`, `init_db.py` refuses to wipe a live database.
* **Security** — production refuses the default `SECRET_KEY`, per-IP **and** per-account login
  throttling, CSRF on every POST.

GST rules and metal-rate pricing are untouched.

## Tests

```bash
python tests.py      # 55 tests, temp DB per run
```
