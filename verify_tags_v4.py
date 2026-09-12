"""Verification for the two-shape / customisable-size tag work.

Run from the app directory:
    <venv>/python verify_tags_v4.py

Checks, in order:
  1. An OLD database (pre-tail schema, holding a shape='hangtag' design)
     migrates cleanly: new columns appear, the design becomes a 'tail' tag and
     keeps its strap length + hole.
  2. /tags renders and offers exactly the two shapes.
  3. tag_art renders both shapes, and the tail only appears on a tail design.
  4. Metal and weight print when the design asks for them.
  5. Padding / spacing / QR size / barcode size actually reach the output.
  6. The designer form round-trips every new field.
  7. /products/<id>/tag renders with the tail in the page size.
"""
import os
import re
import sqlite3
import sys
import tempfile

FAILS = []
PASSES = []


def check(label, ok, detail=""):
    (PASSES if ok else FAILS).append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))


def build_old_db(path):
    """A database exactly as v2 shipped it: no tail_* / pad_* columns, and a
    design using the retired 'hangtag' shape."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE user (
            id INTEGER NOT NULL PRIMARY KEY, username VARCHAR(80) UNIQUE,
            email VARCHAR(120) UNIQUE, password_hash VARCHAR(255),
            is_admin BOOLEAN, created_at DATETIME);
        CREATE TABLE settings (
            id INTEGER NOT NULL PRIMARY KEY, firm_name VARCHAR(120),
            gst_enabled BOOLEAN, show_making_charges BOOLEAN,
            invoice_seq INTEGER, show_gstin BOOLEAN,
            gold_making_mode VARCHAR(10), gold_making_flat INTEGER,
            silver_making_mode VARCHAR(10), silver_making_percent FLOAT,
            gold_manual_price INTEGER, silver_manual_price INTEGER,
            silver_making_charge_per_10gm INTEGER, gst_number VARCHAR(20),
            address TEXT, phone VARCHAR(30));
        CREATE TABLE product (
            id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(200), sku VARCHAR(50),
            barcode VARCHAR(100), description TEXT, item_type VARCHAR(20),
            category VARCHAR(80), unit VARCHAR(10), unit_price INTEGER,
            custom_price INTEGER, cost_price INTEGER, stock_quantity INTEGER,
            stock_weight FLOAT, weight_per_unit FLOAT, created_at DATETIME);
        CREATE TABLE tag_template (
            id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(80), scope VARCHAR(20),
            category VARCHAR(80), width_mm FLOAT, height_mm FLOAT,
            style VARCHAR(20), font_name FLOAT, font_detail FLOAT,
            show_name BOOLEAN, show_sku BOOLEAN, show_category BOOLEAN,
            show_metal BOOLEAN, show_weight BOOLEAN, show_price BOOLEAN,
            show_barcode BOOLEAN, show_qr BOOLEAN, show_firm BOOLEAN,
            show_code BOOLEAN, border VARCHAR(10), note VARCHAR(80),
            copies INTEGER, is_thermal BOOLEAN, shape VARCHAR(10),
            hangtag_strap_mm FLOAT, hangtag_hole_mm FLOAT);
        INSERT INTO user VALUES (1,'admin','admin@example.com','x',1,'2026-01-01');
        INSERT INTO settings (id, firm_name, gst_enabled, show_making_charges,
            invoice_seq, show_gstin, gold_making_mode, gold_making_flat,
            silver_making_mode, silver_making_percent, gold_manual_price,
            silver_manual_price, silver_making_charge_per_10gm)
            VALUES (1,'Sri Balaji Jewellers',1,1,0,1,'percent',0,'flat',0,650000,80000,0);
        INSERT INTO product VALUES (1,'Gold Chain 18K','SKU-1','JWL-000002',
            '','gold','Chains','pcs',7584000,0,0,4,0.0,12.0,'2026-01-01');
        -- the retired fold-over design
        INSERT INTO tag_template VALUES (1,'Old fold-over','default','',
            45.0,22.0,'classic',9.0,7.0,1,0,1,1,1,1,1,1,0,1,'solid','',1,1,
            'hangtag',26.0,4.0);
    """)
    con.commit()
    con.close()


TMP_DB = os.path.join(tempfile.mkdtemp(prefix="tagv4_"), "old.db")
build_old_db(TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + TMP_DB.replace("\\", "/")
os.environ["SECRET_KEY"] = "x" * 32
os.environ.pop("WTF_CSRF_ENABLED", None)

from app import app, db, TagTemplate, Product, User  # noqa: E402
from app import (normalise_tag_shape, tag_total_width_mm, tag_tail_height_mm,  # noqa: E402
                 tag_qr_mm, tag_barcode_h_mm, resolved_tag_geometry)

app.config["WTF_CSRF_ENABLED"] = False

with app.test_client() as cl:
    with cl.session_transaction() as s:
        s["user_id"] = 1
        s["username"] = "admin"
        s["is_admin"] = True

    print("\n1. OLD DATABASE MIGRATES  (hangtag -> tail, columns added)")
    cl.get("/tags")                      # first request runs ensure_schema()
    cols = {r[1] for r in sqlite3.connect(TMP_DB).execute(
        "PRAGMA table_info(tag_template)")}
    for c in ("tail_mm", "tail_h_mm", "tail_pos", "tail_hole_mm",
              "pad_mm", "pad_h_mm", "gap_mm", "qr_mm", "bc_h_mm", "bc_w_pct"):
        check(f"column tag_template.{c} added", c in cols)
    with app.app_context():
        t = db.session.get(TagTemplate, 1)
        check("shape 'hangtag' migrated to 'tail'", t.shape == "tail", repr(t.shape))
        check("strap length carried into tail_mm", t.tail_mm == 26.0, repr(t.tail_mm))
        check("hole carried into tail_hole_mm", t.tail_hole_mm == 4.0, repr(t.tail_hole_mm))
        check("existing flags untouched", t.show_weight is True and t.show_metal is True)
        check("existing size untouched", (t.width_mm, t.height_mm) == (45.0, 22.0))

    print("\n2. /tags OFFERS EXACTLY TWO SHAPES")
    h = cl.get("/tags").get_data(as_text=True)
    picks = re.findall(r'data-shape="([^"]+)"', h)
    check("two shape pickers", sorted(set(picks)) == ["rectangle", "tail"], repr(picks))
    check("no hangtag picker left", 'data-shape="hangtag"' not in h)
    for fld in ("tail_mm", "tail_h_mm", "tail_pos", "tail_hole_mm", "pad_mm",
                "pad_h_mm", "gap_mm", "qr_mm", "bc_h_mm", "bc_w_pct"):
        check(f"designer exposes {fld}", f'name="{fld}"' in h)
    check("Show-everything quick fix present", "setAllFields(true)" in h)
    check("Jewellery-essentials quick fix present", "setEssentialFields()" in h)
    check("blank-design warning present", 'id="empty-warn"' in h)
    check("overflow warning present", 'id="nofit-warn"' in h)

    print("\n3. BOTH SHAPES RENDER  (tail only on a tail design)")
    with app.app_context():
        p = db.session.get(Product, 1)
        tpl = db.session.get(TagTemplate, 1)
        tpl.show_qr = True
        tpl.show_barcode = True
        db.session.commit()
        from app import _tag_art
        qr, bc = _tag_art(p, tpl)
        tail_html = app.jinja_env.get_template("tag_design.html") \
            .module.tag_art(tpl, p, None, qr, bc)
    check("tail shape class", "jt-shape-tail" in tail_html)
    check("tail strip element", 'class="jt-tail"' in tail_html)
    check("tail length var", "--tail:26.0mm" in tail_html, tail_html[:400])
    check("punched hole drawn", 'class="jt-hole"' in tail_html)
    check("no fold/strap leftovers", "jt-fold" not in tail_html and "jt-strap" not in tail_html)
    check("auto tail height = 40% of body", "--tailh:8.8mm" in tail_html)
    check("auto QR = 55% of height", "--qr:12.1mm" in tail_html)
    check("auto bar height = 28% of height", "--bch:6.16mm" in tail_html)

    with app.app_context():
        p = db.session.get(Product, 1)
        tpl = db.session.get(TagTemplate, 1)
        tpl.shape = "rectangle"
        db.session.commit()
        rect_html = app.jinja_env.get_template("tag_design.html") \
            .module.tag_art(tpl, p, None, None, None)
    check("rectangle shape class", "jt-shape-rectangle" in rect_html)
    check("rectangle has no tail", "jt-tail" not in rect_html)
    check("rectangle tail length is 0", "--tail:0mm" in rect_html)

    print("\n4. METAL AND WEIGHT PRINT")
    with app.app_context():
        p = db.session.get(Product, 1)
        tpl = db.session.get(TagTemplate, 1)
        tpl.shape = "tail"
        tpl.show_metal = True
        tpl.show_weight = True
        tpl.show_category = True
        tpl.show_name = True
        tpl.show_price = True
        tpl.show_code = True
        db.session.commit()
        html = app.jinja_env.get_template("tag_design.html") \
            .module.tag_art(tpl, p, None, None, None)
    line = re.search(r'<div class="jt-line">(.*?)</div>', html, re.S).group(1)
    parts = re.findall(r"<span>(.*?)</span>", line)
    check("detail line shows category + metal + weight",
          parts == ["Chains", "Gold", "12 g"], repr(parts))
    check("price printed in whole rupees", "₹75840" in html, html[:200])

    print("\n5. SIZE / SPACING CONTROLS REACH THE OUTPUT")
    with app.app_context():
        p = db.session.get(Product, 1)
        tpl = db.session.get(TagTemplate, 1)
        tpl.pad_mm, tpl.pad_h_mm, tpl.gap_mm = 3.0, 4.5, 1.25
        tpl.qr_mm, tpl.bc_h_mm, tpl.bc_w_pct = 15.0, 9.5, 60.0
        tpl.tail_mm, tpl.tail_h_mm, tpl.tail_pos = 20.0, 7.5, "bottom"
        db.session.commit()
        html = app.jinja_env.get_template("tag_design.html") \
            .module.tag_art(tpl, p, None, None, None)
    for var, want in (("--pad", "3.0mm"), ("--padh", "4.5mm"), ("--gaph", "1.25mm"),
                      ("--qr", "15.0mm"), ("--bch", "9.5mm"), ("--bcw", "60.0%"),
                      ("--tail", "20.0mm"), ("--tailh", "7.5mm"),
                      ("--tailpos", "flex-end")):
        check(f"{var} = {want}", f"{var}:{want}" in html, html[:300])

    print("\n6. DESIGNER FORM ROUND-TRIPS EVERY NEW FIELD")
    form = {"tpl_id": "1", "name": "Round trip", "scope": "default", "category": "",
            "width_mm": "52", "height_mm": "24", "style": "modern", "border": "dashed",
            "font_name": "11", "font_detail": "7.5",
            "show_name": "on", "show_category": "on", "show_metal": "on",
            "show_weight": "on", "show_price": "on", "show_code": "on",
            "show_barcode": "on", "show_qr": "on",
            "note": "22K", "copies": "2", "is_thermal": "on",
            "shape": "tail", "tail_mm": "18", "tail_h_mm": "6",
            "tail_pos": "top", "tail_hole_mm": "3.5",
            "pad_mm": "2.5", "pad_h_mm": "3", "gap_mm": "0.8",
            "qr_mm": "14", "bc_h_mm": "8", "bc_w_pct": "75"}
    cl.post("/tags/save", data=form, follow_redirects=True)
    with app.app_context():
        t = db.session.get(TagTemplate, 1)
        want = {"shape": "tail", "tail_mm": 18.0, "tail_h_mm": 6.0,
                "tail_pos": "top", "tail_hole_mm": 3.5, "pad_mm": 2.5,
                "pad_h_mm": 3.0, "gap_mm": 0.8, "qr_mm": 14.0,
                "bc_h_mm": 8.0, "bc_w_pct": 75.0, "width_mm": 52.0,
                "height_mm": 24.0, "font_name": 11.0, "font_detail": 7.5,
                "copies": 2, "note": "22K"}
        for k, v in want.items():
            check(f"saved {k} = {v}", getattr(t, k) == v, repr(getattr(t, k)))
        check("unchecked fields stay off",
              t.show_sku is False and t.show_firm is False)
        check("border survives", t.border == "dashed", repr(t.border))

    print("\n7. RENDERED PAGES USE THE TAIL WIDTH")
    with app.app_context():
        t = db.session.get(TagTemplate, 1)
        check("total width = body + tail", tag_total_width_mm(t) == 70.0,
              str(tag_total_width_mm(t)))
    r = cl.get("/products/1/tag")
    page = r.get_data(as_text=True)
    check("/products/1/tag returns 200", r.status_code == 200, str(r.status_code))
    check("@page uses body+tail width", "size: 70.0mm 24.0mm" in page, page[:300])
    check("tail input offered on the size form", 'name="tail"' in page)
    check("tail rendered on the page", "jt-shape-tail" in page)
    r = cl.get("/tags/print?layout=roll")
    check("/tags/print returns 200", r.status_code == 200, str(r.status_code))

    print("\n8. HELPERS")
    with app.app_context():
        t = db.session.get(TagTemplate, 1)
        t.tail_h_mm, t.qr_mm, t.bc_h_mm = 0.0, 0.0, 0.0
        t.height_mm = 25.0
        check("tail height auto = 10mm", tag_tail_height_mm(t) == 10.0,
              str(tag_tail_height_mm(t)))
        check("QR auto = 13.75mm", tag_qr_mm(t) == 13.75, str(tag_qr_mm(t)))
        check("bar auto = 7.0mm", tag_barcode_h_mm(t) == 7.0, str(tag_barcode_h_mm(t)))
    for raw, want in (("hangtag", "tail"), ("strap", "tail"), ("tail", "tail"),
                      ("rectangle", "rectangle"), ("", "rectangle"),
                      (None, "rectangle"), ("junk", "rectangle")):
        check(f"normalise_tag_shape({raw!r}) -> {want}",
              normalise_tag_shape(raw) == want, repr(normalise_tag_shape(raw)))

    print("\n9. TAG LENGTH + ACTUAL-SIZE PRINTING")
    # The shopkeeper's own numbers: body 50 long, 10 tall, on a 70 mm label.
    self_geo = None
    form = {"tpl_id": "1", "name": "70mm roll", "scope": "default", "category": "",
            "width_mm": "50", "height_mm": "10", "style": "classic",
            "border": "solid", "font_name": "9", "font_detail": "7",
            "copies": "1", "is_thermal": "on", "shape": "tail",
            "tail_mm": "20", "tail_h_mm": "0", "tail_pos": "middle",
            "tail_hole_mm": "0", "total_mm": "70", "pad_mm": "1",
            "pad_h_mm": "1.5", "gap_mm": "0.4", "qr_mm": "0", "bc_h_mm": "0",
            "bc_w_pct": "100", "show_name": "on", "show_category": "on",
            "show_metal": "on", "show_weight": "on", "show_price": "on",
            "show_code": "on", "show_barcode": "on", "show_qr": "on"}
    cl.post("/tags/save", data=form, follow_redirects=True)
    with app.app_context():
        t = db.session.get(TagTemplate, 1)
        self_geo = resolved_tag_geometry(t)
        check("tag length saved", t.total_mm == 70.0, repr(t.total_mm))
    check("geometry: body 50 / height 10 / tail 20 / total 70",
          self_geo == {"shape": "tail", "body": 50.0, "height": 10.0,
                       "tail": 20.0, "total": 70.0}, repr(self_geo))

    def page_size(html):
        """The @page size actually in force, ignoring commented-out examples."""
        body = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
        m = re.search(r"@page\s*\{([^}]*)\}", body)
        return (m.group(1).strip() if m else "(none)")

    def vars_of(html):
        m = re.search(r'style="--jn(.*?)"', html, re.S)
        blob = m.group(1) if m else ""
        out = {}
        for key in ("tw", "th", "tail", "tailh"):
            hit = re.search(rf"--{key}:([\d.]+)mm", blob)
            out[key] = float(hit.group(1)) if hit else None
        return out

    print("\n   the size form must reproduce exactly what was asked for")
    for qs, want in (
            ("?tpl=1&w=50&h=10&total=70", (50.0, 10.0, 20.0, 70.0, 10.0)),
            # body shrinks, the 70 mm label does not — the tail absorbs it
            ("?tpl=1&w=40&h=10&total=70", (40.0, 10.0, 30.0, 70.0, 10.0)),
            # total=0 must RELEASE the pin, not clamp to the 10 mm minimum
            ("?tpl=1&w=50&h=10&total=0&tail=20", (50.0, 10.0, 20.0, 70.0, 10.0)),
            ("?tpl=1&w=50&h=10&tail=25", (50.0, 10.0, 25.0, 75.0, 10.0)),
            # a total smaller than the body cannot eat the whole tag
            ("?tpl=1&w=50&h=10&total=20", (50.0, 10.0, 4.0, 54.0, 10.0)),
    ):
        html = cl.get("/products/1/tag" + qs).get_data(as_text=True)
        v = vars_of(html)
        size = page_size(html)
        got = (v["tw"], v["th"], v["tail"],
               float(re.search(r"size:\s*([\d.]+)mm", size).group(1)),
               float(re.search(r"size:\s*[\d.]+mm\s*([\d.]+)mm", size).group(1)))
        check(f"{qs} -> body/height/tail/total = {want}", got == want, repr(got))

    print("\n   sheet layout must not pin @page to a single tag's size")
    roll = cl.get("/tags/print?layout=roll").get_data(as_text=True)
    sheet = cl.get("/tags/print?layout=sheet").get_data(as_text=True)
    check("roll layout pins the page to the tag",
          re.search(r"size:\s*70\.0mm 10\.0mm", page_size(roll)) is not None,
          page_size(roll))
    check("sheet layout leaves the paper alone",
          "size:" not in page_size(sheet), page_size(sheet))

    print("\n   print CSS must not shave the tag's edges")
    # .sheet padding + centring used to push a full-width tag off its own page
    for frag in ("padding: 0 !important", "margin: 0 !important",
                 "justify-content: flex-start !important",
                 "align-content: flex-start !important",
                 "print-color-adjust: exact",
                 "break-inside: avoid"):
        check(f"print CSS: {frag}", frag in roll)

    print("\n   calibration strip")
    cal = cl.get("/tags/calibrate?w=70").get_data(as_text=True)
    check("calibration returns 200", cl.get("/tags/calibrate").status_code == 200)
    check("ruler is exactly the tag length",
          "width: 70.0mm" in cal, re.search(r"\.ruler[^}]*}", cal, re.S).group(0)[:120])
    check("ruler has mm ticks", "repeating-linear-gradient" in cal)
    check("calibration page is the ruler plus margins",
          "size: 88.0mm 30mm" in page_size(cal), page_size(cal))
    check("calibration clamps silly input",
          "size: 218.0mm 30mm" in page_size(
              cl.get("/tags/calibrate?w=9999").get_data(as_text=True)))

    print("\n   the print pages tell the shopkeeper the paper size")
    tag_page = cl.get("/products/1/tag?tpl=1").get_data(as_text=True)
    check("paper size shown on the tag page", "70.0 × 10.0 mm" in tag_page)
    check("scale 100% spelled out", "Scale <b>100%</b>" in tag_page)
    check("headers/footers warning shown", "Headers and footers" in tag_page)
    check("ruler link offered", "/tags/calibrate" in tag_page)
    check("tag length field on the size form", 'name="total"' in tag_page)
    check("print layout pinned to 70 x 10",
          "size: 70.0mm 10.0mm" in page_size(tag_page), page_size(tag_page))

    designer = cl.get("/tags?tpl=1").get_data(as_text=True)
    check("designer exposes the tag length", 'name="total_mm"' in designer)
    check("designer labels the fields unambiguously",
          "Body length (mm)" in designer and "Tag height (mm)" in designer
          and "Tag length (mm)" in designer)
    check("designer shows the paper size", 'id="paper-hint"' in designer)
    check("designer links the ruler", "/tags/calibrate" in designer)

print("\n" + "=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILS)} failed")
if FAILS:
    print("\n  FAILURES:")
    for f in FAILS:
        print("   -", f)
print("=" * 62)
sys.exit(1 if FAILS else 0)
