import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from margin import db, digest, tokens
from margin.api import create_app
from margin.config import Config
from margin.demo_data import PAPERS
from margin.mailer import ConsoleMailer
from margin.matcher import AlertSpec, Matcher
from margin.sources import arxiv, openalex, pubmed


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
        self.assertEqual(self.confirm_last().status_code, 200)
        # papers that arrived before confirmation are not back-filled; new ones are matched
        n_before = len(self.mailer.sent)
        db.upsert_paper(self.conn, dict(PAPERS[0], source_id="new1", doi="10.1000/new1", title="Another CRISPR advance"))
        self.conn.commit()
        self.assertEqual(db.run_matching(self.conn), 1)
        self.assertEqual(db.run_matching(self.conn), 0)       # idempotent
        self.assertEqual(digest.send_due(self.conn, self.cfg, self.mailer), 1)
        mail = self.mailer.sent[-1]
        self.assertEqual(len(self.mailer.sent), n_before + 1)
        self.assertIn("Another CRISPR advance", mail["html"])
        self.assertEqual(mail["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        # not due again until a week has passed, and nothing pending anyway
        self.assertEqual(digest.send_due(self.conn, self.cfg, self.mailer), 0)
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

    def test_feedback_link(self):
        t = tokens.sign({"t": "fb", "a": 1, "p": 1, "v": "up"}, "s3cret")
        self.assertEqual(self.client.get(f"/f/{t}").status_code, 200)
        self.assertEqual(self.conn.execute("SELECT value FROM feedback").fetchone()[0], "up")


if __name__ == "__main__":
    unittest.main()
