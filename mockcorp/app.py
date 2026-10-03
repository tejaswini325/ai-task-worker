"""Simulated company: a vendor invoice portal + an internal ERP. Deliberately imperfect
(transient 503s, strict validation, pagination, a credit-note trap, ambiguous vendor names)."""
import re
import threading
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template_string, request, session

INVOICES = [  # id, company, number, type, issued, due, amount
    (1, "Globex Corporation", "INV-1001", "Invoice", "2026-06-02", "2026-07-02", 8200.00),
    (2, "Globex Corporation", "INV-1007", "Invoice", "2026-07-30", "2026-08-29", 9415.50),
    (3, "Globex Corporation", "INV-1012", "Invoice", "2026-09-04", "2026-10-04", 12640.75),
    (4, "Globex Corporation", "INV-1019", "Invoice", "2026-09-28", "2026-10-28", 14980.00),
    (5, "Globex Corporation", "CN-1020", "Credit Note", "2026-09-30", "2026-09-30", -500.00),
    (6, "Acme Corp", "INV-2001", "Invoice", "2026-08-11", "2026-09-10", 3300.00),
    (7, "Acme Industries", "INV-3001", "Invoice", "2026-09-15", "2026-10-15", 780.25),
]
VENDORS = ["Globex Corporation", "Acme Corp", "Acme Industries"]
PAGE_SIZE = 3

SHELL = """<!doctype html><html><head><title>{{title}}</title></head><body>
<nav>{% for href,t in nav %}<a href="{{href}}">{{t}}</a> {% endfor %}</nav><h1>{{title}}</h1>{{body|safe}}</body></html>"""


def page(title, body, nav=()):
    return render_template_string(SHELL, title=title, body=body, nav=nav)


def money(a):
    return ("-" if a < 0 else "") + f"${abs(a):,.2f}"


def human(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")


def login_form(action, title, extra=""):
    return page(title, f"""{extra}<form method="post" action="{action}">
<label for="u">Username</label><input id="u" name="username" type="text">
<label for="p">Password</label><input id="p" name="password" type="password">
<button type="submit">Sign in</button></form>""")


def create_app():
    app = Flask(__name__)
    app.secret_key = "sandbox"
    st = {"flake": 3, "bills": []}
    app.state = st
    VNAV = [("/vendor/", "Home"), ("/vendor/invoices", "All invoices")]
    ENAV = [("/erp/", "Home"), ("/erp/bills", "Bills"), ("/erp/bills/new", "New bill")]

    # ------------------------------------------------------------ vendor portal
    @app.route("/vendor/login", methods=["GET", "POST"])
    def vlogin():
        if request.method == "POST":
            if request.form.get("username") == "vendor" and request.form.get("password") == "demo-pass":
                session["vendor"] = True
                return redirect("/vendor/")
            return login_form("/vendor/login", "Vendor portal login", "<p>Invalid credentials</p>"), 401
        return login_form("/vendor/login", "Vendor portal login")

    def vguard():
        return None if session.get("vendor") else redirect("/vendor/login")

    @app.route("/vendor/")
    def vhome():
        return vguard() or page("Vendor invoice portal", """<p>Search invoices by company name.</p>
<form method="get" action="/vendor/invoices"><label for="c">Company</label>
<input id="c" name="company" type="text"><button type="submit">Search</button></form>""", VNAV)

    @app.route("/vendor/invoices")
    def vlist():
        if g := vguard():
            return g
        q = request.args.get("company", "").strip().lower()
        rows = sorted((i for i in INVOICES if q in i[1].lower()), key=lambda i: i[4])
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        p = min(max(int(request.args.get("page", 1)), 1), pages)
        shown = rows[(p - 1) * PAGE_SIZE: p * PAGE_SIZE]
        trs = "".join(f'<tr><td><a href="/vendor/invoice/{i[0]}">{i[2]}</a></td><td>{i[1]}</td><td>{i[3]}</td>'
                      f"<td>{human(i[4])}</td><td>{money(i[6])}</td></tr>" for i in shown)
        qs = f"company={request.args.get('company', '')}"
        nav = (f'<a href="/vendor/invoices?{qs}&page={p-1}">Previous page</a> ' if p > 1 else "") + \
              (f'<a href="/vendor/invoices?{qs}&page={p+1}">Next page</a>' if p < pages else "")
        return page("Invoices", f"<p>{len(rows)} results. Page {p} of {pages}. Sorted by issue date, oldest first.</p>"
                    f"<table><tr><th>Number</th><th>Company</th><th>Type</th><th>Issued</th><th>Amount</th></tr>{trs}</table>{nav}", VNAV)

    @app.route("/vendor/invoice/<int:iid>")
    def vdetail(iid):
        if g := vguard():
            return g
        if st["flake"] > 0:
            st["flake"] -= 1
            return page("Service unavailable", "<p>Upstream timeout, please retry.</p>"), 503
        i = next((x for x in INVOICES if x[0] == iid), None)
        if not i:
            return page("Not found", "<p>No such invoice</p>", VNAV), 404
        return page(f"{i[3]} {i[2]}", f"""<table><tr><td>Company</td><td>{i[1]}</td></tr>
<tr><td>Document number</td><td>{i[2]}</td></tr><tr><td>Type</td><td>{i[3]}</td></tr>
<tr><td>Issue date</td><td>{human(i[4])}</td></tr><tr><td>Due date</td><td>{human(i[5])}</td></tr>
<tr><td>Amount due</td><td>{money(i[6])}</td></tr><tr><td>Status</td><td>Unpaid</td></tr></table>""", VNAV)

    # ---------------------------------------------------------------------- ERP
    @app.route("/erp/login", methods=["GET", "POST"])
    def elogin():
        if request.method == "POST":
            if request.form.get("username") == "admin" and request.form.get("password") == "demo-pass":
                session["erp"] = True
                return redirect("/erp/")
            return login_form("/erp/login", "ERP login", "<p>Invalid credentials</p>"), 401
        return login_form("/erp/login", "ERP login")

    def eguard():
        return None if session.get("erp") else redirect("/erp/login")

    @app.route("/erp/")
    def ehome():
        return eguard() or page("Internal ERP", "<p>Accounts payable.</p>", ENAV)

    @app.route("/erp/bills")
    def ebills():
        if g := eguard():
            return g
        trs = "".join(f"<tr><td>{b['vendor']}</td><td>{b['invoice_number']}</td><td>{b['amount']:.2f}</td>"
                      f"<td>{b['due_date']}</td><td>Recorded</td></tr>" for b in st["bills"])
        banner = f"<p>Bill saved: {request.args['saved']}</p>" if "saved" in request.args else ""
        return page("Bills", f"{banner}<table><tr><th>Vendor</th><th>Invoice #</th><th>Amount</th><th>Due date</th>"
                    f"<th>Status</th></tr>{trs}</table><p>{len(st['bills'])} bills</p>", ENAV)

    def bill_form(errors=()):
        opts = "".join(f'<option value="{v}">{v}</option>' for v in VENDORS)
        errs = "".join(f"<li>{e}</li>" for e in errors)
        return page("New bill", (f"<ul>{errs}</ul>" if errs else "") + f"""<form method="post" action="/erp/bills/new">
<label for="v">Vendor</label><select id="v" name="vendor">{opts}</select>
<label for="n">Invoice number</label><input id="n" name="invoice_number" type="text">
<label for="a">Amount</label><input id="a" name="amount" type="text">
<label for="d">Due date</label><input id="d" name="due_date" type="text">
<button type="submit">Save bill</button></form>""", ENAV)

    @app.route("/erp/bills/new", methods=["GET", "POST"])
    def enew():
        if g := eguard():
            return g
        if request.method == "GET":
            return bill_form()
        f = {k: request.form.get(k, "").strip() for k in ("vendor", "invoice_number", "amount", "due_date")}
        errs = []
        if f["vendor"] not in VENDORS:
            errs.append("Unknown vendor.")
        if not f["invoice_number"]:
            errs.append("Invoice number is required.")
        if not re.fullmatch(r"\d+(\.\d{1,2})?", f["amount"]):
            errs.append("Amount must be a plain number such as 1234.50 (no currency symbol or commas).")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["due_date"]):
            errs.append("Due date must be in YYYY-MM-DD format.")
        if any(b["vendor"] == f["vendor"] and b["invoice_number"] == f["invoice_number"] for b in st["bills"]):
            errs.append("Duplicate: this invoice number is already recorded for this vendor.")
        if errs:
            return bill_form(errs), 422
        st["bills"].append({**f, "amount": float(f["amount"])})
        return redirect(f"/erp/bills?saved={f['invoice_number']}")

    @app.route("/_test/state")  # ground truth for the evaluator / tests; not linked anywhere
    def state():
        return jsonify(st["bills"])

    return app


def serve(port=5001):
    from werkzeug.serving import make_server
    app = create_app()
    srv = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, app


if __name__ == "__main__":
    create_app().run(port=5001)
