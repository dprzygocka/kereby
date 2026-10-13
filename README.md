# kereby flat monitor

Polls [kereby.dk/bolig](https://kereby.dk/bolig/) and emails you when a flat becomes
available, either as a brand new listing or because a reservation fell through.
Optionally it also requests a viewing automatically.

## How it works

`monitor.py` fetches the listing page, parses every `jorato-case-card` article and
compares the result with `listings_state.json` from the previous run.

| Transition                               | Action             |
| ---------------------------------------- | ------------------ |
| Card id not seen before, `available`     | Email as NEW       |
| Was not available, now `available`       | Email as REPOSTED  |
| Was `available`, now not available       | Log to stdout only |
| No change                                | Nothing            |

The page uses `available`, `reserved` and `completed`. Anything not `available`
counts as unavailable. The state file is overwritten after each comparison and
committed back by the workflow.

### Known blind spots

- **Reserve then cancel between two polls** looks identical in both snapshots and is
  not reported. Shorter polling narrows the window but cannot close it.
- **24 card render cap.** The widget renders at most 24 cards. The monitor compares
  the "N resultater" header with the cards it parsed and emails a warning if cards
  are missing.

## Setup

1. Make the repo **private** and push these files (delete the old `apply_bot.py`
   and `seen.json`).
2. Generate a baseline locally so the first run does not email every flat:
   ```
   pip install -r requirements.txt
   python monitor.py          # writes listings_state.json, sends no email
   DEBUG=1 python monitor.py  # optional: print parsed listings and exit
   git add listings_state.json && git commit -m "Add baseline" && git push
   ```
3. Add Actions secrets (Settings > Secrets and variables > Actions):

   | Secret      | Notes                                   |
   | ----------- | --------------------------------------- |
   | `SMTP_HOST` | e.g. `smtp.gmail.com`                   |
   | `SMTP_PORT` | `587` (STARTTLS)                        |
   | `SMTP_USER` | Also the From address                   |
   | `SMTP_PASS` | Gmail app password, no spaces           |
   | `EMAIL_TO`  | Optional, defaults to `SMTP_USER`       |

   If SMTP is missing the script prints the email to stdout instead of crashing.
4. Actions tab > "kereby flat monitor" > Run workflow, and check the log.

## Schedule

GitHub's cron is unreliable, so use [cron-job.org](https://cron-job.org/) to call
the workflow dispatch API every minute. Each run polls every `POLL_INTERVAL` (10 s)
for `POLL_WINDOW` (50 s).

1. Create a fine-grained GitHub token: resource owner `dprzygocka`, only this repo,
   **Actions: Read and write**, with an expiry date.
2. cron-job.org job, every 1 minute:
   - Method `POST`, expected response `204`
   - URL `https://api.github.com/repos/dprzygocka/kereby/actions/workflows/monitor.yml/dispatches`
   - Body `{"ref":"main"}`
   - Headers:
     ```
     Authorization: Bearer <token>
     Content-Type: application/json
     Accept: application/vnd.github+json
     X-GitHub-Api-Version: 2022-11-28
     ```
3. A `401`/`403` usually means the token expired. Replace it in cron-job.org only.

The workflow's `concurrency` group serialises runs so the state file cannot be corrupted.

## Automatic viewing requests

When a new or reposted flat matches all of these, the monitor requests a viewing:

- Area: Frederiksberg, København K, Nørrebro, Østerbro or Vesterbro
- Rent at most 20,000 DKK (override with repo variable `MAX_MONTHLY_RENT`)
- Optional rooms filter via repo variable `TARGET_ROOMS` (e.g. `2` or `2,3`)
- Slot: first weekday at least two days away, earliest offered time

This is a viewing request, not a reservation. Successful requests are stored in
`listings_state.json` so they are never repeated; failures retry up to 5 times.

Enable with these secrets (never put values in the repo):

| Secret                     | Value                                              |
| -------------------------- | -------------------------------------------------- |
| `AUTO_BOOK_VIEWINGS`       | `1`                                                |
| `BOOKING_NAME`             | Your full name                                     |
| `BOOKING_EMAIL`            | Your email                                         |
| `BOOKING_PHONE`            | Danish number, with or without `+45`               |
| `BOOKING_PRIVACY_ACCEPTED` | `1`, only after reading Kereby's privacy policy    |

Kereby only accepts a request when every screening question is confirmed, so the
monitor answers yes to all of them (for example no RKI registration, no pets, and
a move-in date for some flats). The notification email lists what was confirmed.
Only enable this if those statements are true for you.
