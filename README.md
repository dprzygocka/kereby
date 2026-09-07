# kereby-watcher

Watches https://kereby.dk/bolig/ for 2-room listings and automatically books
a viewing ("Book fremvisning") the moment one appears, then emails you a
confirmation.

## ⚠️ Test before you trust it

Run once locally (or via `workflow_dispatch` with `AUTO_APPLY` temporarily
set to `false`) before letting it apply on autopilot. The form was mapped
from the static HTML, not a live browser session, so field-matching (done
by visible label text via Playwright) should be verified once against the
real site. The workflow also uploads a screenshot taken right after
submission as a build artifact — check the first one or two runs to confirm
the form actually went through (and that no CAPTCHA silently blocked it).

## 1. Create the repo

Push these files to a **private** GitHub repo (private keeps your applicant
details out of public logs; you get 2,000 free Actions minutes/month on
private repos, plenty for a 5-minute poll).

## 2. Add repo secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Purpose |
|---|---|
| `APPLICANT_NAME` | Name filled into the "Navn" field |
| `APPLICANT_EMAIL` | Email filled into the form, and used as SMTP login if you reuse it |
| `APPLICANT_MESSAGE` | Message filled into "Din besked" |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | e.g. `587` |
| `SMTP_USER` | Your SMTP login (e.g. Gmail address) |
| `SMTP_PASS` | An **app password**, not your normal password (Gmail: Google Account → Security → App passwords) |
| `NOTIFY_TO` | Where the "applied for you" email should land |

## 3. Test it

Go to the Actions tab → "Watch Kereby listings" → "Run workflow" to trigger
it manually once, and check the log + the uploaded screenshot artifact.

## 4. Let it run

Once you're confident it works, leave the `schedule` trigger as-is — it
polls every 5 minutes (GitHub may space runs out further under load, so
treat this as "roughly every 5–15 minutes", not exact).

## How it decides what's "new"

Every run scrapes the full listing page, keeps only listings tagged with
`TARGET_ROOMS` rooms (2 by default) that are still clickable (reserved/rented
units aren't linked on the site, so they're naturally excluded), and diffs
against `seen.json` in the repo. Anything new gets applied to, emailed about,
and added to `seen.json`, which the workflow commits back so it's never
re-applied to.

## Known limitations

- If Kereby adds a CAPTCHA to the booking form, automated submission will
  likely fail silently — check the screenshot artifacts periodically.
- If Kereby changes their form's visible labels, the Playwright selectors
  in `apply_bot.py` may need updating.
- GitHub's free scheduler is best-effort timing, not real-time.
- Automating form submissions against a site may be against its terms of
  service — this is intended for personal, low-volume use (checking your
  own housing search), not high-frequency scraping.
