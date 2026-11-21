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
from email.utils import parseaddr
from datetime import timedelta

from urllib.parse import quote

from flask import Flask, Response, g, jsonify, request, send_from_directory

from . import db, tokens
from .config import Config
from .digest import confirm_email, link, when_text
from .mailer import make_mailer
from .matcher import AlertSpec

VALID_SOURCES = {"openalex", "pubmed", "arxiv", "biorxiv", "europepmc"}
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


_PAGE_CSS = """
:root{--bg:#EDF1F7;--panel:#fff;--ink:#101828;--mute:#5B6577;--line:#D9DFEA;--accent:#3A39E8;--accent-ink:#fff;--ok:#0F7B53;
--sans:"Instrument Sans",system-ui,-apple-system,"Segoe UI",sans-serif;--serif:"Newsreader",Georgia,"Times New Roman",serif}
@media (prefers-color-scheme:dark){:root{--bg:#0C1220;--panel:#141C2E;--ink:#E9EDF6;--mute:#9AA5BA;--line:#27324A;--accent:#8B8BFF;--accent-ink:#0C1220;--ok:#4CC999}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:520px;margin:0 auto;padding:28px 20px 56px}
.logo{font:600 22px var(--serif);letter-spacing:-.01em;display:flex;align-items:center;gap:9px;color:var(--ink);text-decoration:none;margin-bottom:28px}
.logo i{width:22px;height:22px;border:2px solid var(--accent);border-right-width:7px;border-radius:3px;display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:34px 28px;text-align:center}
.tick{width:46px;height:46px;border-radius:50%;background:var(--ok);color:var(--panel);display:grid;place-items:center;margin:0 auto 14px;font-size:22px}
h1{font:400 clamp(26px,6vw,32px)/1.15 var(--serif);letter-spacing:-.01em;margin:0 0 10px}
p{color:var(--mute);margin:0 auto 16px;max-width:30em}
.chip{display:inline-block;background:var(--line);color:var(--ink);border-radius:8px;padding:6px 12px;font:600 15px var(--serif);margin:2px 0 14px}
.btn{display:inline-block;background:var(--accent);color:var(--accent-ink);font:600 15px var(--sans);padding:12px 22px;border-radius:10px;text-decoration:none;border:0;cursor:pointer}
.btn:hover{filter:brightness(1.08)}
.btn:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.btn.quiet{background:transparent;color:var(--ink);border:1.5px solid var(--line)}
a{color:var(--accent)}
.more{margin:18px 0 0;font-size:14px}
.fine{font-size:12.5px;color:var(--mute);margin:22px auto 0;max-width:30em;text-align:center}
@media(max-width:480px){.card{padding:28px 20px}}
"""


def render_simple_page(brand, title, body, tick=False, header=None):
    """A small page in the same look as the home page, for confirmation, unsubscribe and expired-link screens."""
    b, h = H.escape(brand), H.escape(header or brand)
    return Response(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f'<title>{H.escape(title)} · {b}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=Newsreader:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">'
        f'<style>{_PAGE_CSS}</style></head><body><div class="wrap">'
        f'<a class="logo" href="/"><i></i>{h}</a>'
        f'<main class="card">{"<div class=tick aria-hidden=true>✓</div>" if tick else ""}<h1>{H.escape(title)}</h1>{body}</main>'
        '</div></body></html>', mimetype="text/html")


_ADS_PRIVACY = (
    "<h2>Sponsored messages</h2>"
    "<p>Some emails carry one clearly labelled sponsored message after the papers. It is chosen because it relates to the topic of one "
    "of your alerts. Sponsors do not receive your email address, your name or what you read, and sponsors cannot buy a place among the "
    "papers or change which papers you get. We do not track whether you click. If you click, you visit the sponsor's own website, "
    "which has its own privacy policy. The link in every sponsored message lets you hide them, and you can turn them back on from the same page.</p>")

_ADS_EXPLAINER = (
    "<p>{brand} is free to read. Some emails carry one clearly labelled sponsored message after the papers, to help cover the running costs.</p>"
    "<p><b>How it's chosen.</b> Only by topic: a sponsor asks to appear alongside certain subjects, and we show it when one of your alerts is about that subject. "
    "Sponsors never receive your email address, your name or what you read, they can't buy a place among the papers, and they can't change which papers you get.</p>"
    "<p><b>If you click,</b> you go to the sponsor's own website, which has its own privacy policy. We don't track whether you click.</p>")


def create_app(cfg: Config | None = None, mailer=None) -> Flask:
    cfg = cfg or Config.from_env()
    if cfg.mailer != "console" and cfg.problems():
        raise RuntimeError("Not ready to send real email:\n- " + "\n- ".join(cfg.problems()))
    app = Flask(__name__)

    def page(title, body, tick=False):
        return render_simple_page(cfg.brand, title, body, tick, cfg.logo_text())

    if cfg.trust_proxy:
        # Behind the host's HTTPS proxy every request arrives from the proxy; believe its one forwarded address
        # so that rate limits apply per visitor and not to everyone at once.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
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
    def headers(r):
        if cfg.allowed_origin:      # only needed if the page is hosted somewhere other than this app
            r.headers["Access-Control-Allow-Origin"] = cfg.allowed_origin
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            r.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Referrer-Policy"] = "no-referrer"          # signed links must never leak in a Referer header
        if request.path.startswith(("/manage", "/confirm", "/unsubscribe", "/f/", "/api/manage")):
            r.headers["Cache-Control"] = "no-store"
        if cfg.base_url.startswith("https://"):
            r.headers["Strict-Transport-Security"] = "max-age=31536000"
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

    def render_page(name, extra=None):
        """Serve a page from web/, filling in the product name so a rename is one setting (MARGIN_BRAND)."""
        text = open(os.path.join(web_dir, name), encoding="utf-8").read()
        js_safe = json.dumps(cfg.brand)[1:-1].replace("'", "\\'").replace("<", "\\u003c")
        values = {"{{BRAND}}": H.escape(cfg.brand), "{{HEADER}}": H.escape(cfg.logo_text()), "{{BRAND_JS}}": js_safe,
                  "{{FROM_ADDR}}": H.escape(parseaddr(cfg.from_addr)[1]), "{{CONTACT}}": H.escape(cfg.contact_email),
                  **(extra or {})}
        for k, v in values.items():
            text = text.replace(k, v)
        return Response(text, mimetype="text/html")

    @app.get("/")
    def home():
        return render_page("index.html")

    @app.get("/manage")
    def manage_page():
        return render_page("manage.html")

    @app.get("/privacy")
    def privacy():
        return render_page("privacy.html", {"{{PROVIDERS}}": H.escape(cfg.providers) if cfg.providers else
                                            "<b>[Name your hosting and email providers here, and where they process data.]</b>",
                                            "{{OPERATOR}}": H.escape(cfg.operator or "[your name or organisation]"),
                     "{{ADS}}": _ADS_PRIVACY if cfg.ads_enabled else "",
                     "{{RETENTION}}": str(cfg.paper_retention_days),
                     "{{UNFILLED}}": "" if (cfg.operator and cfg.providers) else
                         "<p style='background:#FFF6DD;border:1px solid #E8C766;padding:12px;border-radius:8px'><b>Not ready to publish:</b> "
                         "set MARGIN_OPERATOR, MARGIN_CONTACT_EMAIL and MARGIN_PROVIDERS, and review every section against what you actually do.</p>"})

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

    @app.get("/api/stats")
    def stats():
        """What the 'papers indexed' line on the home page describes: how many recent papers we hold, since when, and how fresh."""
        c = conn()
        cutoff = (db.now_dt() - timedelta(days=cfg.paper_retention_days)).date().isoformat()
        n = c.execute("SELECT COUNT(*) c FROM papers").fetchone()["c"]
        since = c.execute("SELECT MIN(published) m FROM papers WHERE published>=?", (cutoff,)).fetchone()["m"]
        updated = c.execute("SELECT MAX(value) m FROM state WHERE substr(key,1,12)='last_ingest_'").fetchone()["m"]
        return jsonify(papers=n, since=since, updated=updated, every_hours=cfg.ingest_every_hours, keep_days=cfg.paper_retention_days)

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
        subject, body, text = confirm_email(cfg, a['name'], a['frequency'], url)
        try:
            mailer.send(email, subject, body, text)
        except Exception as exc:                      # most often a wrong email-provider setting: say so in the log, kindly to the visitor
            app.logger.error("Could not send the confirmation email (%s: %s). Check the SMTP_* and MARGIN_FROM settings.", type(exc).__name__, exc)
            c.execute("DELETE FROM alerts WHERE id=?", (cur.lastrowid,))
            c.commit()
            return jsonify(error="We couldn’t send your confirmation email just now. Please try again in a few minutes."), 503
        # Same response whether or not the address already existed, so nobody can probe who is subscribed.
        return jsonify(status="check_your_inbox"), 202

    @app.get("/confirm/<token>")
    def confirm(token):
        p = tokens.verify(token, cfg.secret, "confirm")
        if not p:
            return page("This link has expired", "<p>Confirmation links work for 7 days. You can set up the alert again in a minute.</p>"
                        f"<a class='btn' href='/'>Create an alert</a>"), 400
        c = conn()
        c.execute("""UPDATE subscribers SET confirmed_at=COALESCE(confirmed_at,?), consent_text=COALESCE(consent_text,?),
                     unsubscribed_at=NULL WHERE id=?""", (db.now_iso(), CONSENT, p["s"]))
        c.execute("UPDATE alerts SET status='active', activated_at=COALESCE(activated_at,?) WHERE id=? AND subscriber_id=?",
                  (db.now_iso(), p["a"], p["s"]))
        c.commit()
        manage = f"{cfg.frontend_url}/manage?t={link(cfg, 'manage', s=p['s'])}"
        al = c.execute("SELECT name, frequency FROM alerts WHERE id=? AND subscriber_id=?", (p["a"], p["s"])).fetchone()
        what = (f"<div class='chip'>{H.escape(al['name'])}</div><p>You’ll get {H.escape(when_text(cfg, al['frequency']))}.</p>"
                if al else "<p>Your alert is active.</p>")
        return page("You’re subscribed", what +
                    "<p>We only send papers that appear from now on, so you’ll hear from us when something new matches. "
                    "You can change or pause this alert any time.</p>"
                    f"<a class='btn' href='{H.escape(manage)}'>Manage your alerts</a>"
                    "<p class='more'><a href='/'>Create another alert</a></p>", tick=True)

    @app.route("/unsubscribe/<token>", methods=["GET", "POST"])
    def unsubscribe(token):
        p = tokens.verify(token, cfg.secret, "unsub")
        if not p:
            return page("This link has expired", "<p>Use the link in a more recent email, or create a fresh alert.</p>"
                        "<a class='btn' href='/'>Back to the home page</a>"), 400
        if request.method == "POST":
            conn().execute("UPDATE subscribers SET unsubscribed_at=? WHERE id=?", (db.now_iso(), p["s"]))
            conn().commit()
            return page("You’ve been unsubscribed", "<p>No more emails will be sent to this address. Sorry to see you go.</p>"
                        "<p class='more'>Changed your mind? <a href='/'>Create a new alert</a></p>", tick=True)
        return page("Unsubscribe?", "<p>This stops every alert on this email address. You can always sign up again later.</p>"
                                    "<form method=post><button class='btn'>Yes, unsubscribe me from all alerts</button></form>"
                                    "<p class='more'><a href='/'>No, keep my alerts</a></p>")

    @app.route("/ads/<token>", methods=["GET", "POST"])
    def ads_choice(token):
        p = tokens.verify(token, cfg.secret, "ads")
        row = conn().execute("SELECT ads_off FROM subscribers WHERE id=?", (p["s"],)).fetchone() if p else None
        if not row:
            return page("This link has expired", "<p>Use the link in a more recent email.</p>"
                        "<a class='btn' href='/'>Back to the home page</a>"), 400
        off = bool(row["ads_off"])
        if request.method == "POST":
            off = request.form.get("off") == "1"
            conn().execute("UPDATE subscribers SET ads_off=? WHERE id=?", (int(off), p["s"]))
            conn().commit()
        explain = _ADS_EXPLAINER.format(brand=H.escape(cfg.brand))
        if off:
            return page("Sponsored messages are hidden", explain + "<form method=post><input type=hidden name=off value=0>"
                        "<button class='btn'>Show them again</button></form>", tick=request.method == "POST")
        return page("Why am I seeing this?", explain + "<form method=post><input type=hidden name=off value=1>"
                    "<button class='btn'>Hide sponsored messages</button></form>")

    @app.get("/f/<token>")
    def feedback(token):
        p = tokens.verify(token, cfg.secret, "fb")
        if not p or p.get("v") not in ("up", "down"):
            return page("This link has expired", ""), 400
        c = conn()
        alert = c.execute("SELECT * FROM alerts WHERE id=?", (p["a"],)).fetchone()
        if not alert:
            return page("This alert no longer exists", "<p>Nothing was recorded.</p>"), 404
        # One vote per paper per alert: a later click replaces an earlier one.
        c.execute("DELETE FROM feedback WHERE alert_id=? AND paper_id=?", (p["a"], p["p"]))
        c.execute("INSERT INTO feedback(alert_id,paper_id,value,created_at) VALUES(?,?,?,?)",
                  (p["a"], p["p"], p["v"], db.now_iso()))
        c.commit()
        manage = f"{cfg.frontend_url}/manage?t={link(cfg, 'manage', s=alert['subscriber_id'])}"
        if p["v"] == "up":
            return page("Thanks", "<p>We’ll rank papers like this one higher in your next digests.</p>", tick=True)
        return page("Thanks", "<p>We’ll rank papers like this one lower in your next digests.</p>"
                              f"<p>Want to block a topic completely? <a href='{H.escape(manage)}'>Add a “leave out” term</a>.</p>", tick=True)

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
        return jsonify(email=sub["email"], unsubscribed=bool(sub["unsubscribed_at"]), alerts=[alert_json(r) for r in rows])

    @app.post("/api/manage/unsubscribe")
    def manage_unsubscribe():
        sub = manage_sub()
        if not sub:
            return jsonify(error="Invalid or expired link"), 401
        conn().execute("UPDATE subscribers SET unsubscribed_at=? WHERE id=?", (db.now_iso(), sub["id"]))
        conn().commit()
        return jsonify(ok=True)

    @app.delete("/api/manage")
    def manage_delete_account():
        """Right to erasure: removes the subscriber, every alert, every queued match and their feedback."""
        sub = manage_sub()
        if not sub:
            return jsonify(error="Invalid or expired link"), 401
        c = conn()
        c.execute("DELETE FROM feedback WHERE alert_id IN (SELECT id FROM alerts WHERE subscriber_id=?)", (sub["id"],))
        c.execute("DELETE FROM subscribers WHERE id=?", (sub["id"],))   # cascades to alerts and matches
        c.commit()
        return ("", 204)

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
        changed_query = any(k in upd for k in ("include", "exclude", "sources", "oa_only", "preprints"))
        if "include" in upd and "name" not in upd and upd["include"] != json.loads(row["include"]):
            upd["name"] = ", ".join(upd["include"])[:80]   # keep a custom name unless the topics themselves changed
        for k in ("include", "exclude", "sources"):
            if k in upd:
                upd[k] = json.dumps(upd[k])
        for k in ("oa_only", "preprints"):
            if k in upd:
                upd[k] = int(upd[k])
        if changed_query:
            # Papers queued under the old settings must not go out under the new ones.
            c.execute("DELETE FROM matches WHERE alert_id=? AND sent_at IS NULL", (aid,))
        if upd:
            c.execute(f"UPDATE alerts SET {','.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), aid))
            c.commit()
        return jsonify(alert_json(c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()))

    return app
