#!/usr/bin/env python3
"""Replay a saved failure fixture against the code that choked on it.

A fixture (data/pipeline/failures/<run_id>/<agency>-<n>.json) holds the exact
response body that failed. This feeds that body back offline, with no network,
so a failure is reproducible on demand instead of only when the remote site
misbehaves again.

That is the whole point of the fixtures: a failure you can replay is a failure
an agent can prove it fixed. Without it, "it works now" and "the site changed
back" are the same observation.

TWO KINDS OF FIXTURE, AND WHY THAT DISTINCTION IS LOAD-BEARING (2026-11-22)

A fixture is either a DOCUMENT fetch (download_pdf asked for an agenda PDF) or
a LISTING fetch (a scraper asked for an index page of meetings). They need
opposite checks, and the first version of this script ran the listing parser at
everything.

The first real fixture the pipeline ever produced was a Del Mar meeting
cancellation notice served as a .docx. This script handed it to the CivicPlus
listing parser, which correctly found no meeting rows in a cancellation notice,
and the script reported `REPLAY FAIL: listing parsed 0 items`. Nothing was
broken. A loop fed that verdict would have spent its budget rewriting a parser
that worked.

So: prefer the fixture's own `phase`, fall back to the response Content-Type,
and keep "I do not support this document type" as a verdict separate from
"something is broken", because only the second one is a bug.

    python3 scripts/replay_failure.py data/pipeline/failures/<run>/carlsbad-1.json
    python3 scripts/replay_failure.py <fixture> --live     # also re-request now
    python3 scripts/replay_failure.py --list               # supported platforms

Exit codes. 0 and 3 keep the repo convention that an empty listing is a failure:
    0  listing: parsed at least one item.  document: the body is a usable PDF,
       so whatever failed at capture time would now succeed.
    3  something is broken: the parser returned nothing or raised, or the
       document is neither a document we support nor anything recognisable.
    4  the document is valid and intact but of a type the pipeline does not
       handle. Not a bug. A decision about what to support.
    2  the fixture itself is unusable, or its platform has no replay parser.
"""

import argparse
import base64
import io
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # scripts/ is not the import root

import civic_utils  # noqa: E402

OLD = datetime(2000, 1, 1)


def _civicplus(html, url):
    from scrapers import civicplus
    return civicplus.parse_agenda_page(html, url)


def _oceanside(html, url):
    from scrapers import oceanside
    return oceanside.parse_meetings(html)


def _nctd(html, url):
    from scrapers import nctd
    return nctd.parse_meetings(html)


def _swagit(html, url):
    from scrapers import swagit
    return swagit.parse_meeting_rows(html)


def _granicus(html, url):
    from scrapers import granicus
    return granicus.parse_rss(html)


def _municode(html, url):
    from scrapers import municode
    return municode.parse_meeting_list(html, url, OLD)


def _lemon_grove(html, url):
    from scrapers import lemon_grove
    return lemon_grove._parse_listing(html)


def _santee(html, url):
    from scrapers import santee
    return santee.parse_pdf_links(html, OLD)


def _elcajon(html, url):
    from scrapers import elcajon
    return elcajon.parse_events_page(html, OLD)


def _solana_beach(html, url):
    from scrapers import solana_beach
    return solana_beach.parse_drupal_page(html, url)


# platform (as agencies.yaml spells it) -> listing parser.
# Agencies whose platform is absent here have no parse function separable from
# fetching yet; the fixture is still saved and still readable by hand.
PARSERS = {
    "civicplus": _civicplus,
    "legistar_html": _oceanside,
    "custom_html": _nctd,
    "swagit": _swagit,
    "granicus": _granicus,
    "municode": _municode,
    "lemon_grove_events": _lemon_grove,
    "santee_cms": _santee,
    "elcajon_cms": _elcajon,
    "solana_drupal": _solana_beach,
}

# Covers 12 of the 22 agencies. The four platforms left out are escribe (6
# agencies), laserfiche (2), legistar_odata and coastal_api, whose listing
# code is not separable from fetching yet. Their fixtures are still captured
# and still readable; only this one-command replay is missing.


def fixture_bytes(fixture):
    """The captured body as raw bytes, which is the only honest form for a
    document. fixture_body() below decodes with errors='replace', which is
    right for HTML and destroys a PDF."""
    body = fixture.get("body", "")
    if fixture.get("body_encoding") == "base64":
        return base64.b64decode(body)
    return body.encode("utf-8", "replace")


def fixture_body(fixture):
    body = fixture.get("body", "")
    if fixture.get("body_encoding") == "base64":
        return base64.b64decode(body).decode("utf-8", "replace")
    return body


# Content types that mean "this response is a document", as opposed to an index
# page listing meetings. Matched as a prefix against the captured Content-Type.
DOCUMENT_TYPES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument",
    "application/msword",
    "application/vnd.ms-",
    "application/octet-stream",
    "application/rtf",
    "text/rtf",
)

LISTING_TYPES = ("text/html", "application/xhtml", "text/xml", "application/xml",
                 "application/rss", "application/atom", "application/json")


def content_type(fixture):
    headers = fixture.get("response_headers") or {}
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            return str(value).split(";")[0].strip().lower()
    return ""


def fixture_kind(fixture):
    """"document", "listing", or "unknown", and how it was decided.

    The fixture's own `phase` wins, because the capture site knows what it was
    asking for. Fixtures captured before 2026-11-22 have no phase, so fall back
    to the Content-Type the server sent.
    """
    phase = (fixture.get("phase") or "").strip().lower()
    if phase == "download":
        return "document", "phase"
    if phase in ("listing", "scrape", "index"):
        return "listing", "phase"

    ctype = content_type(fixture)
    if ctype.startswith(DOCUMENT_TYPES):
        return "document", f"content-type {ctype}"
    if ctype.startswith(LISTING_TYPES):
        return "listing", f"content-type {ctype}"
    return "unknown", f"content-type {ctype!r}" if ctype else "no content-type"


def classify_document(data):
    """What the captured bytes actually are.

    Returns (label, detail, verdict) where verdict is one of:
        "ok"          a PDF: the pipeline would accept this today
        "unsupported" a real document of a type we do not handle
        "broken"      not a document at all
    """
    if data[:4] == b"%PDF":
        return "PDF", f"{len(data)} bytes", "ok"

    if data[:2] == b"PK":
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except Exception as exc:
            return "ZIP", f"corrupt: {type(exc).__name__}", "broken"
        if "word/document.xml" in names:
            return "DOCX", f"{len(names)} entries", "unsupported"
        if any(n.startswith("xl/") for n in names):
            return "XLSX", f"{len(names)} entries", "unsupported"
        if any(n.startswith("ppt/") for n in names):
            return "PPTX", f"{len(names)} entries", "unsupported"
        return "ZIP", f"{len(names)} entries", "unsupported"

    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "OLE2 (legacy .doc/.xls)", f"{len(data)} bytes", "unsupported"

    head = data[:4096].lstrip()[:600].decode("utf-8", "replace").lower()
    if head.startswith("<!doctype html") or head.startswith("<html") or "<body" in head:
        # An HTML body where a document was expected is the shape of a login
        # wall, a Cloudflare interstitial or a 200-with-an-error-page. Those are
        # the cases worth waking somebody for, so they stay "broken".
        for needle, why in (
            ("cloudflare", "Cloudflare interstitial"),
            ("just a moment", "Cloudflare interstitial"),
            ("captcha", "CAPTCHA"),
            ("sign in", "login wall"),
            ("log in", "login wall"),
            ("access denied", "access denied"),
            ("not found", "error page"),
        ):
            if needle in head:
                return "HTML", why, "broken"
        return "HTML", "a web page where a document was expected", "broken"

    return "unrecognised", f"first bytes {data[:16]!r}", "broken"


def docx_text(data):
    """Plain text from a .docx, standard library only. None if it will not."""
    try:
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml")
    except Exception:
        return None
    text = re.sub(r"<[^>]+>", " ", xml.decode("utf-8", "replace"))
    return re.sub(r"\s+", " ", text).strip()


def replay_document(fixture, show):
    data = fixture_bytes(fixture)
    print(f"body:    {len(data)} bytes")
    if not data:
        print("ERROR: the fixture captured no body, so there is nothing to replay.")
        return 2

    label, detail, verdict = classify_document(data)
    print(f"content: {label} ({detail})")

    if verdict == "ok":
        print("REPLAY OK: the body is a valid PDF, so download_pdf would accept "
              "it now. The capture-time failure is not reproducible.")
        return 0

    if verdict == "unsupported":
        if label == "DOCX":
            text = docx_text(data)
            if text:
                print(f"text:    {len(text)} chars extractable without a new dependency")
                print(f"  {text[:show * 160]}")
        print(f"UNSUPPORTED: a valid {label}, intact, of a type the pipeline does "
              "not handle. Not a parser bug. Supporting this type is a decision.")
        return 4

    print(f"REPLAY FAIL: {detail}. A document was expected and this is not one.")
    return 3


def platform_for(fixture):
    """The fixture's own platform, or the agency's as agencies.yaml records it."""
    if fixture.get("platform"):
        return fixture["platform"]
    slug = fixture.get("agency")
    if not slug:
        return None
    try:
        return (civic_utils.load_agencies(enabled_only=False)
                .get(slug, {}).get("platform"))
    except Exception:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("fixture", nargs="?", help="path to a fixture JSON file")
    parser.add_argument("--list", action="store_true",
                        help="print the platforms that can be replayed")
    parser.add_argument("--live", action="store_true",
                        help="also re-request the URL now and compare")
    parser.add_argument("--show", type=int, default=3,
                        help="how many parsed items to print (default 3)")
    args = parser.parse_args(argv)

    if args.list:
        for name in sorted(PARSERS):
            print(name)
        return 0
    if not args.fixture:
        parser.error("a fixture path is required unless --list is given")

    path = Path(args.fixture)
    try:
        fixture = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read fixture {path}: {exc}")
        return 2

    agency = fixture.get("agency") or "?"
    url = fixture.get("url") or ""
    platform = platform_for(fixture)
    kind, how = fixture_kind(fixture)
    print(f"fixture: {path}")
    print(f"agency:  {agency}  platform: {platform or '?'}")
    print(f"url:     {url}")
    print(f"status:  {fixture.get('status')}  captured: {fixture.get('ts')}")
    print(f"kind:    {kind} (from {how})")
    if fixture.get("exc_type"):
        print(f"error:   {fixture['exc_type']}: {fixture.get('exc_msg', '')[:200]}")

    if kind == "document":
        if args.live:
            print("live:    not applicable to a document fixture; "
                  "re-run the pipeline for that agency instead.")
        return replay_document(fixture, args.show)

    if kind == "unknown":
        # Guessing here is exactly the mistake this script was fixed for.
        print("ERROR: cannot tell whether this is a document or a listing. The "
              "fixture has no `phase` and the server sent no usable "
              "Content-Type. Read it by hand, or re-capture with a newer "
              "civic_utils, which records `phase`.")
        return 2

    fn = PARSERS.get(platform)
    if fn is None:
        print(f"ERROR: no replay parser for platform {platform!r}. "
              f"--list shows what is supported.")
        return 2

    body = fixture_body(fixture)
    print(f"body:    {len(body)} chars"
          f"{' (truncated at capture)' if fixture.get('body_truncated') else ''}")

    try:
        items = fn(body, url)
    except Exception as exc:
        print(f"REPLAY FAIL: parser raised {type(exc).__name__}: {exc}")
        return 3

    items = list(items or [])
    print(f"parsed:  {len(items)} items")
    for item in items[:args.show]:
        print(f"  {str(item)[:160]}")

    if args.live:
        try:
            import requests
            resp = requests.get(url, timeout=60,
                                headers={"User-Agent": civic_utils.BROWSER_UA})
            live = list(fn(resp.text, url) or [])
            print(f"live:    HTTP {resp.status_code}, {len(live)} items "
                  f"({len(live) - len(items):+d} vs fixture)")
        except Exception as exc:
            print(f"live:    failed: {type(exc).__name__}: {exc}")

    if not items:
        print("REPLAY FAIL: listing parsed 0 items")
        return 3
    print("REPLAY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
