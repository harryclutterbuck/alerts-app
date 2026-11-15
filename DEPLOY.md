# Putting Margin on the internet

This guide takes you from "works on my laptop" to "real people can sign up". Allow a few hours the first time,
mostly waiting for email DNS records to take effect.

**Not yet verified.** The Docker image in this folder has not been built or run by its author (no Docker was
available), and the OpenAlex and email-provider steps have not been tried against the live services. Treat the
first deploy as a test, and use the smoke-test list in step 7.

## 0. What you need

| Thing | Why | Roughly |
|---|---|---|
| A domain name | Sending email from your own domain is what gets you into inboxes | a few pounds a year |
| A host that runs a Docker container, with a persistent disk | Runs the website and the background worker | check current prices |
| An email-sending service | Delivers the digests (Resend, Postmark or Amazon SES all offer SMTP) | free tiers exist; check limits |
| An OpenAlex key | Widest paper coverage | free |

Host requirements (any provider that offers these will do): runs a Dockerfile; a persistent volume you can mount
at `/data`; **exactly one running instance** (SQLite is a single-writer database, and the in-memory rate limiter
assumes one process); automatic HTTPS; an environment-variables screen; a health check on `/api/health`.

## 1. OpenAlex key (10 minutes)

1. Create a free account at openalex.org, **verify your email address** (an unverified key is refused with a 403).
2. Copy your key from openalex.org/settings/api.
3. On your Mac, from the project folder:
   ```
   export OPENALEX_API_KEY=paste-the-real-key-here
   python3 -m margin openalex-status
   python3 -m margin ingest --days 1 --only openalex
   ```
   `openalex-status` prints your remaining daily budget. The ingest should report thousands of papers seen. If it
   stops early with a message about the daily budget, what it saved is kept.

Cost, from OpenAlex's published price list: a bulk "list" request is 1 credit and returns up to 200 papers, and the
free key allows $1 a day (10,000 such requests). The worker pulls every 6 hours. Measured on a real key: one day of OpenAlex papers (about 1,500) cost 8 credits, so
OpenAlex publishes roughly 9,000+ new papers a day, so the 3-day look-back it uses is about 130 requests a run, around 5% of the daily allowance across four runs. Keyword *search* is ten times dearer, which is why Margin does not use it.
`OPENALEX_MAX_PAGES` caps spend per pull.

## 2. Email provider and DNS (the slow part)

1. Sign up with a provider that offers SMTP (Resend, Postmark, Amazon SES). Add your domain.
2. Use a **subdomain for sending** (for example `alerts.yourdomain.com`), so a mistake never harms your main email.
3. The provider shows DNS records to add at your domain registrar: **SPF**, **DKIM** (usually 2-3 CNAME/TXT records)
   and sometimes a return-path record. Add them exactly as shown and wait for the provider to say "verified".
4. Add a **DMARC** record at `_dmarc.yourdomain.com`, starting gently:
   `v=DMARC1; p=none; rua=mailto:you@yourdomain.com`
   Once mail has flowed cleanly for a few weeks you can tighten `p=none` to `p=quarantine`.
5. Create an SMTP credential and note the host, port (587), username and password for step 4.

Gmail and Yahoo require SPF, DKIM, DMARC and one-click unsubscribe from bulk senders. Margin already sends the
unsubscribe headers; the DNS records are your job. Start with small volumes and let reputation build.

## 3. Choose and prepare the host

Create a project on your chosen host from this folder (it contains the `Dockerfile`). Then:

1. Attach a persistent volume mounted at **/data**.
2. Set the environment variables from `.env.example` (secret values go in the host's secrets/variables screen,
   never in a file you share). `MARGIN_BASE_URL` must be the final public https address.
3. Set the health check path to `/api/health` and the instance count to **1**.
4. Point your domain (or a subdomain such as `alerts.yourdomain.com`) at the app, as your host instructs.

If a required setting is missing, the container refuses to start and its log lists exactly what to fix.

## 4. First run

Open your host's shell or console for the running container and run:

```
python -m margin test-email you@yourdomain.com
```

Check it lands in your inbox, not spam. In Gmail, open the message, choose "Show original", and confirm
**SPF, DKIM and DMARC all say PASS**. Do not go further until they do.

Then load papers once so the live preview has something to show:

```
python -m margin ingest
```

(Each source looks back as far as suits it: PubMed 1 day, arXiv 2, OpenAlex 3. The worker repeats this every 6 hours.)

## 5. Before you tell anyone (legal, UK)

I am not a lawyer; this is a checklist of things to confirm.

- **Privacy notice** at `/privacy`: replace the bracketed sentence naming your hosting and email providers, and read every
  line against what you really do. The page shows a visible warning until `MARGIN_OPERATOR` is set.
- **ICO registration.** Organisations that handle personal data in the UK usually must pay the ICO data protection fee
  unless exempt. Check at ico.org.uk/fee-checker.
- **Consent**: double opt-in is built in, and the time of confirmation is stored. PECR also expects the sender to be
  identifiable: the operator name and contact address appear in every email footer.
- **Data sources.** Check each source's current terms: OpenAlex, PubMed/NLM (abstracts can be copyrighted by
  publishers, which is why emails show a short snippet and link out) and arXiv (which asks users of its API to display
  an acknowledgement; the text is in the email footer and privacy page; confirm its current wording).

## 6. Running it day to day

- **Backups**: the worker writes a small snapshot of your people, alerts and feedback (not the public papers, which are re-downloaded) to
  `/data/backups` every day and keeps 14. That is on the same disk, so also enable your host's volume snapshots, or copy a
  backup somewhere else regularly.
- **Watching it**: set up a free uptime monitor on `https://yourdomain/api/health`. Read the logs after the first few worker
  passes. `python -m margin stats` shows how many people rate papers as relevant.
- **Updating**: redeploy the new version; the database lives on the volume and survives. The schema is unchanged by
  this release; a future one will say if a migration is needed.
- **Digest times**: daily and weekly digests leave at 07:00 UTC (weekly on Mondays). Change with `MARGIN_DIGEST_HOUR` and
  `MARGIN_DIGEST_WEEKDAY`. Instant alerts go out within about 15 minutes of the worker finding a match.
- **Testing a digest quickly**: `python -m margin send --force` sends everything pending, ignoring the schedule.

## 7. Smoke test after deploying

1. `/` loads and shows "Live · N papers indexed".
2. Search a topic; results appear.
3. Create an alert with your own email; the confirmation arrives; clicking it shows "You're subscribed".
4. Create an **Instant** alert for a broad topic; within a few worker passes after new papers arrive, a digest lands.
5. The digest's "Edit or pause" link opens `/manage`; pause, edit and unsubscribe all work.
6. "More like this" and "Not relevant" show a thank-you page.
7. `/privacy` has your name and no warning banner.

## 8. Known limits (fine to launch with, worth knowing)

- One server, one SQLite file. That comfortably handles thousands of subscribers; beyond that, move to Postgres and a
  proper queue. Matching cost does not grow with the number of alerts in any way that will worry you before then.
- The sign-up rate limiter lives in memory and resets on restart.
- There is no admin dashboard yet; `python -m margin stats` and the database are how you look at usage.
- Unsubscribed addresses are kept on a do-not-email list. Subscribers can erase themselves entirely from the manage page.
- Papers are pruned after 35 days unless someone gave feedback on them. Plan disk for roughly 10,000 to 20,000 new papers a day.

## Renaming the product

The name subscribers see (page titles, the logo text, email subject lines and footers, the privacy notice) comes from one
setting, `MARGIN_BRAND` (default `Margin`). To rename: change `MARGIN_BRAND`, set `MARGIN_FROM` to `NewName <alerts@yournewdomain>`,
update `MARGIN_BASE_URL`, and restart. The `MARGIN_…` variable prefix and the Python package name are internal and never shown to
subscribers, so they stay as they are. Do the rename before real subscribers join: links in emails already sent point at the old
address.
