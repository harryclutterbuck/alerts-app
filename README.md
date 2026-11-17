# Margin backend

Research alerts: people subscribe with keywords, and get an email when matching papers appear in OpenAlex, PubMed, Europe PMC (preprints), arXiv or bioRxiv.

## Try it in 30 seconds (no network, no setup beyond Python 3.11+)

    pip install -r requirements.txt
    python -m margin demo          # seeds sample papers, writes a real digest to ./outbox/
    python -m unittest discover -s tests

Open the `.html` file in `./outbox/` to see the email.

## How it works

    sources (OpenAlex, PubMed, arXiv)
        -> ingest.py     pull new papers, merge duplicates by DOI
        -> db.py         SQLite + FTS5 (papers, alerts, matches)
        -> matcher.py    reverse search: many alerts vs each new paper
        -> digest.py     one email per subscriber, grouped by alert, ranked
        -> mailer.py     console (dev) or SMTP (Resend, Postmark, SES)
    api.py               preview, subscribe, confirm, manage, unsubscribe, feedback

Key decisions
- **Matching** uses an inverted index of alert terms, so 5,000 alerts cost roughly the same as 50 per paper (see `test_many_alerts_only_candidates_tested`).
- **Live preview** uses SQLite FTS5 to shortlist papers, then the exact matcher to confirm.
- **Semantics:** `cancer therapy` needs both words; `"base editing"` is an exact phrase; any include term matches; any exclude term vetoes.
- **No accounts.** Every link in an email is a signed token (confirm, manage, unsubscribe, feedback).
- **Consent:** double opt-in per alert. The confirmation time and wording version are stored. GET on the unsubscribe link only shows a button; the POST does the work, so email scanners cannot unsubscribe people. Emails carry `List-Unsubscribe` one-click headers.
- **Re-runs are safe.** Ingest re-scans a lookback window (indexes lag), duplicates merge, and `(alert, paper)` is a primary key, so nobody is notified twice. A preprint later published merges into one record without re-notifying.
- **Alerts only cover papers that arrive after confirmation.**

## Run it for real

    cp .env.example .env     # fill in, then export the variables
    python -m margin init-db
    python -m margin serve   # API on :5000 (use gunicorn behind HTTPS in production)

Schedule with cron:

    */30 * * * *  python -m margin run          # ingest + match + send
    # "instant" alerts go out on each run; daily/weekly respect their own gap

## Going live

See **DEPLOY.md** for the full walkthrough: OpenAlex key, email provider and DNS, hosting, legal checklist and a smoke test.

## Commands

    python -m margin demo            # sample data and a sample digest, fully offline
    python -m margin serve           # development server
    python -m margin worker          # background job: ingest, match, send, clean up, back up
    python -m margin ingest [--days N] [--only openalex arxiv pubmed europepmc]   # default look-back: PubMed 1 day, arXiv 2, OpenAlex 7, Europe PMC 3
    python -m margin match / send [--force]
    python -m margin stats           # how people rate the papers they are sent
    python -m margin openalex-status # check your OpenAlex key and daily budget
    python -m margin test-email you@yourdomain.com
    python -m margin backup / cleanup

## How relevance learning works

Every digest paper has "More like this" and "Not relevant" links. For each alert, Margin compares new matches with the
papers you liked and rejected (word overlap in title and abstract, ignoring the alert's own keywords) and reorders the
digest. Nothing is hidden; unwanted-looking papers just rank lower and fall into the "N more" line.
