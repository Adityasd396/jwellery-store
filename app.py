"""Gold & Silver Billing Software (fixed).

Fixes applied over the original single-file app:
- auth on every mutating/view route, audit logging everywhere
- CSRF (Flask-WTF) with API exemptions, security headers, error handlers
- price cache with TTL (no per-homepage API storm / mass rewrites)
- atomic invoice/payment transactions, stock validation, safe deletes
- QR helper dedupe, pagination/search, CSV export, postgres-safe backup
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from functools import wraps
from itertools import zip_longest
from zoneinfo import ZoneInfo

import qrcode
from flask import (
    Flask, Response, flash, jsonify, make_response, redirect, render_template,
    request, session, url_for,
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from io import BytesIO
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("billing")

app = Flask(__name__)
app.config.from_object(Config)

if app.config["SECRET_KEY"] == "dev-only-change-me-in-production":
    if os.environ.get("FLASK_ENV", "").lower() == "production":
        # Sessions signed with a published key can be forged by anyone.
        raise RuntimeError(
            "SECRET_KEY is still the dev default. Generate one "
            "(python -c \"import secrets;print(secrets.token_hex(32))\") and "
            "set SECRET_KEY in the environment or .env before starting.")
    log.warning("SECRET_KEY is the dev default — never deploy with it!")

# Trust one proxy hop for X-Forwarded-* (set TRUST_PROXY=0 behind no proxy).
if os.environ.get("TRUST_PROXY", "1") not in ("0", "false", "False"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)


# Windows ships no IANA tz database, so ZoneInfo("Asia/Kolkata") can raise
# ZoneInfoNotFoundError out of the box. Install `tzdata` to fix it properly;
# these fixed offsets keep the shop's clock correct even without it.
# Only valid for zones with no DST — India, Gulf, Nepal, etc. all qualify.
_FALLBACK_TZ_HOURS = {
    "Asia/Kolkata": 5.5, "Asia/Calcutta": 5.5, "Asia/Colombo": 5.5,
    "Asia/Kathmandu": 5.75, "Asia/Katmandu": 5.75,
    "Asia/Dhaka": 6.0, "Asia/Dacca": 6.0, "Asia/Thimphu": 6.0,
    "Asia/Karachi": 5.0, "Asia/Yangon": 6.5, "Asia/Rangoon": 6.5,
    "Asia/Bangkok": 7.0, "Asia/Jakarta": 7.0, "Asia/Ho_Chi_Minh": 7.0,
    "Asia/Singapore": 8.0, "Asia/Kuala_Lumpur": 8.0, "Asia/Manila": 8.0,
    "Asia/Shanghai": 8.0, "Asia/Hong_Kong": 8.0, "Asia/Taipei": 8.0,
    "Asia/Tokyo": 9.0, "Asia/Seoul": 9.0, "Australia/Perth": 8.0,
    "Asia/Dubai": 4.0, "Asia/Muscat": 4.0, "Asia/Baku": 4.0,
    "Asia/Tbilisi": 4.0, "Asia/Yerevan": 4.0,
    "Asia/Tehran": 3.5, "Asia/Baghdad": 3.0, "Asia/Riyadh": 3.0,
    "Asia/Kuwait": 3.0, "Asia/Qatar": 3.0, "Asia/Jerusalem": 2.0,
    "Europe/Moscow": 3.0, "Europe/Istanbul": 3.0, "Europe/Kiev": 2.0,
    "Africa/Nairobi": 3.0, "Africa/Lagos": 1.0, "Africa/Cairo": 2.0,
    "Africa/Johannesburg": 2.0, "Pacific/Auckland": 12.0,
    "Pacific/Fiji": 12.0, "Pacific/Honolulu": -10.0,
    "America/New_York": -5.0, "America/Chicago": -6.0,
    "America/Denver": -7.0, "America/Los_Angeles": -8.0,
    "America/Sao_Paulo": -3.0, "America/Toronto": -5.0,
    "Europe/London": 0.0, "Europe/Dublin": 0.0, "Europe/Lisbon": 0.0,
    "UTC": 0.0, "Etc/UTC": 0.0,
}


def _shop_tz():
    name = os.environ.get("SHOP_TZ", "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception:
        pass
    # Second chance: tzdata may be installed but not yet imported/present on
    # the zoneinfo search path in some builds.
    try:
        import tzdata  # noqa: F401
        return ZoneInfo(name)
    except Exception:
        pass
    hours = _FALLBACK_TZ_HOURS.get(name)
    if hours is not None:
        log.info("using fixed UTC%+.2fh offset for %r (install `tzdata` "
                 "for full zone support)", hours, name)
        return timezone(timedelta(hours=hours), name)
    log.warning("unknown SHOP_TZ %r — falling back to UTC", name)
    return timezone.utc


SHOP_TZ = _shop_tz()


def utcnow() -> datetime:
    """Wall-clock time in the SHOP's timezone (default Asia/Kolkata).

    Business dates (bill date, day filters, GSTR-1 periods) must reflect the
    shop's clock, not UTC — otherwise an evening bill lands on yesterday.
    Stored naive on purpose: the whole schema is naive-datetime already.
    """
    return datetime.now(SHOP_TZ).replace(tzinfo=None)


now = utcnow  # readable alias


# ---------------------------------------------------------------- auth
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or not user.is_admin:
            flash("Admin access required.", "danger")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- models
class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    firm_name = db.Column(db.String(200), default="Your Firm Name")
    firm_address = db.Column(db.Text)
    firm_phone = db.Column(db.String(20))
    firm_email = db.Column(db.String(120))
    firm_gst = db.Column(db.String(20))
    gold_making_charge_percent = db.Column(db.Float, default=5.0)
    silver_making_charge_per_10gm = db.Column(db.Integer, default=15000)
    gst_enabled = db.Column(db.Boolean, default=True)
    show_making_charges = db.Column(db.Boolean, default=True)
    gold_manual_price = db.Column(db.Integer, default=0)  # paise per gram
    silver_manual_price = db.Column(db.Integer, default=0)  # paise per gram
    # Monotonic bill-number counter. Never derived from row ids: a deleted bill
    # would otherwise let the next one reuse INV-0007.
    invoice_seq = db.Column(db.Integer, default=0)
    # ---------------------------------------------------------------- GSTIN
    # Print the firm's GSTIN (and the customer's, when known) on every bill.
    show_gstin = db.Column(db.Boolean, default=True)
    # ------------------------------------------------- making-charge basis
    # Each metal can be charged two ways:
    #   percent -> a % of the line's metal value
    #   flat    -> a fixed rupee amount per gram (gold) / per 10 g (silver)
    gold_making_mode = db.Column(db.String(10), default="percent")
    gold_making_flat = db.Column(db.Integer, default=0)      # paise per gram
    silver_making_mode = db.Column(db.String(10), default="flat")
    silver_making_percent = db.Column(db.Float, default=0.0)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    last_login = db.Column(db.DateTime)

    def set_password(self, password: str) -> None:
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    action = db.Column(db.String(50), nullable=False)
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.Integer)
    changes = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=utcnow)
    ip_address = db.Column(db.String(45))
    user = db.relationship("User", backref="audit_logs")


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    address = db.Column(db.Text)
    gstin = db.Column(db.String(20))  # customer GSTIN, printed on B2B bills
    credit_limit = db.Column(db.Integer, default=0)  # paise
    total_purchases = db.Column(db.Integer, default=0)  # paise
    total_payments = db.Column(db.Integer, default=0)  # paise
    balance = db.Column(db.Integer, default=0)  # paise
    invoices = db.relationship("Invoice", backref="client", lazy=True)
    payments = db.relationship("Payment", backref="client", lazy=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    barcode = db.Column(db.String(100), unique=True)
    description = db.Column(db.Text)
    item_type = db.Column(db.String(20), nullable=False)  # gold | silver
    category = db.Column(db.String(80), default="")  # Ring, Handring, Chain…
    unit = db.Column(db.String(10), default="pcs")  # pcs | g (weighed items)
    unit_price = db.Column(db.Integer, nullable=False, default=0)  # paise
    custom_price = db.Column(db.Integer, default=0)  # paise override, 0 = auto
    cost_price = db.Column(db.Integer, default=0)  # paise / unit (profit)
    stock_quantity = db.Column(db.Integer, default=0)  # pieces (unit=pcs)
    stock_weight = db.Column(db.Float, default=0.0)  # grams (unit=g)
    weight_per_unit = db.Column(db.Float, nullable=False)  # required for metals
    created_at = db.Column(db.DateTime, default=utcnow)


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(20), unique=True, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    date_created = db.Column(db.DateTime, default=utcnow)
    date_due = db.Column(db.DateTime)
    status = db.Column(db.String(20), default="pending")
    subtotal = db.Column(db.Integer, default=0)  # paise
    gst_rate = db.Column(db.Float, default=0.0)  # GST rate in percentage
    gst_amount = db.Column(db.Integer, default=0)  # paise
    total = db.Column(db.Integer, default=0)  # paise
    # Metal paise/g rates frozen at billing time (displayed on the invoice)
    gold_rate = db.Column(db.Integer, default=0)
    silver_rate = db.Column(db.Integer, default=0)
    # Discount applied on subtotal, before GST
    discount_type = db.Column(db.String(20), default="none")  # none|percent|flat
    discount_value = db.Column(db.Float, default=0.0)  # % or Rs (display)
    discount_amount = db.Column(db.Integer, default=0)  # paise
    # Customer GSTIN frozen on the bill (client record may change later)
    client_gstin = db.Column(db.String(20))
    # Old-metal exchange taken against this bill
    exchange_metal = db.Column(db.String(20), default="none")  # none|gold|silver
    exchange_weight = db.Column(db.Float, default=0.0)
    exchange_rate = db.Column(db.Float, default=0.0)  # Rs/g display
    exchange_amount = db.Column(db.Integer, default=0)  # paise
    items = db.relationship("InvoiceItem", backref="invoice", lazy=True,
                            cascade="all, delete-orphan")
    invoice_payments = db.relationship("Payment", backref="invoice_payment", lazy=True)


class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    description = db.Column(db.String(200), nullable=False)
    item_type = db.Column(db.String(20), default="general")
    quantity = db.Column(db.Float, default=1.0)  # fractional OK (pearls etc.)
    weight = db.Column(db.Float, default=0.0)  # in grams
    # Basis the charge was raised on: percent (% of metal value) or flat
    # (paise per gram for gold, paise per 10 g for silver).
    making_mode = db.Column(db.String(10), default="percent")
    making_charges = db.Column(db.Float, default=0.0)  # % or per-weight paise
    unit_price = db.Column(db.Integer, nullable=False, default=0)  # paise
    unit_cost = db.Column(db.Integer, default=0)  # paise cost snapshot at sale
    line_total = db.Column(db.Integer, default=0)  # paise
    product = db.relationship("Product")


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"))
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"))
    amount = db.Column(db.Integer, nullable=False)  # paise
    payment_date = db.Column(db.DateTime, default=utcnow)
    payment_method = db.Column(db.String(50))  # cash, UPI, bank_transfer, card
    notes = db.Column(db.Text)


class TagTemplate(db.Model):
    """A jewellery tag design.

    scope='default'   -> used for every product that has no category design
    scope='category'  -> used only for products in `category`

    Sizes are stored in millimetres because thermal jewellery printers are
    58 mm / 80 mm machines; inches are still accepted on the form.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), default="Default tag")
    scope = db.Column(db.String(20), default="default")   # default | category
    category = db.Column(db.String(80), default="")
    width_mm = db.Column(db.Float, default=50.0)
    height_mm = db.Column(db.Float, default=25.0)
    style = db.Column(db.String(20), default="classic")   # see TAG_STYLES
    font_name = db.Column(db.Float, default=10.0)         # pt
    font_detail = db.Column(db.Float, default=8.0)        # pt
    show_name = db.Column(db.Boolean, default=True)
    show_sku = db.Column(db.Boolean, default=False)
    show_category = db.Column(db.Boolean, default=True)
    show_metal = db.Column(db.Boolean, default=True)
    show_weight = db.Column(db.Boolean, default=True)
    show_price = db.Column(db.Boolean, default=True)
    show_barcode = db.Column(db.Boolean, default=True)
    show_qr = db.Column(db.Boolean, default=False)
    show_firm = db.Column(db.Boolean, default=False)
    show_code = db.Column(db.Boolean, default=True)
    border = db.Column(db.String(10), default="solid")    # none|solid|double|dashed
    note = db.Column(db.String(80), default="")           # free footer line
    copies = db.Column(db.Integer, default=1)
    is_thermal = db.Column(db.Boolean, default=True)      # one label per page

    # ---- shape ---------------------------------------------------------
    # Two shapes only, matching the label rolls jewellers actually buy:
    #   "rectangle" — a plain rectangular label
    #   "tail"      — rectangle body + a narrow tail strip running out of the
    #                 right-hand edge, vertically positioned. This is the
    #                 standard rat-tail / dori tag: the tail is wrapped round
    #                 the piece or threaded with string.
    # width_mm is the BODY width for both; the tail is appended to the right,
    # so a 50 mm body with a 16 mm tail prints on 66 mm of paper.
    shape = db.Column(db.String(10), default="rectangle")   # rectangle | tail
    tail_mm = db.Column(db.Float, default=16.0)             # tail length
    tail_h_mm = db.Column(db.Float, default=0.0)            # tail height, 0=auto
    tail_pos = db.Column(db.String(10), default="middle")   # top|middle|bottom
    tail_hole_mm = db.Column(db.Float, default=0.0)         # 0 = no punched hole
    # The full length of the label, body + tail — the number printed on the
    # roll of labels the shop actually buys ("70 mm tags"). When it is set the
    # tail stops being its own decision: it becomes whatever is left after the
    # body, so changing the body length keeps the tag on its 70 mm label.
    # 0 means "not fixed — size the tag from body + tail instead".
    total_mm = db.Column(db.Float, default=0.0)

    # ---- spacing & element sizes ---------------------------------------
    # Everything a shop needs to make a tag fit its own label stock. Lengths
    # are mm, type is pt. A 0 on qr_mm / bc_h_mm means "auto" — scale it from
    # the tag height, which is what the built-in designs rely on.
    pad_mm = db.Column(db.Float, default=1.4)      # padding, top + bottom
    pad_h_mm = db.Column(db.Float, default=2.0)    # padding, left + right
    gap_mm = db.Column(db.Float, default=0.5)      # gap between text lines
    qr_mm = db.Column(db.Float, default=0.0)       # QR side, 0 = auto
    bc_h_mm = db.Column(db.Float, default=0.0)     # bar height, 0 = auto
    bc_w_pct = db.Column(db.Float, default=100.0)  # bar width, % of the column


class DeletedInvoice(db.Model):
    """Permanent archive of a bill that was deleted.

    The Invoice row is really gone (numbers must stay monotonic and the books
    must balance), so everything needed to answer "what was on that bill?"
    is copied here, including a full JSON snapshot of its line items.
    """
    id = db.Column(db.Integer, primary_key=True)
    original_id = db.Column(db.Integer)
    invoice_number = db.Column(db.String(20))
    client_id = db.Column(db.Integer)
    client_name = db.Column(db.String(100))
    client_phone = db.Column(db.String(20))
    client_gstin = db.Column(db.String(20))
    date_created = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime, default=utcnow)
    deleted_by = db.Column(db.String(80))
    deleted_by_id = db.Column(db.Integer)
    reason = db.Column(db.Text)
    status = db.Column(db.String(20))
    subtotal = db.Column(db.Integer, default=0)  # paise
    discount_amount = db.Column(db.Integer, default=0)
    gst_rate = db.Column(db.Float, default=0.0)
    gst_amount = db.Column(db.Integer, default=0)
    exchange_amount = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, default=0)
    paid_amount = db.Column(db.Integer, default=0)
    item_count = db.Column(db.Integer, default=0)
    stock_reversed = db.Column(db.Boolean, default=True)
    payload = db.Column(db.Text)  # full JSON snapshot (items + payments)
    ip_address = db.Column(db.String(45))


def log_audit(action, entity_type=None, entity_id=None, changes=None):
    if "user_id" in session:
        try:
            db.session.add(AuditLog(
                user_id=session["user_id"], action=action,
                entity_type=entity_type, entity_id=entity_id,
                changes=changes,
                ip_address=request.remote_addr if request else None,
            ))
        except Exception as exc:  # audit must never break the request
            log.warning("audit failed: %s", exc)


# ---------------------------------------------------------------- helpers
def generate_qr_data(text: str):
    """Single QR helper (was duplicated in two routes). Returns data-URI or None."""
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        log.warning("QR failed: %s", exc)
        return None


def generate_barcode_data(code: str):
    """Code128 bars as data-URI PNG. Returns None if lib/code missing."""
    if not code:
        return None
    try:
        from barcode import Code128
        from barcode.writer import ImageWriter
        buf = BytesIO()
        Code128(str(code), writer=ImageWriter()).write(
            buf, {"module_width": 0.25, "module_height": 12.0,
                  "font_size": 0, "quiet_zone": 2.0,
                  "background": "white", "foreground": "black"})
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        log.warning("barcode render failed for %r: %s", code, exc)
        return None


def get_settings() -> Settings:
    """Single settings row, created on demand. Prices are manual-only (no API)."""
    settings = Settings.query.first()
    if not settings:
        settings = Settings()
        db.session.add(settings)
        db.session.commit()
    return settings


@app.context_processor
def _template_helpers():
    """Firm name in the header and a clock for the footer, on every page."""
    try:
        s = Settings.query.first()
    except Exception:  # DB not ready yet (first boot, error page) — skip
        s = None
    return {"shop_settings": s, "now": utcnow}


def metal_prices(settings: Settings | None = None):
    """Current paise/g dict for templates (same shape as before)."""
    settings = settings or get_settings()
    now = utcnow()
    return {
        "gold": {"price_per_gram": settings.gold_manual_price or 0,
                 "last_updated": now},
        "silver": {"price_per_gram": settings.silver_manual_price or 0,
                   "last_updated": now},
    }


def effective_metal_price(item_type: str, settings: Settings) -> int:
    """Paise per gram."""
    if item_type == "gold":
        return settings.gold_manual_price or 0
    if item_type == "silver":
        return settings.silver_manual_price or 0
    return 0


def update_all_product_prices(settings: Settings) -> int:
    """Instantly reprice EVERY auto-priced product from the settings paise/g
    rates. Custom-price products are left untouched.
    Returns number of products updated. Past invoices are untouched."""
    n = 0
    for p in Product.query.all():
        if p.item_type in ("gold", "silver") and not (p.custom_price or 0):
            p.unit_price = int(round((p.weight_per_unit or 0)
                                     * effective_metal_price(
                                         p.item_type, settings)))
            n += 1
    return n


def metal_unit_paise(item_type: str, grams_per_unit: float,
                     settings: Settings) -> int:
    """Per-piece metal value in paise = weight (g) x metal rate (paise/g)."""
    return int(round((grams_per_unit or 0)
                     * effective_metal_price(item_type, settings)))


def making_basis(item_type: str, settings: Settings, mode: str | None = None) -> str:
    """How this metal is charged: 'percent' (% of metal value) or 'flat'
    (rupees per gram for gold, rupees per 10 g for silver)."""
    mode = (mode or "").strip().lower()
    if mode in ("percent", "flat"):
        return mode
    if item_type == "gold":
        return (settings.gold_making_mode or "percent").strip().lower()
    if item_type == "silver":
        return (settings.silver_making_mode or "flat").strip().lower()
    return "percent"


def default_making(item_type: str, settings: Settings,
                   mode: str | None = None) -> float:
    """Shop default making charge, in the unit the mode uses (RUPEES).

    percent -> % of metal value · gold flat -> Rs/g · silver flat -> Rs/10g.
    """
    mode = making_basis(item_type, settings, mode)
    if item_type == "gold":
        if mode == "flat":
            return (settings.gold_making_flat or 0) / 100
        return float(settings.gold_making_charge_percent or 0)
    if item_type == "silver":
        if mode == "percent":
            return float(settings.silver_making_percent or 0)
        return (settings.silver_making_charge_per_10gm or 0) / 100
    return 0.0


def calc_line_total(qty: float, unit_paise: int, item_type: str,
                    grams: float, making, settings: Settings,
                    mode: str | None = None):
    """All money in paise. Returns (line_total_paise, making_used, mode).

    Metal lines are: (weight x qty) x metal rate + making charges — no
    separate "unit price" is taken from the browser.
    `grams` is the *total* grams on the line (per-piece weight x qty).

    `making` arrives in the unit its mode speaks:
      percent     -> % of the metal value
      flat (gold) -> rupees per gram
      flat (silver) -> rupees per 10 g
    """
    base = int(round(qty * (unit_paise or 0)))
    if item_type not in ("gold", "silver"):
        return base, 0, "none"
    mode = making_basis(item_type, settings, mode)
    if mode == "flat":
        rate = float(making or 0) if making else default_making(
            item_type, settings, mode)
        # gold: Rs/g · silver: Rs/10g -> both end up Rs per gram
        per_gram_paise = rate * 100 if item_type == "gold" else rate * 100 / 10
        making_total = int(round(per_gram_paise * (grams or 0)))
    else:
        pct = float(making or 0) if making else default_making(
            item_type, settings, mode)
        making_total = int(round(base * (pct / 100)))
    return base + making_total, (making or default_making(item_type, settings, mode)), mode


def validate_rs(value, field_name: str) -> int:
    """Rupees input → integer paise. Rejects non-numeric/negative."""
    try:
        f = float(value or 0)
    except (ValueError, TypeError):
        raise ValueError(f"{field_name} must be a valid number")
    if f < 0:
        raise ValueError(f"{field_name} must be positive")
    return int(round(f * 100))


def rs(value) -> int:
    """Loose rupees → paise (for internal defaults)."""
    try:
        return int(round(float(value or 0) * 100))
    except (ValueError, TypeError):
        return 0


@app.template_filter("inr")
def inr_filter(paise) -> str:
    """Integer paise → 'Rs' display string. Keeps old %.2f look, no commas."""
    try:
        v = (paise or 0) / 100
    except TypeError:
        v = 0
    return f"{v:.2f}"


@app.template_filter("inr0")
def inr0_filter(paise) -> str:
    """Integer paise → whole-rupee display string (old %.0f look)."""
    try:
        return str(int(round((paise or 0) / 100)))
    except TypeError:
        return "0"


@app.template_filter("makingtext")
def makingtext_filter(item) -> str:
    """Render a bill line's making charge the way it was actually charged."""
    if item is None or item.item_type not in ("gold", "silver"):
        return "—"
    val = float(item.making_charges or 0)
    if not val:
        return "—"
    if (item.making_mode or "").strip().lower() == "flat":
        return f"₹{val:g}/g" if item.item_type == "gold" else f"₹{val:g}/10g"
    return f"{val:g}%"


def making_text(item) -> str:
    """Non-template twin of the `makingtext` filter (used by the PDF)."""
    return makingtext_filter(item)


def validate_positive(value, field_name: str) -> float:
    """Legacy float validator — kept for non-money fields (weights, %)."""
    try:
        val = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field_name} must be a valid number")
    if val < 0:
        raise ValueError(f"{field_name} must be positive")
    return val


def invoice_paid_total(invoice_id: int) -> int:
    return db.session.query(db.func.sum(Payment.amount)).filter_by(
        invoice_id=invoice_id).scalar() or 0


def invoice_due_total(invoice_id: int) -> float:
    inv = Invoice.query.get(invoice_id)
    return (inv.total - invoice_paid_total(invoice_id)) if inv else 0


def invoices_with_due(client_id: int):
    """All non-cancelled invoices that still owe money, oldest first.

    (Status alone can't be trusted — an invoice can be marked paid while a
    balance remains, e.g. status/partial mismatch at creation.)
    """
    out = []
    for inv in Invoice.query.filter_by(client_id=client_id).order_by(
            Invoice.date_created).all():
        if inv.status == "cancelled":
            continue
        due = inv.total - invoice_paid_total(inv.id)
        if due > 0:
            inv.paid_amount = inv.total - due
            inv.remaining_amount = due
            out.append(inv)
    return out


def snapshot_invoice(inv: Invoice) -> str:
    """JSON copy of a bill's lines and payments.

    Stored on DeletedInvoice so a deleted bill can still be answered for:
    the Invoice row itself is really removed (bill numbers must stay
    monotonic and the books must balance).
    """
    return json.dumps({
        "invoice_number": inv.invoice_number,
        "client": {"id": inv.client_id,
                   "name": inv.client.name if inv.client else None,
                   "phone": inv.client.phone if inv.client else None,
                   "gstin": inv.client_gstin},
        "date_created": (inv.date_created.isoformat()
                         if inv.date_created else None),
        "status": inv.status,
        "subtotal": (inv.subtotal or 0) / 100,
        "discount_type": inv.discount_type,
        "discount_value": inv.discount_value,
        "discount_amount": (inv.discount_amount or 0) / 100,
        "gst_rate": inv.gst_rate,
        "gst_amount": (inv.gst_amount or 0) / 100,
        "exchange_metal": inv.exchange_metal,
        "exchange_weight": inv.exchange_weight,
        "exchange_rate": inv.exchange_rate,
        "exchange_amount": (inv.exchange_amount or 0) / 100,
        "total": (inv.total or 0) / 100,
        "gold_rate": (inv.gold_rate or 0) / 100,
        "silver_rate": (inv.silver_rate or 0) / 100,
        "items": [{"description": it.description, "item_type": it.item_type,
                   "quantity": it.quantity, "weight": it.weight,
                   "making_mode": it.making_mode,
                   "making_charges": it.making_charges,
                   "unit_price": (it.unit_price or 0) / 100,
                   "unit_cost": (it.unit_cost or 0) / 100,
                   "line_total": (it.line_total or 0) / 100,
                   "product_id": it.product_id} for it in inv.items],
        "payments": [{"amount": (p.amount or 0) / 100,
                      "method": p.payment_method,
                      "date": (p.payment_date.isoformat()
                               if p.payment_date else None),
                      "notes": p.notes}
                     for p in Payment.query.filter_by(invoice_id=inv.id).all()],
    })


def archive_deleted_invoice(inv: Invoice, reason: str = "",
                            stock_reversed: bool = True,
                            paid: int | None = None) -> DeletedInvoice:
    """Copy a bill into the permanent delete archive. Call BEFORE deleting."""
    if paid is None:
        paid = invoice_paid_total(inv.id)
    try:
        payload = snapshot_invoice(inv)
    except Exception as exc:  # never let archiving block a delete
        log.warning("snapshot failed for %s: %s", inv.invoice_number, exc)
        payload = None
    user = (User.query.get(session["user_id"])
            if "user_id" in session else None)
    row = DeletedInvoice(
        original_id=inv.id, invoice_number=inv.invoice_number,
        client_id=inv.client_id,
        client_name=inv.client.name if inv.client else "",
        client_phone=inv.client.phone if inv.client else "",
        client_gstin=inv.client_gstin,
        date_created=inv.date_created, status=inv.status,
        deleted_by=(user.username if user
                    else (session.get("username") or "unknown")),
        deleted_by_id=session.get("user_id"),
        reason=(reason or "").strip(),
        subtotal=inv.subtotal or 0,
        discount_amount=inv.discount_amount or 0,
        gst_rate=inv.gst_rate or 0,
        gst_amount=inv.gst_amount or 0,
        exchange_amount=inv.exchange_amount or 0,
        total=inv.total or 0, paid_amount=paid,
        item_count=len(inv.items),
        stock_reversed=stock_reversed,
        payload=payload,
        ip_address=request.remote_addr if request else None,
    )
    db.session.add(row)
    return row


def allocate_invoice_number(settings: Settings | None = None) -> str:
    """Monotonic INV-#### taken from a persisted counter.

    Row ids are NOT used: SQLite hands max(id)+1 back out after the newest
    bill is deleted, which would reissue a number that already existed in the
    books. Existing databases are seeded from the highest id so numbering
    continues where it left off.
    """
    row = db.session.query(Settings).with_for_update().order_by(
        Settings.id).first()
    if row is None:
        row = get_settings()
    if not (row.invoice_seq or 0):
        last = (Invoice.query.filter(~Invoice.invoice_number.like("TMP-%"))
                .order_by(Invoice.id.desc()).first())
        row.invoice_seq = last.id if last else 0
    row.invoice_seq = (row.invoice_seq or 0) + 1
    db.session.flush()
    return f"INV-{row.invoice_seq:04d}"


def calc_discount(subtotal_paise: int, dtype: str, dvalue):
    """Paise in/out. Percent clamps 0..100, flat clamps 0..subtotal."""
    dtype = (dtype or "none").strip().lower()
    try:
        dvalue = float(dvalue or 0)
    except (ValueError, TypeError):
        raise ValueError("Discount must be a number")
    if dtype not in ("none", "percent", "flat"):
        raise ValueError("Discount type must be none, percent or flat")
    if dtype == "percent":
        if dvalue < 0 or dvalue > 100:
            raise ValueError("Discount % must be 0..100")
        return int(round(subtotal_paise * dvalue / 100)), dtype, dvalue
    if dtype == "flat":
        flat = int(round(dvalue * 100))
        if flat < 0 or flat > subtotal_paise:
            raise ValueError("Flat discount must be 0..subtotal")
        return flat, dtype, dvalue
    return 0, "none", 0.0


def calc_exchange(metal: str, amount=None, gross_paise: int = 0,
                  weight=None, rate=None):
    """Old-metal exchange as a DIRECT rupee amount (like flat discount).
    `amount` wins; legacy weight×rate is accepted as fallback.
    Rejects exchange bigger than the bill so totals never go negative."""
    metal = (metal or "none").strip().lower()
    if metal not in ("none", "gold", "silver"):
        raise ValueError("Exchange metal must be none, gold or silver")
    try:
        amount_f = float(amount or 0)
        weight = float(weight or 0)
        rate = float(rate or 0)
    except (ValueError, TypeError):
        raise ValueError("Exchange values must be numbers")
    if metal == "none" or (amount_f <= 0 and weight <= 0):
        return 0, "none", 0.0, 0.0
    amount = int(round(amount_f * 100)) if amount_f > 0 else 0
    if not amount:
        if weight < 0 or rate <= 0:
            raise ValueError("Exchange needs an amount or weight × rate")
        amount = int(round(weight * rate * 100))
    if amount > gross_paise:
        raise ValueError(
            f"Exchange Rs.{amount / 100:.2f} exceeds bill Rs.{gross_paise / 100:.2f}")
    return amount, metal, weight, rate


_SCHEMA_OK = False


def ensure_schema():
    """Add newer Invoice columns to pre-existing DBs (create_all only
    handles fresh ones). Safe to call repeatedly."""
    global _SCHEMA_OK
    if _SCHEMA_OK:
        return
    from sqlalchemy import inspect as sa_inspect
    # Brand-new tables (tag_template, deleted_invoice, …) are not covered by
    # the ALTER/rebuild logic below — create_all only adds what is missing.
    try:
        db.create_all()
    except Exception as exc:  # pragma: no cover - engine not ready yet
        log.warning("schema: create_all skipped: %s", exc)
    insp = sa_inspect(db.engine)
    wanted_tables = {
        "invoice": {
            "gold_rate": "INTEGER DEFAULT 0",
            "silver_rate": "INTEGER DEFAULT 0",
            "discount_type": "VARCHAR(20) DEFAULT 'none'",
            "discount_value": "FLOAT DEFAULT 0",
            "discount_amount": "INTEGER DEFAULT 0",
            "exchange_metal": "VARCHAR(20) DEFAULT 'none'",
            "exchange_weight": "FLOAT DEFAULT 0",
            "exchange_rate": "FLOAT DEFAULT 0",
            "exchange_amount": "INTEGER DEFAULT 0",
            "client_gstin": "VARCHAR(20)",
        },
        "settings": {
            "gst_enabled": "BOOLEAN DEFAULT 1",
            "show_making_charges": "BOOLEAN DEFAULT 1",
            "invoice_seq": "INTEGER DEFAULT 0",
            "show_gstin": "BOOLEAN DEFAULT 1",
            "gold_making_mode": "VARCHAR(10) DEFAULT 'percent'",
            "gold_making_flat": "INTEGER DEFAULT 0",
            "silver_making_mode": "VARCHAR(10) DEFAULT 'flat'",
            "silver_making_percent": "FLOAT DEFAULT 0",
        },
        "product": {
            "category": "VARCHAR(80) DEFAULT ''",
            "unit": "VARCHAR(10) DEFAULT 'pcs'",
            "custom_price": "INTEGER DEFAULT 0",
            "cost_price": "INTEGER DEFAULT 0",
            "stock_weight": "FLOAT DEFAULT 0",
        },
        "client": {
            "gstin": "VARCHAR(20)",
        },
        "invoice_item": {
            "unit_cost": "INTEGER DEFAULT 0",
            "making_mode": "VARCHAR(10) DEFAULT 'percent'",
        },
        "tag_template": {
            # Deliberately NO DEFAULT on the new columns. ADD COLUMN ... DEFAULT
            # fills every existing row immediately, which would destroy the
            # one piece of information the migration below needs — that a row
            # predates the column, and may hold an old hangtag strap length to
            # carry across. Left NULL here, filled in explicitly afterwards.
            "shape": "VARCHAR(10) DEFAULT 'rectangle'",
            "tail_mm": "FLOAT",
            "tail_h_mm": "FLOAT",
            "tail_pos": "VARCHAR(10)",
            "tail_hole_mm": "FLOAT",
            "total_mm": "FLOAT",
            "pad_mm": "FLOAT",
            "pad_h_mm": "FLOAT",
            "gap_mm": "FLOAT",
            "qr_mm": "FLOAT",
            "bc_h_mm": "FLOAT",
            "bc_w_pct": "FLOAT",
        },
    }
    # money columns that must end up INTEGER paise (old FLOAT rupees ×100)
    paise_tables = {
        "settings": ("Settings", ["gold_manual_price", "silver_manual_price",
                                  "silver_making_charge_per_10gm"]),
        "product": ("Product", ["unit_price", "custom_price", "cost_price"]),
        "client": ("Client", ["credit_limit", "total_purchases",
                              "total_payments", "balance"]),
        "invoice": ("Invoice", ["subtotal", "gst_amount", "total", "gold_rate",
                                "silver_rate", "discount_amount",
                                "exchange_amount"]),
        "invoice_item": ("InvoiceItem", ["unit_price", "line_total"]),
        "payment": ("Payment", ["amount"]),
    }
    dead_columns = {
        "settings": ["gold_api_url", "silver_api_url", "gold_fallback_price",
                     "silver_fallback_price", "use_manual_prices"],
    }
    with db.engine.begin() as conn:
        rebuilt = set()
        for table, (model_name, paise_cols) in paise_tables.items():
            model = globals()[model_name]
            try:
                existing = {c["name"]: c for c in insp.get_columns(table)}
            except Exception:
                continue  # table not created yet; create_all handles it
            needs_rebuild = any(
                col in existing and "INTEGER" not in str(
                    type(existing[col]["type"])).upper()
                for col in paise_cols if col in existing)
            # fresh tables already match the models; old ones get rebuilt
            has_all_model_cols = all(
                c.name in existing for c in model.__table__.columns)
            if needs_rebuild or not has_all_model_cols:
                _rebuild_table(conn, model, existing, paise_cols)
                rebuilt.add(table)
        # plain ADD COLUMN for anything still missing (excl. placeholder).
        # NOTE: rebuilt tables come straight from model metadata, so they
        # are complete — skip them (reflection caches go stale across DDL).
        insp2 = sa_inspect(db.engine)
        for table, wanted in wanted_tables.items():
            if table in rebuilt:
                continue
            try:
                existing = {c["name"] for c in insp2.get_columns(table)}
            except Exception:
                continue
            for col, ddl in wanted.items():
                if ddl is None or col in existing:
                    continue
                conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                log.info("schema: added %s.%s", table, col)
        # drop dead API-era columns (best effort)
        insp3 = sa_inspect(db.engine)
        for table, cols in dead_columns.items():
            try:
                existing = {c["name"] for c in insp3.get_columns(table)}
            except Exception:
                continue
            for col in cols:
                if col in existing:
                    try:
                        conn.execute(db.text(
                            f"ALTER TABLE {table} DROP COLUMN {col}"))
                        log.info("schema: dropped %s.%s", table, col)
                    except Exception as exc:
                        log.warning("schema: cannot drop %s.%s: %s",
                                    table, col, exc)

        # ---- tag designs: retire the "hangtag" fold-over shape ----------
        # It is replaced by "tail" (rectangle + rat-tail strip). Carry the old
        # strap length / hole diameter across so a saved design keeps its
        # proportions instead of silently jumping back to the defaults.
        try:
            tag_cols = {c["name"] for c in
                        sa_inspect(db.engine).get_columns("tag_template")}
        except Exception:
            tag_cols = set()
        if "shape" in tag_cols:
            try:
                # 1. the retired fold-over shape becomes the rat-tail shape
                conn.execute(db.text(
                    "UPDATE tag_template SET shape = 'tail' "
                    "WHERE LOWER(COALESCE(shape, '')) IN "
                    "('hangtag', 'strap', 'rat-tail', 'rat_tail')"))
                # 2. carry the old strap length / hole across BEFORE the
                #    defaults are filled in, so a saved design keeps its
                #    proportions instead of jumping to 16 mm and no hole.
                for old, new in (("hangtag_strap_mm", "tail_mm"),
                                 ("hangtag_hole_mm", "tail_hole_mm")):
                    if old in tag_cols:
                        conn.execute(db.text(
                            f"UPDATE tag_template SET {new} = {old} "
                            f"WHERE {new} IS NULL AND {old} IS NOT NULL"))
                # 3. anything still unset gets the model's default
                for col, default in (("tail_mm", 16), ("tail_h_mm", 0),
                                     ("tail_hole_mm", 0), ("pad_mm", 1.4),
                                     ("pad_h_mm", 2), ("gap_mm", 0.5),
                                     ("qr_mm", 0), ("bc_h_mm", 0),
                                     ("bc_w_pct", 100), ("total_mm", 0)):
                    conn.execute(db.text(
                        f"UPDATE tag_template SET {col} = {default} "
                        f"WHERE {col} IS NULL"))
                conn.execute(db.text(
                    "UPDATE tag_template SET tail_pos = 'middle' "
                    "WHERE tail_pos IS NULL "
                    "OR tail_pos NOT IN ('top', 'middle', 'bottom')"))
                # The old hangtag always drew a hole; a tail does not have to,
                # so a sub-millimetre value means "no hole", not a 0.5 mm hole.
                conn.execute(db.text(
                    "UPDATE tag_template SET tail_hole_mm = 0 "
                    "WHERE tail_hole_mm IS NOT NULL AND tail_hole_mm <= 1"))
                conn.execute(db.text(
                    "UPDATE tag_template SET shape = 'rectangle' "
                    "WHERE shape IS NULL "
                    "OR shape NOT IN ('rectangle', 'tail')"))
            except Exception as exc:
                log.warning("schema: tag shape migration skipped: %s", exc)

    _SCHEMA_OK = True


def _rebuild_table(conn, model, existing, paise_cols):
    """Recreate `model`'s table from metadata. Only columns still stored as
    FLOAT rupees are converted (ROUND(x*100)); already-INTEGER columns pass
    through untouched. Data preserved."""
    table = model.__table__.name
    model_cols = [c.name for c in model.__table__.columns]
    old = [c for c in existing if c in model_cols]
    convert = {c for c in paise_cols
               if c in existing and "INTEGER" not in str(
                   type(existing[c]["type"])).upper()}
    select = ", ".join(
        f"CAST(ROUND({c} * 100) AS INTEGER)" if c in convert else c
        for c in old)
    # crash-safe: a previous interrupted migration may have left {table}_old
    conn.execute(db.text(f"DROP TABLE IF EXISTS {table}_old"))
    conn.execute(db.text(f"ALTER TABLE {table} RENAME TO {table}_old"))
    try:
        model.__table__.create(bind=conn)
        conn.execute(db.text(
            f"INSERT INTO {table} ({', '.join(old)}) "
            f"SELECT {select} FROM {table}_old"))
        # New columns aren't in the INSERT: ORM Python-side defaults don't
        # apply to raw SQL, so backfill scalar ones explicitly (else NULLs).
        # Callable defaults (e.g. timestamps) are left for the ORM.
        for c in model.__table__.columns:
            if c.name not in old and c.default is not None \
                    and isinstance(c.default.arg, (int, float, str, bool)):
                arg = c.default.arg
                lit = f"'{arg}'" if isinstance(arg, str) else str(int(arg))
                conn.execute(db.text(
                    f"UPDATE {table} SET {c.name} = {lit} "
                    f"WHERE {c.name} IS NULL"))
        conn.execute(db.text(f"DROP TABLE {table}_old"))
        log.info("schema: rebuilt %s to paise", table)
    except Exception:
        # undo: drop the half-built table, restore the original
        conn.execute(db.text(f"DROP TABLE IF EXISTS {table}"))
        conn.execute(db.text(f"ALTER TABLE {table}_old RENAME TO {table}"))
        raise


@app.before_request
def _ensure_schema_once():
    try:
        ensure_schema()
    except Exception as exc:
        log.warning("schema check skipped: %s", exc)


def paginate(query, page: int, per_page: int = 25):
    page = max(1, page or 1)
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return items, {"page": page, "per_page": per_page, "total": total,
                   "pages": max(1, (total + per_page - 1) // per_page)}


# ---------------------------------------------------------------- security plumbing
@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


@app.teardown_appcontext
def _teardown(exc):
    if exc:
        db.session.rollback()


@app.errorhandler(404)
def _404(e):
    try:
        return render_template("404.html"), 404
    except Exception:
        return "Not found", 404


@app.errorhandler(500)
def _500(e):
    db.session.rollback()
    log.exception("500: %s", e)
    try:
        return render_template("500.html"), 500
    except Exception:
        return "Internal error — administrators: check logs.", 500


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "time": utcnow().isoformat()})


csrf.exempt(healthz)


# ---------------------------------------------------------------- auth routes
_LOGIN_ATTEMPTS: dict = {}
_LOGIN_WINDOW = int(os.environ.get("LOGIN_WINDOW_SECONDS", 900))
_LOGIN_MAX = int(os.environ.get("LOGIN_MAX_ATTEMPTS", 10))


def _throttle_key(username: str = "") -> str:
    """Per-IP and per-IP+account keys, so one attacker can't lock out the
    whole shop (shared IP) and a rotated username can't dodge the IP bucket."""
    ip = request.remote_addr or "unknown"
    return f"{ip}|{(username or '').strip().lower()}"


def _attempts_left(key, now, limit: int | None = None) -> int:
    limit = _LOGIN_MAX if limit is None else limit
    fresh = [t for t in _LOGIN_ATTEMPTS.get(key, [])
             if (now - t).total_seconds() < _LOGIN_WINDOW]
    if fresh:
        _LOGIN_ATTEMPTS[key] = fresh
    elif key in _LOGIN_ATTEMPTS:
        _LOGIN_ATTEMPTS.pop(key, None)  # window expired: don't leak keys
    return limit - len(fresh)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        ip_key = _throttle_key()
        acct_key = _throttle_key(username)
        now = utcnow()
        # IP bucket is wider so username-guessing still gets slowed
        if (_attempts_left(ip_key, now, _LOGIN_MAX * 3) <= 0
                or _attempts_left(acct_key, now) <= 0):
            flash("Too many attempts — try again in 15 minutes.", "danger")
            return render_template("login.html")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(request.form.get("password", "")):
            _LOGIN_ATTEMPTS.pop(ip_key, None)
            _LOGIN_ATTEMPTS.pop(acct_key, None)
            session.clear()  # rotate: no fixation from a pre-login session
            session["user_id"] = user.id
            session["username"] = user.username
            session["is_admin"] = user.is_admin
            user.last_login = utcnow()
            db.session.commit()
            log_audit("login")
            db.session.commit()
            flash("Login successful!", "success")
            return redirect(url_for("index"))
        for k in (ip_key, acct_key):
            _LOGIN_ATTEMPTS.setdefault(k, []).append(now)
        flash("Invalid username or password", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    if "user_id" in session:
        log_audit("logout")
        db.session.commit()
    session.clear()
    flash("You have been logged out", "info")
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
@admin_required
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        is_admin = request.form.get("is_admin") == "on"
        if not username or not email or not password:
            flash("All fields are required", "danger")
            return redirect(url_for("register"))
        if len(password) < 8:
            flash("Password must be at least 8 characters", "danger")
            return redirect(url_for("register"))
        if User.query.filter_by(username=username).first():
            flash("Username already exists", "danger")
            return redirect(url_for("register"))
        if User.query.filter_by(email=email).first():
            flash("Email already exists", "danger")
            return redirect(url_for("register"))
        user = User(username=username, email=email, is_admin=is_admin)
        user.set_password(password)
        db.session.add(user)
        log_audit("create", "User", None, f"Created user: {username}")
        db.session.commit()
        flash("User created successfully!", "success")
        return redirect(url_for("users"))
    return render_template("register.html")


@app.route("/users")
@admin_required
def users():
    return render_template("users.html", users=User.query.all())


@app.route("/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    pw = request.form.get("new_password") or ""
    if len(pw) < 8:
        flash("New password must be at least 8 characters.", "danger")
        return redirect(url_for("users"))
    user.set_password(pw)
    log_audit("update", "User", user.id, f"Password reset for {user.username}")
    db.session.commit()
    flash(f"Password reset for {user.username}.", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == session.get("user_id"):
        flash("Cannot delete your own account", "danger")
        return redirect(url_for("users"))
    # keep the audit trail readable instead of dangling
    AuditLog.query.filter_by(user_id=user.id).update({"user_id": None})
    db.session.delete(user)
    log_audit("delete", "User", user_id, f"Deleted user: {user.username}")
    db.session.commit()
    flash(f"User {user.username} deleted successfully!", "success")
    return redirect(url_for("users"))


@app.route("/audit-logs")
@admin_required
def audit_logs():
    q = (request.args.get("q") or "").strip()
    query = AuditLog.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(AuditLog.action.ilike(like),
                                     AuditLog.entity_type.ilike(like),
                                     AuditLog.changes.ilike(like)))
    query = query.order_by(AuditLog.timestamp.desc())
    items, pg = paginate(query, request.args.get("page", type=int) or 1,
                         per_page=50)
    return render_template("audit_logs.html", logs=items, pagination=pg, q=q)


def _export_payload():
    paise = lambda v: (v or 0) / 100
    return {
        "export_date": utcnow().isoformat(),
        "currency": "INR",
        "clients": [{"id": c.id, "name": c.name, "phone": c.phone, "email": c.email,
                     "address": c.address, "total_purchases": paise(c.total_purchases),
                     "total_payments": paise(c.total_payments), "balance": paise(c.balance)}
                    for c in Client.query.all()],
        "products": [{"id": p.id, "name": p.name, "sku": p.sku, "barcode": p.barcode,
                      "item_type": p.item_type, "unit_price": paise(p.unit_price),
                      "stock_quantity": p.stock_quantity, "weight_per_unit": p.weight_per_unit}
                     for p in Product.query.all()],
        "invoices": [{"id": i.id, "invoice_number": i.invoice_number, "client_id": i.client_id,
                      "date_created": i.date_created.isoformat(),
                      "date_due": i.date_due.isoformat() if i.date_due else None,
                      "status": i.status, "subtotal": paise(i.subtotal),
                      "discount_type": i.discount_type,
                      "discount_value": i.discount_value,
                      "discount_amount": paise(i.discount_amount),
                      "exchange_metal": i.exchange_metal,
                      "exchange_weight": i.exchange_weight,
                      "exchange_rate": i.exchange_rate,
                      "exchange_amount": paise(i.exchange_amount),
                      "gold_rate": paise(i.gold_rate), "silver_rate": paise(i.silver_rate),
                      "gst_rate": i.gst_rate,
                      "gst_amount": paise(i.gst_amount), "total": paise(i.total)}
                     for i in Invoice.query.all()],
        "payments": [{"id": p.id, "invoice_id": p.invoice_id, "client_id": p.client_id,
                      "amount": paise(p.amount),
                      "payment_date": p.payment_date.isoformat() if p.payment_date else None,
                      "payment_method": p.payment_method, "notes": p.notes}
                     for p in Payment.query.all()],
    }


@app.route("/export/data")
@admin_required
def export_data():
    log_audit("export", "Database", None, "Exported all data (JSON)")
    db.session.commit()
    resp = jsonify(_export_payload())
    resp.headers["Content-Disposition"] = (
        f'attachment; filename=billing_export_{utcnow().strftime("%Y%m%d_%H%M%S")}.json')
    return resp


@app.route("/export/invoices.csv")
@admin_required
def export_invoices_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice_number", "client", "date", "status", "subtotal",
                "gst_rate", "gst_amount", "total"])
    for i in Invoice.query.order_by(Invoice.date_created.desc()).all():
        w.writerow([i.invoice_number, i.client.name if i.client else "",
                    i.date_created.isoformat(), i.status, f"{i.subtotal / 100:.2f}",
                    i.gst_rate, f"{i.gst_amount / 100:.2f}", f"{i.total / 100:.2f}"])
    log_audit("export", "Invoice", None, "Exported invoices CSV")
    db.session.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=invoices.csv"})


@app.route("/backup/download")
@admin_required
def backup_download():
    """Send the live SQLite file to the browser. Postgres: use pg_dump."""
    from flask import send_file
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if not uri.startswith("sqlite"):
        flash("Postgres detected: use pg_dump for backup.", "warning")
        return redirect(url_for("settings"))
    db_path = uri.replace("sqlite:///", "")
    if not os.path.isabs(db_path):
        db_path = os.path.join(app.instance_path, db_path)
    if not os.path.exists(db_path):
        flash("Database file not found.", "danger")
        return redirect(url_for("settings"))
    log_audit("backup", "Database", None, "Downloaded backup")
    db.session.commit()
    return send_file(db_path, as_attachment=True,
                     download_name=f"billing_backup_{utcnow().strftime('%Y%m%d_%H%M%S')}.db")


@app.route("/backup/restore", methods=["POST"])
@admin_required
def backup_restore():
    """Replace the live SQLite DB from an uploaded .db file.

    The current file is first copied aside as pre-restore-<ts>.db."""
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if not uri.startswith("sqlite"):
        flash("Restore is supported for SQLite only.", "danger")
        return redirect(url_for("settings"))
    f = request.files.get("backup_file")
    if not f or not f.filename.lower().endswith(".db"):
        flash("Upload a valid .db backup file.", "danger")
        return redirect(url_for("settings"))
    db_path = uri.replace("sqlite:///", "")
    if not os.path.isabs(db_path):
        db_path = os.path.join(app.instance_path, db_path)
    try:
        import sqlite3
        tmp = db_path + ".upload_tmp"
        f.save(tmp)
        # sanity: must be a sqlite DB with our tables
        con = sqlite3.connect(tmp)
        tables = {r[0] for r in
                  con.execute("select name from sqlite_master where type='table'")}
        con.close()
        if not {"invoice", "client", "product"} <= tables:
            os.remove(tmp)
            raise ValueError("Not a billing database (missing core tables)")
        if os.path.exists(db_path):
            shutil.copy2(db_path,
                         f"pre-restore-{utcnow().strftime('%Y%m%d_%H%M%S')}.db")
        db.engine.dispose()
        shutil.move(tmp, db_path)
        global _SCHEMA_OK
        _SCHEMA_OK = False
        ensure_schema()  # migrate the restored file forward if needed
        log_audit("restore", "Database", None,
                  f"Restored from upload {f.filename}")
        db.session.commit()
        flash("Database restored (old file kept as pre-restore-*.db).", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Restore failed: {exc}", "danger")
    return redirect(url_for("settings"))


@app.route("/export/gstr1.csv")
@admin_required
def export_gstr1():
    """GSTR-1 style outward-supplies export (B2C summary by rate)."""
    q, _f, _t, _s = _report_query()
    by_rate = {}
    for i in q.all():
        if i.status == "cancelled":
            continue
        taxable = (i.subtotal or 0) - (i.discount_amount or 0) - (i.exchange_amount or 0)
        key = i.gst_rate or 0
        r = by_rate.setdefault(key, {"bills": 0, "taxable": 0, "gst": 0})
        r["bills"] += 1
        r["taxable"] += taxable
        r["gst"] += i.gst_amount or 0
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["gst_rate_pct", "bills", "taxable_value_inr", "gst_inr",
                "total_inr"])
    for rate in sorted(by_rate):
        r = by_rate[rate]
        w.writerow([rate, r["bills"], f"{r['taxable'] / 100:.2f}",
                    f"{r['gst'] / 100:.2f}",
                    f"{(r['taxable'] + r['gst']) / 100:.2f}"])
    log_audit("export", "GSTR-1", None, "Exported GSTR-1 summary")
    db.session.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=gstr1.csv"})


@app.route("/backup/database")
@admin_required
def backup_database():
    """Legacy server-side copy; kept for cron-style use. Prefer download."""
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    try:
        if uri.startswith("sqlite"):
            db_path = uri.replace("sqlite:///", "")
            if not os.path.isabs(db_path):
                db_path = os.path.join(app.instance_path, db_path)
            if not os.path.exists(db_path):
                # fallback: default instance location
                alt = os.path.join(app.instance_path, "billing.db")
                db_path = alt if os.path.exists(alt) else db_path
            backup_path = f"backup_{utcnow().strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy2(db_path, backup_path)
            log_audit("backup", "Database", None, f"Created backup: {backup_path}")
            db.session.commit()
            flash(f"Database backup created: {backup_path}", "success")
        else:
            log_audit("backup", "Database", None,
                      "Postgres backup requested — use pg_dump")
            db.session.commit()
            flash("Postgres detected: run pg_dump for backup "
                  "(sqlite file-copy only works for SQLite).", "warning")
    except Exception as exc:
        db.session.rollback()
        flash(f"Backup failed: {exc}", "danger")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------- dashboard / prices
@app.route("/")
@login_required
def index():
    settings = get_settings()
    monthly = []
    now = utcnow()
    for back in range(5, -1, -1):
        y = now.year + (now.month - 1 - back) // 12
        m = (now.month - 1 - back) % 12 + 1
        start = datetime(y, m, 1)
        end = datetime(y + (m // 12), (m % 12) + 1, 1)
        rev = db.session.query(db.func.sum(Invoice.total)).filter(
            Invoice.date_created >= start, Invoice.date_created < end,
            Invoice.status != "cancelled").scalar() or 0
        monthly.append({"label": start.strftime("%b"), "revenue": rev})
    peak = max([x["revenue"] for x in monthly] + [1])
    live = Invoice.status != "cancelled"
    return render_template(
        "index.html",
        total_invoices=Invoice.query.filter(live).count(),
        total_clients=Client.query.count(),
        total_products=Product.query.count(),
        total_revenue=db.session.query(db.func.sum(Invoice.total)).filter(
            live).scalar() or 0,
        pending_value=db.session.query(
            db.func.sum(Invoice.total)).filter(
            Invoice.status == "pending").scalar() or 0,
        low_stock=Product.query.filter(
            Product.unit != "g", Product.stock_quantity > 0,
            Product.stock_quantity <= 5).count(),
        settings=settings, metal_prices=metal_prices(settings),
        monthly=monthly, peak=peak,
        recent_invoices=Invoice.query.order_by(
            Invoice.date_created.desc()).limit(5).all())


@app.route("/api/prices")
@login_required
def get_prices():
    settings = get_settings()
    mp = metal_prices(settings)
    return jsonify({"gold": mp["gold"]["price_per_gram"] / 100,
                    "silver": mp["silver"]["price_per_gram"] / 100,
                    "last_updated": mp["gold"]["last_updated"].isoformat()})


csrf.exempt(get_prices)


@app.route("/prices/quick", methods=["POST"])
@login_required
def quick_prices():
    """Dashboard rate editor: same instant reprice as Settings."""
    settings = get_settings()
    try:
        new_gold = validate_rs(
            request.form.get("gold_price", 0), "Gold price (Rs/g)")
        new_silver = validate_rs(
            request.form.get("silver_price", 0), "Silver price (Rs/g)")
        if new_gold <= 0 or new_silver <= 0:
            raise ValueError("Prices must be greater than zero")
        settings.gold_manual_price = new_gold
        settings.silver_manual_price = new_silver
        n = update_all_product_prices(settings)
        log_audit("update", "Settings", settings.id,
                  f"Dashboard reprice gold Rs.{new_gold / 100:.2f}/g silver "
                  f"Rs.{new_silver / 100:.2f}/g; {n} products")
        db.session.commit()
        flash(f"Rates live — {n} product(s) repriced!", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    settings = get_settings()
    if request.method == "POST":
        try:
            settings.firm_name = request.form.get("firm_name", settings.firm_name)
            settings.firm_address = request.form.get("firm_address", "")
            settings.firm_phone = request.form.get("firm_phone", "")
            settings.firm_email = request.form.get("firm_email", "")
            settings.firm_gst = request.form.get("firm_gst", "")
            settings.gold_making_mode = (
                request.form.get("gold_making_mode", "percent").strip().lower())
            if settings.gold_making_mode not in ("percent", "flat"):
                settings.gold_making_mode = "percent"
            settings.silver_making_mode = (
                request.form.get("silver_making_mode", "flat").strip().lower())
            if settings.silver_making_mode not in ("percent", "flat"):
                settings.silver_making_mode = "flat"
            settings.gold_making_charge_percent = validate_positive(
                request.form.get("gold_making_charge_percent", 5), "Gold making charge")
            settings.gold_making_flat = validate_rs(
                request.form.get("gold_making_flat", 0), "Gold flat making charge")
            settings.silver_making_charge_per_10gm = validate_rs(
                request.form.get("silver_making_charge_per_10gm", 150), "Silver making charge")
            settings.silver_making_percent = validate_positive(
                request.form.get("silver_making_percent", 0), "Silver making %")
            settings.gst_enabled = request.form.get("gst_enabled") == "on"
            settings.show_gstin = request.form.get("show_gstin") == "on"
            settings.show_making_charges = (
                request.form.get("show_making_charges") == "on")
            new_gold = validate_rs(
                request.form.get("gold_price", 0), "Gold price (Rs/g)")
            new_silver = validate_rs(
                request.form.get("silver_price", 0), "Silver price (Rs/g)")
            if new_gold <= 0 or new_silver <= 0:
                raise ValueError("Gold and silver prices must be greater than zero")
            settings.gold_manual_price = new_gold
            settings.silver_manual_price = new_silver
            # INSTANT: reprice every product in the same save
            n = update_all_product_prices(settings)
            log_audit("update", "Settings", settings.id,
                      f"Prices gold Rs.{new_gold / 100:.2f}/g silver Rs.{new_silver / 100:.2f}/g; "
                      f"repriced {n} products")
            db.session.commit()
            flash(f"Prices saved — {n} product(s) repriced instantly!", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("settings"))
    return render_template("settings.html", settings=settings)


# ---------------------------------------------------------------- clients
@app.route("/clients")
@login_required
def clients():
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "name")
    query = Client.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Client.name.ilike(like), Client.phone.ilike(like)))
    sorts = {
        "name": Client.name.asc(),
        "balance_desc": Client.balance.desc(),
        "balance_asc": Client.balance.asc(),
        "purchases_desc": Client.total_purchases.desc(),
        "recent": Client.id.desc(),
    }
    query = query.order_by(sorts.get(sort, Client.name.asc()))
    items, pg = paginate(query, request.args.get("page", type=int) or 1)
    return render_template("clients.html", clients=items, pagination=pg,
                           q=q, sort=sort if sort in sorts else "name")


@app.route("/clients/add", methods=["GET", "POST"])
@login_required
def add_client():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        if not name or not phone:
            flash("Name and phone are required", "danger")
            return redirect(url_for("add_client"))
        if Client.query.filter_by(phone=phone).first():
            flash("A client with this phone already exists", "danger")
            return redirect(url_for("add_client"))
        try:
            credit = validate_rs(request.form.get("credit_limit", 0),
                                 "Credit limit")
        except ValueError:
            flash("Credit limit must be a positive number", "danger")
            return redirect(url_for("add_client"))
        c = Client(name=name, phone=phone, email=(request.form.get("email") or "").strip(),
                   address=request.form.get("address", ""), credit_limit=credit,
                   gstin=(request.form.get("gstin") or "").strip())
        db.session.add(c)
        log_audit("create", "Client", None, f"Added client: {name}")
        db.session.commit()
        flash("Client added successfully!", "success")
        return redirect(url_for("clients"))
    return render_template("add_client.html")


@app.route("/clients/<int:client_id>")
@login_required
def view_client(client_id):
    client = Client.query.get_or_404(client_id)
    invoices = Invoice.query.filter_by(client_id=client_id).order_by(
        Invoice.date_created.desc()).all()
    payments = Payment.query.filter_by(client_id=client_id).order_by(
        Payment.payment_date.desc()).all()
    for inv in invoices:
        inv.paid_amount = invoice_paid_total(inv.id)
        inv.remaining_amount = inv.total - inv.paid_amount
    # Metal purchase history across all this client's bills
    metal_lines = db.session.query(
        Invoice.date_created, Invoice.invoice_number,
        InvoiceItem.description, InvoiceItem.item_type,
        InvoiceItem.quantity, InvoiceItem.weight,
        InvoiceItem.line_total).join(
        InvoiceItem, InvoiceItem.invoice_id == Invoice.id).filter(
        Invoice.client_id == client_id).order_by(
        Invoice.date_created.desc()).all()
    metal_summary = {}
    for _dt, _no, _desc, itype, qty, wt, amt in metal_lines:
        m = metal_summary.setdefault(
            itype or "general", {"qty": 0, "weight": 0.0, "amount": 0.0})
        m["qty"] += qty or 0
        m["weight"] += (wt or 0) * (qty or 0)
        m["amount"] += amt or 0
    pay_by_method = {}
    for p in payments:
        pay_by_method[p.payment_method or "cash"] = pay_by_method.get(
            p.payment_method or "cash", 0) + (p.amount or 0)
    return render_template("view_client.html", client=client,
                           invoices=invoices, payments=payments,
                           metal_lines=metal_lines,
                           metal_summary=metal_summary,
                           pay_by_method=pay_by_method)


@app.route("/clients/<int:client_id>/statement")
@login_required
def client_statement(client_id):
    """Printable account statement for the customer."""
    client = Client.query.get_or_404(client_id)
    invoices = Invoice.query.filter_by(client_id=client_id).order_by(
        Invoice.date_created).all()
    for inv in invoices:
        inv.paid_amount = invoice_paid_total(inv.id)
        inv.remaining_amount = inv.total - inv.paid_amount
    payments = Payment.query.filter_by(client_id=client_id).order_by(
        Payment.payment_date).all()
    return render_template("statement.html", client=client,
                           invoices=invoices, payments=payments,
                           settings=Settings.query.first())


@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def edit_client(client_id):
    client = Client.query.get_or_404(client_id)
    if request.method == "POST":
        client.name = request.form.get("name", client.name).strip() or client.name
        client.phone = request.form.get("phone", client.phone).strip() or client.phone
        client.email = request.form.get("email", "")
        client.address = request.form.get("address", "")
        client.gstin = (request.form.get("gstin") or "").strip()
        try:
            # paise column — rupees in ×100, exactly like add_client does
            client.credit_limit = validate_rs(
                request.form.get("credit_limit", (client.credit_limit or 0) / 100),
                "Credit limit")
        except ValueError:
            flash("Credit limit must be a positive number", "danger")
            return redirect(url_for("edit_client", client_id=client_id))
        log_audit("update", "Client", client.id, f"Updated client: {client.name}")
        db.session.commit()
        flash("Client updated successfully!", "success")
        return redirect(url_for("view_client", client_id=client_id))
    return render_template("edit_client.html", client=client)


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def delete_client(client_id):
    client = Client.query.get_or_404(client_id)
    if Invoice.query.filter_by(client_id=client_id).count():
        flash("Cannot delete client with invoices — delete invoices first.", "danger")
        return redirect(url_for("view_client", client_id=client_id))
    Payment.query.filter_by(client_id=client_id).delete()
    db.session.delete(client)
    log_audit("delete", "Client", client_id, f"Deleted client: {client.name}")
    db.session.commit()
    flash("Client deleted.", "success")
    return redirect(url_for("clients"))


@app.route("/clients/<int:client_id>/pay-remaining", methods=["GET", "POST"])
@login_required
def pay_remaining(client_id):
    client = Client.query.get_or_404(client_id)
    if request.method == "POST":
        try:
            amount = validate_rs(request.form.get("amount", 0), "Amount")
            if amount <= 0:
                raise ValueError("Amount must be greater than zero")
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("pay_remaining", client_id=client_id))
        method = request.form.get("payment_method", "cash")
        if method not in ("cash", "UPI", "bank_transfer", "card"):
            flash("Invalid payment method", "danger")
            return redirect(url_for("pay_remaining", client_id=client_id))
        notes = request.form.get("notes", "")
        try:
            owing = invoices_with_due(client_id)
            if not owing:
                flash("No outstanding balance on any invoice.", "warning")
                return redirect(url_for("view_client", client_id=client_id))
            total_due = sum(i.remaining_amount for i in owing)
            if amount > total_due:
                flash(f"Overpayment: total outstanding is Rs.{total_due / 100:.2f}", "danger")
                return redirect(url_for("pay_remaining", client_id=client_id))
            remaining = amount
            for inv in owing:
                if remaining <= 0:
                    break
                paid_before = inv.paid_amount
                due = inv.remaining_amount
                part = min(remaining, due)
                db.session.add(Payment(invoice_id=inv.id, client_id=client_id,
                                       amount=part, payment_method=method, notes=notes))
                db.session.flush()
                client.total_payments += part
                remaining -= part
                if paid_before + part >= inv.total:
                    inv.status = "paid"
            client.balance = client.total_purchases - client.total_payments
            log_audit("create", "Payment", None,
                      f"Client {client_id} paid Rs.{amount / 100:.2f} ({method})")
            db.session.commit()
            flash(f"Payment of Rs.{amount / 100:.2f} processed!", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"Payment failed: {exc}", "danger")
        return redirect(url_for("view_client", client_id=client_id))
    unpaid = invoices_with_due(client_id)
    return render_template("pay_remaining.html", client=client, unpaid_invoices=unpaid)


# ---------------------------------------------------------------- invoices
@app.route("/invoices")
@login_required
def invoices():
    q = (request.args.get("q") or "").strip()
    query = Invoice.query
    if q:
        query = query.join(Client).filter(db.or_(
            Invoice.invoice_number.ilike(f"%{q}%"), Client.name.ilike(f"%{q}%")))
    query = query.order_by(Invoice.date_created.desc())
    items, pg = paginate(query, request.args.get("page", type=int) or 1)
    for inv in items:
        inv.paid_amount = invoice_paid_total(inv.id)
    return render_template("invoices.html", invoices=items, pagination=pg, q=q)


@app.route("/invoices/create", methods=["GET", "POST"])
@login_required
def create_invoice():
    clients = Client.query.order_by(Client.name).all()
    products = Product.query.all()
    settings = get_settings()
    selected_client_id = request.args.get("client_id", type=int)
    if request.method == "POST":
        try:
            client_id = request.form.get("client_id")
            client = Client.query.get(client_id) if client_id else None
            if not client:
                flash("Valid client is required", "danger")
                return redirect(url_for("create_invoice"))
            descriptions = request.form.getlist("description")
            quantities = request.form.getlist("quantity")
            unit_prices = request.form.getlist("unit_price")
            item_types = request.form.getlist("item_type")
            weights = request.form.getlist("weight")
            making_charges = request.form.getlist("making_charges")
            making_modes = request.form.getlist("making_mode")
            product_codes = request.form.getlist("product_code")
            if not any(d.strip() for d in descriptions if d):
                flash("Add at least one item", "danger")
                return redirect(url_for("create_invoice"))

            import uuid
            invoice = Invoice(invoice_number=f"TMP-{uuid.uuid4().hex[:12]}",
                              client_id=client.id,
                              date_due=utcnow() + timedelta(days=30),
                              status=request.form.get("status", "pending"))
            if invoice.status not in ("pending", "paid"):
                invoice.status = "pending"
            db.session.add(invoice)
            db.session.flush()  # get invoice.id without committing

            subtotal, stock_moves = 0, []
            # zip_longest, not zip: zip stops at the shortest list, so a request
            # that omits one field for one row silently dropped EVERY line and
            # then failed with the misleading "Invoice total must be positive".
            rows = zip_longest(descriptions, quantities, unit_prices, item_types,
                               weights, making_charges, making_modes,
                               product_codes, fillvalue="")
            for desc, qty, price, item_type, weight, making, mk_mode, code in rows:
                if not (desc or "").strip():
                    continue
                try:
                    qty_f = float(qty or 0)
                except (ValueError, TypeError):
                    raise ValueError("Quantity must be a number")
                if qty_f <= 0:
                    raise ValueError("Quantity must be positive")
                item_type = (item_type or "general").strip() or "general"
                if item_type not in ("gold", "silver", "general"):
                    raise ValueError(f"Bad item type: {item_type}")
                try:
                    weight_f = float(weight or 0) if weight else 0.0
                except (ValueError, TypeError):
                    raise ValueError("Weight must be a number")
                product = None
                unit_cost = 0
                grams = (weight_f or 0) * qty_f
                if code:
                    product = Product.query.filter_by(barcode=code.strip()).first()
                if product:
                    unit_cost = product.cost_price or 0
                if item_type in ("gold", "silver"):
                    # Metal is sold by weight: line = weight (g) x metal rate
                    # + making charges. A metal price never comes from the form.
                    weighed = bool(product and (product.unit or "pcs") == "g")
                    if weighed:
                        # Weighed goods (pearls, etc.): quantity IS the grams.
                        # There is no "weight per unit" for them — the unit is
                        # one gram, so the line's weight follows the quantity.
                        weight_f, grams, per_unit_g = 1.0, qty_f, 1.0
                    else:
                        if weight_f <= 0:
                            raise ValueError(
                                "Weight (g) is required for gold and silver items")
                        grams, per_unit_g = weight_f * qty_f, weight_f
                    rate = effective_metal_price(item_type, settings)
                    if rate <= 0:
                        raise ValueError(
                            f"{item_type.capitalize()} rate is not set — "
                            f"update it in Settings")
                    price_paise = metal_unit_paise(item_type, per_unit_g, settings)
                elif product:
                    # SERVER-SIDE price: the bill always uses the master price,
                    # never the browser-submitted price.
                    price_paise = product.unit_price or 0
                else:
                    try:
                        price_paise = int(round(float(price or 0) * 100))
                    except (ValueError, TypeError):
                        raise ValueError("Price must be a number")
                    if price_paise < 0:
                        raise ValueError("Price must be >= 0")
                if item_type in ("gold", "silver"):
                    try:
                        making_v = float(making or 0) if making else 0.0
                    except (ValueError, TypeError):
                        raise ValueError("Making charges must be a number")
                    if making_v < 0:
                        raise ValueError("Making charges must be positive")
                    row_mode = making_basis(item_type, settings, mk_mode)
                else:
                    making_v, row_mode = 0, "none"
                line_total, making_used, row_mode = calc_line_total(
                    qty_f, price_paise, item_type, grams, making_v, settings,
                    row_mode)
                subtotal += line_total
                if product:
                    if (product.unit or "pcs") == "g":
                        # weighed goods (pearls etc.): qty = grams
                        have = product.stock_weight or 0
                        if have < qty_f - 1e-9:
                            raise ValueError(
                                f"Insufficient stock for {product.name}: "
                                f"have {have:.2f}g, need {qty_f:g}g")
                        stock_moves.append((product, qty_f))
                    else:
                        if abs(qty_f - round(qty_f)) > 1e-9:
                            raise ValueError(
                                f"{product.name} sells by piece — "
                                f"quantity must be whole")
                        if (product.stock_quantity or 0) < int(round(qty_f)):
                            raise ValueError(
                                f"Insufficient stock for {product.name}: "
                                f"have {product.stock_quantity}, "
                                f"need {int(round(qty_f))}")
                        stock_moves.append((product, int(round(qty_f))))
                db.session.add(InvoiceItem(
                    invoice_id=invoice.id,
                    product_id=product.id if product else None,
                    description=desc.strip(), item_type=item_type, quantity=qty_f,
                    weight=weight_f, making_charges=making_used,
                    making_mode=row_mode,
                    unit_price=price_paise, unit_cost=unit_cost,
                    line_total=line_total))
            if subtotal <= 0:
                raise ValueError("Invoice total must be positive")

            db.session.flush()
            items_now = InvoiceItem.query.filter_by(invoice_id=invoice.id).all()
            has_metal = any(i.item_type in ("gold", "silver") for i in items_now)
            gst_rate = (3.0 if has_metal else 0.0) if settings.gst_enabled else 0.0
            disc_amt, disc_type, disc_val = calc_discount(
                subtotal, request.form.get("discount_type", "none"),
                request.form.get("discount_value", 0))
            taxable = subtotal - disc_amt
            invoice.subtotal = subtotal
            # GSTIN typed on the bill wins; otherwise freeze the client's.
            invoice.client_gstin = (
                (request.form.get("client_gstin") or "").strip()
                or (client.gstin or ""))
            invoice.discount_type = disc_type
            invoice.discount_value = disc_val
            invoice.discount_amount = disc_amt
            invoice.gold_rate = settings.gold_manual_price or 0
            invoice.silver_rate = settings.silver_manual_price or 0
            invoice.gst_rate = gst_rate
            invoice.gst_amount = int(round(taxable * gst_rate / 100))
            gross = taxable + invoice.gst_amount
            exch_amt, exch_metal, exch_wt, exch_rate = calc_exchange(
                request.form.get("exchange_metal", "none"),
                request.form.get("exchange_amount", 0), gross,
                request.form.get("exchange_weight", 0),
                request.form.get("exchange_rate", 0))
            invoice.exchange_metal = exch_metal
            invoice.exchange_weight = exch_wt
            invoice.exchange_rate = exch_rate
            invoice.exchange_amount = exch_amt
            invoice.total = gross - exch_amt

            if client.credit_limit and client.balance + invoice.total > client.credit_limit:
                raise ValueError(
                    f"Credit limit exceeded (limit Rs.{client.credit_limit / 100:.2f}, "
                    f"would owe Rs.{(client.balance + invoice.total) / 100:.2f})")

            for product, move in stock_moves:
                if (product.unit or "pcs") == "g":
                    product.stock_weight = (product.stock_weight or 0) - move
                else:
                    product.stock_quantity = (product.stock_quantity or 0) - move

            ptype = request.form.get("payment_type", "full")
            method = request.form.get("payment_method", "cash")
            if method not in ("cash", "UPI", "bank_transfer", "card"):
                method = "cash"
            paid_now = 0
            if ptype == "partial":
                try:
                    part = int(round(float(request.form.get("partial_amount", 0) or 0) * 100))
                except (ValueError, TypeError):
                    raise ValueError("Partial amount must be a number")
                if part < 0 or part > invoice.total:
                    raise ValueError("Partial amount must be 0..total")
                if part > 0:
                    db.session.add(Payment(
                        invoice_id=invoice.id, client_id=client.id, amount=part,
                        payment_method=method, notes="Partial payment at creation"))
                    client.total_payments += part
                    paid_now = part
            else:  # full (or anything else): record whole total
                db.session.add(Payment(
                    invoice_id=invoice.id, client_id=client.id, amount=invoice.total,
                    payment_method=method, notes="Full payment at creation"))
                client.total_payments += invoice.total
                paid_now = invoice.total
            # Status follows the money — never trust the dropdown alone
            invoice.status = "paid" if paid_now >= invoice.total else "pending"
            # Monotonic number from the persisted counter (never reused)
            invoice.invoice_number = allocate_invoice_number(settings)

            client.total_purchases += invoice.total
            client.balance = client.total_purchases - client.total_payments
            log_audit("create", "Invoice", None, f"Created {invoice.invoice_number}")
            db.session.commit()
            flash(f"Invoice {invoice.invoice_number} created!", "success")
            return redirect(url_for("invoices"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("create_invoice"))
        except Exception as exc:
            db.session.rollback()
            log.exception("create_invoice failed")
            flash(f"Could not create invoice: {exc}", "danger")
            return redirect(url_for("create_invoice"))
    return render_template("create_invoice.html", clients=clients, products=products,
                           settings=settings, selected_client_id=selected_client_id)


@app.route("/invoices/<int:invoice_id>")
@login_required
def view_invoice(invoice_id):
    from urllib.parse import quote
    invoice = Invoice.query.get_or_404(invoice_id)
    total_paid = invoice_paid_total(invoice_id)
    remaining = invoice.total - total_paid
    settings = Settings.query.first()
    firm = settings.firm_name if settings else "Jewelry"
    lines = [f"*{firm}*", f"Invoice {invoice.invoice_number} "
             f"({invoice.date_created.strftime('%d-%b-%Y')})",
             f"Bill To: {invoice.client.name}"]
    for it in invoice.items:
        lines.append(f"- {it.description} x{it.quantity:g}: "
                     f"Rs.{it.line_total / 100:.2f}")
    lines.append(f"Subtotal: Rs.{invoice.subtotal / 100:.2f}")
    if invoice.discount_amount:
        lines.append(f"Discount: -Rs.{invoice.discount_amount / 100:.2f}")
    if invoice.gst_rate:
        lines.append(f"GST ({invoice.gst_rate}%): Rs.{invoice.gst_amount / 100:.2f}")
    if invoice.exchange_amount:
        lines.append(f"Old {invoice.exchange_metal} exchange "
                     f"({invoice.exchange_weight}g): -Rs.{invoice.exchange_amount / 100:.2f}")
    lines.append(f"*Total: Rs.{invoice.total / 100:.2f}*")
    lines.append(f"Paid: Rs.{total_paid / 100:.2f} | Balance: Rs.{remaining / 100:.2f}")
    # Share the PDF itself when the app is reachable from the phone, otherwise
    # the WhatsApp button attaches the file straight from the browser.
    host = (request.host or "").split(":")[0]
    if host and host not in ("localhost", "127.0.0.1", "0.0.0.0"):
        lines.append(url_for("invoice_pdf", invoice_id=invoice.id, _external=True))
    wa_text = quote("\n".join(lines))
    digits = "".join(ch for ch in (invoice.client.phone or "") if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    wa_link = (f"https://wa.me/{digits}?text={wa_text}" if digits
               else f"https://wa.me/?text={wa_text}")
    return render_template("view_invoice.html", invoice=invoice,
                           settings=settings, wa_link=wa_link,
                           total_paid=total_paid, remaining=remaining)


@app.route("/invoices/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    """PDF copy of a bill — used by the Print/Download/WhatsApp buttons."""
    invoice = Invoice.query.get_or_404(invoice_id)
    settings = Settings.query.first()
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle)
    except ImportError:  # pragma: no cover - optional dependency
        flash("PDF needs ReportLab: pip install reportlab", "danger")
        return redirect(url_for("view_invoice", invoice_id=invoice_id))

    def money(paise):
        return f"Rs.{(paise or 0) / 100:,.2f}"

    gold_rate = (invoice.gold_rate or (settings.gold_manual_price if settings else 0) or 0)
    silver_rate = (invoice.silver_rate or (settings.silver_manual_price if settings else 0) or 0)
    show_making = bool(settings is None or settings.show_making_charges)

    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=8.5,
                          leading=11, spaceAfter=0)
    small = ParagraphStyle("small", parent=cell, fontSize=7.5, textColor=colors.HexColor("#666666"))
    head = ParagraphStyle("head", parent=styles["Title"], fontSize=17, leading=20,
                          alignment=0, spaceAfter=2)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=13 * mm, bottomMargin=13 * mm,
                            title=invoice.invoice_number,
                            author=(settings.firm_name if settings else ""))

    firm = settings.firm_name if settings else "Your Firm Name"
    addr_bits = [b for b in [settings.firm_address if settings else "",
                             settings.firm_phone if settings else "",
                             settings.firm_email if settings else ""] if b]
    story = [Paragraph(firm, head),
             Paragraph("Gold &amp; Silver Jewellery", small)]
    if addr_bits:
        story.append(Paragraph(" · ".join(addr_bits), small))
    show_gstin = bool(settings is None or settings.show_gstin)
    if settings and settings.firm_gst and show_gstin:
        story.append(Paragraph(f"GSTIN: {settings.firm_gst}", small))
    story.append(Spacer(1, 6))

    meta = Table([[Paragraph(f"<b>{invoice.invoice_number}</b>", cell),
                   Paragraph(f"Date: {invoice.date_created.strftime('%d %b %Y')}", cell),
                   Paragraph(f"Status: {invoice.status.capitalize()}", cell)]],
                 colWidths=[60 * mm, 60 * mm, 62 * mm])
    meta.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f6f4ef")),
                              ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9d2c4")),
                              ("LEFTPADDING", (0, 0), (-1, -1), 6),
                              ("TOPPADDING", (0, 0), (-1, -1), 4),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [meta, Spacer(1, 8)]

    client = invoice.client
    story.append(Paragraph(
        f"<b>Bill To:</b> {client.name if client else '—'}"
        + (f" &nbsp;·&nbsp; {client.phone}" if client and client.phone else "")
        + (f"<br/>{client.address}" if client and client.address else "")
        + (f"<br/>GSTIN: {invoice.client_gstin}"
           if show_gstin and invoice.client_gstin else ""), cell))
    story.append(Spacer(1, 8))

    headers = ["#", "Item", "Qty", "Weight", "Metal rate", "Amount"]
    if show_making:
        headers.insert(4, "Making")
    rows = [headers]
    for n, it in enumerate(invoice.items, 1):
        rate = (gold_rate if it.item_type == "gold"
                else silver_rate if it.item_type == "silver" else 0)
        if it.item_type in ("gold", "silver"):
            grams = (it.weight or 0) * (it.quantity or 0)
            metal_value = int(round(grams * rate))
            note = (f"{grams:.3f} g x Rs.{rate / 100:,.2f}/g = {money(metal_value)}"
                    + (f" + making {money(it.line_total - metal_value)}"
                       if it.line_total - metal_value else ""))
            detail = Paragraph(f"<b>{it.description}</b><br/><font size=7 color='#666666'>{note}</font>", cell)
            rate_txt = f"Rs.{rate / 100:,.2f}/g"
        else:
            detail = Paragraph(f"<b>{it.description}</b>", cell)
            rate_txt = money(it.unit_price)
        row = [str(n), detail, f"{it.quantity:g}",
               f"{(it.weight or 0):.3f} g" if it.weight else "—", rate_txt,
               money(it.line_total)]
        if show_making:
            row.insert(4, making_text(it).replace("₹", "Rs."))
        rows.append(row)

    data = []
    for i, row in enumerate(rows):
        if i == 0:
            data.append([Paragraph(f"<b>{h}</b>", cell) for h in row])
        else:
            data.append([c if isinstance(c, Paragraph) else Paragraph(str(c), cell)
                         for c in row])
    widths = [8 * mm, 58 * mm, 14 * mm, 20 * mm]
    if show_making:
        widths += [20 * mm]
    widths += [26 * mm, 32 * mm]
    tbl = Table(data, colWidths=widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#faf7f0")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e3dccd")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [tbl, Spacer(1, 8)]

    total_paid = invoice_paid_total(invoice.id)
    totals = [("Subtotal", money(invoice.subtotal))]
    if invoice.discount_amount:
        label = "Discount"
        if invoice.discount_type == "percent":
            label += f" ({invoice.discount_value:g}%)"
        totals.append((label, "- " + money(invoice.discount_amount)))
    if invoice.gst_rate:
        totals.append((f"GST ({invoice.gst_rate:g}%)", money(invoice.gst_amount)))
    if invoice.exchange_amount:
        totals.append((f"Old {invoice.exchange_metal} exchange", "- " + money(invoice.exchange_amount)))
    totals.append(("Total", money(invoice.total)))
    totals.append(("Paid", money(total_paid)))
    totals.append(("Balance", money(invoice.total - total_paid)))
    tt = Table([[Paragraph(t, cell), Paragraph(v, cell)] for t, v in totals],
               colWidths=[130 * mm, 40 * mm])
    tt.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"),
                            ("LINEABOVE", (0, len(totals) - 3), (-1, len(totals) - 3), 0.8, colors.black),
                            ("FONTSIZE", (0, len(totals) - 3), (-1, len(totals) - 3), 10),
                            ("TOPPADDING", (0, 0), (-1, -1), 3),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [tt, Spacer(1, 10)]
    story.append(Paragraph(
        f"Rates on this bill — Gold Rs.{gold_rate / 100:,.2f}/g · "
        f"Silver Rs.{silver_rate / 100:,.2f}/g", small))
    story.append(Paragraph("Thank you for your business.", small))

    doc.build(story)
    pdf = buf.getvalue()
    buf.close()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = (
        f'inline; filename="{invoice.invoice_number}.pdf"')
    return resp


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
@login_required
def delete_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    try:
        # A cancelled bill was already restocked and reversed when it was
        # cancelled — reversing again would invent stock and push the client's
        # ledger negative.
        already_reversed = inv.status == "cancelled"
        paid = invoice_paid_total(inv.id)
        if not already_reversed:
            for item in inv.items:
                if item.product_id:
                    prod = Product.query.get(item.product_id)
                    if prod:
                        if (prod.unit or "pcs") == "g":
                            prod.stock_weight = (prod.stock_weight or 0) + (
                                item.quantity or 0)
                        else:
                            prod.stock_quantity = (prod.stock_quantity or 0) + int(
                                round(item.quantity or 0))
            if inv.client:
                inv.client.total_purchases -= inv.total
                inv.client.total_payments -= paid
                inv.client.balance = (inv.client.total_purchases
                                      - inv.client.total_payments)
        # Archive BEFORE the row disappears — the bill stays auditable in
        # /deleted-bills even though it is gone from the books.
        archive_deleted_invoice(
            inv, reason=request.form.get("reason", ""),
            stock_reversed=not already_reversed, paid=paid)
        for p in Payment.query.filter_by(invoice_id=inv.id).all():
            db.session.delete(p)
        num = inv.invoice_number
        db.session.delete(inv)
        log_audit("delete", "Invoice", invoice_id,
                  f"Deleted {num}"
                  + ("" if already_reversed else ", restocked, reversed ledger"))
        db.session.commit()
        flash(f"Invoice {num} deleted — kept in Deleted bills." +
              ("" if already_reversed else
               " Stock restored, ledger reversed."), "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Delete failed: {exc}", "danger")
    return redirect(url_for("invoices"))


@app.route("/invoices/<int:invoice_id>/update_status", methods=["POST"])
@login_required
def update_invoice_status(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    st = request.form.get("status")
    if st not in ("pending", "paid", "cancelled"):
        flash("Invalid status", "danger")
        return redirect(url_for("view_invoice", invoice_id=invoice_id))
    if st == "cancelled" and inv.status != "cancelled":
        # Void the bill: restock + reverse ledger + drop its payments,
        # so a cancelled bill can never hold stock, dues or collections.
        for item in inv.items:
            if item.product_id:
                prod = Product.query.get(item.product_id)
                if prod:
                    if (prod.unit or "pcs") == "g":
                        prod.stock_weight = (prod.stock_weight or 0) + (
                            item.quantity or 0)
                    else:
                        prod.stock_quantity = (prod.stock_quantity or 0) + int(
                            round(item.quantity or 0))
        paid = invoice_paid_total(inv.id)
        if inv.client:
            inv.client.total_purchases -= inv.total
            inv.client.total_payments -= paid
            inv.client.balance = (inv.client.total_purchases
                                  - inv.client.total_payments)
        Payment.query.filter_by(invoice_id=inv.id).delete()
        inv.status = "cancelled"
        log_audit("cancel", "Invoice", inv.id,
                  f"Cancelled {inv.invoice_number}, restocked, ledger reversed")
        db.session.commit()
        flash("Bill cancelled: stock restored, ledger reversed.", "success")
    elif st == "cancelled":
        flash("Bill is already cancelled.", "info")
    else:
        if inv.status == "cancelled":
            flash("Cancelled bills cannot be reopened — create a new bill.",
                  "danger")
        else:
            inv.status = st
            log_audit("update", "Invoice", inv.id, f"Status -> {st}")
            db.session.commit()
            flash(f"Status -> {st}", "success")
    return redirect(url_for("view_invoice", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>/payment", methods=["GET", "POST"])
@login_required
def add_payment(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if request.method == "POST":
        try:
            if inv.status == "cancelled":
                raise ValueError("Cancelled bills cannot collect payments")
            amount = validate_rs(request.form.get("amount", 0), "Amount")
            if amount <= 0:
                raise ValueError("Amount must be positive")
            due = inv.total - invoice_paid_total(inv.id)
            if amount > due:
                raise ValueError(f"Overpayment: due Rs.{due / 100:.2f}")
            paid_before = inv.total - due
            method = request.form.get("payment_method", "cash")
            if method not in ("cash", "UPI", "bank_transfer", "card"):
                raise ValueError("Invalid payment method")
            db.session.add(Payment(invoice_id=inv.id, client_id=inv.client_id,
                                   amount=amount, payment_method=method,
                                   notes=request.form.get("notes", "")))
            inv.client.total_payments += amount
            inv.client.balance = inv.client.total_purchases - inv.client.total_payments
            if paid_before + amount >= inv.total:
                inv.status = "paid"
            log_audit("create", "Payment", None, f"Rs.{amount / 100:.2f} on {inv.invoice_number}")
            db.session.commit()
            flash("Payment added!", "success")
            return redirect(url_for("view_invoice", invoice_id=invoice_id))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("add_payment", invoice_id=invoice_id))
    return render_template("add_payment.html", invoice=inv)


# ---------------------------------------------------------------- products
@app.route("/products")
@login_required
def products():
    q = (request.args.get("q") or "").strip()
    cat = (request.args.get("cat") or "").strip()
    query = Product.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like),
                                     Product.barcode.ilike(like),
                                     Product.category.ilike(like)))
    if cat:
        query = query.filter_by(category=cat)
    all_p = query.order_by(Product.name).all()
    categories = sorted({c for (c,) in db.session.query(
        Product.category).distinct() if c})
    gold = [p for p in all_p if p.item_type == "gold"]
    silver = [p for p in all_p if p.item_type == "silver"]

    def empty(p):
        return ((p.unit or "pcs") == "g" and not (p.stock_weight or 0)
                ) or ((p.unit or "pcs") != "g" and not (p.stock_quantity or 0))

    out_stock = [p for p in all_p if empty(p)]
    in_stock = [p for p in all_p if not empty(p)]

    def stock_stats(plist):
        return {"count": len(plist),
                "pieces": sum(p.stock_quantity or 0 for p in plist
                              if (p.unit or "pcs") != "g"),
                "weight": sum(((p.weight_per_unit or 0) * (p.stock_quantity or 0)
                               if (p.unit or "pcs") != "g"
                               else (p.stock_weight or 0)) for p in plist)}

    items, pg = paginate(query.order_by(Product.name),
                         request.args.get("page", type=int) or 1)
    return render_template("products.html", products=items, pagination=pg,
                           q=q, cat=cat, categories=categories,
                           gold_products=gold, silver_products=silver,
                           out_products=out_stock, in_products=in_stock,
                           gold_stats=stock_stats(gold),
                           silver_stats=stock_stats(silver),
                           all_stats=stock_stats(all_p))


@app.route("/products/add", methods=["GET", "POST"])
@login_required
def add_product():
    if request.method == "POST":
        try:
            item_type = request.form.get("item_type", "gold")
            if item_type not in ("gold", "silver"):
                raise ValueError("item_type must be gold or silver")
            unit = request.form.get("unit", "pcs")
            if unit not in ("pcs", "g"):
                raise ValueError("Unit must be pieces or grams")
            # Goods sold by weight have no "weight per unit" — their unit IS
            # one gram, so the per-gram metal rate is their price.
            weight = 1.0 if unit == "g" else validate_positive(
                request.form.get("weight_per_unit", 0), "Weight")
            if weight <= 0:
                raise ValueError("Weight must be positive")
            stock = int(request.form.get("stock_quantity", 0) or 0)
            if stock < 0:
                raise ValueError("Stock cannot be negative")
            name = (request.form.get("name") or "").strip()
            if not name:
                raise ValueError("Name is required")
            category = (request.form.get("category") or "").strip()
            try:
                cost = validate_rs(request.form.get("cost_price", 0),
                                   "Cost price")
            except ValueError:
                raise ValueError("Cost price must be a positive number")
            try:
                custom = validate_rs(request.form.get("custom_price", 0),
                                     "Custom price")
            except ValueError:
                raise ValueError("Custom price must be a positive number")
            settings = get_settings()
            auto_price = weight * effective_metal_price(item_type, settings)
            # Integer column: round once here, Postgres rejects a raw float.
            unit_price = int(round(custom if custom > 0 else auto_price))
            if unit == "g":
                try:
                    sw = float(request.form.get("stock_weight", 0) or 0)
                    if sw < 0:
                        raise ValueError
                except ValueError:
                    raise ValueError("Stock weight must be positive")
                stock, stock_w = 0, sw
            else:
                stock, stock_w = stock, 0.0
            last = Product.query.order_by(Product.id.desc()).first()
            n = (last.id + 1) if last else 1
            while Product.query.filter_by(sku=f"SKU-{n:04d}").first():
                n += 1
            p = Product(name=name, sku=f"SKU-{n:04d}", barcode=f"JWL-{n:06d}",
                        description=request.form.get("description", ""),
                        item_type=item_type, category=category, unit=unit,
                        unit_price=unit_price, custom_price=custom,
                        cost_price=cost, stock_quantity=stock,
                        stock_weight=stock_w, weight_per_unit=weight)
            db.session.add(p)
            log_audit("create", "Product", None, f"Added {name} ({p.sku})")
            db.session.commit()
            flash("Product added!", "success")
            return redirect(url_for("products"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("add_product"))
    return render_template("add_product.html")


@app.route("/products/<int:product_id>")
@login_required
def view_product(product_id):
    p = Product.query.get_or_404(product_id)
    return render_template("view_product.html", product=p,
                           qr_data=generate_qr_data(product_qr_payload(p)),
                           barcode_data=generate_barcode_data(p.barcode),
                           settings=Settings.query.first())


@app.route("/products/<int:product_id>/tag")
@login_required
def print_product_tag(product_id):
    """Single-product tag, rendered with that item's design (category first)."""
    _flash_tag_default()
    p = Product.query.get_or_404(product_id)
    tpl_id = request.args.get("tpl", type=int)
    tpl = TagTemplate.query.get(tpl_id) if tpl_id else tag_template_for(p)
    qr, bc = _tag_art(p, tpl)

    # ?w=&h=&tail=&total= are MILLIMETRES — the unit the form's inputs are
    # labelled in. These used to be multiplied by 25.4 as if they were inches,
    # so clicking "Apply size" with the pre-filled 50 x 25 turned a 50 mm tag
    # into a 1270 mm one. They are applied to this render only; the saved
    # design is never touched from a GET.
    def _opt(name, low, high, zero_means_clear=False):
        """Query override, or None when absent/garbage (=> use the design).

        `zero_means_clear` is for the tag length, where 0 is a real, meaningful
        value: "not pinned — size the tag from body + tail after all". It has
        to survive as a 0, because clamping it up to the minimum would invent a
        tag length nobody asked for, and treating it as "absent" would let the
        design's pin win and make the field look broken.
        """
        raw = request.args.get(name)
        if raw in (None, ""):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if zero_means_clear:
            return 0.0 if value <= 0 else max(low, min(high, value))
        return max(low, min(high, value))

    geo = resolved_tag_geometry(tpl,
                                w=_opt("w", 10.0, 300.0),
                                h=_opt("h", 5.0, 300.0),
                                tail=_opt("tail", MIN_TAIL_MM, 120.0),
                                total=_opt("total", 10.0, 400.0,
                                           zero_means_clear=True))
    return render_template("product_tag.html", product=p, tpl=tpl,
                           templates=TagTemplate.query.order_by(
                               TagTemplate.scope.desc(),
                               TagTemplate.name).all(),
                           qr_data=qr, barcode_data=bc,
                           geo=geo,
                           presets=TAG_PRESETS,
                           settings=Settings.query.first())


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    p = Product.query.get_or_404(product_id)
    if request.method == "POST":
        try:
            p.name = request.form.get("name", p.name).strip() or p.name
            p.description = request.form.get("description", "")
            p.category = (request.form.get("category") or "").strip()
            try:
                p.cost_price = validate_rs(request.form.get("cost_price", 0),
                                           "Cost price")
                custom = validate_rs(request.form.get("custom_price", 0),
                                     "Custom price")
            except ValueError:
                raise ValueError("Cost/custom price must be positive numbers")
            p.custom_price = custom
            if (p.unit or "pcs") == "g":
                try:
                    sw = float(request.form.get("stock_weight", 0) or 0)
                    if sw < 0:
                        raise ValueError
                except ValueError:
                    raise ValueError("Stock weight must be positive")
                p.stock_weight = sw
            else:
                p.stock_quantity = int(request.form.get("stock_quantity", 0) or 0)
                if p.stock_quantity < 0:
                    raise ValueError("Stock cannot be negative")
            if (p.unit or "pcs") == "g":
                # weighed goods have no weight-per-unit: their unit is 1 gram
                p.weight_per_unit = 1.0
            else:
                p.weight_per_unit = validate_positive(
                    request.form.get("weight_per_unit", 0), "Weight")
            settings = get_settings()
            p.unit_price = int(round(
                custom if custom > 0 else
                p.weight_per_unit * effective_metal_price(p.item_type, settings)))
            log_audit("update", "Product", p.id, f"Updated {p.name}")
            db.session.commit()
            flash("Product updated!", "success")
            return redirect(url_for("view_product", product_id=product_id))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("edit_product.html", product=p)


@app.route("/products/<int:product_id>/delete", methods=["POST"])
@login_required
def delete_product(product_id):
    p = Product.query.get_or_404(product_id)
    if InvoiceItem.query.filter_by(product_id=product_id).count():
        flash("Cannot delete: product is used in invoices.", "danger")
        return redirect(url_for("view_product", product_id=product_id))
    db.session.delete(p)
    log_audit("delete", "Product", product_id, f"Deleted {p.name}")
    db.session.commit()
    flash("Product deleted.", "success")
    return redirect(url_for("products"))


@app.route("/products/scan/<barcode>")
@login_required
def scan_product(barcode):
    # Strip like the invoice write path does (see create_invoice): a code that
    # arrives with stray whitespace would otherwise be "not found" here but
    # accepted when the bill is saved.
    p = Product.query.filter_by(barcode=barcode.strip()).first()
    if not p:
        return jsonify({"success": False, "message": "Product not found"})
    settings = Settings.query.first()
    if settings:
        mode = making_basis(p.item_type, settings)
        making = default_making(p.item_type, settings, mode)
    else:
        mode, making = "percent", 0
    return jsonify({"success": True, "product": {
        "id": p.id, "name": p.name, "sku": p.sku, "barcode": p.barcode,
        "description": p.description, "item_type": p.item_type,
        "category": p.category or "", "unit": p.unit or "pcs",
        "unit_price": (p.unit_price or 0) / 100,
        "stock_quantity": p.stock_quantity,
        "stock_weight": p.stock_weight or 0,
        "weight_per_unit": p.weight_per_unit, "making_charges": making,
        "making_mode": mode}})


csrf.exempt(scan_product)


# ---------------------------------------------------------------- tags
# Sizes are millimetres: every thermal jewellery printer is a 58 mm or 80 mm
# machine, and the inch presets are just converted for convenience.
TAG_STYLES = ("classic", "modern", "compact", "bold", "minimal")
# Exactly two shapes. Anything else (including the retired "hangtag") is
# coerced to "rectangle" on the way in — see normalise_tag_shape().
TAG_SHAPES = ("rectangle", "tail")
TAG_SHAPE_LABELS = {"rectangle": "Rectangle", "tail": "Rectangle + tail"}
TAG_TAIL_POSITIONS = ("top", "middle", "bottom")
# Legacy shape names -> current ones. "hangtag" was a fold-over loop tag with
# a strap; it is now the same idea done as a rat-tail label.
TAG_SHAPE_ALIASES = {"hangtag": "tail", "strap": "tail", "rat-tail": "tail",
                     "rat_tail": "tail", "plain": "rectangle", "": "rectangle"}
TAG_PRESETS = [
    ("Thermal 40 × 25 mm", 40, 25),
    ("Thermal 50 × 25 mm", 50, 25),
    ("Thermal 58 × 40 mm", 58, 40),
    ("Thermal 80 × 50 mm", 80, 50),
    ("Tail 30 + 14 × 12 mm", 30, 12),
    ("Tail 40 + 16 × 15 mm", 40, 15),
    ("Tail 50 + 20 × 20 mm", 50, 20),
    ("Tag 1.5 × 0.5 in", 38.1, 12.7),
    ("Tag 2 × 1 in", 50.8, 25.4),
    ("Tag 2.5 × 1.25 in", 63.5, 31.75),
    ("Tag 3 × 2 in", 76.2, 50.8),
]


def normalise_tag_shape(shape) -> str:
    """Map any shape value (current, legacy or junk) onto TAG_SHAPES."""
    key = (shape or "").strip().lower()
    if key in TAG_SHAPES:
        return key
    return TAG_SHAPE_ALIASES.get(key, "rectangle")


def tag_total_width_mm(tpl) -> float:
    """Paper width one tag occupies: body + tail (tail only on 'tail')."""
    return resolved_tag_geometry(tpl)["total"]


# A tail shorter than this is not a tail, it is a printing artefact. Also the
# floor used when a tag length is set that leaves no room for one.
MIN_TAIL_MM = 4.0


def resolved_tag_geometry(tpl, w=None, h=None, tail=None, total=None) -> dict:
    """The tag's physical size in mm, resolved once, for every caller.

    A design can state its size two ways, and this is the only place that
    decides which wins:

      * body + tail as entered, or
      * a **total tag length** (`total_mm`), which is what is printed on the
        label roll — a 70 mm tag is 70 mm of paper whatever the body does.

    When a total is set it is authoritative and the tail is whatever is left
    after the body. Any of `w` / `h` / `tail` / `total` may be passed to
    override the design for a single render (the ?w=&h= form on the tag page).

    Returns {"shape", "body", "height", "tail", "total"}.
    """
    shape = normalise_tag_shape(getattr(tpl, "shape", None))
    body = float(w if w is not None else (tpl.width_mm or 50.0))
    height = float(h if h is not None else (tpl.height_mm or 25.0))
    if shape != "tail":
        return {"shape": shape, "body": body, "height": height,
                "tail": 0.0, "total": body}

    want_total = _resolve_total(tpl, total, tail)
    if want_total > 0:
        tail_mm = max(MIN_TAIL_MM, want_total - body)
    else:
        tail_mm = float(tail if tail is not None else (tpl.tail_mm or 16.0))
        tail_mm = max(MIN_TAIL_MM, tail_mm)
    return {"shape": shape, "body": body, "height": height,
            "tail": tail_mm, "total": body + tail_mm}


def _resolve_total(tpl, total, tail) -> float:
    """Which tag length, if any, is in force. 0 means "not pinned".

    Precedence, and it matters:
      1. an explicit `total` — including an explicit 0, which is how the size
         form *releases* a pinned length (0 must not be treated as "absent",
         or the design's pin would silently win and the field would look
         broken);
      2. an explicit `tail` — typing a tail means the tail now leads;
      3. otherwise the design's own `total_mm`.
    """
    if total is not None:
        return float(total)
    if tail is not None:
        return 0.0
    return float(getattr(tpl, "total_mm", 0.0) or 0.0)


def tag_tail_height_mm(tpl) -> float:
    """Resolved tail height. 0 on the design means "auto" = 40% of the body."""
    explicit = getattr(tpl, "tail_h_mm", 0.0) or 0.0
    if explicit > 0:
        return explicit
    return max(2.0, (tpl.height_mm or 25.0) * 0.40)


def tag_qr_mm(tpl) -> float:
    """Resolved QR side in mm. 0 on the design means "auto"."""
    explicit = getattr(tpl, "qr_mm", 0.0) or 0.0
    if explicit > 0:
        return explicit
    return round(max(4.0, (tpl.height_mm or 25.0) * 0.55), 2)


def tag_barcode_h_mm(tpl) -> float:
    """Resolved barcode height in mm. 0 on the design means "auto"."""
    explicit = getattr(tpl, "bc_h_mm", 0.0) or 0.0
    if explicit > 0:
        return explicit
    return round(max(2.0, (tpl.height_mm or 25.0) * 0.28), 2)


def tag_template_for(product: Product | None = None) -> TagTemplate:
    """Design to use for a product: its category's, else the default one."""
    if product is not None and (product.category or "").strip():
        hit = TagTemplate.query.filter_by(
            scope="category", category=product.category).first()
        if hit:
            return hit
    hit = TagTemplate.query.filter_by(scope="default").order_by(
        TagTemplate.id).first()
    if hit:
        return hit
    # first run: no design saved yet — hand back an in-memory default
    return TagTemplate(name="Default tag", scope="default")


def _flash_tag_default():
    """Persist the built-in default design the first time it is needed.

    The flags are set explicitly rather than leaning on the column defaults,
    because this design is what every product without its own category design
    prints with. A shop that has never opened the designer must still get a
    tag carrying the metal and the weight — that is the whole point of a
    jewellery tag. (Previously this row was created from column defaults and
    any later save from the designer could — and did — leave every field off,
    producing a tag with nothing on it but the firm name and a QR code.)
    """
    if TagTemplate.query.filter_by(scope="default").first():
        return
    db.session.add(TagTemplate(
        name="Default tag", scope="default",
        width_mm=50.0, height_mm=25.0, shape="rectangle",
        show_name=True, show_category=True, show_metal=True,
        show_weight=True, show_price=True, show_code=True,
        show_barcode=True, show_qr=False, show_firm=False, show_sku=False,
        font_name=10.0, font_detail=8.0, border="solid",
        pad_mm=1.4, pad_h_mm=2.0, gap_mm=0.5,
    ))
    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover
        db.session.rollback()
        log.warning("could not seed default tag design: %s", exc)


def tag_sample_product() -> Product | None:
    """The item the designer previews with.

    Deliberately *not* simply the first product alphabetically: a shop's first
    product by name is often a gift box or a stone with weight 0, and the
    preview would then hide the weight line even though the design has it
    ticked — which reads as "weight is broken". Prefer the first item that
    actually has something to show.
    """
    products = Product.query.order_by(Product.name).all()
    for p in products:
        if (p.weight_per_unit or 0) > 0 and (p.unit or "pcs") != "g":
            return p
    for p in products:
        if (p.weight_per_unit or 0) > 0:
            return p
    return products[0] if products else None


def product_qr_payload(product: Product) -> str:
    """What a product's QR encodes.

    The barcode, and nothing else — deliberately. Every extra segment pushes
    the QR to a higher version: "JWL-000001|Gold Ring 22K|5.0|350000" is 35
    chars / 29x29 modules, while "JWL-000001" is 10 chars / 21x21. On a 25 mm
    tag the QR is drawn at 0.55 x height, so the shorter code nearly doubles
    the printed module size — and the tag already prints the name, weight and
    price in clear text right beside the code. (It also removes a units trap:
    the old payload appended `unit_price`, which is stored in *paise*, next to
    a price printed in *rupees*.)

    The invoice scanner only ever reads the segment before the first "|", so
    tags printed with the old payload still scan.
    """
    return str(product.barcode or "")


def _tag_art(product: Product, tpl: TagTemplate):
    """QR / barcode images for one tag — only what the design actually shows."""
    qr = bc = None
    if tpl.show_qr:
        qr = generate_qr_data(product_qr_payload(product))
    if tpl.show_barcode:
        bc = generate_barcode_data(product.barcode)
    return qr, bc


def apply_tag_form(tpl: TagTemplate, form) -> TagTemplate:
    """Copy the designer form onto a TagTemplate (create and edit share it)."""
    tpl.name = (form.get("name") or "Tag design").strip() or "Tag design"
    scope = (form.get("scope") or "default").strip().lower()
    tpl.scope = "category" if scope == "category" else "default"
    tpl.category = (form.get("category") or "").strip() if tpl.scope == "category" else ""
    try:
        tpl.width_mm = max(10.0, min(300.0, float(form.get("width_mm", 50) or 50)))
        tpl.height_mm = max(5.0, min(300.0, float(form.get("height_mm", 25) or 25)))
    except (TypeError, ValueError):
        raise ValueError("Tag size must be a number")
    tpl.style = form.get("style", "classic") if form.get("style") in TAG_STYLES else "classic"
    try:
        tpl.font_name = max(4.0, min(40.0, float(form.get("font_name", 10) or 10)))
        tpl.font_detail = max(3.0, min(30.0, float(form.get("font_detail", 8) or 8)))
    except (TypeError, ValueError):
        raise ValueError("Font sizes must be numbers")
    tpl.border = form.get("border", "solid")
    if tpl.border not in ("none", "solid", "double", "dashed"):
        tpl.border = "solid"
    for flag in ("show_name", "show_sku", "show_category", "show_metal",
                 "show_weight", "show_price", "show_barcode", "show_qr",
                 "show_firm", "show_code"):
        setattr(tpl, flag, form.get(flag) == "on")
    tpl.note = (form.get("note") or "").strip()[:80]
    try:
        tpl.copies = max(1, min(10, int(form.get("copies", 1) or 1)))
    except (TypeError, ValueError):
        tpl.copies = 1
    # A ticked checkbox submits "on" and an unticked one submits nothing, so
    # comparing against "off" made this permanently True — the shopkeeper
    # could never turn thermal mode off. Match the pattern used for the
    # show_* flags just above.
    tpl.is_thermal = form.get("is_thermal") == "on"

    # ---- shape + tail geometry ----------------------------------------
    tpl.shape = normalise_tag_shape(form.get("shape"))
    tpl.tail_pos = (form.get("tail_pos") or "middle").strip().lower()
    if tpl.tail_pos not in TAG_TAIL_POSITIONS:
        tpl.tail_pos = "middle"

    def _num(field, default, low, high, label):
        """Clamped float from the form; blank/absent keeps the default."""
        raw = form.get(field)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return max(low, min(high, float(raw)))
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number")

    tpl.tail_mm = _num("tail_mm", 16.0, 4.0, 120.0, "Tail length")
    # 0 = the tag is sized from body + tail rather than from a fixed length
    tpl.total_mm = _num("total_mm", 0.0, 0.0, 400.0, "Tag length")
    # 0 = auto (40% of the body height)
    tpl.tail_h_mm = _num("tail_h_mm", 0.0, 0.0, 300.0, "Tail height")
    # 0 = no punched hole in the tail
    tpl.tail_hole_mm = _num("tail_hole_mm", 0.0, 0.0, 10.0, "Tail hole")
    # ---- spacing + element sizes (0 = auto where it means anything) ----
    tpl.pad_mm = _num("pad_mm", 1.4, 0.0, 20.0, "Padding")
    tpl.pad_h_mm = _num("pad_h_mm", 2.0, 0.0, 20.0, "Side padding")
    tpl.gap_mm = _num("gap_mm", 0.5, 0.0, 10.0, "Line spacing")
    tpl.qr_mm = _num("qr_mm", 0.0, 0.0, 100.0, "QR size")
    tpl.bc_h_mm = _num("bc_h_mm", 0.0, 0.0, 60.0, "Barcode height")
    tpl.bc_w_pct = _num("bc_w_pct", 100.0, 10.0, 100.0, "Barcode width")
    return tpl


@app.route("/tags")
@login_required
def tag_designer():
    """Design + print page: one design per category, plus a global default."""
    _flash_tag_default()
    tpl_id = request.args.get("tpl", type=int)
    tpl = TagTemplate.query.get(tpl_id) if tpl_id else None
    if tpl is None:
        tpl = TagTemplate.query.filter_by(scope="default").order_by(
            TagTemplate.id).first() or TagTemplate(scope="default")
    templates = TagTemplate.query.order_by(
        TagTemplate.scope.desc(), TagTemplate.category, TagTemplate.name).all()
    categories = sorted({c for (c,) in
                         db.session.query(Product.category).distinct() if c})
    products = Product.query.order_by(Product.name).all()
    sample = tag_sample_product()
    art = _tag_art(sample, tpl) if sample else (None, None)
    geo = resolved_tag_geometry(tpl)
    return render_template("tags.html", templates=templates, tpl=tpl,
                           categories=categories, styles=TAG_STYLES,
                           presets=TAG_PRESETS, sample=sample,
                           sample_qr=art[0], sample_bc=art[1],
                           products=products,
                           shape_labels=TAG_SHAPE_LABELS,
                           tail_positions=TAG_TAIL_POSITIONS,
                           # resolved (auto-applied) sizes, so the form can
                           # show the shopkeeper the number that will print
                           geo=geo,
                           tail_h_mm=tag_tail_height_mm(tpl),
                           qr_mm=tag_qr_mm(tpl),
                           bc_h_mm=tag_barcode_h_mm(tpl),
                           settings=Settings.query.first())


@app.route("/tags/save", methods=["POST"])
@login_required
def save_tag_template():
    tpl_id = request.form.get("tpl_id", type=int)
    tpl = TagTemplate.query.get(tpl_id) if tpl_id else TagTemplate()
    try:
        apply_tag_form(tpl, request.form)
        if not tpl.id:
            db.session.add(tpl)
        log_audit("update" if tpl_id else "create", "TagTemplate", tpl.id,
                  f"Saved tag design: {tpl.name}")
        db.session.commit()
        flash("Tag design saved.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("tag_designer", tpl=tpl.id))


@app.route("/tags/<int:tpl_id>/delete", methods=["POST"])
@login_required
def delete_tag_template(tpl_id):
    tpl = TagTemplate.query.get_or_404(tpl_id)
    if tpl.scope == "default" and TagTemplate.query.filter_by(
            scope="default").count() <= 1:
        flash("Keep at least one default design.", "danger")
        return redirect(url_for("tag_designer"))
    db.session.delete(tpl)
    log_audit("delete", "TagTemplate", tpl_id, f"Deleted tag design: {tpl.name}")
    db.session.commit()
    flash("Design deleted.", "success")
    return redirect(url_for("tag_designer"))


@app.route("/tags/print")
@login_required
def print_tags():
    """Print a batch of tags — a whole category, a selection, or the lot."""
    _flash_tag_default()
    tpl_id = request.args.get("tpl", type=int)
    cat = (request.args.get("cat") or "").strip()
    layout = "sheet" if request.args.get("layout") == "sheet" else "roll"
    copies = max(1, min(10, request.args.get("copies", type=int) or 1))
    sel = [int(x) for x in (request.args.get("ids") or "").split(",")
           if x.strip().isdigit()]
    per_tpl = not tpl_id  # no design chosen -> use each item's own design

    query = Product.query
    if sel:
        query = query.filter(Product.id.in_(sel))
    elif cat:
        query = query.filter_by(category=cat)
    products = query.order_by(Product.name).all()
    if not products:
        flash("Pick at least one product to print.", "warning")
        return redirect(url_for("tag_designer"))

    tpl = TagTemplate.query.get(tpl_id) if tpl_id else None
    tags = []
    for p in products:
        design = tpl or (tag_template_for(p) if per_tpl else
                         tag_template_for(None))
        qr, bc = _tag_art(p, design)
        # each tag carries its own resolved mm geometry, because with
        # "each item's own design" two tags in one batch can be different sizes
        geo = resolved_tag_geometry(design)
        for _ in range(copies if not per_tpl else (design.copies or 1)):
            tags.append({"product": p, "tpl": design, "qr": qr, "bc": bc,
                         "geo": geo})
    return render_template("tags_print.html", tags=tags, layout=layout,
                           tpl=tpl, cat=cat, copies=copies,
                           settings=Settings.query.first())


@app.route("/tags/calibrate")
@login_required
def tag_calibration():
    """A ruler printed at 100%, to prove the printer is not scaling the page.

    This is the answer to "the tag comes out the wrong size": a browser will
    happily stretch the page to whatever paper the driver reports, and no
    stylesheet can override the print dialog. Measuring this bar tells the
    shopkeeper in five seconds whether the scale is right, without spending
    label stock on a tag that comes out wrong.
    """
    bar = request.args.get("w", type=float) or 50.0
    return render_template("tags_calibrate.html",
                           bar_mm=max(20.0, min(200.0, bar)),
                           settings=Settings.query.first())


# ---------------------------------------------------------------- reports
def _report_query(include_cancelled: bool = False):
    """Filtered invoice query for reports. Returns (query, f, t, status).

    Cancelled bills are void, so they stay out of money totals unless the
    caller asks for them (or filters on status=cancelled).
    """
    # The filter form posts from/to; the quick chips and CSV links use the
    # short f/t aliases. Accept both — ignoring one silently disables the
    # other and every "last 30 days" chip would report all-time figures.
    f = request.args.get("from") or request.args.get("f") or ""
    t = request.args.get("to") or request.args.get("t") or ""
    status = request.args.get("status") or "all"
    q = Invoice.query
    if f:
        try:
            q = q.filter(Invoice.date_created >= datetime.strptime(f, "%Y-%m-%d"))
        except ValueError:
            f = ""
    if t:
        try:
            q = q.filter(Invoice.date_created < datetime.strptime(t, "%Y-%m-%d")
                         + timedelta(days=1))
        except ValueError:
            t = ""
    if status in ("pending", "paid", "cancelled"):
        q = q.filter(Invoice.status == status)
    elif not include_cancelled:
        q = q.filter(Invoice.status != "cancelled")
    return q, f, t, status


def _line_grams(it, prod) -> float:
    """Grams on a bill line. Weighed goods: the quantity IS the grams."""
    if prod is not None and (prod.unit or "pcs") == "g":
        return float(it.quantity or 0)
    return float(it.weight or 0) * float(it.quantity or 0)


def _pct_change(new: float, old: float):
    """Percent delta vs a previous period. None when there is no baseline."""
    if not old:
        return None
    return round((new - old) / abs(old) * 100, 1)


@app.route("/reports")
@login_required
def reports():
    q, f, t, status = _report_query()
    invoices = q.order_by(Invoice.date_created.desc()).all()
    ids = [i.id for i in invoices]
    items = InvoiceItem.query.filter(
        InvoiceItem.invoice_id.in_(ids)).all() if ids else []
    # product lookup once, so per-line analysis does not re-query in a loop
    prods = {p.id: p for p in Product.query.all()}

    # ---- headline money ----
    revenue = sum((i.total or 0) for i in invoices)
    gst_total = sum((i.gst_amount or 0) for i in invoices)
    net_sales = revenue - gst_total          # turnover ex-GST
    discount_total = sum((i.discount_amount or 0) for i in invoices)
    exchange_total = sum((i.exchange_amount or 0) for i in invoices)
    collected = sum(invoice_paid_total(i.id) for i in invoices)
    outstanding = revenue - collected
    avg_bill = int(round(revenue / len(invoices))) if invoices else 0

    # ---- period comparison (same length, immediately before) ----
    if invoices:
        d0 = min(i.date_created for i in invoices)
        d1 = max(i.date_created for i in invoices)
    else:
        d0 = d1 = utcnow()
    span_days = max(1, (d1 - d0).days + 1)
    prev_q = Invoice.query.filter(
        Invoice.date_created >= d0 - timedelta(days=span_days),
        Invoice.date_created < d0,
        Invoice.status != "cancelled")
    prev_invoices = prev_q.all()
    prev_revenue = sum((i.total or 0) for i in prev_invoices)
    prev_bills = len(prev_invoices)
    prev_avg = int(round(prev_revenue / prev_bills)) if prev_bills else 0
    delta = {
        "revenue": _pct_change(revenue, prev_revenue),
        "bills": _pct_change(len(invoices), prev_bills),
        "avg_bill": _pct_change(avg_bill, prev_avg),
        "prev_revenue": prev_revenue,
        "prev_bills": prev_bills,
        "prev_avg": prev_avg,
        "prev_label": f"{(d0 - timedelta(days=span_days)).strftime('%d %b')}"
                      f" – {(d0 - timedelta(days=1)).strftime('%d %b %Y')}",
    }

    # The all-time view has no baseline of its own (nothing predates the
    # oldest bill), so its deltas are always empty and the default page would
    # never show a trend. Fall back to a rolling 30-vs-30 day comparison.
    rolling = None
    if not f and not t:
        cur_from = utcnow() - timedelta(days=30)
        prev_from = utcnow() - timedelta(days=60)
        live = Invoice.status != "cancelled"
        cur_rows = Invoice.query.filter(
            live, Invoice.date_created >= cur_from).all()
        prev_rows = Invoice.query.filter(
            live, Invoice.date_created >= prev_from,
            Invoice.date_created < cur_from).all()
        cur_rev = sum((i.total or 0) for i in cur_rows)
        prev_rev = sum((i.total or 0) for i in prev_rows)
        rolling = {
            "revenue": _pct_change(cur_rev, prev_rev),
            "bills": _pct_change(len(cur_rows), len(prev_rows)),
            "cur_revenue": cur_rev, "prev_revenue": prev_rev,
            "cur_bills": len(cur_rows), "prev_bills": len(prev_rows),
        }

    # ---- trend: daily for short ranges, monthly for long ones ----
    daily = span_days <= 62
    trend = {}
    for i in invoices:
        k = (i.date_created.strftime("%Y-%m-%d") if daily
             else i.date_created.strftime("%Y-%m"))
        trend[k] = trend.get(k, 0) + (i.total or 0)
    trend = sorted(trend.items())

    # ---- payment mix ----
    pay_by_method = {}
    if ids:
        for m, amt in db.session.query(
                Payment.payment_method,
                db.func.sum(Payment.amount)).filter(
                Payment.invoice_id.in_(ids)).group_by(
                Payment.payment_method).all():
            pay_by_method[m or "cash"] = pay_by_method.get(
                m or "cash", 0) + (amt or 0)

    # ---- metal / category / product / client breakdowns ----
    metal_rev, cat_sales, prod_sales, by_client = {}, {}, {}, {}
    for it in items:
        prod = prods.get(it.product_id)
        grams = _line_grams(it, prod)
        m = metal_rev.setdefault(
            it.item_type or "general",
            {"qty": 0, "weight": 0.0, "amount": 0.0, "bills": 0})
        m["qty"] += it.quantity or 0
        m["weight"] += grams
        m["amount"] += it.line_total or 0
        cat = (prod.category if prod and prod.category else
               (prod.category or "")) or "Uncategorised"
        c = cat_sales.setdefault(
            cat, {"qty": 0, "weight": 0.0, "amount": 0.0})
        c["qty"] += it.quantity or 0
        c["weight"] += grams
        c["amount"] += it.line_total or 0
        key = (prod.name if prod else it.description)
        ps = prod_sales.setdefault(
            key, {"qty": 0, "weight": 0.0, "amount": 0.0})
        ps["qty"] += it.quantity or 0
        ps["weight"] += grams
        ps["amount"] += it.line_total or 0
    for i in invoices:
        name = i.client.name if i.client else "—"
        cl = by_client.setdefault(
            name, {"revenue": 0, "bills": 0, "paid": 0, "phone":
                   (i.client.phone if i.client else "")})
        cl["revenue"] += i.total or 0
        cl["bills"] += 1
        cl["paid"] += invoice_paid_total(i.id)
    top_products = sorted(prod_sales.items(), key=lambda x: -x[1]["amount"])[:10]
    top_categories = sorted(cat_sales.items(), key=lambda x: -x[1]["amount"])[:10]
    top_clients = sorted(by_client.items(), key=lambda x: -x[1]["revenue"])[:10]

    # ---- receivables aging (only what is still owed) ----
    today = utcnow()
    aging = {"0-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    for i in invoices:
        due = (i.total or 0) - invoice_paid_total(i.id)
        if due <= 0:
            continue
        age = (today - i.date_created).days
        bucket = ("0-30" if age <= 30 else "31-60" if age <= 60
                  else "61-90" if age <= 90 else "90+")
        aging[bucket] += due

    # ---- profit (cost snapshot per row; falls back to current cost) ----
    est_cost = 0.0
    for it in items:
        if it.unit_cost:
            unit_cost = it.unit_cost
        else:
            prod = prods.get(it.product_id)
            unit_cost = (prod.cost_price or 0) if prod else 0
        est_cost += int(round(unit_cost * (it.quantity or 0)))
    # Two genuinely different numbers (they used to be the same value twice):
    #   gross = sales − cost, before the tax collected is set aside
    #   net   = sales − GST − cost, i.e. what the shop actually keeps
    gross_profit = revenue - est_cost
    net_profit = net_sales - est_cost
    margin_pct = round(net_profit / net_sales * 100, 1) if net_sales else 0.0
    missing_cost = sum(1 for p in prods.values() if not (p.cost_price or 0))

    # ---- GST details ----
    gst_bills = sum(1 for i in invoices if (i.gst_amount or 0) > 0)
    gst_by_month = {}
    for i in invoices:
        if (i.gst_amount or 0) > 0:
            k = i.date_created.strftime("%Y-%m")
            gst_by_month[k] = gst_by_month.get(k, 0) + (i.gst_amount or 0)

    # ---- voided + deleted bills in the same window ----
    cq, _cf, _ct, _cs = _report_query(include_cancelled=True)
    cancelled_invoices = cq.filter(Invoice.status == "cancelled").all()
    cancelled_total = sum((i.total or 0) for i in cancelled_invoices)
    del_q = DeletedInvoice.query
    if f:
        try:
            del_q = del_q.filter(DeletedInvoice.deleted_at
                                 >= datetime.strptime(f, "%Y-%m-%d"))
        except ValueError:
            pass
    if t:
        try:
            del_q = del_q.filter(DeletedInvoice.deleted_at
                                 < datetime.strptime(t, "%Y-%m-%d")
                                 + timedelta(days=1))
        except ValueError:
            pass
    deleted_rows = del_q.order_by(DeletedInvoice.deleted_at.desc()).all()
    deleted_total = sum((d.total or 0) for d in deleted_rows)

    # ---- stock management ----
    all_products = list(prods.values())
    stock_value_sale = sum((p.unit_price or 0) * (p.stock_quantity or 0)
                           for p in all_products if (p.unit or "pcs") != "g")
    stock_value_sale += sum((p.unit_price or 0) * (p.stock_weight or 0)
                            for p in all_products if (p.unit or "pcs") == "g")
    stock_value_cost = sum((p.cost_price or 0) * (p.stock_quantity or 0)
                           for p in all_products if (p.unit or "pcs") != "g")
    stock_value_cost += sum((p.cost_price or 0) * (p.stock_weight or 0)
                            for p in all_products if (p.unit or "pcs") == "g")
    low_stock = [p for p in all_products
                 if (p.unit or "pcs") != "g" and 0 < (p.stock_quantity or 0) <= 5]
    out_stock = [p for p in all_products
                 if ((p.unit or "pcs") == "g" and not (p.stock_weight or 0))
                 or ((p.unit or "pcs") != "g" and not (p.stock_quantity or 0))]
    sold_ids = {it.product_id for it in InvoiceItem.query.all() if it.product_id}
    dead_stock = sorted((p for p in all_products if p.id not in sold_ids),
                        key=lambda p: -(p.unit_price or 0)
                        * ((p.stock_quantity or 0) or (p.stock_weight or 0)))
    dead_value = sum((p.unit_price or 0)
                     * ((p.stock_quantity or 0) or (p.stock_weight or 0))
                     for p in dead_stock)
    new_clients = Client.query.filter(
        Client.id.in_([i.client_id for i in invoices])).count() if invoices else 0
    quick = {
        "today": today.strftime("%Y-%m-%d"),
        "week": (today - timedelta(days=6)).strftime("%Y-%m-%d"),
        "month": (today - timedelta(days=29)).strftime("%Y-%m-%d"),
        "mtd": today.replace(day=1).strftime("%Y-%m-%d"),
    }

    return render_template(
        "reports.html", quick=quick,
        total_invoices=len(invoices),
        total_revenue=revenue,
        total_gst=gst_total,
        total_discount=discount_total,
        total_exchange=exchange_total,
        collected=collected, outstanding=outstanding, avg_bill=avg_bill,
        pending_invoices=sum(1 for i in invoices if i.status == "pending"),
        recent_invoices=invoices[:25],
        metal_revenue=metal_rev, f=f, t=t, status=status, delta=delta,
        rolling=rolling,
        daily=daily, trend=trend, pay_by_method=pay_by_method,
        top_products=top_products, top_categories=top_categories,
        top_clients=top_clients, aging=aging,
        gross_profit=gross_profit, net_profit=net_profit, net_sales=net_sales,
        margin_pct=margin_pct, est_cost=est_cost, gst_bills=gst_bills,
        gst_by_month=gst_by_month, cancelled_count=len(cancelled_invoices),
        cancelled_total=cancelled_total,
        deleted_count=len(deleted_rows), deleted_total=deleted_total,
        stock_value_sale=stock_value_sale, stock_value_cost=stock_value_cost,
        low_stock=low_stock, out_stock=out_stock, dead_stock=dead_stock,
        dead_value=dead_value, missing_cost=missing_cost,
        new_clients=new_clients, span_days=span_days)


@app.route("/reports.csv")
@login_required
def reports_csv():
    q, _f, _t, _s = _report_query()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice", "date", "client", "status", "subtotal", "discount",
                "gst", "total", "paid", "balance"])
    for i in q.order_by(Invoice.date_created.desc()).all():
        paid = invoice_paid_total(i.id)
        w.writerow([i.invoice_number,
                    i.date_created.strftime("%Y-%m-%d %H:%M"),
                    i.client.name if i.client else "", i.status,
                    f"{i.subtotal / 100:.2f}", f"{(i.discount_amount or 0) / 100:.2f}",
                    f"{i.gst_amount / 100:.2f}", f"{i.total / 100:.2f}",
                    f"{paid / 100:.2f}", f"{(i.total - paid) / 100:.2f}"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=report.csv"})


@app.route("/reports/products.csv")
@login_required
def reports_products_csv():
    """Item-level sales export for the same filters as the reports page."""
    q, _f, _t, _s = _report_query()
    ids = [i.id for i in q.all()]
    prods = {p.id: p for p in Product.query.all()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["item", "category", "metal", "type", "qty", "weight_g",
                "revenue_inr"])
    rows = {}
    if ids:
        for it, prod in db.session.query(InvoiceItem, Product).outerjoin(
                Product, InvoiceItem.product_id == Product.id).filter(
                InvoiceItem.invoice_id.in_(ids)).all():
            key = (prod.name if prod else it.description)
            r = rows.setdefault(key, [0.0, 0.0, 0, ""])
            r[0] += it.quantity or 0
            r[1] += _line_grams(it, prod)
            r[2] += it.line_total or 0
            r[3] = f"{(prod.category if prod else '') or ''}|" \
                   f"{prod.item_type if prod else it.item_type}"
    for name, (qty, grams, amount, meta) in sorted(
            rows.items(), key=lambda x: -x[1][2]):
        cat, kind = (meta.split("|") + [""])[:2]
        w.writerow([name, cat or "Uncategorised", kind, "", f"{qty:g}",
                    f"{grams:.3f}", f"{amount / 100:.2f}"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=item_sales.csv"})


# ---------------------------------------------------------------- deleted bills
@app.route("/deleted-bills")
@admin_required
def deleted_bills():
    """Archive of every bill that was deleted — nothing is ever truly lost."""
    q = (request.args.get("q") or "").strip()
    query = DeletedInvoice.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(
            DeletedInvoice.invoice_number.ilike(like),
            DeletedInvoice.client_name.ilike(like),
            DeletedInvoice.client_gstin.ilike(like),
            DeletedInvoice.deleted_by.ilike(like)))
    query = query.order_by(DeletedInvoice.deleted_at.desc())
    items, pg = paginate(query, request.args.get("page", type=int) or 1,
                         per_page=25)
    totals = {
        "count": query.count(),
        "value": db.session.query(
            db.func.sum(DeletedInvoice.total)).scalar() or 0,
        "paid": db.session.query(
            db.func.sum(DeletedInvoice.paid_amount)).scalar() or 0,
    }
    return render_template("deleted_bills.html", rows=items, pagination=pg,
                           q=q, totals=totals)


@app.route("/deleted-bills/<int:row_id>")
@admin_required
def view_deleted_bill(row_id):
    row = DeletedInvoice.query.get_or_404(row_id)
    try:
        data = json.loads(row.payload) if row.payload else None
    except (ValueError, TypeError):
        data = None
    return render_template("view_deleted_bill.html", row=row, data=data)


@app.route("/deleted-bills.csv")
@admin_required
def deleted_bills_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice_number", "client", "phone", "gstin", "bill_date",
                "deleted_at", "deleted_by", "reason", "status", "subtotal",
                "gst", "total", "paid", "items"])
    for d in DeletedInvoice.query.order_by(
            DeletedInvoice.deleted_at.desc()).all():
        w.writerow([d.invoice_number, d.client_name, d.client_phone,
                    d.client_gstin or "",
                    d.date_created.strftime("%Y-%m-%d") if d.date_created else "",
                    d.deleted_at.strftime("%Y-%m-%d %H:%M") if d.deleted_at else "",
                    d.deleted_by, d.reason or "", d.status,
                    f"{(d.subtotal or 0) / 100:.2f}",
                    f"{(d.gst_amount or 0) / 100:.2f}",
                    f"{(d.total or 0) / 100:.2f}",
                    f"{(d.paid_amount or 0) / 100:.2f}", d.item_count])
    log_audit("export", "DeletedInvoice", None, "Exported deleted bills CSV")
    db.session.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=deleted_bills.csv"})


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=False)
