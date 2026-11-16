import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .matcher import AlertSpec, Matcher
from .text import tokens

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doi TEXT UNIQUE,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  title TEXT NOT NULL,
  abstract TEXT NOT NULL DEFAULT '',
  authors TEXT NOT NULL DEFAULT '[]',
  venue TEXT NOT NULL DEFAULT '',
  published TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  is_oa INTEGER NOT NULL DEFAULT 0,
  is_preprint INTEGER NOT NULL DEFAULT 0,
  ingested_at TEXT NOT NULL,
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS papers_published ON papers(published);
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
  title, abstract, content='papers', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
  INSERT INTO papers_fts(rowid,title,abstract) VALUES (new.id,new.title,new.abstract); END;
CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
  INSERT INTO papers_fts(papers_fts,rowid,title,abstract) VALUES('delete',old.id,old.title,old.abstract); END;
CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
  INSERT INTO papers_fts(papers_fts,rowid,title,abstract) VALUES('delete',old.id,old.title,old.abstract);
  INSERT INTO papers_fts(rowid,title,abstract) VALUES (new.id,new.title,new.abstract); END;

CREATE TABLE IF NOT EXISTS subscribers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  confirmed_at TEXT,            -- double opt-in consent timestamp
  consent_text TEXT,            -- which wording they agreed to
  unsubscribed_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  include TEXT NOT NULL,        -- JSON list of raw terms
  exclude TEXT NOT NULL DEFAULT '[]',
  sources TEXT NOT NULL DEFAULT '[]',
  oa_only INTEGER NOT NULL DEFAULT 0,
  preprints INTEGER NOT NULL DEFAULT 1,
  frequency TEXT NOT NULL DEFAULT 'weekly',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | active | paused
  created_at TEXT NOT NULL,
  activated_at TEXT,
  last_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS matches(
  alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  score INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  PRIMARY KEY(alert_id, paper_id)
);
CREATE INDEX IF NOT EXISTS matches_pending ON matches(alert_id) WHERE sent_at IS NULL;
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_id INTEGER NOT NULL, paper_id INTEGER NOT NULL,
  value TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(now_dt())


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)          # the web app and the worker share this file; wait, don't fail
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def get_state(conn, key, default=None):
    r = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_state(conn, key, value):
    conn.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


# ---------- papers ----------

def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    for pre in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if d.startswith(pre):
            d = d[len(pre):]
    return d or None


def upsert_paper(conn, p: dict) -> tuple[int, bool]:
    """Insert a paper, merging duplicates by DOI. Returns (id, is_new).

    If the same work arrives twice (e.g. a preprint, then its journal version) we keep one row,
    upgrade it to the published venue, and do NOT re-notify anyone."""
    doi = norm_doi(p.get("doi"))
    row = None
    if doi:
        row = conn.execute("SELECT * FROM papers WHERE doi=?", (doi,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM papers WHERE source=? AND source_id=?",
                           (p["source"], p["source_id"])).fetchone()
    if row is not None:
        upd = {}
        if row["is_preprint"] and not p.get("is_preprint"):
            upd.update(venue=p.get("venue", ""), is_preprint=0, url=p.get("url") or row["url"])
        if not row["abstract"] and p.get("abstract"):
            upd["abstract"] = p["abstract"]
        if not row["is_oa"] and p.get("is_oa"):
            upd["is_oa"] = 1
        if upd:
            sets = ",".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE papers SET {sets} WHERE id=?", (*upd.values(), row["id"]))
        return row["id"], False
    cur = conn.execute(
        """INSERT INTO papers(doi,source,source_id,title,abstract,authors,venue,published,url,is_oa,is_preprint,ingested_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (doi, p["source"], p["source_id"], p["title"].strip(), p.get("abstract", "").strip(),
         json.dumps(p.get("authors", [])), p.get("venue", ""), p["published"], p.get("url", ""),
         int(bool(p.get("is_oa"))), int(bool(p.get("is_preprint"))), now_iso()))
    return cur.lastrowid, True


# ---------- alerts ----------

def spec_from_row(r) -> AlertSpec:
    return AlertSpec.build(r["id"], json.loads(r["include"]), json.loads(r["exclude"]),
                           json.loads(r["sources"]), r["oa_only"], r["preprints"], r["activated_at"] or "")


# ---------- matching ----------

def run_matching(conn, batch=1000) -> int:
    """Match papers ingested since the last run against all active alerts. Idempotent."""
    rows = conn.execute("""SELECT a.* FROM alerts a JOIN subscribers s ON s.id=a.subscriber_id
                           WHERE a.status='active' AND s.confirmed_at IS NOT NULL
                             AND s.unsubscribed_at IS NULL""").fetchall()
    last = int(get_state(conn, "matcher_last_paper_id", 0))
    if not rows:
        top = conn.execute("SELECT COALESCE(MAX(id),0) m FROM papers").fetchone()["m"]
        set_state(conn, "matcher_last_paper_id", top)
        conn.commit()
        return 0
    matcher = Matcher([spec_from_row(r) for r in rows])
    n, ts = 0, now_iso()
    while True:
        papers = conn.execute("SELECT * FROM papers WHERE id>? ORDER BY id LIMIT ?", (last, batch)).fetchall()
        if not papers:
            break
        for p in papers:
            for alert, score in matcher.match(p):
                if p["ingested_at"] < alert.activated_at:
                    continue  # alerts only cover papers that arrive after they were confirmed
                cur = conn.execute("INSERT OR IGNORE INTO matches(alert_id,paper_id,score,created_at) VALUES(?,?,?,?)",
                                   (alert.id, p["id"], score, ts))
                n += cur.rowcount
            last = p["id"]
        set_state(conn, "matcher_last_paper_id", last)
        conn.commit()
    return n


# ---------- live preview ----------

def fts_query(spec: AlertSpec) -> str:
    return " OR ".join(t.fts() for t in spec.include)


def preview(conn, spec: AlertSpec, days=30, limit=50):
    """Matches among recent papers. FTS5 narrows candidates fast; the matcher then applies exact rules."""
    if not spec.include:
        return {"count": 0, "capped": False, "papers": []}
    since = (now_dt() - timedelta(days=days)).date().isoformat()
    cands = conn.execute(
        """SELECT p.* FROM papers_fts f JOIN papers p ON p.id=f.rowid
           WHERE papers_fts MATCH ? AND p.published>=? ORDER BY p.published DESC LIMIT 2000""",
        (fts_query(spec), since)).fetchall()
    capped = len(cands) >= 2000                    # more matched than we examined: the count is "at least"
    m = Matcher([spec])
    hits = []
    for p in cands:
        res = m.match(p)
        if res:
            hits.append((res[0][1], p))
    hits.sort(key=lambda x: (x[0], x[1]["published"]), reverse=True)
    return {"count": len(hits), "capped": capped, "papers": [paper_dict(p, s) for s, p in hits[:limit]]}


def paper_dict(p, score=0):
    return {"id": p["id"], "title": p["title"], "abstract": p["abstract"], "authors": json.loads(p["authors"]),
            "venue": p["venue"], "source": p["source"], "published": p["published"], "url": p["url"],
            "is_oa": bool(p["is_oa"]), "is_preprint": bool(p["is_preprint"]), "score": score}


# ---------- housekeeping ----------

def cleanup(conn, retention_days=120, signup_days=7, now=None) -> dict:
    """Keep only what is needed: drop sign-ups never confirmed, and papers too old to matter."""
    now = now or now_dt()
    stale_signups = iso(now - timedelta(days=signup_days))
    old_papers = (now - timedelta(days=retention_days)).date().isoformat()
    a = conn.execute("DELETE FROM subscribers WHERE confirmed_at IS NULL AND created_at < ?", (stale_signups,)).rowcount
    # Papers someone gave feedback on stay: they are what the ranking learns from.
    p = conn.execute("""DELETE FROM papers WHERE published < ? AND id NOT IN (SELECT paper_id FROM feedback)""",
                     (old_papers,)).rowcount
    conn.commit()
    return {"unconfirmed_signups_removed": a, "old_papers_removed": p}


def backup(conn, directory: str, keep: int = 14, now=None) -> str:
    """Snapshot of everything that can't be re-created: subscribers, alerts, queued matches and feedback.

    Papers are public and re-downloadable, and they are ~99% of the file (about 2 GB at launch volume), so the
    snapshot keeps only the papers people gave feedback on. A restore brings back every person and alert;
    the next ingest refills the papers. Safe to run while the app is live."""
    import os
    now = now or now_dt()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"margin-data-{now.strftime('%Y%m%d-%H%M%S')}.db")
    dest = sqlite3.connect(path)
    with dest:
        conn.backup(dest)
    dest.execute("DELETE FROM papers WHERE id NOT IN (SELECT paper_id FROM feedback)")
    dest.execute("DELETE FROM matches WHERE paper_id NOT IN (SELECT id FROM papers)")
    dest.execute("INSERT INTO papers_fts(papers_fts) VALUES('rebuild')")
    dest.commit()
    dest.execute("VACUUM")
    dest.close()
    old = sorted(f for f in os.listdir(directory) if f.startswith("margin-data-") and f.endswith(".db"))
    for f in old[:-keep]:
        os.remove(os.path.join(directory, f))
    return path
