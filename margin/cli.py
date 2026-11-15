import argparse
import json
import logging

from . import db, digest, rank
from .config import Config
from .ingest import run_ingest
from .mailer import make_mailer


def main(argv=None):
    ap = argparse.ArgumentParser(prog="margin", description="Research alerts backend")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the database")
    i = sub.add_parser("ingest", help="pull new papers from the sources")
    i.add_argument("--days", type=int, default=None, help="lookback window for every source (default: PubMed 1, arXiv 2, OpenAlex 7)")
    i.add_argument("--only", nargs="*", choices=["openalex", "arxiv", "pubmed"])
    sub.add_parser("match", help="match new papers against active alerts")
    sd = sub.add_parser("send", help="send digests that are due")
    sd.add_argument("--force", action="store_true", help="ignore the daily/weekly schedule and send everything pending (for testing)")
    w = sub.add_parser("worker", help="run the background job: ingest, match and send on a schedule")
    w.add_argument("--interval", type=int, default=15, help="minutes between passes (default 15)")
    w.add_argument("--once", action="store_true", help="run a single pass and exit")
    te = sub.add_parser("test-email", help="send a test message through your email provider")
    te.add_argument("to")
    sub.add_parser("cleanup", help="remove stale sign-ups and old papers")
    sub.add_parser("backup", help="write a snapshot of the database to the backup folder")
    r = sub.add_parser("run", help="ingest + match + send (what cron should call)")
    r.add_argument("--days", type=int, default=None)
    s = sub.add_parser("serve", help="run the API (development server)")
    s.add_argument("--port", type=int, default=5000)
    sub.add_parser("openalex-status", help="check your OpenAlex key and remaining daily budget")
    sub.add_parser("stats", help="how people rate the papers they are sent")
    sub.add_parser("demo", help="seed sample data and write a sample digest to ./outbox (works offline)")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = Config.from_env()

    if a.cmd == "serve":
        from .api import create_app
        create_app(cfg).run(port=a.port, debug=False)
        return
    conn = db.connect(cfg.db_path)
    if a.cmd == "init-db":
        print(f"database ready at {cfg.db_path}")
    elif a.cmd == "ingest":
        print(json.dumps(run_ingest(conn, cfg, a.days, a.only), indent=2))
    elif a.cmd == "match":
        print(f"{db.run_matching(conn)} new matches")
    elif a.cmd == "send":
        print(f"{digest.send_due(conn, cfg, make_mailer(cfg), force=a.force)} emails sent")
    elif a.cmd == "worker":
        from . import worker
        if a.once:
            print(json.dumps(worker.tick(conn, cfg, make_mailer(cfg)), indent=2, default=str))
        else:
            conn.close()
            worker.run_forever(cfg, a.interval)
    elif a.cmd == "test-email":
        mailer = make_mailer(cfg)
        mailer.send(a.to, f"{cfg.brand} test email", f"<p>If you can read this, {cfg.brand} can send email. "
                    "Check it landed in your inbox (not spam), then view the original message and confirm SPF, DKIM and DMARC all say PASS.</p>",
                    f"If you can read this, {cfg.brand} can send email. Check it landed in your inbox, not spam.")
        print("sent. Check the inbox (and spam folder) of", a.to)
    elif a.cmd == "cleanup":
        print(db.cleanup(conn, cfg.paper_retention_days))
    elif a.cmd == "backup":
        print("snapshot written to", db.backup(conn, cfg.backup_dir))
    elif a.cmd == "run":
        print(json.dumps(run_ingest(conn, cfg, a.days), indent=2))
        print(f"{db.run_matching(conn)} new matches")
        print(f"{digest.send_due(conn, cfg, make_mailer(cfg))} emails sent")
    elif a.cmd == "openalex-status":
        import urllib.error
        from .sources import openalex
        if not cfg.openalex_api_key:
            print("OPENALEX_API_KEY is not set. Create a free key at openalex.org/settings/api, then:\n  export OPENALEX_API_KEY=your-key")
        try:
            print(json.dumps(openalex.budget(cfg.openalex_api_key), indent=2))
        except urllib.error.HTTPError as e:
            print(openalex.explain(e.code))
    elif a.cmd == "stats":
        rows = rank.feedback_stats(conn)
        print(f"{'alert':32} {'sent':>5} {'liked':>6} {'rejected':>9} {'relevant':>9} {'responded':>10}")
        for r in rows:
            pct = lambda x: "-" if x is None else f"{int(x * 100)}%"
            print(f"{r['name'][:32]:32} {r['sent']:>5} {r['up']:>6} {r['down']:>9} {pct(r['relevant_rate']):>9} {pct(r['response_rate']):>10}")
    elif a.cmd == "demo":
        demo(conn, cfg)


def demo(conn, cfg):
    from datetime import timedelta
    from .demo_data import PAPERS
    cfg.mailer = "console"
    past = db.iso(db.now_dt() - timedelta(hours=1))
    conn.execute("INSERT OR IGNORE INTO subscribers(email,created_at,confirmed_at,consent_text) VALUES(?,?,?,?)",
                 ("demo@example.com", past, past, "demo"))
    sid = conn.execute("SELECT id FROM subscribers WHERE email='demo@example.com'").fetchone()["id"]
    for name, inc, exc in [("CRISPR", ["CRISPR"], ["review"]), ("Large language models", ['"large language models"'], [])]:
        conn.execute("""INSERT INTO alerts(subscriber_id,name,include,exclude,sources,oa_only,preprints,frequency,status,created_at,activated_at)
                        VALUES(?,?,?,?,?,0,1,'weekly','active',?,?)""",
                     (sid, name, json.dumps(inc), json.dumps(exc), json.dumps(["openalex", "pubmed", "arxiv", "biorxiv"]), past, past))
    for p in PAPERS:
        db.upsert_paper(conn, p)
    conn.commit()
    print(f"{db.run_matching(conn)} matches")
    print(f"{digest.send_due(conn, cfg, make_mailer(cfg), force=True)} email written to {cfg.outbox_dir}/ (open the .html file)")
