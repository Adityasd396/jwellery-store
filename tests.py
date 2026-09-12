"""Fixed test suite: auth, stock, GST, payments, deletes, pricing, paise."""
import atexit as _atexit
import os as _os
import re
import tempfile as _tempfile
import unittest

_TEST_DIR = _tempfile.mkdtemp(prefix="billing_test_")
_TEST_DB = _os.path.join(_TEST_DIR, "test.db").replace("\\", "/")

# Set BEFORE importing app: Flask-SQLAlchemy freezes the engine at import
# (init_app), so later config overrides are silently ignored and every test
# would hit the real instance/billing.db file.
_os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"


def _cleanup_test_db():
    import shutil
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


_atexit.register(_cleanup_test_db)

from app import (app, db, User, Client, Product, Invoice, InvoiceItem,
                 Payment, Settings, TagTemplate, resolved_tag_geometry)
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{_TEST_DB}"
    WTF_CSRF_ENABLED = False


def _csrf_off():
    app.config.from_object(TestConfig)


class BillingTestCase(unittest.TestCase):
    def setUp(self):
        _csrf_off()
        self.app = app.test_client()
        with app.app_context():
            db.create_all()
            s = Settings(gold_manual_price=600000, silver_manual_price=8000)
            db.session.add(s)
            u = User(username="testuser", email="test@example.com", is_admin=True)
            u.set_password("testpass123")
            db.session.add(u)
            c = Client(name="C1", phone="1111111111")
            db.session.add(c)
            db.session.commit()
            self.client_id = c.id

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
        import app as appmod
        appmod._LOGIN_ATTEMPTS.clear()
        appmod._SCHEMA_OK = False

    def login(self):
        return self.app.post("/login", data={"username": "testuser",
                                             "password": "testpass123"},
                             follow_redirects=True)

    def test_login(self):
        self.assertEqual(self.login().status_code, 200)

    def test_auth_required(self):
        # logged-out access to protected pages redirects to login
        for url in ["/", "/clients", "/products", "/invoices", "/reports",
                    "/clients/1", "/invoices/1", "/products/1"]:
            r = self.app.get(url, follow_redirects=False)
            self.assertIn(r.status_code, (301, 302), url)

    def test_index_no_api_storm(self):
        self.login()
        self.assertEqual(self.app.get("/").status_code, 200)

    def test_add_client_duplicate_phone(self):
        self.login()
        r = self.app.post("/clients/add", data={"name": "Dup", "phone": "1111111111"},
                          follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            self.assertEqual(Client.query.filter_by(phone="1111111111").count(), 1)

    def test_add_product(self):
        self.login()
        r = self.app.post("/products/add", data={"name": "Ring", "item_type": "gold",
                                                 "weight_per_unit": 10.0, "stock_quantity": 5},
                          follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            p = Product.query.filter_by(name="Ring").first()
            self.assertIsNotNone(p)
            self.assertAlmostEqual(p.unit_price, 10.0 * 600000)

    def _make_product(self, name="Ring", w=10.0, stock=5, t="gold"):
        with app.app_context():
            p = Product(name=name, sku=f"SKU-{name}", barcode=f"BC-{name}",
                        item_type=t, unit_price=w * 600000,
                        stock_quantity=stock, weight_per_unit=w)
            db.session.add(p)
            db.session.commit()
            return p.barcode

    def _post_invoice(self, barcode, qty=1, price=60000.0, ptype="full"):
        return self.app.post("/invoices/create", data={
            "client_id": str(self.client_id), "status": "pending",
            "payment_type": ptype, "payment_method": "cash",
            "description": ["Gold ring"], "quantity": [str(qty)],
            "unit_price": [str(price)], "item_type": ["gold"],
            "weight": ["10.0"], "making_charges": ["5"],
            "product_code": [barcode],
        }, follow_redirects=True)

    def test_invoice_gst_and_stock(self):
        self.login()
        bc = self._make_product()
        r = self._post_invoice(bc)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            inv = Invoice.query.first()
            # base 60000 + 5% making = 63000; +3% GST = 64890
            self.assertAlmostEqual(inv.subtotal, 6300000)
            self.assertEqual(inv.gst_rate, 3.0)
            self.assertAlmostEqual(inv.total, 6489000)
            self.assertEqual(Product.query.filter_by(barcode=bc).first().stock_quantity, 4)
            self.assertEqual(inv.status, "paid")  # full payment

    def test_invoice_insufficient_stock_blocked(self):
        self.login()
        bc = self._make_product(stock=1)
        r = self._post_invoice(bc, qty=5)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            self.assertEqual(Invoice.query.count(), 0)
            self.assertEqual(Product.query.filter_by(barcode=bc).first().stock_quantity, 1)

    def test_delete_invoice_restores_stock_and_ledger(self):
        self.login()
        bc = self._make_product()
        self._post_invoice(bc)
        with app.app_context():
            inv = Invoice.query.first()
            iid = inv.id
        r = self.app.post(f"/invoices/{iid}/delete", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            self.assertEqual(Invoice.query.count(), 0)
            self.assertEqual(Product.query.filter_by(barcode=bc).first().stock_quantity, 5)
            c = Client.query.get(self.client_id)
            self.assertAlmostEqual(c.total_purchases, 0)
            self.assertAlmostEqual(c.balance, 0)

    def test_delete_product_blocked_when_in_invoice(self):
        self.login()
        bc = self._make_product()
        self._post_invoice(bc)
        with app.app_context():
            p = Product.query.filter_by(barcode=bc).first()
            pid = p.id
        r = self.app.post(f"/products/{pid}/delete", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            self.assertIsNotNone(Product.query.get(pid))

    def test_overpayment_blocked(self):
        self.login()
        bc = self._make_product()
        self._post_invoice(bc, ptype="partial")
        with app.app_context():
            inv = Invoice.query.first()
            iid, total = inv.id, inv.total
        r = self.app.post(f"/invoices/{iid}/payment",
                          data={"amount": str(total + 100), "payment_method": "cash"},
                          follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            from app import invoice_paid_total
            self.assertAlmostEqual(invoice_paid_total(iid), 0)

    def test_general_item_zero_gst(self):
        self.login()
        with app.app_context():
            c = self.client_id
        r = self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "UPI",
            "description": ["Box"], "quantity": ["2"],
            "unit_price": ["100.0"], "item_type": ["general"],
            "weight": ["0"], "making_charges": ["0"], "product_code": [""],
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            inv = Invoice.query.first()
            self.assertEqual(inv.gst_rate, 0.0)
            self.assertAlmostEqual(inv.total, 20000)

    def test_export_and_health(self):
        self.login()
        self.assertEqual(self.app.get("/export/data").status_code, 200)
        self.assertEqual(self.app.get("/export/invoices.csv").status_code, 200)
        self.assertEqual(self.app.get("/healthz").status_code, 200)

    # ---- deep coverage: helpers, new UI, math variants ----
    def test_qr_and_barcode_helpers(self):
        from app import generate_qr_data, generate_barcode_data
        qr = generate_qr_data("JWL-000001|Ring|10|60000")
        self.assertTrue(qr.startswith("data:image/png;base64,"))
        bc = generate_barcode_data("JWL-000001")
        self.assertTrue(bc.startswith("data:image/png;base64,"))
        self.assertIsNone(generate_barcode_data(""))
        self.assertIsNone(generate_barcode_data(None))

    def test_product_page_shows_code_and_barcode(self):
        self.login()
        self._make_product()
        with app.app_context():
            p = Product.query.filter_by(name="Ring").first()
            pid = p.id
        r = self.app.get(f"/products/{pid}")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn(p.barcode, html)
        self.assertIn("Barcode JWL" if "JWL" in p.barcode else p.barcode, html)
        self.assertIn("data:image/png;base64", html)
        self.assertNotIn("making_charges_percent", html)  # fixed template bug

    def test_tag_page_sizes_and_code(self):
        self.login()
        bc = self._make_product()
        with app.app_context():
            p = Product.query.filter_by(barcode=bc).first()
            pid = p.id
        r = self.app.get(f"/products/{pid}/tag")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn(bc, html)
        self.assertIn("@media print", html)
        # The size form is in millimetres and must be *read* as millimetres.
        # It used to be multiplied by 25.4, so applying the pre-filled 50 x 25
        # produced a 1270 x 635 mm page.
        self.assertIn('name="w"', html)
        self.assertIn("@page { size: 50.0mm 25.0mm;", html)
        r2 = self.app.get(f"/products/{pid}/tag?w=80&h=50").data.decode()
        self.assertIn("@page { size: 80.0mm 50.0mm;", r2)
        # a GET must never rewrite the saved design
        with app.app_context():
            t = TagTemplate.query.filter_by(scope="default").first()
            self.assertAlmostEqual(t.width_mm, 50.0)
            self.assertAlmostEqual(t.height_mm, 25.0)

    def test_invoice_page_has_search_and_scanner(self):
        self.login()
        r = self.app.get("/invoices/create")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        for marker in ["client-search", "client-hint", "data-balance",
                        "openScanner", "scan-modal", "scan-video",
                        "qr-upload", "switchCamera", "GOLD_MAKING_DEFAULT",
                        'value="general"']:
            self.assertIn(marker, html)
        self.assertNotIn("inputs[2]", html)  # old broken positional JS gone

    def test_mixed_metals_gst(self):
        self.login()
        with app.app_context():
            c = self.client_id
        r = self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "description": ["Gold ring", "Box"], "quantity": ["1", "2"],
            "unit_price": ["60000.0", "100.0"], "item_type": ["gold", "general"],
            "weight": ["10.0", "0"], "making_charges": ["5", "0"],
            "product_code": ["", ""],
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            inv = Invoice.query.first()
            # gold line 63000 + general 200 = 63200; GST 3% = 1896; total 65096
            self.assertAlmostEqual(inv.subtotal, 6320000)
            self.assertEqual(inv.gst_rate, 3.0)
            self.assertAlmostEqual(inv.total, 6509600)

    def test_silver_making_math(self):
        self.login()
        with app.app_context():
            c = self.client_id
        r = self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "description": ["Silver coin"], "quantity": ["2"],
            "unit_price": ["1600.0"], "item_type": ["silver"],
            "weight": ["20.0"], "making_charges": ["150"],
            "product_code": [""],
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            inv = Invoice.query.first()
            # base 3200 + (150/10)*20*2=600 → 3800; GST 3% = 114; total 3914
            self.assertAlmostEqual(inv.subtotal, 380000)
            self.assertAlmostEqual(inv.total, 391400)

    def test_pay_remaining_fifo(self):
        self.login()
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="partial")
        with app.app_context():
            inv = Invoice.query.first()
            iid = inv.id
        r = self.app.post(f"/clients/{self.client_id}/pay-remaining",
                          data={"amount": "64890", "payment_method": "cash",
                                "notes": ""}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            self.assertEqual(Invoice.query.get(iid).status, "paid")
            c = Client.query.get(self.client_id)
            self.assertAlmostEqual(c.balance, 0)

    def test_unauthenticated_posts_denied(self):
        for url in ["/invoices/1/delete", "/products/1/delete",
                    "/clients/1/delete", "/clients/add", "/products/add"]:
            r = self.app.post(url, follow_redirects=False)
            self.assertIn(r.status_code, (302, 400), url)

    def test_security_headers(self):
        self.login()
        r = self.app.get("/")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")

    def test_status_follows_money_not_dropdown(self):
        # Status=Paid + partial must create a PENDING invoice (was: stuck paid)
        self.login()
        with app.app_context():
            c = self.client_id
        self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "paid",
            "payment_type": "partial", "payment_method": "cash",
            "partial_amount": "47445",
            "description": ["Gold"], "quantity": ["1"],
            "unit_price": ["60000.0"], "item_type": ["gold"],
            "weight": ["10.0"], "making_charges": ["5"], "product_code": [""],
        }, follow_redirects=True)
        with app.app_context():
            inv = Invoice.query.first()
            self.assertEqual(inv.status, "pending")
            self.assertAlmostEqual(inv.total, 6489000)
        # ...and pay-remaining must then clear it in full
        r = self.app.post(f"/clients/{c}/pay-remaining",
                          data={"amount": "17445", "payment_method": "UPI",
                                "notes": ""}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            from app import invoice_paid_total
            inv = Invoice.query.first()
            self.assertAlmostEqual(invoice_paid_total(inv.id), 6489000)
            self.assertEqual(inv.status, "paid")
            self.assertAlmostEqual(Client.query.get(c).balance, 0)
            self.assertEqual(inv.invoice_payments[-1].payment_method, "UPI")

    def test_pay_remaining_collects_paid_with_balance(self):
        # Legacy stuck row: status paid, money still owed
        self.login()
        with app.app_context():
            c = Client.query.get(self.client_id)
            c.total_purchases, c.total_payments, c.balance = 6489000, 4744500, 1744500
            inv = Invoice(invoice_number="INV-0001", client_id=c.id,
                          status="paid", subtotal=6300000, gst_rate=3.0,
                          gst_amount=189000, total=6489000)
            db.session.add(inv)
            db.session.flush()
            db.session.add(InvoiceItem(
                invoice_id=inv.id, description="Gold", item_type="gold",
                quantity=1, weight=10.0, making_charges=5.0,
                unit_price=6000000, line_total=6300000))
            db.session.add(Payment(invoice_id=inv.id, client_id=c.id,
                                   amount=4744500, payment_method="cash"))
            db.session.commit()
            iid = inv.id
            cid = c.id
        r = self.app.get(f"/clients/{cid}/pay-remaining")
        self.assertIn("INV-0001", r.data.decode())
        self.app.post(f"/clients/{cid}/pay-remaining",
                      data={"amount": "17445", "payment_method": "cash",
                            "notes": ""}, follow_redirects=True)
        with app.app_context():
            from app import invoice_paid_total
            self.assertAlmostEqual(invoice_paid_total(iid), 6489000)
            self.assertAlmostEqual(Client.query.get(cid).balance, 0)

    def test_tag_price_is_optional_and_scanner_all_formats(self):
        """A tag shows a price only when its design asks for one."""
        self.login()
        bc = self._make_product()
        with app.app_context():
            p = Product.query.filter_by(barcode=bc).first()
            pid = p.id
        tag = self.app.get(f"/products/{pid}/tag").data.decode()
        self.assertIn(bc, tag)          # the code always prints
        with app.app_context():
            tid = TagTemplate.query.filter_by(scope="default").first().id
        self.app.post("/tags/save", data={
            "tpl_id": str(tid), "name": "No price", "scope": "default",
            "shape": "rectangle", "width_mm": "50", "height_mm": "25",
            "style": "classic", "border": "solid", "font_name": "10",
            "font_detail": "8", "copies": "1", "is_thermal": "on",
            "show_name": "on", "show_code": "on"}, follow_redirects=True)
        tag = self.app.get(f"/products/{pid}/tag").data.decode()
        self.assertNotIn("₹", tag)
        self.assertIn(bc, tag)
        inv = self.app.get("/invoices/create").data.decode()
        self.assertIn("new BarcodeDetector()", inv)

    # ---- tag shapes, sizes and the metal/weight line ------------------

    def _default_design_id(self):
        """The built-in default design. The app seeds it on first use, so ask
        for a page that seeds it before reaching into the table."""
        self.app.get("/tags")
        with app.app_context():
            return TagTemplate.query.filter_by(scope="default").first().id

    def _save_design(self, tid, **overrides):
        data = {"tpl_id": str(tid), "name": "Test design", "scope": "default",
                "category": "", "width_mm": "50", "height_mm": "25",
                "style": "classic", "border": "solid", "font_name": "10",
                "font_detail": "8", "copies": "1", "is_thermal": "on",
                "shape": "rectangle", "tail_mm": "16", "tail_h_mm": "0",
                "tail_pos": "middle", "tail_hole_mm": "0",
                "pad_mm": "1.4", "pad_h_mm": "2", "gap_mm": "0.5",
                "qr_mm": "0", "bc_h_mm": "0", "bc_w_pct": "100",
                "show_name": "on", "show_category": "on", "show_metal": "on",
                "show_weight": "on", "show_price": "on", "show_code": "on"}
        data.update({k: str(v) for k, v in overrides.items()})
        for k, v in list(data.items()):
            if v is None:
                del data[k]              # None = leave the box unticked
        return self.app.post("/tags/save", data=data, follow_redirects=True)

    def test_only_two_tag_shapes_exist(self):
        """Rectangle and rectangle-with-tail — nothing else is offered, and
        the retired 'hangtag' value still saves as a tail tag."""
        self.login()
        page = self.app.get("/tags").data.decode()
        shapes = set(re.findall(r'data-shape="([^"]+)"', page))
        self.assertEqual(shapes, {"rectangle", "tail"})
        self.assertNotIn('data-shape="hangtag"', page)
        with app.app_context():
            tid = TagTemplate.query.filter_by(scope="default").first().id
        self._save_design(tid, shape="hangtag")
        with app.app_context():
            self.assertEqual(
                TagTemplate.query.get(tid).shape, "tail")

    def test_tail_only_draws_on_a_tail_design(self):
        self.login()
        bc = self._make_product(name="TailProbe")
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="rectangle")
        rect = self.app.get(f"/products/{pid}/tag").data.decode()
        self.assertIn("jt-shape-rectangle", rect)
        # the stylesheet always mentions .jt-tail; the *markup* must not
        self.assertNotIn('class="jt-tail"', rect)
        self._save_design(tid, shape="tail", tail_mm="20", tail_hole_mm="4")
        tail = self.app.get(f"/products/{pid}/tag").data.decode()
        self.assertIn("jt-shape-tail", tail)
        self.assertIn('class="jt-tail"', tail)
        self.assertIn('class="jt-hole"', tail)
        self.assertIn("--tail:20.0mm", tail)

    def test_tag_size_and_spacing_controls_reach_the_print(self):
        """Every size knob the shopkeeper sets must survive to the page."""
        self.login()
        bc = self._make_product(name="SizeProbe")
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="tail", width_mm="52", height_mm="24",
                          tail_mm="18", tail_h_mm="6", tail_pos="bottom",
                          pad_mm="3", pad_h_mm="4.5", gap_mm="1.25",
                          qr_mm="15", bc_h_mm="9.5", bc_w_pct="60")
        page = self.app.get(f"/products/{pid}/tag").data.decode()
        for fragment in ("--pad:3.0mm", "--padh:4.5mm", "--gaph:1.25mm",
                         "--qr:15.0mm", "--bch:9.5mm", "--bcw:60.0%",
                         "--tail:18.0mm", "--tailh:6.0mm",
                         "--tailpos:flex-end"):
            self.assertIn(fragment, page)
        # the tail is extra paper, so it widens the page, not the body
        self.assertIn("size: 70.0mm 24.0mm", page)

    def test_auto_sizes_scale_with_the_tag(self):
        """0 means auto, and auto is derived from the tag height."""
        self.login()
        bc = self._make_product(name="AutoProbe")
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="tail", width_mm="50", height_mm="20",
                          qr_mm="0", bc_h_mm="0", tail_h_mm="0")
        page = self.app.get(f"/products/{pid}/tag").data.decode()
        self.assertIn("--qr:11.0mm", page)    # 20 * 0.55
        self.assertIn("--bch:5.6mm", page)    # 20 * 0.28
        self.assertIn("--tailh:8.0mm", page)  # 20 * 0.40

    def test_tag_prints_metal_and_weight(self):
        """The reason a jewellery tag exists: metal and grams."""
        self.login()
        bc = self._make_product(name="MetalProbe", w=7.5)
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="rectangle")
        page = self.app.get(f"/products/{pid}/tag").data.decode()
        line = re.search(r'<div class="jt-line">(.*?)</div>', page, re.S).group(1)
        spans = re.findall(r"<span>(.*?)</span>", line)
        self.assertIn("Gold", spans)
        self.assertIn("7.5 g", spans)
        # and turning them off really does remove them
        self._save_design(tid, shape="rectangle", show_metal=None,
                          show_weight=None)
        page = self.app.get(f"/products/{pid}/tag").data.decode()
        line = re.search(r'<div class="jt-line">(.*?)</div>', page, re.S).group(1)
        spans = re.findall(r"<span>(.*?)</span>", line)
        self.assertNotIn("Gold", spans)
        self.assertNotIn("7.5 g", spans)

    def test_default_design_shows_metal_and_weight(self):
        """A shop that never opens the designer must still get a usable tag."""
        with app.app_context():
            db.session.query(TagTemplate).delete()
            db.session.commit()
            from app import _flash_tag_default
            _flash_tag_default()
            t = TagTemplate.query.filter_by(scope="default").first()
            self.assertIsNotNone(t)
            for flag in ("show_name", "show_category", "show_metal",
                         "show_weight", "show_price", "show_code"):
                self.assertTrue(getattr(t, flag), f"{flag} should default on")
            self.assertEqual(t.shape, "rectangle")
            self.assertGreaterEqual(t.pad_mm, 0)

    def test_designer_warns_before_printing_a_blank_tag(self):
        self.login()
        page = self.app.get("/tags").data.decode()
        self.assertIn('id="empty-warn"', page)
        self.assertIn("setAllFields(true)", page)
        self.assertIn("setEssentialFields()", page)
        self.assertIn('id="nofit-warn"', page)

    # ---- tag length and actual-size printing --------------------------

    def _tag_vars(self, html):
        """The mm geometry the tag markup actually carries."""
        blob = re.search(r'style="--jn(.*?)"', html, re.S).group(1)
        return {k: float(re.search(rf"--{k}:([\d.]+)mm", blob).group(1))
                for k in ("tw", "th", "tail", "tailh")}

    def _page_size(self, html):
        body = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
        m = re.search(r"@page\s*\{([^}]*)\}", body)
        return m.group(1).strip() if m else ""

    def test_tag_length_pins_the_physical_size(self):
        """The shopkeeper's own label: 50 mm body, 10 mm tall, 70 mm long."""
        self.login()
        bc = self._make_product(name="LenProbe")
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="tail", width_mm="50", height_mm="10",
                          tail_mm="20", total_mm="70")
        with app.app_context():
            geo = resolved_tag_geometry(TagTemplate.query.get(tid))
        self.assertEqual(geo, {"shape": "tail", "body": 50.0, "height": 10.0,
                               "tail": 20.0, "total": 70.0})
        page = self.app.get(f"/products/{pid}/tag").data.decode()
        self.assertIn("size: 70.0mm 10.0mm", self._page_size(page))
        self.assertIn("70.0 × 10.0 mm", page)     # told to the shopkeeper too

    def test_a_pinned_length_survives_a_body_change(self):
        """Change the body and the tail absorbs it — the label is still 70 mm."""
        self.login()
        bc = self._make_product(name="PinProbe")
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="tail", width_mm="50", height_mm="10",
                          tail_mm="20", total_mm="70")
        page = self.app.get(f"/products/{pid}/tag?w=40").data.decode()
        v = self._tag_vars(page)
        self.assertEqual((v["tw"], v["tail"]), (40.0, 30.0))
        self.assertIn("size: 70.0mm 10.0mm", self._page_size(page))

    def test_size_form_can_release_a_pinned_length(self):
        """total=0 must clear the pin, not clamp up to the minimum."""
        self.login()
        bc = self._make_product(name="RelProbe")
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="tail", width_mm="50", height_mm="10",
                          tail_mm="20", total_mm="70")
        page = self.app.get(f"/products/{pid}/tag?total=0&tail=25").data.decode()
        v = self._tag_vars(page)
        self.assertEqual(v["tail"], 25.0)
        self.assertIn("size: 75.0mm 10.0mm", self._page_size(page))

    def test_a_length_shorter_than_the_body_cannot_eat_the_tag(self):
        self.login()
        bc = self._make_product(name="ShortProbe")
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="tail", width_mm="50", height_mm="10",
                          total_mm="20")
        v = self._tag_vars(self.app.get(f"/products/{pid}/tag").data.decode())
        self.assertEqual(v["tail"], 4.0)          # the floor, not a negative tail

    def test_sheet_layout_does_not_shrink_the_whole_sheet(self):
        """Regression: @page was pinned to one tag's size even for a sheet, so
        an A4 page of tags printed squeezed onto 71 x 22 mm — the "garbage
        print". The sheet layout must leave the paper size alone."""
        self.login()
        self._make_product(name="SheetProbe")     # a batch needs something in it
        tid = self._default_design_id()
        self._save_design(tid, shape="tail", width_mm="50", height_mm="10",
                          total_mm="70")
        roll = self.app.get("/tags/print?layout=roll").data.decode()
        sheet = self.app.get("/tags/print?layout=sheet").data.decode()
        self.assertIn("size: 70.0mm 10.0mm", self._page_size(roll))
        self.assertNotIn("size:", self._page_size(sheet))

    def test_print_css_cannot_shave_the_tag_edges(self):
        """Regression: .sheet kept its screen padding in print, pushing a
        full-width tag off its own page so both edges were cut away."""
        self.login()
        self._make_product(name="EdgeProbe")
        page = self.app.get("/tags/print?layout=roll").data.decode()
        for rule in ("padding: 0 !important",
                     "justify-content: flex-start !important",
                     "print-color-adjust: exact"):
            self.assertIn(rule, page)

    def test_calibration_ruler_matches_the_tag(self):
        self.login()
        page = self.app.get("/tags/calibrate?w=70").data.decode()
        self.assertIn("width: 70.0mm", page)
        self.assertIn("size: 88.0mm 30mm", self._page_size(page))
        self.assertEqual(self.app.get("/tags/calibrate").status_code, 200)

    def test_print_pages_state_the_paper_size(self):
        self.login()
        bc = self._make_product(name="PaperProbe")
        with app.app_context():
            pid = Product.query.filter_by(barcode=bc).first().id
        tid = self._default_design_id()
        self._save_design(tid, shape="tail", width_mm="50", height_mm="10",
                          total_mm="70")
        page = self.app.get(f"/products/{pid}/tag").data.decode()
        self.assertIn("70.0 × 10.0 mm", page)
        self.assertIn("Scale <b>100%</b>", page)
        self.assertIn("Headers and footers", page)
        self.assertIn('name="total"', page)


    def test_settings_price_change_reprices_all_instantly(self):
        self.login()
        bc_g = self._make_product(name="GRing", w=10.0, stock=5, t="gold")
        bc_s = self._make_product(name="SCoin", w=100.0, stock=3, t="silver")
        # snapshot a past invoice at old prices
        self._post_invoice(bc_g, qty=1, price=60000.0, ptype="partial")
        with app.app_context():
            old_total = Invoice.query.first().total
        r = self.app.post("/settings", data={
            "firm_name": "Test", "gold_price": "7000", "silver_price": "100",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            g = Product.query.filter_by(barcode=bc_g).first()
            s = Product.query.filter_by(barcode=bc_s).first()
            self.assertAlmostEqual(g.unit_price, 10.0 * 700000)
            self.assertAlmostEqual(s.unit_price, 100.0 * 10000)
            # past invoice untouched
            self.assertAlmostEqual(Invoice.query.first().total, old_total)

    def test_settings_rejects_zero_price(self):
        self.login()
        r = self.app.post("/settings", data={
            "firm_name": "Test", "gold_price": "0", "silver_price": "100",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            self.assertAlmostEqual(Settings.query.first().gold_manual_price, 600000)


    def test_discount_percent_and_rates(self):
        self.login()
        bc = self._make_product()
        with app.app_context():
            c = self.client_id
        self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "discount_type": "percent", "discount_value": "10",
            "description": ["Gold ring"], "quantity": ["1"],
            "unit_price": ["60000.0"], "item_type": ["gold"],
            "weight": ["10.0"], "making_charges": ["5"], "product_code": [bc],
        }, follow_redirects=True)
        with app.app_context():
            inv = Invoice.query.first()
            # 63000 − 10% (6300) = 56700; GST 3% = 1701; total 58401
            self.assertAlmostEqual(inv.discount_amount, 630000)
            self.assertAlmostEqual(inv.total, 5840100)
            self.assertAlmostEqual(inv.gold_rate, 600000)
            self.assertAlmostEqual(inv.silver_rate, 8000)
            iid = inv.id
        html = self.app.get(f"/invoices/{iid}").data.decode()
        self.assertIn("6000.00", html)
        self.assertIn("6300.00", html)

    def test_discount_flat_and_invalid_rejected(self):
        self.login()
        with app.app_context():
            c = self.client_id
        base = {"client_id": str(c), "status": "pending",
                "payment_type": "partial", "payment_method": "cash",
                "description": ["Box"], "quantity": ["2"],
                "unit_price": ["100.0"], "item_type": ["general"],
                "weight": ["0"], "making_charges": ["0"], "product_code": [""]}
        self.app.post("/invoices/create",
                      data=dict(base, discount_type="flat",
                                discount_value="50"),
                      follow_redirects=True)
        with app.app_context():
            inv = Invoice.query.first()
            self.assertAlmostEqual(inv.total, 15000)  # 200−50, no GST
        # 150% must be rejected, invoice count unchanged
        self.app.post("/invoices/create",
                      data=dict(base, discount_type="percent",
                                discount_value="150"),
                      follow_redirects=True)
        with app.app_context():
            self.assertEqual(Invoice.query.count(), 1)

    def test_old_db_gains_new_columns(self):
        # simulate a pre-feature DB: invoice table without new columns
        from app import ensure_schema, db as _db
        with app.app_context():
            _db.session.execute(_db.text(
                "CREATE TABLE IF NOT EXISTS oldcheck (id INTEGER PRIMARY KEY)"))
            import app as appmod
            appmod._SCHEMA_OK = False
            ensure_schema()  # idempotent, must not raise
            ensure_schema()
            cols = {col["name"] for col in
                    __import__("sqlalchemy").inspect(_db.engine).get_columns("invoice")}
            for col in ("gold_rate", "silver_rate", "discount_type",
                        "discount_value", "discount_amount"):
                self.assertIn(col, cols)


    def test_gst_toggle_off_bills_tax_free(self):
        self.login()
        # turn GST off (checkbox unchecked => field absent)
        self.app.post("/settings", data={
            "firm_name": "Test", "gold_price": "6000", "silver_price": "80",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150"}, follow_redirects=True)
        with app.app_context():
            self.assertFalse(Settings.query.first().gst_enabled)
            c = self.client_id
        bc = self._make_product()
        self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "description": ["Gold ring"], "quantity": ["1"],
            "unit_price": ["60000.0"], "item_type": ["gold"],
            "weight": ["10.0"], "making_charges": ["5"], "product_code": [bc],
        }, follow_redirects=True)
        with app.app_context():
            inv = Invoice.query.first()
            self.assertEqual(inv.gst_rate, 0.0)
            self.assertAlmostEqual(inv.total, 6300000)
        html = self.app.get("/invoices/create").data.decode()
        self.assertIn("var GST_ENABLED = false", html)
        # turn back on
        self.app.post("/settings", data={
            "firm_name": "Test", "gold_price": "6000", "silver_price": "80",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150",
            "gst_enabled": "on"}, follow_redirects=True)
        with app.app_context():
            self.assertTrue(Settings.query.first().gst_enabled)


    def test_reports_filters_and_csv(self):
        self.login()
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        r = self.app.get("/reports?status=paid")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn("Metal-wise", html)
        self.assertIn("Collected", html)
        r = self.app.get("/reports?status=pending")
        self.assertIn("No bills in this period", r.data.decode())
        r = self.app.get("/reports.csv?status=paid")
        self.assertEqual(r.status_code, 200)
        self.assertIn("INV-", r.data.decode())
        self.assertIn("text/csv", r.content_type)

    def test_clients_search_and_account_page(self):
        self.login()
        self.app.post("/clients/add",
                      data={"name": "Ramesh Kumar", "phone": "9876543210"},
                      follow_redirects=True)
        r = self.app.get("/clients?q=ramesh")
        self.assertIn("Ramesh Kumar", r.data.decode())
        r = self.app.get("/clients?q=zzzznope")
        self.assertIn('No match for', r.data.decode())
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="partial")
        with app.app_context():
            iid = Invoice.query.first().id
        # a real payment so the method breakdown renders
        self.app.post(f"/invoices/{iid}/payment",
                      data={"amount": "1000", "payment_method": "UPI",
                            "notes": ""}, follow_redirects=True)
        with app.app_context():
            cid = Client.query.filter_by(phone="1111111111").first().id
        # move the invoice to Ramesh for a non-trivial account page
        with app.app_context():
            inv = Invoice.query.first()
            newc = Client.query.filter_by(phone="9876543210").first()
            inv.client_id = newc.id
            for p in Payment.query.filter_by(invoice_id=inv.id).all():
                p.client_id = newc.id
            newc.total_purchases += inv.total
            newc.balance = newc.total_purchases - newc.total_payments
            db.session.commit()
            cid = newc.id
        html = self.app.get(f"/clients/{cid}").data.decode()
        self.assertIn("Metal Purchase History", html)
        self.assertIn("Gold", html)
        self.assertIn("10.0g", html)
        self.assertIn("Paid via", html)

    def test_invoice_whatsapp_gst_hidden_no_duedate(self):
        self.login()
        self.app.post("/settings", data={
            "firm_name": "Test", "gold_price": "6000", "silver_price": "80",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150"}, follow_redirects=True)
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        with app.app_context():
            iid = Invoice.query.first().id
        html = self.app.get(f"/invoices/{iid}").data.decode()
        self.assertIn("wa.me/", html)
        self.assertIn("Share on WhatsApp", html)
        self.assertNotIn("Due Date", html)
        self.assertNotIn("GST (", html)  # GST off: no mention
        # flip GST on: row returns
        self.app.post("/settings", data={
            "firm_name": "Test", "gold_price": "6000", "silver_price": "80",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150",
            "gst_enabled": "on"}, follow_redirects=True)
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        with app.app_context():
            iid2 = Invoice.query.order_by(Invoice.id.desc()).first().id
        html2 = self.app.get(f"/invoices/{iid2}").data.decode()
        self.assertIn("GST (3.0%)", html2)

    def test_scanner_layered_fallbacks(self):
        self.login()
        html = self.app.get("/invoices/create").data.decode()
        self.assertIn("/static/js/jsqr.min.js", html)
        self.assertIn("decodeImageVariants", html)
        self.assertIn("loadQuagga", html)
        self.assertIn("scanDiagnosis", html)
        self.assertIn("isSecureContext", html)
        self.assertNotIn("scanLoop(det, video", html)


    def test_bills_search_and_shop_address(self):
        self.login()
        self.app.post("/settings", data={
            "firm_name": "Test", "firm_phone": "123",
            "firm_address": "12 Main Road, Mumbai", "firm_email": "s@test.com",
            "gold_price": "6000", "silver_price": "80",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150",
            "gst_enabled": "on"}, follow_redirects=True)
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        with app.app_context():
            iid = Invoice.query.first().id
            inv_no = Invoice.query.first().invoice_number
        r = self.app.get(f"/invoices?q={inv_no}")
        self.assertIn(inv_no, r.data.decode())
        r = self.app.get("/invoices?q=zzzznothing")
        self.assertIn("No match for", r.data.decode())
        html = self.app.get(f"/invoices/{iid}").data.decode()
        self.assertIn("12 Main Road, Mumbai", html)
        self.assertIn("2px solid #000", html)


    def test_old_gold_exchange_deducted(self):
        self.login()
        bc = self._make_product()
        with app.app_context():
            c = self.client_id
        self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "description": ["Gold ring"], "quantity": ["1"],
            "unit_price": ["60000.0"], "item_type": ["gold"],
            "weight": ["10.0"], "making_charges": ["5"], "product_code": [bc],
            "exchange_metal": "gold", "exchange_amount": "12000",
        }, follow_redirects=True)
        with app.app_context():
            inv = Invoice.query.first()
            # 63000 + 1890 GST − 12000 exchange = 52890
            self.assertAlmostEqual(inv.exchange_amount, 1200000)
            self.assertEqual(inv.exchange_metal, "gold")
            self.assertAlmostEqual(inv.total, 5289000)
            iid = inv.id
        html = self.app.get(f"/invoices/{iid}").data.decode()
        self.assertIn("Old Gold Exchange", html)
        self.assertIn("12000.00", html)

    def test_exchange_bigger_than_bill_rejected(self):
        self.login()
        with app.app_context():
            c = self.client_id
        self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "description": ["Box"], "quantity": ["1"],
            "unit_price": ["100.0"], "item_type": ["general"],
            "weight": ["0"], "making_charges": ["0"], "product_code": [""],
            "exchange_metal": "gold", "exchange_amount": "60000",
        }, follow_redirects=True)
        with app.app_context():
            self.assertEqual(Invoice.query.count(), 0)


    def test_responsive_stock_and_dark_invoice(self):
        self.login()
        # responsive shell
        base = self.app.get("/").data.decode()
        self.assertIn("nav-toggle", base)
        self.assertIn("@media (max-width: 768px)", base)
        self.assertIn("item-row { grid-template-columns", base)
        # stock: 10g x 5pcs gold + 100g x 3pcs silver
        self._make_product(name="GRing", w=10.0, stock=5, t="gold")
        self._make_product(name="SCoin", w=100.0, stock=3, t="silver")
        html = self.app.get("/products").data.decode()
        self.assertIn("5 pcs", html)
        self.assertIn("50.0 g in stock", html)   # 10*5
        self.assertIn("300.0 g in stock", html)  # 100*3
        self.assertIn("8 pcs", html)
        self.assertIn("350.0 g in stock", html)
        self.assertIn("2 items", html)
        # dark 14px invoice
        self._post_invoice("BC-GRing", qty=1, price=60000.0, ptype="full")
        with app.app_context():
            iid = Invoice.query.first().id
        inv = self.app.get(f"/invoices/{iid}").data.decode()
        self.assertIn("color: #000", inv)
        self.assertIn("font-size: 14px", inv)


    def test_category_and_metal_sections(self):
        self.login()
        self.app.post("/products/add", data={
            "name": "Band", "item_type": "gold", "category": "Handring",
            "unit": "pcs", "weight_per_unit": "5.0", "stock_quantity": "2",
            "cost_price": "20000", "custom_price": "0"}, follow_redirects=True)
        html = self.app.get("/products").data.decode()
        self.assertIn("Handring", html)
        self.assertIn("Out of Stock", html)
        r = self.app.get("/products?cat=Handring")
        self.assertIn("Band", r.data.decode())
        r = self.app.get("/products?cat=Chain")
        self.assertNotIn("Band", r.data.decode())

    def test_custom_price_skips_auto_reprice(self):
        self.login()
        self.app.post("/products/add", data={
            "name": "Fixed", "item_type": "gold", "category": "",
            "unit": "pcs", "weight_per_unit": "10.0", "stock_quantity": "1",
            "cost_price": "50000", "custom_price": "99999"},
            follow_redirects=True)
        self.app.post("/settings", data={
            "firm_name": "T", "gold_price": "7000", "silver_price": "100",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150",
            "gst_enabled": "on"}, follow_redirects=True)
        with app.app_context():
            p = Product.query.filter_by(name="Fixed").first()
            self.assertAlmostEqual(p.unit_price, 9999900)

    def test_weighed_sale_fractional_qty(self):
        self.login()
        self.app.post("/products/add", data={
            "name": "Pearls", "item_type": "gold", "category": "Pearls",
            "unit": "g", "weight_per_unit": "1.0", "stock_weight": "50.0",
            "cost_price": "0", "custom_price": "0"}, follow_redirects=True)
        with app.app_context():
            p = Product.query.filter_by(name="Pearls").first()
            bc = p.barcode
            c = self.client_id
        self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "description": ["Pearls"], "quantity": ["2.5"],
            "unit_price": ["6000.0"], "item_type": ["gold"],
            "weight": ["2.5"], "making_charges": ["0"],
            "product_code": [bc]}, follow_redirects=True)
        with app.app_context():
            p = Product.query.filter_by(name="Pearls").first()
            self.assertAlmostEqual(p.stock_weight, 47.5)
            it = InvoiceItem.query.first()
            self.assertAlmostEqual(it.quantity, 2.5)

    def test_fractional_pieces_rejected(self):
        self.login()
        bc = self._make_product()
        with app.app_context():
            c = self.client_id
        self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "description": ["Ring"], "quantity": ["1.5"],
            "unit_price": ["60000.0"], "item_type": ["gold"],
            "weight": ["10.0"], "making_charges": ["5"],
            "product_code": [bc]}, follow_redirects=True)
        with app.app_context():
            self.assertEqual(Invoice.query.count(), 0)

    def test_making_column_toggle(self):
        self.login()
        self.app.post("/settings", data={
            "firm_name": "T", "gold_price": "6000", "silver_price": "80",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150",
            "gst_enabled": "on", "show_making_charges": "on"},
            follow_redirects=True)
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        with app.app_context():
            iid = Invoice.query.first().id
        html = self.app.get(f"/invoices/{iid}").data.decode()
        self.assertIn("<th>Making</th>", html)
        self.assertIn("SHOW_MAKING = true", self.app.get(
            "/invoices/create").data.decode())
        # now hide it
        self.app.post("/settings", data={
            "firm_name": "T", "gold_price": "6000", "silver_price": "80",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150",
            "gst_enabled": "on"}, follow_redirects=True)
        html = self.app.get(f"/invoices/{iid}").data.decode()
        self.assertNotIn("<th>Making</th>", html)
        self.assertIn("SHOW_MAKING = false", self.app.get(
            "/invoices/create").data.decode())

    def test_metal_rate_column_on_bill(self):
        self.login()
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        with app.app_context():
            iid = Invoice.query.first().id
        html = self.app.get(f"/invoices/{iid}").data.decode()
        self.assertIn("Metal Rate", html)
        self.assertIn("6000.00/g", html)

    def test_dashboard_quick_price(self):
        self.login()
        bc = self._make_product()
        self.app.post("/prices/quick",
                      data={"gold_price": "8000", "silver_price": "90"},
                      follow_redirects=True)
        with app.app_context():
            p = Product.query.filter_by(barcode=bc).first()
            self.assertAlmostEqual(p.unit_price, 8000000)
        self.assertIn("dash-gold", self.app.get("/").data.decode())

    def test_client_sort_by_balance(self):
        self.login()
        self.app.post("/clients/add", data={"name": "Rich", "phone": "90001"},
                      follow_redirects=True)
        with app.app_context():
            rich = Client.query.filter_by(phone="90001").first()
            rich.balance = 5000
            db.session.commit()
        html = self.app.get("/clients?sort=balance_desc").data.decode()
        self.assertLess(html.index("Rich"), html.index("C1"))

    def test_reports_profit_gst_stock(self):
        self.login()
        self.app.post("/products/add", data={
            "name": "Band", "item_type": "gold", "category": "",
            "unit": "pcs", "weight_per_unit": "10.0", "stock_quantity": "5",
            "cost_price": "50000", "custom_price": "0"},
            follow_redirects=True)
        with app.app_context():
            bc = Product.query.filter_by(name="Band").first().barcode
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        html = self.app.get("/reports").data.decode()
        self.assertIn("Gross Profit", html)
        self.assertIn("Net Profit", html)
        self.assertIn("GST collected", html)
        self.assertIn("Stock position", html)
        # revenue 64890, gst 1890, cost 50000 → gross 13000, net 13000
        self.assertIn("13000", html.replace(",", ""))

    def test_old_invoice_item_rebuilt_to_float(self):
        import sqlalchemy as sa
        with app.app_context():
            db.session.execute(db.text("DROP TABLE IF EXISTS invoice_item"))
            db.session.execute(db.text(
                "CREATE TABLE invoice_item (id INTEGER PRIMARY KEY,"
                " invoice_id INTEGER NOT NULL, product_id INTEGER,"
                " description VARCHAR(200) NOT NULL,"
                " item_type VARCHAR(20), quantity INTEGER DEFAULT 1,"
                " weight FLOAT DEFAULT 0, making_charges FLOAT DEFAULT 0,"
                " unit_price FLOAT NOT NULL, line_total FLOAT DEFAULT 0)"))
            db.session.execute(db.text(
                "INSERT INTO invoice_item (id, invoice_id, description,"
                " item_type, quantity, unit_price, line_total)"
                " VALUES (1, 1, 'Old', 'gold', 2, 100.0, 200.0)"))
            db.session.commit()
            import app as appmod
            appmod._SCHEMA_OK = False
            from app import ensure_schema
            ensure_schema()
            cols = {c["name"]: c for c in
                    sa.inspect(db.engine).get_columns("invoice_item")}
            self.assertIn("FLOAT", str(type(cols["quantity"]["type"])).upper())
            it = InvoiceItem.query.get(1)
            self.assertEqual(it.quantity, 2)
            self.assertEqual(it.description, "Old")


    def test_cancel_reverses_everything(self):
        self.login()
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        with app.app_context():
            iid = Invoice.query.first().id
            c = self.client_id
        r = self.app.post(f"/invoices/{iid}/update_status",
                          data={"status": "cancelled"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            inv = Invoice.query.get(iid)
            self.assertEqual(inv.status, "cancelled")
            self.assertEqual(Product.query.filter_by(barcode=bc).first().stock_quantity, 5)
            cl = Client.query.get(c)
            self.assertEqual(cl.total_purchases, 0)
            self.assertEqual(cl.total_payments, 0)
            self.assertEqual(cl.balance, 0)
            self.assertEqual(Payment.query.filter_by(invoice_id=iid).count(), 0)
        # reopen blocked
        self.app.post(f"/invoices/{iid}/update_status",
                      data={"status": "paid"}, follow_redirects=True)
        with app.app_context():
            self.assertEqual(Invoice.query.get(iid).status, "cancelled")

    def test_login_throttle(self):
        for _ in range(10):
            r = self.app.post("/login", data={"username": "nope",
                                              "password": "wrong"},
                              follow_redirects=True)
            self.assertIn("Invalid username", r.data.decode())
        r = self.app.post("/login", data={"username": "nope",
                                          "password": "wrong"},
                          follow_redirects=True)
        self.assertIn("Too many attempts", r.data.decode())

    def test_server_side_pricing_ignores_tampered_price(self):
        self.login()
        bc = self._make_product()
        with app.app_context():
            c = self.client_id
        # attacker submits Rs.1 for a Rs.60000 item with a valid barcode
        self.app.post("/invoices/create", data={
            "client_id": str(c), "status": "pending",
            "payment_type": "partial", "payment_method": "cash",
            "description": ["Gold ring"], "quantity": ["1"],
            "unit_price": ["1.0"], "item_type": ["gold"],
            "weight": ["10.0"], "making_charges": ["5"],
            "product_code": [bc]}, follow_redirects=True)
        with app.app_context():
            inv = Invoice.query.first()
            self.assertAlmostEqual(inv.subtotal, 6300000)
            self.assertEqual(inv.items[0].unit_price, 6000000)

    def test_invoice_numbers_unique_and_sequential(self):
        self.login()
        bc = self._make_product()
        for _ in range(3):
            self._post_invoice(bc, qty=1, price=60000.0, ptype="partial")
        with app.app_context():
            nums = [i.invoice_number for i in
                    Invoice.query.order_by(Invoice.id).all()]
            self.assertEqual(nums, ["INV-0001", "INV-0002", "INV-0003"])

    def test_paise_exactness_no_float_dust(self):
        from app import calc_line_total
        with app.app_context():
            s = Settings.query.first()
            total, _, _ = calc_line_total(3, 1999, "general", 0, 0, s)
            self.assertEqual(total, 5997)
            self.assertIsInstance(total, int)
        self.login()
        # fractional rupees stored exactly
        self.app.post("/settings", data={
            "firm_name": "T", "gold_price": "6000.55", "silver_price": "80",
            "gold_making_charge_percent": "5",
            "silver_making_charge_per_10gm": "150",
            "gst_enabled": "on", "show_making_charges": "on"},
            follow_redirects=True)
        with app.app_context():
            self.assertEqual(Settings.query.first().gold_manual_price, 600055)

    def test_old_float_db_migrates_to_paise(self):
        import sqlalchemy as sa
        with app.app_context():
            for t in ("product_old", "settings_old", "product", "settings"):
                db.session.execute(db.text(f"DROP TABLE IF EXISTS {t}"))
            db.session.execute(db.text(
                "CREATE TABLE product (id INTEGER PRIMARY KEY, name VARCHAR(200),"
                " sku VARCHAR(50), barcode VARCHAR(100), description TEXT,"
                " item_type VARCHAR(20), unit_price FLOAT, stock_quantity INTEGER,"
                " weight_per_unit FLOAT)"))
            db.session.execute(db.text(
                "INSERT INTO product (id, name, sku, item_type, unit_price,"
                " stock_quantity, weight_per_unit)"
                " VALUES (1, 'Old', 'S1', 'gold', 60000.0, 5, 10.0)"))
            db.session.execute(db.text("DROP TABLE IF EXISTS settings"))
            db.session.execute(db.text(
                "CREATE TABLE settings (id INTEGER PRIMARY KEY,"
                " firm_name VARCHAR(200), gold_manual_price FLOAT,"
                " silver_manual_price FLOAT, gold_api_url VARCHAR(300))"))
            db.session.execute(db.text(
                "INSERT INTO settings (id, firm_name, gold_manual_price,"
                " silver_manual_price, gold_api_url)"
                " VALUES (1, 'OldFirm', 6000.0, 80.0, 'http://x')"))
            db.session.commit()
            import app as appmod
            appmod._SCHEMA_OK = False
            from app import ensure_schema
            ensure_schema()
            pcols = {c["name"]: c for c in
                     sa.inspect(db.engine).get_columns("product")}
            self.assertIn("INTEGER", str(type(pcols["unit_price"]["type"])).upper())
            p = Product.query.get(1)
            self.assertEqual(p.unit_price, 6000000)
            self.assertEqual(p.stock_quantity, 5)
            scols = {c["name"] for c in
                     sa.inspect(db.engine).get_columns("settings")}
            self.assertNotIn("gold_api_url", scols)
            s = Settings.query.get(1)
            self.assertEqual(s.gold_manual_price, 600000)

    def test_gstr1_backup_audit_reset_statement_chart(self):
        self.login()
        bc = self._make_product()
        self._post_invoice(bc, qty=1, price=60000.0, ptype="full")
        r = self.app.get("/export/gstr1.csv")
        self.assertEqual(r.status_code, 200)
        body = r.data.decode()
        self.assertIn("gst_rate_pct", body)
        self.assertIn("1890.00", body)
        r = self.app.get("/backup/download", follow_redirects=False)
        self.assertIn(r.status_code, (200, 302))
        r = self.app.post("/backup/restore", data={}, follow_redirects=True)
        self.assertIn("valid .db", r.data.decode())
        r = self.app.get("/audit-logs?q=login")
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            uid = User.query.filter_by(username="testuser").first().id
        r = self.app.post(f"/users/{uid}/reset-password",
                          data={"new_password": "newpass123"},
                          follow_redirects=True)
        self.assertIn("Password reset", r.data.decode())
        self.app.get("/logout", follow_redirects=True)
        r = self.app.post("/login", data={"username": "testuser",
                                          "password": "newpass123"},
                          follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            cid = Client.query.first().id
        r = self.app.get(f"/clients/{cid}/statement")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Account Statement", r.data.decode())
        self.assertIn("last 6 months", self.app.get("/").data.decode().lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
