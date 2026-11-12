#!/usr/bin/env python3
"""
Swagit/Granicus video portal scraper — paginated HTML listings with agenda PDFs.

Handles sites on carlsbadca.new.swagit.com and similar Swagit portals.
Each body is a slug under /views/{view_id}/{body_slug}. Listings are
paginated (?page=N), 10 meetings per page. Agenda PDFs live at
/videos/{video_id}/agenda (302 redirect to granicus CDN).

Usage:
    python swagit.py fetch --agency carlsbad [--years N] [--deep]
    python swagit.py list --agency carlsbad

Requires: requests, beautifulsoup4, lxml
"""

import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup, Comment

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    load_agencies, agency_data_dir, make_meeting_id,
    cmd_list_meetings, rebuild_doc_index, log_discovery,
    meeting_needs_recheck,
)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})


def _body_from_title(title):
    """Extract body name from Swagit meeting title like 'Housing Commission Meeting'."""
    body = re.sub(r'\s+(?:Regular |Special )?(?:Meeting|Hearing|Session|Workshop)\s*$', '', title, flags=re.I).strip()
    return body or None


def _duration_seconds(text):
    """Parse a Swagit duration string like '03h 29m' or '01m 45s' into seconds."""
    total = 0
    for value, unit in re.findall(r"(\d+)\s*([hms])", text or "", flags=re.I):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit.lower()]
    return total


def _is_special(title):
    """True for a special or closed-session listing row."""
    return bool(re.search(r"\bspecial\b|\bclosed\b", title or "", flags=re.I))


def _row_rank(m):
    """Sort key deciding which same-day row owns the canonical date-only ID.

    A regular meeting outranks a special/closed session, and a longer video
    outranks a stub, so the substantive meeting keeps the plain ID. Closed
    sessions are not broadcast, so their rows are typically under two minutes.
    """
    return (
        _is_special(m.get("title")),
        -_duration_seconds(m.get("duration")),
        int(m["video_id"]),
    )


def _assign_meeting_ids(prefix, rows):
    """Return [(row, meeting_id, sibling_ids)] with same-day collisions resolved.

    Swagit routinely lists two meetings for one body on one day: a short
    closed-session special plus the regular meeting. A date-only ID collapses
    them onto a single record, so the second row is either skipped as
    already-seen or clobbers the first one's agenda PDF.

    A date-only ID is unique for the overwhelming majority of meetings and is
    kept as-is so existing records stay addressable. When a body does meet
    twice in a day, the primary row keeps the plain ID and each other row gets
    a 'v{video_id}' suffix appended with no separator, so prefix matching on
    '{mid}-' (used by meeting_needs_recheck and the doc index) cannot mistake
    one meeting's documents for another's.
    """
    groups = {}
    for m in rows:
        groups.setdefault((m["effective_body"], m["date"]), []).append(m)

    assigned = []
    for (body, dt), group in groups.items():
        base = make_meeting_id(prefix, body, dt)
        ordered = sorted(group, key=_row_rank)
        ids = [base] + [f"{base}v{m['video_id']}" for m in ordered[1:]]
        for idx, (m, mid) in enumerate(zip(ordered, ids)):
            m["_same_day_primary"] = idx == 0
            assigned.append((m, mid, [i for i in ids if i != mid]))

    assigned.sort(key=lambda t: (-t[0]["date"].timestamp(), _row_rank(t[0])))
    return assigned


def _supersede_stale_docs(docs_dir, mid):
    """Move this scraper's agenda files for mid aside when they belong to another video.

    Used when a date-only ID previously resolved to a different meeting, so the
    wrong agenda text is not left attached to the ID. Only the agenda files
    this scraper produces are moved, including the pre-split '-agenda-partNN'
    chunks, which extraction reads directly and would otherwise be left behind
    describing the wrong meeting. Documents other scrapers filed under the same
    ID (action agendas, minutes) are left alone, since nothing here can
    regenerate them. Files are moved to documents/_superseded/ rather than
    deleted. These globs cannot match a sibling's '{mid}v{video_id}-*' files.
    """
    stale = sorted(
        set(docs_dir.glob(f"{mid}-agenda.*")) | set(docs_dir.glob(f"{mid}-agenda-part*"))
    )
    if not stale:
        return 0
    attic = docs_dir / "_superseded"
    attic.mkdir(exist_ok=True)
    for f in stale:
        f.rename(attic / f.name)
    return len(stale)


def get_agency_config(slug):
    cfg = load_agencies(enabled_only=False).get(slug)
    if not cfg:
        print(f"Agency '{slug}' not found in agencies.yaml")
        sys.exit(1)
    return cfg


def fetch_page(url):
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_body_tabs(html):
    """Extract body name -> slug from navigation tabs."""
    soup = BeautifulSoup(html, "lxml")
    tabs = {}
    for a in soup.select("ul.nav-tabs-swagit a[href]"):
        href = a["href"]
        m = re.search(r"/views/\d+/(.+)$", href)
        if m:
            tabs[a.get_text(strip=True)] = m.group(1)
    return tabs


def parse_meeting_rows(html):
    """Parse meetings from video-table. Returns list of dicts."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="video-table")
    if not table:
        return []

    meetings = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue

        link = cells[0].find("a", href=True)
        if not link:
            continue

        href = link["href"]
        video_match = re.search(r"/videos/(\d+)", href)
        if not video_match:
            continue

        video_id = video_match.group(1)
        title = link.get_text(strip=True)
        date_text = cells[1].get_text(strip=True)

        try:
            dt = datetime.strptime(date_text, "%b %d, %Y")
        except ValueError:
            continue

        duration = cells[2].get_text(strip=True) if len(cells) > 2 else ""

        # Agenda link may be in a commented-out column
        has_agenda = False
        for comment in tr.find_all(string=lambda t: isinstance(t, Comment)):
            if "/agenda" in comment:
                has_agenda = True
                break
        if not has_agenda:
            for a in tr.find_all("a", href=True):
                if "/agenda" in a["href"]:
                    has_agenda = True
                    break

        meetings.append({
            "video_id": video_id,
            "title": title,
            "date": dt,
            "duration": duration,
            "has_agenda": has_agenda,
        })

    return meetings


def get_last_page(html):
    """Extract total page count from pagination."""
    soup = BeautifulSoup(html, "lxml")
    pag = soup.find("ul", class_="pagination")
    if not pag:
        return 1
    max_page = 1
    for li in pag.find_all("li"):
        a = li.find("a", href=True)
        text = (a or li.find("span") or li).get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))
    return max_page


def paginate_body(base_url, view_id, body_slug, cutoff_date):
    """Yield all meetings for a body, paginating until cutoff or end."""
    page = 1
    while True:
        url = f"{base_url}/views/{view_id}/{body_slug}"
        if page > 1:
            url += f"?page={page}"

        html = fetch_page(url)
        meetings = parse_meeting_rows(html)

        if not meetings:
            break

        past_cutoff = False
        for m in meetings:
            if m["date"] < cutoff_date:
                past_cutoff = True
                break
            yield m

        if past_cutoff:
            break

        last_page = get_last_page(html)
        if page >= last_page:
            break

        page += 1
        time.sleep(0.5)


def download_agenda_pdf(base_url, video_id, dest_path):
    """Download agenda PDF via /videos/{id}/agenda redirect."""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True

    url = f"{base_url}/videos/{video_id}/agenda"
    try:
        resp = SESSION.get(url, timeout=60, allow_redirects=True, stream=True)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()

        content = resp.content
        if b"%PDF" not in content[:20]:
            return False

        dest_path.write_bytes(content)
        return True
    except Exception as e:
        print(f"    Agenda download failed for {video_id}: {e}")
        return False


def cmd_fetch(args):
    slug = args.agency
    cfg = get_agency_config(slug)
    prefix = cfg.get("swagit_prefix", slug[:2])

    base_url = cfg["base_url"]
    view_id = cfg.get("swagit_view_id", "295")
    body_slugs = cfg.get("swagit_bodies", {})

    if not body_slugs:
        # Auto-discover from tabs on first page
        html = fetch_page(f"{base_url}/views/{view_id}")
        body_slugs = parse_body_tabs(html)
        if not body_slugs:
            print(f"No body slugs found for {slug}")
            return

    data_dir = agency_data_dir(slug)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(data_dir / "state.json") or {"last_fetch": None, "meetings": {}}

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching {cfg.get('name', slug)} meetings from Swagit...")
    new_count = 0
    doc_count = 0

    # video_id -> every meeting_id already claiming it on disk. Used to tell a
    # contested ID (whose previous video now has a record of its own) from a
    # genuine mismatch that nothing explains. It has to be a set of owners, not
    # one: the stale record under examination still claims its old video, so
    # asking "who owns it" would just name that record back.
    video_owner = {}
    for mdir in sorted(p for p in meetings_dir.iterdir() if p.is_dir()):
        meta_on_disk = load_json(mdir / "meeting.json") or {}
        vid = meta_on_disk.get("video_id")
        if vid:
            video_owner.setdefault(str(vid), set()).add(mdir.name)

    bodies_config = cfg.get("bodies", [])
    archive_slugs = cfg.get("swagit_archive_slugs", {})

    for body_name, body_slug in body_slugs.items():
        if bodies_config and body_name not in bodies_config:
            continue

        print(f"\n  {body_name}...")
        meeting_count = 0

        # Build list of slugs to scrape: main slug first, then archive slugs
        slugs_to_scrape = [body_slug]
        if body_name in archive_slugs:
            for archive_slug in archive_slugs[body_name]:
                # Extract year from slug (e.g. "city-council-2021" -> 2021)
                year_match = re.search(r"(\d{4})(?:-(\d{4}))?$", archive_slug)
                if year_match:
                    slug_year = int(year_match.group(2) or year_match.group(1))
                    if cutoff.year <= slug_year:
                        slugs_to_scrape.append(archive_slug)
                else:
                    slugs_to_scrape.append(archive_slug)

        # Collect every listing row for this body before minting IDs, so a body
        # that meets twice on one day gets one record per meeting instead of
        # one row silently displacing the other.
        rows = []
        seen_videos = set()
        for current_slug in slugs_to_scrape:
            for m in paginate_body(base_url, view_id, current_slug, cutoff):
                if m["video_id"] in seen_videos:
                    continue
                seen_videos.add(m["video_id"])
                # For mixed-body slugs, derive body from meeting title
                m["effective_body"] = _body_from_title(m["title"]) or body_name
                rows.append(m)
                meeting_count += 1

        for m, mid, siblings in _assign_meeting_ids(prefix, rows):
            dt = m["date"]
            effective_body = m["effective_body"]
            if siblings:
                print(f"    SAME-DAY: {effective_body} — {dt.strftime('%Y-%m-%d')} "
                      f"video {m['video_id']} ({m['duration']}) -> {mid}")
            mdir = meetings_dir / mid
            mdir.mkdir(exist_ok=True)

            meta = {
                "body": effective_body,
                "date": dt.strftime("%m/%d/%Y"),
                "title": f"{effective_body} — {dt.strftime('%b %d, %Y')}",
                "id": mid,
                "source": "swagit",
                "agency": slug,
                "video_id": m["video_id"],
                "video_url": f"{base_url}/videos/{m['video_id']}",
                "duration": m["duration"],
            }
            if siblings:
                meta["same_day_meetings"] = siblings
                meta["same_day_role"] = "primary" if m.get("_same_day_primary") else "additional"

            meeting_file = mdir / "meeting.json"
            existing = load_json(meeting_file) or {}
            if not existing:
                save_json(meeting_file, meta)
                new_count += 1
                print(f"    NEW: {effective_body} — {dt.strftime('%Y-%m-%d')}")
            elif not existing.get("video_id"):
                # Metadata predates video_id tracking. Backfill the field only;
                # the documents already on disk are this meeting's.
                save_json(meeting_file, {**meta, **{k: v for k, v in existing.items() if v}})
            elif existing.get("video_id") != m["video_id"] and (
                siblings
                or any(o != mid for o in video_owner.get(str(existing["video_id"]), ()))
            ):
                # A contested date-only ID that used to resolve to a different
                # meeting. Either the listing says another meeting shares this
                # body and date, or the video this ID used to describe now has a
                # record of its own (the archive slugs title some rows
                # differently, e.g. "City Council Closed", which mints a second
                # ID for what used to collide). Either way the old occupant is
                # safely homed elsewhere, so re-file this ID and move the wrong
                # agenda text aside so the right one is fetched.
                moved = _supersede_stale_docs(docs_dir, mid)
                save_json(meeting_file, meta)
                state["meetings"].pop(mid, None)
                print(f"    REPAIR: {mid} was video {existing.get('video_id')}, "
                      f"now video {m['video_id']} ({moved} stale agenda files superseded)")
            elif existing.get("video_id") != m["video_id"]:
                # Unexplained: the video this ID describes is not accounted for
                # anywhere else. Touch nothing at all — not the metadata, not
                # the agenda, not the state entry. Updating only some of those
                # is how meeting.json and state.json end up disagreeing.
                print(f"    WARNING: {mid} metadata says video {existing.get('video_id')}, "
                      f"listing says {m['video_id']}, and nothing explains it; skipped entirely")
                continue
            elif existing.get("same_day_meetings") != meta.get("same_day_meetings"):
                save_json(meeting_file, {**existing, **meta})

            if mid in state["meetings"] and not getattr(args, "force", False):
                if not meeting_needs_recheck(mid, docs_dir, dt.strftime("%m/%d/%Y")):
                    continue
                print(f"    RECHECK: {effective_body} — {dt.strftime('%Y-%m-%d')} (no documents yet)")

            pdf_name = f"{mid}-agenda.pdf"
            pdf_path = docs_dir / pdf_name
            if download_agenda_pdf(base_url, m["video_id"], pdf_path):
                text = extract_text(pdf_path)
                if text:
                    doc_count += 1
                time.sleep(0.3)

            state["meetings"][mid] = {
                "fetched": datetime.now().isoformat(),
                "body": effective_body,
                "video_id": m["video_id"],
                "date": dt.strftime("%m/%d/%Y"),
            }

        print(f"    {meeting_count} meetings")
        time.sleep(0.5)

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(slug, state, docs_dir)
    save_json(data_dir / "state.json", state)
    log_discovery(slug, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(args.agency)


def main():
    parser = argparse.ArgumentParser(description="Swagit/Granicus video portal scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--agency", required=True)
    p_fetch.add_argument("--years", default="1")
    p_fetch.add_argument("--deep", action="store_true")
    p_fetch.add_argument("--force", action="store_true")

    p_list = sub.add_parser("list")
    p_list.add_argument("--agency", required=True)

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
