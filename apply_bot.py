#!/usr/bin/env python3
"""
kereby-watcher
--------------
Polls https://kereby.dk/bolig/ for apartment listings with a given number of
rooms, and for any *new* matching listing:
  1. Books a viewing ("Book fremvisning") via the listing's own contact form,
     driven by a real headless browser (Playwright) so it behaves like a
     normal visitor filling out the form.
  2. Sends you a confirmation email.
  3. Remembers the listing (in seen.json) so it never applies twice.

IMPORTANT: run once with AUTO_APPLY=false to dry-run against the live site
before trusting this unattended. Kereby's form internals were not inspected
live (only static HTML), so field-matching is done by visible label text,
which is robust to markup changes but should still be verified once.
"""

import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://kereby.dk/bolig/"
SEEN_FILE = Path(__file__).parent / "seen.json"

TARGET_ROOMS = int(os.environ.get("TARGET_ROOMS", "2"))
AUTO_APPLY = os.environ.get("AUTO_APPLY", "false").lower() == "true"

APPLICANT_NAME = os.environ.get("APPLICANT_NAME", "")
APPLICANT_EMAIL = os.environ.get("APPLICANT_EMAIL", "")
APPLICANT_PHONE = os.environ.get("APPLICANT_PHONE", "")
APPLICANT_LANGUAGE = os.environ.get("APPLICANT_LANGUAGE", "English")  # "English" or "Dansk"
APPLICANT_MESSAGE = os.environ.get(
    "APPLICANT_MESSAGE",
    "Hej, jeg er meget interesseret i denne bolig og vil gerne booke en fremvisning. Mvh",
)

# These two must reflect the truth about the applicant - the booking wizard's
# final step has the applicant explicitly confirm both. Do not flip these to
# "true" unless they are actually true for you.
CONFIRM_NOT_IN_RKI = os.environ.get("CONFIRM_NOT_IN_RKI", "true").lower() == "true"
CONFIRM_NO_PETS = os.environ.get("CONFIRM_NO_PETS", "true").lower() == "true"

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
NOTIFY_TO = os.environ.get("NOTIFY_TO", APPLICANT_EMAIL)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; apartment-watch/1.0; personal use)"
}


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


def fetch_listings():
    """Return list of dicts: {url, rooms, text} for every *available*
    (i.e. clickable / not reserved-rented) listing on the page."""
    resp = requests.get(LISTING_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    listings = []
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].rstrip("/")
        if not href.startswith("https://kereby.dk/bolig/"):
            continue
        if href == "https://kereby.dk/bolig":
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        text = a.get_text(" ", strip=True)
        m = re.search(r"(\d+)\s*v[æa]relser?", text, re.IGNORECASE)
        if not m:
            continue
        rooms = int(m.group(1))
        listings.append({"url": href + "/", "rooms": rooms, "text": text})

    return listings


def send_email(subject: str, body: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and NOTIFY_TO):
        print("[email] SMTP not configured, skipping notification. Body was:")
        print(body)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [NOTIFY_TO], msg.as_string())
    print(f"[email] sent: {subject}")


def apply_to_listing(url: str, submit: bool = True) -> bool:
    """Open the listing page in a headless browser and fill (and, if
    submit=True, send) the 'Book fremvisning' viewing-request form.
    Returns True on apparent success. When submit=False, fills the form,
    screenshots it, and stops WITHOUT clicking send -- used for testing
    the field-matching logic against a real listing without actually
    applying to it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)

        try:
            return _fill_and_maybe_submit(page, submit, url)
        except Exception:
            debug_path = f"/tmp/debug_{re.sub(r'[^a-zA-Z0-9]', '_', url)[-40:]}.png"
            page.screenshot(path=debug_path, full_page=True)
            print(f"[apply] FAILED - saved debug screenshot to {debug_path}")
            raise
        finally:
            browser.close()


BOOKING_TEXT_RE = re.compile("Book fremvisning|Bestil fremvisning", re.IGNORECASE)


def _click_next_when_enabled(dialog, page, step_label: str, max_wait_ms: int = 6000) -> None:
    """Click the wizard's 'next step' button. IMPORTANT: the calendar widget
    has its own month-navigation arrow that can ALSO carry an accessible
    name of 'Næste', so matching by role+name alone risks clicking that
    instead of actually advancing the wizard. The site's markup uses a
    consistent data-jorato-booking-* attribute convention, so prefer a
    data-jorato-booking-next attribute if present; fall back to the LAST
    (not first) "Næste"-named button, since the wizard's real button sits
    after the calendar in DOM order."""
    next_by_attr = dialog.locator("[data-jorato-booking-next]:visible")
    if next_by_attr.count() > 0:
        next_button = next_by_attr.first
        print(f"[debug] [{step_label}] using data-jorato-booking-next attribute selector (visible one)")
    else:
        next_button = dialog.get_by_role("button", name=re.compile("Næste|Next", re.IGNORECASE)).last
        print(f"[debug] [{step_label}] no data-jorato-booking-next found, falling back to last 'Næste' button")

    next_button.wait_for(state="visible", timeout=10000)
    try:
        html = next_button.evaluate("el => el.outerHTML")
        print(f"[debug] [{step_label}] next-button markup: {html[:200]}")
    except Exception:
        pass

    waited = 0
    is_disabled = True
    while waited < max_wait_ms:
        try:
            is_disabled = next_button.evaluate(
                "el => el.disabled === true || el.getAttribute('aria-disabled') === 'true' "
                "|| el.classList.contains('disabled')"
            )
        except Exception:
            is_disabled = False
        if not is_disabled:
            break
        page.wait_for_timeout(250)
        waited += 250

    print(f"[debug] [{step_label}] Næste button disabled={is_disabled} after {waited}ms wait")
    next_button.click(timeout=5000)
    page.wait_for_timeout(600)


def _dismiss_cookie_banner(page) -> None:
    """Best-effort dismissal of the Cookiebot consent banner that overlays
    the page on first load and can block clicks on elements underneath it."""
    accept_labels = ["Tillad alle", "Accepter alle", "Accept all", "Tillad valgte"]

    # Try on the main page first.
    for label in accept_labels:
        try:
            page.get_by_role("button", name=re.compile(label, re.IGNORECASE)).first.click(timeout=3000)
            print(f"[cookies] dismissed via page button '{label}'")
            return
        except Exception:
            continue

    # Cookiebot commonly renders inside an iframe (id="CybotCookiebotDialog").
    try:
        frame = page.frame_locator("#CybotCookiebotDialog")
        for label in accept_labels:
            try:
                frame.get_by_role("button", name=re.compile(label, re.IGNORECASE)).click(timeout=3000)
                print(f"[cookies] dismissed via iframe button '{label}'")
                return
            except Exception:
                continue
    except Exception:
        pass

    print("[cookies] no cookie banner found/dismissed (may not have been present)")


def _fill_and_maybe_submit(page, submit: bool, url: str) -> bool:
    _dismiss_cookie_banner(page)
    page.wait_for_timeout(500)  # let the banner's exit animation/removal settle

    # Step 1: open the "Fremvisning" booking wizard. The trigger might be a
    # <button>, a <div>, or an <a> depending on the theme markup, and there
    # are TWO "Book fremvisning" elements on the page (one near the top
    # summary, one in the bottom "Interesseret?" section) - either should
    # open the same wizard, so .first is fine.
    click_strategies = [
        lambda: page.get_by_role("button", name=BOOKING_TEXT_RE).first,
        lambda: page.get_by_role("link", name=BOOKING_TEXT_RE).first,
        lambda: page.get_by_text(BOOKING_TEXT_RE).first,
    ]
    opened = False
    for strategy in click_strategies:
        try:
            strategy().click(timeout=7000)
            opened = True
            break
        except Exception:
            continue
    if not opened:
        raise RuntimeError("Could not find/open the booking wizard trigger ('Book fremvisning').")

    # Once open, there should be exactly one dialog on the page (the cookie
    # banner is already dismissed/removed from the a11y tree by this point).
    dialog = page.get_by_role("dialog").first
    dialog.wait_for(state="visible", timeout=10000)

    # Step 2: the wizard's first screen is a date/time picker ("Ønsket
    # fremvisningsdato" / "Ønsket fremvisningstidspunkt") - a date must be
    # picked, then a time slot, before "Næste" (Next) reveals the contact
    # fields. Try successive calendar days until one has an available time
    # slot (some dates may be fully booked).
    # Query enabled calendar days precisely via the Pikaday "is-disabled"
    # class on the day's <td> (seen in earlier debug logs), rather than
    # trial-and-error clicking every day - which risked misfiring on
    # adjacent elements (month-nav arrows etc.) and landing on the wrong date.
    enabled_days = dialog.locator("td:not(.is-disabled) button.pika-day")
    day_count = enabled_days.count()

    if day_count == 0:
        # Current month may be fully booked/past - advance to next month once.
        try:
            dialog.locator(".pika-next, [aria-label='Next Month']").first.click(timeout=3000)
            page.wait_for_timeout(500)
            enabled_days = dialog.locator("td:not(.is-disabled) button.pika-day")
            day_count = enabled_days.count()
        except Exception as e:
            print(f"[debug] could not advance to next month: {e}")

    if day_count == 0:
        raise RuntimeError("No enabled calendar dates found, even after trying the next month.")

    day_html = enabled_days.first.evaluate("el => el.outerHTML")
    print(f"[debug] {day_count} enabled day(s) found; selecting first: {day_html[:200]}")
    enabled_days.first.click(timeout=5000)
    page.wait_for_timeout(600)

    # Time slots may not be real <button> elements, so match by visible
    # text. Playwright's click() will time out on slots that are visually
    # disabled (pointer-events blocked), so try each candidate in turn.
    time_re = re.compile(r"^\d{1,2}:\d{2}$")
    time_candidates = dialog.get_by_text(time_re, exact=True)
    tcount = time_candidates.count()
    print(f"[debug] found {tcount} time-slot element(s) on the selected day")
    time_selected = False
    for j in range(tcount):
        try:
            html = time_candidates.nth(j).evaluate("el => el.outerHTML")
            print(f"[debug]   slot {j} markup: {html[:250]}")
        except Exception as e:
            print(f"[debug]   slot {j}: could not read markup ({e})")
        try:
            time_candidates.nth(j).click(timeout=2000)
            time_selected = True
            print(f"[debug]   slot {j}: click SUCCEEDED")
            break
        except Exception as e:
            print(f"[debug]   slot {j}: click failed ({e})")
            continue

    if not time_selected:
        raise RuntimeError("Could not find an available time slot on the first enabled date.")

    _click_next_when_enabled(dialog, page, step_label="date/time -> contact details")

    # --- Step 2: contact details (Navn, Email, Telefon, Sprog, Kommentar) +
    # data-processing consent checkbox, revealed after the first "Næste". ---
    print("[debug] step 2: filling contact details")
    navn_field = dialog.get_by_label(re.compile("Navn", re.IGNORECASE))
    try:
        navn_field.wait_for(state="visible", timeout=10000)
    except Exception:
        try:
            dialog_html = dialog.evaluate("el => el.outerHTML")
        except Exception:
            dialog_html = "<could not read dialog HTML>"
        print(f"[debug] Navn field never became visible. Current dialog HTML:\n{dialog_html[:3000]}")
        raise
    navn_field.fill(APPLICANT_NAME)
    dialog.get_by_label(re.compile("Email", re.IGNORECASE)).fill(APPLICANT_EMAIL)

    if APPLICANT_PHONE:
        try:
            dialog.get_by_label(re.compile("Telefon|Phone|Mobil", re.IGNORECASE)).fill(APPLICANT_PHONE)
        except Exception as e:
            print(f"[apply] could not fill phone field: {e}")

    # Language may be a native <select> or a custom dropdown - try both.
    try:
        lang_field = dialog.get_by_label(re.compile("Sprog|Language", re.IGNORECASE))
        try:
            lang_field.select_option(label=re.compile(APPLICANT_LANGUAGE, re.IGNORECASE))
        except Exception:
            # Custom dropdown fallback: click to open, then click the option text.
            lang_field.click(timeout=3000)
            dialog.get_by_text(re.compile(f"^{APPLICANT_LANGUAGE}$", re.IGNORECASE)).first.click(
                timeout=3000
            )
    except Exception as e:
        print(f"[apply] could not set language field: {e}")

    try:
        dialog.get_by_label(re.compile("besked|kommentar|comment|message", re.IGNORECASE)).fill(
            APPLICANT_MESSAGE
        )
    except Exception:
        print("[apply] no message field found in dialog (may not exist) - continuing")

    # NEVER fill the honeypot field ("Udfyld ikke dette felt" = spam trap).

    # Data-processing consent checkbox (GDPR), if present, scoped to the dialog.
    try:
        dialog.get_by_role("checkbox").first.check(timeout=5000)
    except Exception:
        print("[apply] no step-2 checkbox found/needed")

    _click_next_when_enabled(dialog, page, step_label="contact details -> confirmations")

    # --- Step 3: two confirmation toggles (RKI registration status, pets),
    # then the final booking button. These are factual statements about the
    # applicant - only flip them on if CONFIRM_* env vars say they're true. ---
    print("[debug] step 3: setting confirmation toggles")
    if CONFIRM_NOT_IN_RKI:
        try:
            dialog.get_by_label(re.compile("RKI", re.IGNORECASE)).check(timeout=5000)
        except Exception as e:
            print(f"[apply] could not check RKI confirmation toggle: {e}")
    if CONFIRM_NO_PETS:
        try:
            dialog.get_by_label(re.compile("kæledyr|pets", re.IGNORECASE)).check(timeout=5000)
        except Exception as e:
            print(f"[apply] could not check pets confirmation toggle: {e}")

    if not submit:
        # Test mode: prove every field/toggle across all 3 steps was found &
        # set correctly, but stop here WITHOUT clicking the final booking button.
        screenshot_path = f"/tmp/preview_{re.sub(r'[^a-zA-Z0-9]', '_', url)[-40:]}.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"[apply] TEST MODE - all steps filled but NOT submitted. Screenshot: {screenshot_path}")
        return True

    submitted = False
    for label_re in [
        re.compile("Book", re.IGNORECASE),
        re.compile("Bekræft", re.IGNORECASE),
        re.compile("Send", re.IGNORECASE),
        BOOKING_TEXT_RE,
        re.compile("Godkend|Afslut|Submit", re.IGNORECASE),
    ]:
        try:
            dialog.get_by_role("button", name=label_re).last.click(timeout=5000)
            submitted = True
            break
        except Exception:
            continue

    page.wait_for_timeout(3000)  # let any async confirmation render
    screenshot_path = f"/tmp/confirm_{re.sub(r'[^a-zA-Z0-9]', '_', url)[-40:]}.png"
    page.screenshot(path=screenshot_path, full_page=True)
    return submitted


def main():
    test_url = os.environ.get("TEST_URL", "").strip()
    if test_url:
        # Manual test: fill the form on a real listing WITHOUT submitting,
        # to verify field-matching works. Triggered via workflow_dispatch input.
        if not (APPLICANT_NAME and APPLICANT_EMAIL):
            print("APPLICANT_NAME/APPLICANT_EMAIL are not set.", file=sys.stderr)
            sys.exit(1)
        print(f"[test] Filling (not submitting) form at {test_url}")
        ok = apply_to_listing(test_url, submit=False)
        print("[test] Form fields were found and filled." if ok else "[test] FAILED to fill form.")
        return

    if AUTO_APPLY and not (APPLICANT_NAME and APPLICANT_EMAIL):
        print("AUTO_APPLY is true but APPLICANT_NAME/APPLICANT_EMAIL are not set.", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    listings = fetch_listings()
    matches = [l for l in listings if l["rooms"] == TARGET_ROOMS]

    new_matches = [l for l in matches if l["url"] not in seen]
    print(f"Found {len(listings)} available listings, {len(matches)} with {TARGET_ROOMS} rooms, "
          f"{len(new_matches)} new.")

    for listing in new_matches:
        print(f"NEW: {listing['url']}  -> {listing['text'][:120]}")

        if AUTO_APPLY:
            try:
                ok = apply_to_listing(listing["url"])
            except Exception as e:
                ok = False
                print(f"[apply] ERROR: {e}")
            status = "Applied for viewing" if ok else "Found listing, but could NOT confirm the form submitted"
            send_email(
                subject=f"[Kereby] {status}: {listing['text'][:60]}",
                body=(
                    f"{status}.\n\n"
                    f"Listing: {listing['url']}\n"
                    f"Details: {listing['text']}\n"
                ),
            )
        else:
            send_email(
                subject=f"[Kereby] New {TARGET_ROOMS}-room listing (dry run, not applied)",
                body=f"Listing: {listing['url']}\nDetails: {listing['text']}\n\n"
                     f"AUTO_APPLY is false, so no application was submitted.",
            )

        seen.add(listing["url"])

    save_seen(seen)


if __name__ == "__main__":
    main()
