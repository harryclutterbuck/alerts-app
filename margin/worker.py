"""The background job: keeps papers flowing in, alerts matched, and digests going out.

Run it as its own process (`python -m margin worker`). Each pass is cheap; the expensive steps run on their own
timers so a restart never causes a burst of API calls or a duplicate email."""
import logging
import signal
import time
from datetime import datetime, timedelta

from . import db, digest
from .config import Config
from .ingest import run_ingest
from .mailer import make_mailer

log = logging.getLogger("margin.worker")


def tick(conn, cfg: Config, mailer, now=None, fetch_map=None) -> dict:
    now = now or db.now_dt()
    out = {}

    def due(key, hours):
        last = db.get_state(conn, key)
        return not last or now - datetime.fromisoformat(last) >= timedelta(hours=hours)

    if due("worker_last_ingest", cfg.ingest_every_hours):
        out["ingest"] = run_ingest(conn, cfg, days=cfg.ingest_days, fetch_map=fetch_map)
        db.set_state(conn, "worker_last_ingest", db.iso(now))
        conn.commit()
    out["new_matches"] = db.run_matching(conn)
    out["emails_sent"] = digest.send_due(conn, cfg, mailer, now=now)
    if due("worker_last_housekeeping", 24):
        out["cleanup"] = db.cleanup(conn, cfg.paper_retention_days, now=now)
        out["backup"] = db.backup(conn, cfg.backup_dir, now=now)
        db.set_state(conn, "worker_last_housekeeping", db.iso(now))
        conn.commit()
    return out


def run_forever(cfg: Config, interval_minutes: int = 15):
    mailer = make_mailer(cfg)
    stop = {"now": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.update(now=True))
    log.info("worker started: checking every %d min, pulling papers every %dh", interval_minutes, cfg.ingest_every_hours)
    while not stop["now"]:
        try:
            conn = db.connect(cfg.db_path)
            try:
                log.info("pass complete: %s", tick(conn, cfg, mailer))
            finally:
                conn.close()
        except Exception:
            log.exception("worker pass failed; will try again next time")
        for _ in range(interval_minutes * 60):
            if stop["now"]:
                break
            time.sleep(1)
    log.info("worker stopped")
