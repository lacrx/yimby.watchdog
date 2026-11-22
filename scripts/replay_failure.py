#!/usr/bin/env python3
"""Replay a saved failure fixture against the parser that choked on it.

A fixture (data/pipeline/failures/<run_id>/<agency>-<n>.json) holds the exact
response body that failed. This feeds that body back to the agency's listing
parser offline, with no network, so a failure is reproducible on demand instead
of only when the remote site misbehaves again.

That is the whole point of the fixtures: a failure you can replay is a failure
an agent can prove it fixed. Without it, "it works now" and "the site changed
back" are the same observation.

    python3 scripts/replay_failure.py data/pipeline/failures/<run>/carlsbad-1.json
    python3 scripts/replay_failure.py <fixture> --live     # also re-request now
    python3 scripts/replay_failure.py --list               # supported platforms

Exit codes follow the repo convention that an empty listing is a failure:
    0  the parser returned at least one item
    3  the parser returned nothing, or raised
    2  the fixture or its platform could not be used
"""

import argparse
import base64
import json
import sys
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


def fixture_body(fixture):
    body = fixture.get("body", "")
    if fixture.get("body_encoding") == "base64":
        return base64.b64decode(body).decode("utf-8", "replace")
    return body


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
    print(f"fixture: {path}")
    print(f"agency:  {agency}  platform: {platform or '?'}")
    print(f"url:     {url}")
    print(f"status:  {fixture.get('status')}  captured: {fixture.get('ts')}")
    if fixture.get("exc_type"):
        print(f"error:   {fixture['exc_type']}: {fixture.get('exc_msg', '')[:200]}")

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
