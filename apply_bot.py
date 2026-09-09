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
APPLICANT_MESSAGE = os.environ.get(
    "APPLICANT_MESSAGE",
    "Hej, jeg er meget interesseret i denne bolig og vil gerne booke en fremvisning. Mvh",
)

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


def _fill_and_maybe_submit(page, submit: bool, url: str) -> bool:
    # The "Bestil fremvisning" trigger might be a <button>, a <div>, or an
    # <a> depending on the theme markup, so try several locator strategies
    # rather than assuming one element type.
    dialog = page.get_by_role("dialog", name=re.compile("Bestil fremvisning", re.IGNORECASE))
    opened = False
    if dialog.count() > 0 and dialog.first.is_visible():
        opened = True  # dialog already present/visible, no click needed
    else:
        click_strategies = [
            lambda: page.get_by_role("button", name=re.compile("Bestil fremvisning", re.IGNORECASE)).first,
            lambda: page.get_by_role("link", name=re.compile("Bestil fremvisning", re.IGNORECASE)).first,
            lambda: page.get_by_text(re.compile("Bestil fremvisning", re.IGNORECASE)).first,
        ]
        for strategy in click_strategies:
            try:
                strategy().click(timeout=7000)
                opened = True
                break
            except Exception:
                continue

    if not opened:
        raise RuntimeError(
            "Could not find/open the 'Bestil fremvisning' booking dialog trigger on the page."
        )

    dialog = page.get_by_role("dialog", name=re.compile("Bestil fremvisning", re.IGNORECASE))
    dialog.wait_for(state="visible", timeout=10000)

    # Fill by visible label text, scoped to the dialog - robust to
    # internal field names/ids.
    dialog.get_by_label(re.compile("Navn", re.IGNORECASE)).fill(APPLICANT_NAME)
    dialog.get_by_label(re.compile("Email", re.IGNORECASE)).fill(APPLICANT_EMAIL)
    try:
        dialog.get_by_label(re.compile("besked|kommentar|message", re.IGNORECASE)).fill(
            APPLICANT_MESSAGE
        )
    except Exception:
        print("[apply] no message field found in dialog (may not exist) - continuing")

    # NEVER fill the honeypot field ("Udfyld ikke dette felt" = spam trap).

    # Consent checkbox (GDPR), if present, scoped to the dialog.
    try:
        dialog.get_by_role("checkbox").first.check(timeout=5000)
    except Exception:
        print("[apply] no checkbox found/needed")

    if not submit:
        # Test mode: prove the fields were found & filled, but do not send.
        screenshot_path = f"/tmp/preview_{re.sub(r'[^a-zA-Z0-9]', '_', url)[-40:]}.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"[apply] TEST MODE - form filled but NOT submitted. Screenshot: {screenshot_path}")
        return True

    submitted = False
    for label in ["Send", "Bestil fremvisning", "Submit"]:
        try:
            dialog.get_by_role("button", name=re.compile(label, re.IGNORECASE)).last.click(
                timeout=5000
            )
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
