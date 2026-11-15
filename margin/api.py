"""HTTP API for the signup page. Endpoints:

POST   /api/preview                 live matches for a draft alert
POST   /api/subscribe               create a pending alert and email a confirmation link
GET    /confirm/<token>             double opt-in: activates the alert
GET    /unsubscribe/<token>         confirmation page (prevents link scanners unsubscribing people)
POST   /unsubscribe/<token>         one-click unsubscribe (RFC 8058)
GET    /f/<token>                   "more like this" / "not relevant" from the email
GET    /api/manage                  list alerts           (Authorization: Bearer <manage token>)
PATCH  /api/manage/alerts/<id>      edit or pause an alert
DELETE /api/manage/alerts/<id>      delete an alert
"""
import html as H
import json
import os
import re
import time
from collections import defaultdict

from urllib.parse import quote

from flask import Flask, Response, g, jsonify, request, send_from_directory

from . import db, tokens
from .config import Config
from .digest import link
from .mailer import make_mailer
from .matcher import AlertSpec

VALID_SOURCES = {"openalex", "pubmed", "arxiv", "biorxiv"}
FREQS = {"instant", "daily", "weekly"}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
CONSENT = "double-opt-in:v1"


class BadInput(Exception):
    pass


def clean_alert(d: dict, partial=False) -> dict:
    """Validate and normalise an alert payload from the browser."""
    out = {}
    def terms(key):
        v = d.get(key, [])
        if not isinstance(v, list) or len(v) > 20:
            raise BadInput(f"{key} must be a list of at most 20 terms")
        v = [str(x).strip() for x in v if str(x).strip()]
        if any(len(x) > 100 for x in v):
            raise BadInput(f"{key} terms must be under 100 characters")
        return v
    if not partial or "include" in d:
        out["include"] = terms("include")
        if not AlertSpec.build(0, out["include"]).include:
            raise BadInput("Add at least one topic to follow.")
    if not partial or "exclude" in d:
        out["exclude"] = terms("exclude")
    if not partial or "sources" in d:
        s = d.get("sources") or sorted(VALID_SOURCES)
        if not set(s) <= VALID_SOURCES:
            raise BadInput("Unknown source.")
        out["sources"] = sorted(set(s))
    if not partial or "oa_only" in d:
        out["oa_only"] = bool(d.get("oa_only", False))
    if not partial or "preprints" in d:
        out["preprints"] = bool(d.get("preprints", True))
    if not partial or "frequency" in d:
        f = str(d.get("frequency", "weekly")).lower()
        if f not in FREQS:
            raise BadInput("Frequency must be instant, daily or weekly.")
        out["frequency"] = f
    if not partial and not d.get("name") and out.get("include"):
        out["name"] = ", ".join(out["include"])[:80]
    elif d.get("name"):
        out["name"] = str(d["name"])[:80]
    return out


def page(title, body):
    return Response(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{H.escape(title)}</title><body style=\"font:16px/1.5 system-ui;max-width:30em;margin:12vh auto;padding:0 20px\">"
        f"<h1 style='font:400 30px Georgia,serif'>{H.escape(title)}</h1>{body}</body>", mimetype="text/html")


def create_app(cfg: Config | None = None, mailer=None) -> Flask:
    cfg = cfg or Config.from_env()
    if cfg.mailer != "console" and cfg.secret == Config.secret:
        raise RuntimeError("Set MARGIN_SECRET to a long random value before sending real email.")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    mailer = mailer or make_mailer(cfg)
    app.margin_mailer = mailer
    hits: dict[str, list[float]] = defaultdict(list)   # tiny in-memory rate limiter; use Redis behind many workers

    def conn():
        if "db" not in g:
            g.db = db.connect(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def close(_):
        c = g.pop("db", None)
        if c:
            c.close()

    @app.after_request
    def cors(r):
        r.headers["Access-Control-Allow-Origin"] = cfg.allowed_origin
        r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        return r

    @app.errorhandler(BadInput)
    def bad(e):
        return jsonify(error=str(e)), 400

    def limited(key, n, per):
        now = time.time()
        hits[key] = [t for t in hits[key] if now - t < per]
        if len(hits[key]) >= n:
            return True
        hits[key].append(now)
        return False

    web_dir = os.path.join(os.path.dirname(__file__), "web")
    outbox = os.path.abspath(cfg.outbox_dir)

    @app.get("/")
    def home():
        return send_from_directory(web_dir, "index.html")

    if cfg.mailer == "console":
        # Development only: lets you read the emails the app would have sent.
        @app.get("/dev/outbox")
        def dev_outbox():
            files = sorted((f for f in os.listdir(outbox) if f.endswith(".html")),
                           key=lambda f: os.path.getmtime(os.path.join(outbox, f)), reverse=True) if os.path.isdir(outbox) else []
            items = "".join(f"<li><a href='/dev/outbox/{quote(f)}'>{H.escape(f)}</a></li>" for f in files[:30])
            return page("Dev outbox", f"<p>Newest first. Open the latest to find your confirmation link.</p><ul>{items or '<li>No emails yet.</li>'}</ul>")

        @app.get("/dev/outbox/<path:name>")
        def dev_email(name):
            return send_from_directory(outbox, name)

    @app.route("/api/<path:_>", methods=["OPTIONS"])
    def options(_):
        return ("", 204)

    @app.get("/api/health")
    def health():
        n = conn().execute("SELECT COUNT(*) c FROM papers").fetchone()["c"]
        return jsonify(ok=True, papers=n)

    @app.post("/api/preview")
    def preview():
        if limited("pv:" + (request.remote_addr or ""), 120, 60):
            return jsonify(error="Too many requests"), 429
        a = clean_alert(request.get_json(force=True, silent=True) or {})
        spec = AlertSpec.build(0, a["include"], a["exclude"], a["sources"], a["oa_only"], a["preprints"])
        return jsonify(db.preview(conn(), spec))

    @app.post("/api/subscribe")
    def subscribe():
        if limited("sub:" + (request.remote_addr or ""), 5, 3600):
            return jsonify(error="Too many sign-ups from this address. Try again later."), 429
        d = request.get_json(force=True, silent=True) or {}
        email = str(d.get("email", "")).strip().lower()
        if not EMAIL_RE.match(email) or len(email) > 254:
            raise BadInput("Enter a valid email address.")
        a = clean_alert(d.get("alert") or d)
        c = conn()
        c.execute("INSERT OR IGNORE INTO subscribers(email,created_at) VALUES(?,?)", (email, db.now_iso()))
        sub = c.execute("SELECT * FROM subscribers WHERE email=?", (email,)).fetchone()
        if c.execute("SELECT COUNT(*) c FROM alerts WHERE subscriber_id=?", (sub["id"],)).fetchone()["c"] >= cfg.max_alerts_per_subscriber:
            raise BadInput("This address already has the maximum number of alerts.")
        cur = c.execute(
            """INSERT INTO alerts(subscriber_id,name,include,exclude,sources,oa_only,preprints,frequency,status,created_at)
               VALUES(?,?,?,?,?,?,?,?, 'pending', ?)""",
            (sub["id"], a["name"], json.dumps(a["include"]), json.dumps(a["exclude"]), json.dumps(a["sources"]),
             int(a["oa_only"]), int(a["preprints"]), a["frequency"], db.now_iso()))
        c.commit()
        url = f"{cfg.base_url}/confirm/{link(cfg, 'confirm', s=sub['id'], a=cur.lastrowid)}"
        text = (f"Confirm your Margin alert for “{a['name']}” ({a['frequency']}):\n{url}\n\n"
                "If you didn't ask for this, ignore this email and nothing will happen.")
        body = (f"<p style='font:16px system-ui'>Confirm your alert for <b>“{H.escape(a['name'])}”</b> "
                f"({a['frequency']}).</p><p><a href='{url}' style='font:600 16px system-ui'>Confirm my alert</a></p>"
                "<p style='color:#5B6577;font:13px system-ui'>If you didn’t ask for this, ignore this email and nothing will happen.</p>")
        mailer.send(email, "Confirm your Margin alert", body, text)
        # Same response whether or not the address already existed, so nobody can probe who is subscribed.
        return jsonify(status="check_your_inbox"), 202

    @app.get("/confirm/<token>")
    def confirm(token):
        p = tokens.verify(token, cfg.secret, "confirm")
        if not p:
            return page("This link has expired", "<p>Request a new alert from the signup page.</p>"), 400
        c = conn()
        c.execute("""UPDATE subscribers SET confirmed_at=COALESCE(confirmed_at,?), consent_text=COALESCE(consent_text,?),
                     unsubscribed_at=NULL WHERE id=?""", (db.now_iso(), CONSENT, p["s"]))
        c.execute("UPDATE alerts SET status='active', activated_at=COALESCE(activated_at,?) WHERE id=? AND subscriber_id=?",
                  (db.now_iso(), p["a"], p["s"]))
        c.commit()
        return page("You’re subscribed", "<p>Your alert is active. New matching papers will arrive in your next digest.</p>")

    @app.route("/unsubscribe/<token>", methods=["GET", "POST"])
    def unsubscribe(token):
        p = tokens.verify(token, cfg.secret, "unsub")
        if not p:
            return page("This link has expired", "<p>Use the link in a more recent email.</p>"), 400
        if request.method == "POST":
            conn().execute("UPDATE subscribers SET unsubscribed_at=? WHERE id=?", (db.now_iso(), p["s"]))
            conn().commit()
            return page("You’ve been unsubscribed", "<p>No more emails will be sent.</p>")
        return page("Unsubscribe?", f"<form method=post><button style='font:600 16px system-ui;padding:10px 18px'>"
                                    "Yes, unsubscribe me from all alerts</button></form>")

    @app.get("/f/<token>")
    def feedback(token):
        p = tokens.verify(token, cfg.secret, "fb")
        if not p:
            return page("This link has expired", ""), 400
        conn().execute("INSERT INTO feedback(alert_id,paper_id,value,created_at) VALUES(?,?,?,?)",
                       (p["a"], p["p"], p["v"], db.now_iso()))
        conn().commit()
        return page("Thanks", "<p>We’ll use this to improve your results.</p>")

    def manage_sub():
        auth = request.headers.get("Authorization", "")
        p = tokens.verify(auth.removeprefix("Bearer ").strip(), cfg.secret, "manage")
        if not p:
            return None
        return conn().execute("SELECT * FROM subscribers WHERE id=?", (p["s"],)).fetchone()

    def alert_json(r):
        return {"id": r["id"], "name": r["name"], "include": json.loads(r["include"]), "exclude": json.loads(r["exclude"]),
                "sources": json.loads(r["sources"]), "oa_only": bool(r["oa_only"]), "preprints": bool(r["preprints"]),
                "frequency": r["frequency"], "status": r["status"]}

    @app.get("/api/manage")
    def manage_list():
        sub = manage_sub()
        if not sub:
            return jsonify(error="Invalid or expired link"), 401
        rows = conn().execute("SELECT * FROM alerts WHERE subscriber_id=? ORDER BY id", (sub["id"],)).fetchall()
        return jsonify(email=sub["email"], alerts=[alert_json(r) for r in rows])

    @app.route("/api/manage/alerts/<int:aid>", methods=["PATCH", "DELETE"])
    def manage_alert(aid):
        sub = manage_sub()
        if not sub:
            return jsonify(error="Invalid or expired link"), 401
        c = conn()
        row = c.execute("SELECT * FROM alerts WHERE id=? AND subscriber_id=?", (aid, sub["id"])).fetchone()
        if not row:
            return jsonify(error="Not found"), 404
        if request.method == "DELETE":
            c.execute("DELETE FROM alerts WHERE id=?", (aid,))
            c.commit()
            return ("", 204)
        d = request.get_json(force=True, silent=True) or {}
        upd = clean_alert(d, partial=True)
        if "status" in d:
            if d["status"] not in ("active", "paused") or row["status"] == "pending":
                raise BadInput("Status can only switch between active and paused.")
            upd["status"] = d["status"]
        for k in ("include", "exclude", "sources"):
            if k in upd:
                upd[k] = json.dumps(upd[k])
        for k in ("oa_only", "preprints"):
            if k in upd:
                upd[k] = int(upd[k])
        if upd:
            c.execute(f"UPDATE alerts SET {','.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), aid))
            c.commit()
        return jsonify(alert_json(c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()))

    return app
