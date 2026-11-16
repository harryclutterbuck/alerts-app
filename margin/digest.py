"""Turn pending matches into one tidy email per subscriber and send it."""
import html as H
import json
import re
from datetime import datetime, timedelta

from . import db, rank, tokens
from .config import Config

YEAR = 365 * 24 * 3600

INK, MUTE, LINE, ACCENT, HL = "#101828", "#5B6577", "#D9DFEA", "#3A39E8", "#FFE98A"
SANS = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman',serif"


def latest_slot(freq, now, cfg):
    """The most recent moment a digest of this frequency was scheduled to go out (or None for instant)."""
    if freq == "instant":
        return None
    slot = now.replace(hour=cfg.digest_hour_utc, minute=0, second=0, microsecond=0)
    if freq == "daily":
        return slot if now >= slot else slot - timedelta(days=1)
    back = (now.weekday() - cfg.digest_weekday) % 7          # weekly: the most recent chosen weekday
    slot -= timedelta(days=back)
    return slot if now >= slot else slot - timedelta(days=7)


def is_due(alert, now, cfg, force=False) -> bool:
    """Daily and weekly digests leave at a fixed time (default 07:00 UTC, Mondays for weekly), so they don't
    drift around the clock. A new alert waits for the next slot after it was activated."""
    if force or alert["frequency"] == "instant":
        return True
    baseline = alert["last_sent_at"] or alert["activated_at"] or alert["created_at"]
    return datetime.fromisoformat(baseline) < latest_slot(alert["frequency"], now, cfg)


def link(cfg, kind, **kw):
    ttl = {"confirm": 7 * 24 * 3600, "manage": 2 * YEAR}.get(kind, YEAR)   # sign-up links expire with the unconfirmed-signup cleanup
    return tokens.sign({"t": kind, **kw}, cfg.secret, ttl=ttl)


def _snippet(text, n=300):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _highlight(escaped, words):
    for w in sorted(words, key=len, reverse=True):
        escaped = re.sub(f"({re.escape(H.escape(w))})", rf'<span style="background:{HL}">\1</span>', escaped, flags=re.I)
    return escaped


def _words(alert):
    out = set()
    for t in json.loads(alert["include"]):
        out.update(w for w in re.findall(r"\w+", t) if len(w) > 2 and w.lower() != "and")
    return out


def collect(conn, sub, now, cfg: Config, force=False):
    """Pending matches for each due alert of one subscriber: [(alert, [papers], extra_count)]."""
    groups = []
    for a in conn.execute("SELECT * FROM alerts WHERE subscriber_id=? AND status='active'", (sub["id"],)):
        if not is_due(a, now, cfg, force):
            continue
        rows = conn.execute("""SELECT p.*, m.score FROM matches m JOIN papers p ON p.id=m.paper_id
                               WHERE m.alert_id=? AND m.sent_at IS NULL
                               ORDER BY m.score DESC, p.published DESC""", (a["id"],)).fetchall()
        if rows:
            rows = rank.rerank(conn, a, rows)     # best-first, using this person's "more like this" / "not relevant" history
            groups.append((a, rows[:cfg.max_per_alert], max(0, len(rows) - cfg.max_per_alert)))
    return groups


def render(cfg: Config, sub, groups):
    total = sum(len(p) + x for _, p, x in groups)
    first = groups[0][0]
    if len(groups) == 1:
        subject = f"{total} new {'paper' if total == 1 else 'papers'} for “{first['name']}”"
    else:
        subject = f"{total} new papers across your {len(groups)} alerts"
    manage = f"{cfg.frontend_url}/manage?t={link(cfg, 'manage', s=sub['id'])}"
    unsub = f"{cfg.base_url}/unsubscribe/{link(cfg, 'unsub', s=sub['id'])}"

    h, t = [], [f"{cfg.brand}: {subject}", ""]
    for a, papers, extra in groups:
        words = _words(a)
        h.append(f'<h2 style="font:600 13px {SANS};color:{MUTE};margin:28px 0 4px">{H.escape(a["name"])}</h2>')
        t += [f"== {a['name']} ==", ""]
        for p in papers:
            authors = json.loads(p["authors"])
            by = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
            tags = " · ".join(x for x in [p["venue"], p["published"], "Open access" if p["is_oa"] else "",
                                          "Preprint" if p["is_preprint"] else ""] if x)
            up = f"{cfg.base_url}/f/{link(cfg, 'fb', a=a['id'], p=p['id'], v='up')}"
            down = f"{cfg.base_url}/f/{link(cfg, 'fb', a=a['id'], p=p['id'], v='down')}"
            h.append(
                f'<div style="padding:14px 0;border-top:1px solid {LINE}">'
                f'<a href="{H.escape(p["url"])}" style="font:600 18px/1.3 {SERIF};color:{ACCENT};text-decoration:none">'
                f'{H.escape(p["title"])}</a>'
                f'<div style="font:12.5px {SANS};color:{MUTE};margin:3px 0 6px">{H.escape(by)}<br>{H.escape(tags)}</div>'
                f'<div style="font:14px/1.5 {SANS};color:{INK}">{_highlight(H.escape(_snippet(p["abstract"])), words)}</div>'
                f'<div style="font:12.5px {SANS};margin-top:7px"><a href="{up}" style="color:{ACCENT}">More like this</a>'
                f' &nbsp;·&nbsp; <a href="{down}" style="color:{MUTE}">Not relevant</a></div></div>')
            t += [p["title"], f"{by} | {tags}", _snippet(p["abstract"]), p["url"], ""]
        if extra:
            h.append(f'<div style="font:13px {SANS};color:{MUTE};padding:10px 0;border-top:1px solid {LINE}">'
                     f'{extra} more matched this period. Add “leave out” terms to narrow this alert.</div>')
            t += [f"+ {extra} more matched. Add 'leave out' terms to narrow this alert.", ""]
    body = "".join(h)
    who = f" {H.escape(cfg.brand)} is run by {H.escape(cfg.operator)}; reply or write to {H.escape(cfg.contact_email)}." if cfg.operator else ""
    html = (f'<!doctype html><html><body style="margin:0;background:#EDF1F7;padding:24px 12px">'
            f'<div style="max-width:620px;margin:0 auto;background:#fff;border:1px solid {LINE};border-radius:14px;padding:26px 28px">'
            f'<div style="font:600 22px {SERIF};color:{INK}">{H.escape(cfg.brand)}</div>'
            f'<div style="font:italic 16px {SERIF};color:{INK};margin-top:14px">Here’s what’s new for your alerts.</div>'
            f'{body}'
            f'<div style="border-top:1px solid {LINE};margin-top:26px;padding-top:14px;font:12px {SANS};color:{MUTE}">'
            f'<a href="{manage}" style="color:{ACCENT}">Edit or pause your alerts</a> &nbsp;·&nbsp; '
            f'<a href="{unsub}" style="color:{MUTE}">Unsubscribe</a><br>'
            f'You’re receiving this because you confirmed an alert at {H.escape(cfg.brand)}.{who}<br>Paper data from OpenAlex, PubMed and arXiv. Thank you to arXiv for use of its open access interoperability.</div></div></body></html>')
    text = "\n".join(t) + f"\nEdit or pause: {manage}\nUnsubscribe: {unsub}\n" + (f"\n{cfg.brand} is run by {cfg.operator} ({cfg.contact_email}).\n" if cfg.operator else "")
    headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return subject, html, text, headers


def send_due(conn, cfg: Config, mailer, now=None, force=False) -> int:
    """Send one email per subscriber with all their due alerts. Matches are marked sent only after
    the mailer succeeds, so a crash re-sends rather than silently drops."""
    now = now or db.now_dt()
    sent = 0
    subs = conn.execute("SELECT * FROM subscribers WHERE confirmed_at IS NOT NULL AND unsubscribed_at IS NULL").fetchall()
    for sub in subs:
        groups = collect(conn, sub, now, cfg, force)
        if not groups:
            continue
        subject, html, text, headers = render(cfg, sub, groups)
        try:
            mailer.send(sub["email"], subject, html, text, headers)
        except Exception as e:
            print(f"[mail] failed for subscriber {sub['id']}: {e}")
            continue
        ts = db.iso(now)
        for a, _, _ in groups:
            conn.execute("UPDATE matches SET sent_at=? WHERE alert_id=? AND sent_at IS NULL", (ts, a["id"]))
            conn.execute("UPDATE alerts SET last_sent_at=? WHERE id=?", (ts, a["id"]))
        conn.commit()
        sent += 1
    return sent
