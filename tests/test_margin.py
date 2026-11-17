import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from margin import db, digest, tokens
from margin.api import create_app
from margin.config import Config
from margin.demo_data import PAPERS
from margin.mailer import ConsoleMailer
from margin.matcher import AlertSpec, Matcher
from margin.sources import arxiv, europepmc, openalex, pubmed


def paper(title, abstract="", source="openalex", oa=False, pre=False):
    return {"title": title, "abstract": abstract, "source": source, "is_oa": oa, "is_preprint": pre}


def hit(spec, p):
    return bool(Matcher([spec]).match(p))


class MatcherTests(unittest.TestCase):
    def test_words_phrases_and_excludes(self):
        p = paper("Base editing with CRISPR", "A review of delivery methods.")
        self.assertTrue(hit(AlertSpec.build(1, ["crispr"]), p))
        self.assertTrue(hit(AlertSpec.build(1, ["editing base"]), p))          # all words, any order
        self.assertTrue(hit(AlertSpec.build(1, ['"base editing"']), p))
        self.assertFalse(hit(AlertSpec.build(1, ['"editing base"']), p))       # phrase keeps order
        self.assertFalse(hit(AlertSpec.build(1, ["crispr"], exclude=["review"]), p))
        self.assertTrue(hit(AlertSpec.build(1, ["\u201cbase editing\u201d"]), p))  # curly quotes

    def test_and_or_and_brackets(self):
        p = paper("Tumour immunotherapy in mice", "We study cancer and rehabilitation.")
        h = lambda q, **kw: hit(AlertSpec.build(1, [q], **kw), p)
        self.assertTrue(h("tumour AND immunotherapy"))
        self.assertFalse(h("tumour AND zebrafish"))
        self.assertTrue(h("zebrafish OR tumour"))
        self.assertFalse(h("zebrafish OR newt"))
        self.assertTrue(h("tumour and immunotherapy"))                    # any case
        self.assertTrue(h("(zebrafish OR tumour) AND immunotherapy"))
        self.assertFalse(h("(zebrafish OR newt) AND immunotherapy"))
        self.assertTrue(h("zebrafish AND newt OR tumour"))                # AND binds tighter than OR
        self.assertFalse(h("zebrafish AND (newt OR tumour)"))
        self.assertTrue(h('"cancer and rehabilitation" OR zebrafish'))    # inside quotes they are ordinary words
        self.assertTrue(h("tumour AND"))                                  # dangling and stray symbols are forgiven
        self.assertTrue(h("OR tumour"))
        self.assertTrue(h("((tumour"))
        self.assertTrue(h("tumour))"))
        self.assertTrue(h("tumour OR OR zebrafish"))
        self.assertTrue(AlertSpec.build(1, ["AND", "OR", "( )"]).include == [])  # nothing searchable
        self.assertFalse(h("tumour", exclude=["zebrafish OR immunotherapy"]))   # leave-out takes OR too
        self.assertTrue(h("tumour", exclude=["zebrafish OR newt"]))
        self.assertTrue(h("tumour", exclude=["zebrafish AND immunotherapy"]))

    def test_or_term_is_indexed_under_every_branch_and_counted_once(self):
        spec = AlertSpec.build(1, ["tumour OR cancer"])
        self.assertEqual(len(Matcher([spec]).match(paper("Cancer and tumour", ""))), 1)
        self.assertEqual(len(Matcher([spec]).match(paper("Only cancer here", ""))), 1)
        self.assertEqual(len(Matcher([spec]).match(paper("Neither", ""))), 0)
        self.assertEqual(Matcher([spec]).match(paper("Cancer and tumour", ""))[0][1], 3)   # one term, title hit

    def test_accents_and_case(self):
        self.assertTrue(hit(AlertSpec.build(1, ["Pérez"]), paper("Work by perez et al")))

    def test_phrase_cannot_straddle_title_and_abstract(self):
        self.assertFalse(hit(AlertSpec.build(1, ['"gene therapy"']), paper("A study of gene", "therapy outcomes")))

    def test_filters(self):
        p = paper("CRISPR", source="arxiv", oa=False, pre=True)
        self.assertFalse(hit(AlertSpec.build(1, ["crispr"], sources=["pubmed"]), p))
        self.assertFalse(hit(AlertSpec.build(1, ["crispr"], oa_only=True), p))
        self.assertFalse(hit(AlertSpec.build(1, ["crispr"], preprints=False), p))
        self.assertTrue(hit(AlertSpec.build(1, ["crispr"]), p))

    def test_title_hits_rank_higher(self):
        m = Matcher([AlertSpec.build(1, ["crispr"])])
        t = m.match(paper("CRISPR trial"))[0][1]
        a = m.match(paper("A trial", "uses crispr"))[0][1]
        self.assertGreater(t, a)

    def test_many_alerts_only_candidates_tested(self):
        alerts = [AlertSpec.build(i, [f"topic{i}"]) for i in range(5000)] + [AlertSpec.build(9999, ["crispr"])]
        res = Matcher(alerts).match(paper("CRISPR news"))
        self.assertEqual([a.id for a, _ in res], [9999])


class SourceTests(unittest.TestCase):
    def test_openalex(self):
        w = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/X", "display_name": "T", "publication_date": "2026-10-01",
             "type": "preprint", "open_access": {"is_oa": True},
             "primary_location": {"source": {"display_name": "bioRxiv"}},
             "authorships": [{"author": {"display_name": "A B"}}],
             "abstract_inverted_index": {"hello": [0], "world": [1], "again": [2]}}
        p = openalex.normalise(w)
        self.assertEqual((p["abstract"], p["source"], p["is_preprint"], p["source_id"]), ("hello world again", "biorxiv", True, "W1"))

    def test_openalex_paging(self):
        pages = [{"results": [{"id": "https://openalex.org/W1", "display_name": "A", "publication_date": "2026-10-01"}],
                  "meta": {"next_cursor": "c2"}},
                 {"results": [{"id": "https://openalex.org/W2", "display_name": "B", "publication_date": "2026-10-01"}], "meta": {"next_cursor": None}}]
        it = iter(pages)
        got = list(openalex.fetch(date(2026, 10, 1), "a@b.c", http=lambda *a, **k: json.dumps(next(it)).encode()))
        self.assertEqual([p["source_id"] for p in got], ["W1", "W2"])

    def test_arxiv(self):
        xml = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2610.01234v2</id>
        <published>2026-10-28T10:00:00Z</published><title>Deep\n  nets</title><summary> Some  abstract. </summary>
        <author><name>Ada L</name></author></entry></feed>"""
        p = arxiv.parse(xml)[0]
        self.assertEqual((p["source_id"], p["title"], p["doi"], p["published"]),
                         ("2610.01234", "Deep nets", "10.48550/arxiv.2610.01234", "2026-10-28"))

    def test_pubmed(self):
        xml = b"""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
        <Journal><Title>J Test</Title></Journal><ArticleTitle>A <i>fine</i> title</ArticleTitle>
        <Abstract><AbstractText Label="A">First.</AbstractText><AbstractText>Second.</AbstractText></Abstract>
        <AuthorList><Author><LastName>Smith</LastName><Initials>J</Initials></Author></AuthorList>
        <ArticleDate><Year>2026</Year><Month>10</Month><Day>5</Day></ArticleDate></Article></MedlineCitation>
        <PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/abc</ArticleId><ArticleId IdType="pmc">PMC1</ArticleId></ArticleIdList></PubmedData>
        </PubmedArticle></PubmedArticleSet>"""
        p = pubmed.parse(xml)[0]
        self.assertEqual((p["title"], p["abstract"], p["authors"], p["published"], p["is_oa"], p["doi"]),
                         ("A fine title", "First. Second.", ["Smith J"], "2026-10-05", True, "10.1/abc"))


class TokenTests(unittest.TestCase):
    def test_roundtrip_tamper_expiry(self):
        t = tokens.sign({"t": "x", "s": 1}, "k")
        self.assertEqual(tokens.verify(t, "k", "x")["s"], 1)
        self.assertIsNone(tokens.verify(t, "other", "x"))
        self.assertIsNone(tokens.verify(t, "k", "y"))
        self.assertIsNone(tokens.verify(t[:-2] + "AA", "k", "x"))
        self.assertIsNone(tokens.verify(tokens.sign({"t": "x"}, "k", ttl=-5), "k", "x"))


def next_monday_8am():
    now = db.now_dt()
    return (now + timedelta(days=(7 - now.weekday()) % 7 or 7)).replace(hour=8, minute=0, second=0, microsecond=0)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=os.path.join(self.tmp.name, "t.db"), outbox_dir=self.tmp.name, secret="s3cret")
        self.mailer = ConsoleMailer(self.cfg)
        self.app = create_app(self.cfg, self.mailer)
        self.client = self.app.test_client()
        self.conn = db.connect(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def subscribe(self, email="a@b.co", **alert):
        body = {"email": email, "alert": {"include": ["crispr"], "exclude": ["review"], "frequency": "weekly", **alert}}
        r = self.client.post("/api/subscribe", json=body)
        self.assertEqual(r.status_code, 202)
        return r

    def confirm_last(self):
        import re
        url = re.search(r"/confirm/(\S+)", self.mailer.sent[-1]["text"]).group(1)
        return self.client.get(f"/confirm/{url}")

    def test_dedupe_merges_preprint_into_published_version(self):
        pre = dict(PAPERS[1])
        pub = dict(pre, source="openalex", source_id="W7", is_preprint=False, venue="Nature", doi=pre["doi"].upper())
        id1, new1 = db.upsert_paper(self.conn, pre)
        id2, new2 = db.upsert_paper(self.conn, pub)
        self.assertEqual((id1, new1, new2), (id2, True, False))
        row = self.conn.execute("SELECT venue,is_preprint FROM papers WHERE id=?", (id1,)).fetchone()
        self.assertEqual((row["venue"], row["is_preprint"]), ("Nature", 0))

    def test_preview_uses_fts_and_exact_rules(self):
        for p in PAPERS:
            db.upsert_paper(self.conn, p)
        self.conn.commit()
        r = self.client.post("/api/preview", json={"include": ["CRISPR"], "exclude": ["review"]}).get_json()
        self.assertEqual(r["count"], 2 + 0 + 1 - 0)          # prime editing, base editor, T cell screen
        self.assertTrue(all("review" not in (p["title"] + p["abstract"]).lower() for p in r["papers"]))
        r = self.client.post("/api/preview", json={"include": ["CRISPR"], "oa_only": True}).get_json()
        self.assertEqual({p["source"] for p in r["papers"]} <= {"pubmed", "biorxiv"}, True)
        self.assertEqual(self.client.post("/api/preview", json={"include": []}).status_code, 400)

    def test_full_flow_double_optin_digest_unsubscribe(self):
        self.subscribe()
        self.assertIn("Confirm", self.mailer.sent[-1]["subject"])
        # nothing is sent to an unconfirmed address
        for p in PAPERS:
            db.upsert_paper(self.conn, p)
        self.conn.commit()
        self.assertEqual(db.run_matching(self.conn), 0)
        mon = next_monday_8am()
        self.assertEqual(self.confirm_last().status_code, 200)
        # a weekly alert does not send the moment it is confirmed
        self.assertEqual(digest.send_due(self.conn, self.cfg, self.mailer), 0)
        # papers that arrived before confirmation are not back-filled; new ones are matched
        n_before = len(self.mailer.sent)
        db.upsert_paper(self.conn, dict(PAPERS[0], source_id="new1", doi="10.1000/new1", title="Another CRISPR advance"))
        self.conn.commit()
        self.assertEqual(db.run_matching(self.conn), 1)
        self.assertEqual(db.run_matching(self.conn), 0)       # idempotent
        self.assertEqual(digest.send_due(self.conn, self.cfg, self.mailer, now=mon), 1)
        mail = self.mailer.sent[-1]
        self.assertEqual(len(self.mailer.sent), n_before + 1)
        self.assertIn("Another CRISPR advance", mail["html"])
        self.assertEqual(mail["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        # not due again until a week has passed, and nothing pending anyway
        self.assertEqual(digest.send_due(self.conn, self.cfg, self.mailer, now=mon), 0)
        # one-click unsubscribe
        import re
        unsub = re.search(r"/unsubscribe/(\S+)", mail["text"]).group(1)
        self.assertEqual(self.client.get(f"/unsubscribe/{unsub}").status_code, 200)
        self.assertIsNone(self.conn.execute("SELECT unsubscribed_at FROM subscribers").fetchone()[0])  # GET does nothing
        self.assertEqual(self.client.post(f"/unsubscribe/{unsub}").status_code, 200)
        db.upsert_paper(self.conn, dict(PAPERS[0], source_id="new2", doi="10.1000/new2", title="Yet another CRISPR paper"))
        self.conn.commit()
        self.assertEqual(db.run_matching(self.conn), 0)

    def test_manage_and_validation(self):
        self.subscribe()
        self.confirm_last()
        tok = tokens.sign({"t": "manage", "s": 1}, "s3cret")
        h = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.client.get("/api/manage").status_code, 401)
        alerts = self.client.get("/api/manage", headers=h).get_json()["alerts"]
        self.assertEqual(alerts[0]["status"], "active")
        r = self.client.patch(f"/api/manage/alerts/{alerts[0]['id']}", headers=h, json={"status": "paused", "frequency": "daily"})
        self.assertEqual((r.get_json()["status"], r.get_json()["frequency"]), ("paused", "daily"))
        self.assertEqual(self.client.delete(f"/api/manage/alerts/{alerts[0]['id']}", headers=h).status_code, 204)
        bad = self.client.post("/api/subscribe", json={"email": "nope", "alert": {"include": ["x"]}})
        self.assertEqual(bad.status_code, 400)
        bad = self.client.post("/api/subscribe", json={"email": "a@b.co", "alert": {"include": ["x"], "frequency": "hourly"}})
        self.assertEqual(bad.status_code, 400)

    def test_page_and_dev_outbox(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"/api/preview", r.data)
        self.subscribe()
        r = self.client.get("/dev/outbox")
        self.assertEqual(r.status_code, 200)
        import re
        href = re.search(r"href='(/dev/outbox/[^']+)'", r.get_data(as_text=True)).group(1)
        self.assertIn(b"Confirm my alert", self.client.get(href).data)

    def test_manage_page_edit_resets_queue_unsubscribe_and_erase(self):
        self.subscribe()
        self.confirm_last()
        tok = tokens.sign({"t": "manage", "s": 1}, "s3cret")
        h = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.client.get("/manage").status_code, 200)
        self.assertIn(b"/api/manage", self.client.get("/manage").data)
        # a queued match must be dropped when the alert's query changes
        db.upsert_paper(self.conn, dict(PAPERS[0], source_id="n1", doi="10.1000/n1", title="Fresh CRISPR result"))
        self.conn.commit()
        self.assertEqual(db.run_matching(self.conn), 1)
        r = self.client.patch("/api/manage/alerts/1", headers=h, json={"include": ["prime editing"], "frequency": "daily"})
        j = r.get_json()
        self.assertEqual((j["include"], j["name"], j["frequency"]), (["prime editing"], "prime editing", "daily"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
        # another subscriber's alert is out of reach
        self.subscribe("other@b.co")
        self.confirm_last()
        self.assertEqual(self.client.patch("/api/manage/alerts/2", headers=h, json={"status": "paused"}).status_code, 404)
        # unsubscribe from the page
        self.assertEqual(self.client.post("/api/manage/unsubscribe", headers=h).status_code, 200)
        self.assertTrue(self.client.get("/api/manage", headers=h).get_json()["unsubscribed"])
        self.assertEqual(self.client.post("/api/manage/unsubscribe").status_code, 401)
        # erase everything
        self.client.get(f"/f/{tokens.sign({'t': 'fb', 'a': 1, 'p': 1, 'v': 'up'}, 's3cret')}")
        self.assertEqual(self.client.delete("/api/manage", headers=h).status_code, 204)
        for table in ("alerts", "feedback"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {'subscriber_id' if table=='alerts' else 'alert_id'}=1").fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT id FROM subscribers WHERE id=1").fetchone())
        self.assertEqual(self.client.get("/api/manage", headers=h).status_code, 401)   # the old link no longer works

    def test_openalex_key_budget_and_refusals(self):
        import urllib.error
        seen = []
        def ok(url, params=None, **k):
            seen.append(params)
            return json.dumps({"results": [{"id": "https://openalex.org/W%d" % len(seen), "display_name": "T",
                                            "publication_date": "2026-11-01"}], "meta": {"next_cursor": "more"}}).encode()
        got = list(openalex.fetch(date(2026, 11, 1), "a@b.c", "KEY123", max_pages=3, http=ok))
        self.assertEqual(len(got), 3)                                  # the page cap stops an unbounded crawl
        self.assertTrue(all(p["api_key"] == "KEY123" for p in seen))
        self.assertTrue(all(p["sort"] == "publication_date:desc" for p in seen))
        self.assertNotIn("api_key", (lambda: (seen.clear(), list(openalex.fetch(date(2026, 11, 1), "a@b.c", "", 1, ok)), seen[0])[2])())
        def refuse(code):
            def f(*a, **k):
                raise urllib.error.HTTPError("u", code, "x", {}, None)
            return f
        for code, word in ((403, "verification"), (429, "midnight"), (401, "OPENALEX_API_KEY")):
            with self.assertRaises(RuntimeError) as cm:
                list(openalex.fetch(date(2026, 11, 1), "a@b.c", "k", http=refuse(code)))
            self.assertIn(word, str(cm.exception))
        # a refusal is reported, saved papers are kept, and other sources still run
        from margin.ingest import run_ingest
        def two_then_refuse():
            yield dict(PAPERS[0], source_id="oa1", doi="10.1/oa1", source="openalex")
            raise RuntimeError("budget used")
        stats = run_ingest(self.conn, self.cfg, fetch_map={"openalex": two_then_refuse, "arxiv": lambda: iter([PAPERS[2]])})
        self.assertEqual((stats["openalex"]["new"], stats["arxiv"]["new"]), (1, 1))
        self.assertIn("openalex", stats["_errors"])

    def test_preview_understands_and_or_and_brackets(self):
        for p in PAPERS:
            db.upsert_paper(self.conn, p)
        self.conn.commit()
        count = lambda q, **kw: self.client.post("/api/preview", json={"include": [q], **kw}).get_json()["count"]
        crispr, editing = count("CRISPR"), count("editing")
        self.assertGreater(crispr, 0)
        self.assertEqual(count("CRISPR AND editing"), count("CRISPR editing"))
        self.assertEqual(count("crispr OR zzznotaword"), crispr)
        self.assertEqual(count("zzznotaword AND crispr"), 0)
        self.assertEqual(count("CRISPR OR editing"), count("(CRISPR) OR (editing)"))
        self.assertGreaterEqual(count("CRISPR OR editing"), max(crispr, editing))
        self.assertEqual(count("(zzznotaword OR crispr) AND editing"), count("crispr AND editing"))
        self.assertEqual(count("CRISPR", exclude=["editing OR zzznotaword"]), count("CRISPR", exclude=["editing"]))
        self.assertEqual(self.client.post("/api/preview", json={"include": ["AND"]}).status_code, 400)

    def test_confirmation_email_and_landing_page_are_branded(self):
        self.subscribe(include=["(stroke OR <b>x</b>) AND rehab"], exclude=[], frequency="weekly")
        mail = self.mailer.sent[-1]
        self.assertIn("Confirm my alert", mail["html"])
        self.assertIn("Button not working?", mail["html"])
        self.assertNotIn("<b>x</b>", mail["html"])                          # the alert name is escaped
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", mail["html"])
        self.assertIn("Monday", mail["html"])
        self.assertIn("/confirm/", mail["text"])
        r = self.confirm_last()
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("You’re subscribed", page)
        self.assertIn("Monday", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)                          # escaped on the page too
        self.assertIn("Instrument+Sans", page)                              # same fonts as the home page
        bad = self.client.get("/confirm/not-a-real-token")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("Create an alert", bad.get_data(as_text=True))

    def test_when_wording_for_each_frequency(self):
        from margin.digest import when_text
        cfg = Config()
        self.assertIn("shortly after", when_text(cfg, "instant"))
        self.assertIn("each morning", when_text(cfg, "daily"))
        self.assertIn("Monday", when_text(cfg, "weekly"))

    def test_and_or_alert_can_be_subscribed(self):
        self.subscribe(include=["(zzznotaword OR crispr) AND editing"], exclude=[])
        self.assertTrue(self.mailer.sent)

    def test_preview_says_when_a_broad_search_was_cut_short(self):
        self.conn.executemany("INSERT INTO papers(doi,source,source_id,title,abstract,authors,venue,published,url,is_oa,is_preprint,ingested_at) "
                              "VALUES(?,?,?,?,?,'[]','J',?,'u',0,0,'x')",
                              [(f"10.2/{i}", "pubmed", f"b{i}", "zzbroad finding", "text", db.now_dt().date().isoformat()) for i in range(2005)])
        self.conn.commit()
        r = self.client.post("/api/preview", json={"include": ["zzbroad"]}).get_json()
        self.assertEqual((r["count"], r["capped"]), (2000, True))
        r = self.client.post("/api/preview", json={"include": ["zzbroad finding"], "exclude": ["text"]}).get_json()
        self.assertEqual(r["capped"], True)                              # still truncated before the exclude was applied
        self.assertFalse(self.client.post("/api/preview", json={"include": ["nonexistentword"]}).get_json()["capped"])

    def test_schedule_is_fixed_not_drifting(self):
        cfg = self.cfg
        mk = lambda freq, last=None, act="2026-11-09T10:00:00+00:00": {"frequency": freq, "last_sent_at": last, "activated_at": act, "created_at": act}
        D = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        # 2026-11-09 is a Monday. Weekly alert activated Mon 10:00 must wait for the NEXT Monday 07:00.
        w = mk("weekly")
        self.assertFalse(digest.is_due(w, D("2026-11-09T11:00:00"), cfg))
        self.assertFalse(digest.is_due(w, D("2026-11-15T23:00:00"), cfg))          # Sunday: not yet
        self.assertFalse(digest.is_due(w, D("2026-11-16T06:59:00"), cfg))          # Monday before 07:00
        self.assertTrue(digest.is_due(w, D("2026-11-16T07:00:00"), cfg))
        sent = mk("weekly", last="2026-11-16T07:30:00+00:00")                       # sent a little late: no drift
        self.assertFalse(digest.is_due(sent, D("2026-11-22T12:00:00"), cfg))
        self.assertTrue(digest.is_due(sent, D("2026-11-23T07:00:00"), cfg))
        d = mk("daily", last="2026-11-10T07:05:00+00:00")
        self.assertFalse(digest.is_due(d, D("2026-11-10T20:00:00"), cfg))
        self.assertTrue(digest.is_due(d, D("2026-11-11T07:00:00"), cfg))
        self.assertTrue(digest.is_due(mk("instant", last="2026-11-10T07:05:00+00:00"), D("2026-11-10T07:06:00"), cfg))
        self.assertTrue(digest.is_due(w, D("2026-11-10T01:00:00"), cfg, force=True))
        # a down worker misses Monday; the digest still goes out as soon as it is back
        self.assertTrue(digest.is_due(sent, D("2026-11-25T15:00:00"), cfg))

    def test_cleanup_and_backup(self):
        import glob
        self.subscribe("stale@b.co")                       # never confirmed
        self.conn.execute("UPDATE subscribers SET created_at='2020-01-01T00:00:00+00:00'")
        old, _ = db.upsert_paper(self.conn, dict(PAPERS[0], source_id="o", doi="10.1/o", published="2020-01-01"))
        kept, _ = db.upsert_paper(self.conn, dict(PAPERS[1], source_id="k", doi="10.1/k", published="2020-01-01"))
        self.conn.execute("INSERT INTO feedback(alert_id,paper_id,value,created_at) VALUES(1,?,'up','x')", (kept,))
        fresh, _ = db.upsert_paper(self.conn, PAPERS[2])
        self.conn.commit()
        r = db.cleanup(self.conn, retention_days=120)
        self.assertEqual(r, {"unconfirmed_signups_removed": 1, "old_papers_removed": 1})
        ids = {x[0] for x in self.conn.execute("SELECT id FROM papers")}
        self.assertEqual(ids, {kept, fresh})               # old paper gone; one with feedback and the fresh one stay
        for i in range(4):
            path = db.backup(self.conn, os.path.join(self.tmp.name, "bk"), keep=2, now=db.now_dt() + timedelta(seconds=i))
        self.assertEqual(len(glob.glob(os.path.join(self.tmp.name, "bk", "margin-data-*.db"))), 2)
        snap = db.connect(path)                       # a backup holds people, alerts and feedback, not the whole paper corpus
        self.assertEqual(snap.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)          # only the paper with feedback
        self.assertEqual(snap.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], self.conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0])

    def test_production_checks_headers_proxy_and_privacy(self):
        bad = Config(mailer="smtp")
        self.assertGreaterEqual(len(bad.problems()), 4)
        with self.assertRaises(RuntimeError):
            create_app(Config(mailer="smtp", db_path=self.cfg.db_path))
        good = Config(mailer="smtp", secret="x" * 40, base_url="https://margin.example.org", from_addr="Margin <a@margin.example.org>",
                      contact_email="hello@margin.example.org", smtp_host="h", smtp_user="u", smtp_password="p", operator="Harry C",
                      providers="Render (hosting) and Resend (email)")
        self.assertEqual(good.problems(), [])
        r = self.client.get("/privacy")
        self.assertIn(b"Not ready to publish", r.data)      # unfilled template cannot go live unnoticed
        app = create_app(good, self.mailer)
        r = app.test_client().get("/privacy")
        self.assertIn(b"Harry C", r.data)
        self.assertNotIn(b"Not ready to publish", r.data)
        self.assertNotIn(b"{{", r.data)
        h = app.test_client().get("/manage").headers
        self.assertEqual((h["X-Frame-Options"], h["Referrer-Policy"], h["Cache-Control"]), ("DENY", "no-referrer", "no-store"))
        self.assertIn("Strict-Transport-Security", h)
        self.assertNotIn("Access-Control-Allow-Origin", h)
        # rate limit is per visitor behind a proxy, not shared by everyone
        from dataclasses import replace
        px = create_app(replace(self.cfg, trust_proxy=True), self.mailer).test_client()
        body = {"email": "a@b.co", "alert": {"include": ["x"]}}
        codes = [px.post("/api/subscribe", json=body, headers={"X-Forwarded-For": "1.1.1.1"}).status_code for _ in range(6)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(px.post("/api/subscribe", json=body, headers={"X-Forwarded-For": "2.2.2.2"}).status_code, 202)

    def test_each_source_has_its_own_lookback(self):
        from unittest import mock
        from margin.ingest import run_ingest, LOOKBACK_DAYS
        seen = {}
        def rec(name):
            return lambda since, *a, **k: (seen.__setitem__(name, since), iter([]))[1]
        today = db.now_dt().date()
        with mock.patch("margin.ingest.openalex.fetch", rec("openalex")), mock.patch("margin.ingest.arxiv.fetch", rec("arxiv")), \
             mock.patch("margin.ingest.pubmed.fetch", rec("pubmed")), mock.patch("margin.ingest.europepmc.fetch", rec("europepmc")):
            run_ingest(self.conn, self.cfg)
            self.assertEqual({k: (today - v).days for k, v in seen.items()}, LOOKBACK_DAYS)
            run_ingest(self.conn, self.cfg, days=5)                      # an explicit window applies to all
            self.assertEqual({(today - v).days for v in seen.values()}, {5})
        self.assertLess(LOOKBACK_DAYS["pubmed"] * 7000, 10000)          # a PubMed window must stay under its 10,000 cap

    def test_openalex_asks_for_reviews_too(self):
        seen = []
        def grab(url, params=None, **k):
            seen.append(params["filter"])
            return json.dumps({"results": [], "meta": {"next_cursor": None}}).encode()
        list(openalex.fetch(date(2026, 11, 1), "a@b.c", http=grab))
        self.assertIn("type:article|review|preprint", seen[0])
        self.assertIn("has_abstract:true", seen[0])

    def test_europepmc_parsing_paging_and_sources(self):
        rec = lambda i, **kw: {"id": f"PPR{i}", "source": "PPR", "doi": f"10.1234/x{i}", "title": f"Preprint {i}",
                               "abstractText": "<h4>Background</h4><p>Gait &amp; balance after <i>stroke</i>.</p>",
                               "authorString": "Smith J, Doe A.", "firstPublicationDate": "2026-11-15", "isOpenAccess": "Y",
                               "bookOrReportDetails": {"publisher": "Research Square"}, **kw}
        pages = [{"resultList": {"result": [rec(1), rec(2, doi="10.1101/2026.11.01.555"), rec(3, doi="10.48550/arXiv.2611.0001"),
                                            rec(4, abstractText=""), rec(5, title="")]}, "nextCursorMark": "AAA"},
                 {"resultList": {"result": [rec(6, doi=None)]}, "nextCursorMark": "AAA"}]      # same cursor back = the end
        calls, it = [], iter(pages)
        def http(url, params):
            calls.append(params)
            return json.dumps(next(it)).encode()
        got = list(europepmc.fetch(date(2026, 11, 14), http=http))
        self.assertEqual([p["source_id"] for p in got], ["PPR1", "PPR2", "PPR3", "PPR6"])   # no abstract / no title skipped
        self.assertEqual([p["source"] for p in got], ["europepmc", "biorxiv", "arxiv", "europepmc"])
        self.assertEqual(got[0]["abstract"], "Background Gait & balance after stroke.")
        self.assertEqual(europepmc.clean("H<sub>2</sub>O and <b>CO</b><sub>2</sub>"), "H2O and CO2")
        self.assertEqual(got[0]["authors"], ["Smith J", "Doe A"])
        self.assertTrue(got[0]["is_preprint"] and got[0]["is_oa"])
        self.assertEqual(got[0]["url"], "https://doi.org/10.1234/x1")
        self.assertEqual(got[3]["url"], "https://europepmc.org/article/PPR/PPR6")
        self.assertIn("SRC:PPR", calls[0]["query"])
        self.assertIn("FIRST_PDATE:[2026-11-14 TO", calls[0]["query"])
        self.assertEqual((calls[0]["cursorMark"], calls[1]["cursorMark"], calls[0]["resultType"]), ("*", "AAA", "core"))
        self.assertEqual(list(europepmc.fetch(date(2026, 11, 14), http=lambda *a, **k: b'{"hitCount":0}')), [])

    def test_europepmc_preprint_merges_with_the_openalex_copy(self):
        oa = dict(PAPERS[1], source="biorxiv", source_id="W9", doi="10.1101/2026.11.01.555", abstract="short")
        ep = dict(PAPERS[1], source="biorxiv", source_id="PPR2", doi="10.1101/2026.11.01.555")
        id1, new1 = db.upsert_paper(self.conn, oa)
        id2, new2 = db.upsert_paper(self.conn, ep)
        self.assertEqual((id1, new1, new2), (id2, True, False))

    def test_other_preprints_is_a_valid_source_and_existing_alerts_are_migrated(self):
        self.assertEqual(self.client.post("/api/preview", json={"include": ["crispr"], "sources": ["europepmc"]}).status_code, 200)
        self.assertEqual(self.client.post("/api/preview", json={"include": ["crispr"], "sources": ["nonsense"]}).status_code, 400)
        old = ["openalex", "pubmed", "arxiv", "biorxiv"]
        mk = lambda name, srcs: self.conn.execute(
            "INSERT INTO subscribers(email,created_at) VALUES(?,?)", (name, db.now_iso())).lastrowid
        sid = mk("m@x.co", None)
        ins = lambda srcs: self.conn.execute(
            "INSERT INTO alerts(subscriber_id,name,include,exclude,sources,oa_only,preprints,frequency,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", (sid, "a", '["x"]', "[]", json.dumps(srcs), 0, 1, "daily", "active", db.now_iso())).lastrowid
        everything, narrowed = ins(sorted(old)), ins(["pubmed"])
        self.conn.execute("DELETE FROM state WHERE key='migrated_europepmc_source'")
        self.conn.commit()
        db._migrate(self.conn)
        get = lambda i: json.loads(self.conn.execute("SELECT sources FROM alerts WHERE id=?", (i,)).fetchone()[0])
        self.assertEqual(get(everything), sorted(old + ["europepmc"]))       # watched everything -> now includes the new source
        self.assertEqual(get(narrowed), ["pubmed"])                          # someone's own choice is left alone
        later = ins(sorted(old))
        db._migrate(self.conn)                                               # runs once only
        self.assertEqual(get(later), sorted(old))

    def test_other_preprints_reach_alerts_that_watch_it(self):
        pre = dict(PAPERS[1], source="europepmc", source_id="PPR1", doi="10.31234/osf.io/abc", is_preprint=True)
        self.assertTrue(hit(AlertSpec.build(1, [pre["title"].split()[0]], sources=["europepmc"]), pre))
        self.assertFalse(hit(AlertSpec.build(1, [pre["title"].split()[0]], sources=["pubmed"]), pre))
        self.assertFalse(hit(AlertSpec.build(1, [pre["title"].split()[0]], sources=["europepmc"], preprints=False), pre))

    def test_worker_tick_timers(self):
        from dataclasses import replace
        from margin import worker
        cfg = replace(self.cfg, backup_dir=os.path.join(self.tmp.name, "bk"))
        calls = []
        fm = {"arxiv": lambda: (calls.append(1), iter([]))[1]}
        t0 = db.now_dt()
        r1 = worker.tick(self.conn, cfg, self.mailer, now=t0, fetch_map=fm)
        self.assertEqual((len(calls), "backup" in r1), (1, True))              # first pass does everything
        r2 = worker.tick(self.conn, cfg, self.mailer, now=t0 + timedelta(minutes=15), fetch_map=fm)
        self.assertEqual((len(calls), "ingest" in r2, "backup" in r2), (1, False, False))   # heavy steps wait
        r3 = worker.tick(self.conn, cfg, self.mailer, now=t0 + timedelta(hours=cfg.ingest_every_hours, minutes=1), fetch_map=fm)
        self.assertEqual((len(calls), "backup" in r3), (2, False))

    def test_feedback_link_one_vote_per_paper_and_unknown_alert(self):
        self.subscribe()
        self.confirm_last()
        pid, _ = db.upsert_paper(self.conn, PAPERS[0])
        self.conn.commit()
        up = tokens.sign({"t": "fb", "a": 1, "p": pid, "v": "up"}, "s3cret")
        down = tokens.sign({"t": "fb", "a": 1, "p": pid, "v": "down"}, "s3cret")
        self.assertIn(b"higher", self.client.get(f"/f/{up}").data)
        r = self.client.get(f"/f/{down}")                      # changed their mind
        self.assertIn(b"lower", r.data)
        self.assertIn(b"/manage?t=", r.data)                   # offers a leave-out term
        rows = self.conn.execute("SELECT value FROM feedback WHERE alert_id=1 AND paper_id=?", (pid,)).fetchall()
        self.assertEqual([x["value"] for x in rows], ["down"])
        ghost = tokens.sign({"t": "fb", "a": 99, "p": pid, "v": "up"}, "s3cret")
        self.assertEqual(self.client.get(f"/f/{ghost}").status_code, 404)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 1)

    def test_feedback_reorders_digest_but_never_hides(self):
        from margin import rank
        self.subscribe(include=["microbiome"], exclude=[])
        self.confirm_last()
        mk = lambda sid, title, abstract, day: db.upsert_paper(self.conn, dict(
            source="pubmed", source_id=sid, doi=f"10.1/{sid}", title=title, abstract=abstract, authors=["A B"],
            venue="J", published=day, url="u"))[0]
        liked = mk("l", "Gut microbiome shapes immunotherapy response in melanoma patients",
                   "Tumour response to checkpoint immunotherapy depends on gut bacteria in melanoma patients.", "2026-11-01")
        bad = mk("d", "Microbiome engineering for methane reduction in dairy cattle",
                 "Rumen bacteria and seaweed supplements lower methane emissions from dairy cattle.", "2026-11-01")
        like_new = mk("c1", "Microbiome profiles predict immunotherapy response in a melanoma cohort",
                      "Gut bacteria composition separates responders to checkpoint immunotherapy in melanoma.", "2026-11-02")
        bad_new = mk("c2", "Rumen microbiome shifts in dairy cattle fed seaweed",
                     "Seaweed supplements change bacteria in the cattle rumen and lower methane from dairy cows.", "2026-11-09")
        self.conn.commit()
        alert = self.conn.execute("SELECT * FROM alerts WHERE id=1").fetchone()
        rows = lambda: self.conn.execute("SELECT p.*, 3 AS score FROM papers p WHERE id IN (?,?) ORDER BY published DESC", (like_new, bad_new)).fetchall()
        order = lambda: [r["source_id"] for r in rank.rerank(self.conn, alert, rows())]
        self.assertEqual(order(), ["c2", "c1"])                 # no feedback: same keyword score, newest first
        for pid, v in ((liked, "up"), (bad, "down")):
            self.client.get("/f/" + tokens.sign({"t": "fb", "a": 1, "p": pid, "v": v}, "s3cret"))
        self.assertEqual(order(), ["c1", "c2"])                 # learned: liked-style paper now leads
        self.assertEqual(len(order()), 2)                       # and nothing was dropped

    def test_stats(self):
        from margin import rank
        self.subscribe()
        self.confirm_last()
        pid, _ = db.upsert_paper(self.conn, PAPERS[0])
        self.conn.execute("INSERT INTO matches(alert_id,paper_id,score,created_at,sent_at) VALUES(1,?,3,'x','y')", (pid,))
        self.conn.commit()
        self.client.get("/f/" + tokens.sign({"t": "fb", "a": 1, "p": pid, "v": "up"}, "s3cret"))
        s = rank.feedback_stats(self.conn)[0]
        self.assertEqual((s["sent"], s["up"], s["down"], s["relevant_rate"], s["response_rate"]), (1, 1, 0, 1.0, 1.0))

    def test_header_can_differ_from_the_product_name(self):
        from dataclasses import replace
        cfg = replace(self.cfg, brand="In The Margin", header="The Margin", operator="Op", contact_email="hi@x.org")
        mailer = ConsoleMailer(cfg)
        c = create_app(cfg, mailer).test_client()
        home, manage, privacy = (c.get(p).data.decode() for p in ("/", "/manage", "/privacy"))
        self.assertIn('<div class="logo"><i></i>The Margin</div>', home)
        self.assertIn('<i></i>The Margin</a>', manage)
        self.assertIn("← The Margin", privacy)
        self.assertIn("<title>In The Margin – research alerts</title>", home)           # the product name is unchanged elsewhere
        self.assertIn("In The Margin is run by Op", privacy)
        self.assertIn("In The Margin", c.get("/confirm/bad").data.decode())             # simple pages: title/product name kept...
        self.assertIn("<i></i>The Margin</a>", c.get("/confirm/bad").data.decode())     # ...logo uses the header
        c.post("/api/subscribe", json={"email": "a@b.co", "alert": {"include": ["x"], "exclude": [], "frequency": "weekly"}})
        self.assertIn("In The Margin", mailer.sent[-1]["subject"])
        plain = create_app(replace(self.cfg, brand="Plain"), ConsoleMailer(self.cfg)).test_client().get("/").data.decode()
        self.assertIn('<div class="logo"><i></i>Plain</div>', plain)                    # no header set: the brand is used

    def test_search_tips_are_tucked_away_and_signup_hints_at_junk_folder(self):
        home = self.client.get("/").data.decode()
        self.assertIn('<details class="tip"><summary>Search tips</summary>', home)
        self.assertIn("Published papers", home)                                   # sources are grouped, with one preprint switch
        self.assertIn("Include preprints from all preprint servers", home)
        self.assertIn("What’s a preprint?", home)
        for server in ("arXiv", "bioRxiv", "medRxiv", "SSRN", "Research Square", "PsyArXiv"):
            self.assertIn(server, home)
        self.assertNotIn("Other preprints", home)
        self.assertNotIn('value="arXiv"', home)                                   # no per-server preprint tick boxes any more
        manage = self.client.get("/manage").data.decode()
        self.assertIn("Include preprints from all preprint servers", manage)
        self.assertIn("What’s a preprint?", manage)
        self.assertIn("AND is applied before OR", home)
        self.assertIn("Press Enter after each topic.", home)
        self.assertIn("junk or spam folder", home)
        from email.utils import parseaddr
        self.assertIn(parseaddr(self.cfg.from_addr)[1], home.split("junk or spam folder")[1][:200])

    def test_preprints_are_labelled_in_the_digest_email(self):
        pre = dict(PAPERS[1], source_id="L1", doi="10.1101/label.1", title="Labelled CRISPR preprint", is_preprint=True)
        pub = dict(PAPERS[0], source_id="L2", doi="10.1000/label.2", title="Labelled CRISPR journal paper", is_preprint=False)
        self.subscribe(include=["crispr"], exclude=[])
        self.confirm_last()
        for p in (pre, pub):
            db.upsert_paper(self.conn, p)
        self.conn.commit()
        db.run_matching(self.conn)
        digest.send_due(self.conn, self.cfg, self.mailer, now=next_monday_8am())
        mail = self.mailer.sent[-1]
        self.assertEqual(mail["html"].count("Preprint (not peer reviewed)"), 1)   # only the preprint carries the label
        self.assertEqual(mail["text"].count("Preprint (not peer reviewed)"), 1)

    def test_emails_carry_reply_to_and_a_message_id_on_our_own_domain(self):
        from dataclasses import replace
        from margin.mailer import build_message
        cfg = replace(self.cfg, brand="In The Margin", from_addr="In The Margin <alerts@inthemargin.uk>", contact_email="hello@inthemargin.uk")
        m = build_message(cfg, "a@b.co", "Hi", "<p>x</p>", "x")
        self.assertTrue(m["Message-ID"].endswith("@inthemargin.uk>"))
        self.assertEqual(m["Reply-To"], "In The Margin <hello@inthemargin.uk>")
        m2 = build_message(replace(cfg, contact_email="you@example.com"), "a@b.co", "Hi", "<p>x</p>", "x")
        self.assertIsNone(m2["Reply-To"])                                                # placeholder address: don't advertise it

    def test_brand_is_one_setting_everywhere_subscribers_look(self):
        import re
        from dataclasses import replace
        cfg = replace(self.cfg, brand="Test'Name", operator="Op Ltd", contact_email="hi@x.org")
        mailer = ConsoleMailer(cfg)
        c = create_app(cfg, mailer).test_client()
        for path in ("/", "/manage", "/privacy"):
            body = c.get(path).data.decode()
            self.assertNotIn("{{", body, path)
            self.assertNotIn("Margin", body, path)
            self.assertIn("Test", body, path)
        self.assertIn("From Test\\'Name", c.get("/").data.decode())      # inside a JS string, so the quote is escaped
        r = c.post("/api/subscribe", json={"email": "a@b.co", "alert": {"include": ["crispr"], "exclude": [], "frequency": "weekly"}})
        self.assertEqual(r.status_code, 202)
        self.assertIn("Test'Name", mailer.sent[-1]["subject"])
        self.assertNotIn("Margin", mailer.sent[-1]["subject"] + mailer.sent[-1]["text"] + mailer.sent[-1]["html"])
        c.get("/confirm/" + re.search(r"/confirm/(\S+)", mailer.sent[-1]["text"]).group(1))
        db.upsert_paper(self.conn, dict(PAPERS[0], source_id="b1", doi="10.1000/b1", title="Brand test CRISPR paper"))
        self.conn.commit()
        db.run_matching(self.conn)
        self.assertEqual(digest.send_due(self.conn, cfg, mailer, now=next_monday_8am()), 1)
        mail = mailer.sent[-1]
        self.assertIn("Test&#x27;Name", mail["html"])
        self.assertIn("Test'Name", mail["text"])
        self.assertNotIn("Margin", mail["html"] + mail["text"])

    def test_settings_typos_are_caught_or_tidied(self):
        from margin.config import _url
        self.assertEqual(_url(" HTTPS://alerts.onrender.com/ "), "https://alerts.onrender.com")
        base = dict(mailer="smtp", secret="x" * 40, base_url="https://a.org", contact_email="hi@a.org",
                    smtp_host="h", smtp_user="u", smtp_password="p", operator="O")
        for ok in ("Margin <alerts@a.org>", "alerts@a.org", "onboarding@resend.dev"):
            self.assertEqual(Config(**base, from_addr=ok).problems(), [], ok)
        for bad in ("Margin onboarding@resend.dev", "Margin <alerts@a.org", "alerts", "<>"):
            self.assertTrue(any("MARGIN_FROM" in x for x in Config(**base, from_addr=bad).problems()), bad)

    def test_failed_confirmation_email_gives_clear_message_and_leaves_nothing_pending(self):
        class Broken:
            def send(self, *a, **k):
                raise OSError("auth failed")
        c = create_app(self.cfg, Broken()).test_client()
        r = c.post("/api/subscribe", json={"email": "a@b.co", "alert": {"include": ["crispr"], "exclude": [], "frequency": "weekly"}})
        self.assertEqual(r.status_code, 503)
        self.assertIn("couldn", r.get_json()["error"])
        self.assertNotIn("auth failed", r.get_json()["error"])          # no internals shown to visitors
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    def test_privacy_warning_until_providers_are_named(self):
        from dataclasses import replace
        c = create_app(replace(self.cfg, operator="Op", contact_email="hi@x.org", providers="Render <EU> and Resend"), self.mailer).test_client()
        body = c.get("/privacy").data.decode()
        self.assertIn("Render &lt;EU&gt; and Resend", body)            # escaped
        self.assertNotIn("Not ready to publish", body)
        c2 = create_app(replace(self.cfg, operator="Op", contact_email="hi@x.org"), self.mailer).test_client()
        self.assertIn("Not ready to publish", c2.get("/privacy").data.decode())

    def test_stats_describe_what_the_counter_counts(self):
        from datetime import timedelta
        today = db.now_dt().date()
        for i, (d, src) in enumerate([(today - timedelta(days=1), "arxiv"), (today - timedelta(days=3), "pubmed"), (today.replace(year=today.year - 5), "pubmed")]):
            db.upsert_paper(self.conn, dict(PAPERS[0], source=src, source_id=f"st{i}", doi=f"10.1000/st{i}", published=d.isoformat()))
        db.set_state(self.conn, "last_ingest_arxiv", db.now_iso())
        self.conn.commit()
        j = self.client.get("/api/stats").get_json()
        self.assertEqual(j["papers"], 3)
        self.assertEqual(j["since"], (today - timedelta(days=3)).isoformat())     # ignores the five-year-old record
        self.assertTrue(j["updated"].endswith("+00:00"))
        self.assertEqual((j["every_hours"], j["keep_days"]), (self.cfg.ingest_every_hours, self.cfg.paper_retention_days))
        home = self.client.get("/").data.decode()
        self.assertIn("/api/stats", home)
        self.assertNotIn("papers indexed", home)

if __name__ == "__main__":
    unittest.main()
