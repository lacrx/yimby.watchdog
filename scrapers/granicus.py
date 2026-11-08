#!/usr/bin/env python3
"""
Granicus Publisher scraper — generic for any Granicus-hosted agency.

Reads agency config from agencies.yaml. Fetches meeting listings via RSS,
downloads agenda PDFs from AgendaViewer/MetaViewer pages, extracts text.

Usage:
    python granicus.py fetch --agency encinitas [--years N] [--deep]
    python granicus.py list --agency encinitas

Requires: requests, beautifulsoup4, lxml
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    load_agencies, agency_data_dir, USER_AGENT,
    safe_filename, cmd_list_meetings, rebuild_doc_index, log_discovery,
    meeting_needs_recheck,
)
import civic_utils

RECHECK_DAYS = getattr(civic_utils, "RECHECK_DAYS", 90)

GRAN_NS = "https://www.granicus.com/schema/rss-supplements"

DATE_PATTERNS = [
    (r"(\w+ \d{1,2},?\s+\d{4})", ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]),
    (r"(\d{1,2}/\d{1,2}/\d{4})", ["%m/%d/%Y"]),
]


def parse_date_from_title(title):
    for pattern, fmts in DATE_PATTERNS:
        m = re.search(pattern, title)
        if m:
            s = m.group(1)
            for fmt in fmts:
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
    return None


def extract_body_from_title(title):
    """Extract body name from RSS item title like 'Regular City Council Meeting 6:00 p.m. - Jun 24, 2026'."""
    # Strip date portion (everything after " - Mon DD, YYYY" pattern)
    body = re.split(r'\s*-\s*(?:\w{3}\s+\d{1,2},?\s+\d{4}|\d{1,2}:\d{2})', title)[0].strip()
    # Strip leading "Regular/Special" and trailing "Meeting/Hearing/Session"
    body = re.sub(r'^(?:Regular|Special)\s+', '', body, flags=re.I)
    body = re.sub(r'\s+\d{1,2}:\d{2}\s*[ap]\.?m\.?\s*$', '', body, flags=re.I).strip()
    body = re.sub(r'\s+(?:Regular |Special )?(?:Meeting|Hearing|Session|Workshop)\s*$', '', body, flags=re.I).strip()
    return body or title.strip()


def meeting_id_from_link(link):
    """Extract event_id or clip_id from AgendaViewer URL."""
    parsed = urlparse(link)
    params = parse_qs(parsed.query)
    if "event_id" in params:
        return f"evt-{params['event_id'][0]}"
    if "clip_id" in params:
        return f"clip-{params['clip_id'][0]}"
    return hashlib.md5(link.encode()).hexdigest()[:12]


def fetch_rss(base_url, view_id, mode="agendas"):
    url = f"{base_url}/ViewPublisherRSS.php?view_id={view_id}&mode={mode}"
    resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


def parse_rss(xml_text, cutoff_date=None):
    root = ElementTree.fromstring(xml_text)
    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        guid = item.findtext("guid", "").strip()

        dt = parse_date_from_title(title)
        if not dt:
            pub_parts = item.find(f"{{{GRAN_NS}}}pubDateParts")
            if pub_parts is not None:
                try:
                    dt = datetime(
                        int(pub_parts.get("yr")),
                        int(pub_parts.get("mo")),
                        int(pub_parts.get("day")),
                    )
                except (ValueError, TypeError):
                    pass

        if cutoff_date and dt and dt < cutoff_date:
            continue

        items.append({
            "title": title,
            "link": link,
            "guid": guid,
            "date": dt,
            "body": extract_body_from_title(title),
            "meeting_id": meeting_id_from_link(link),
        })

    return items


def fetch_viewpublisher_html(base_url, view_id, cutoff_date=None):
    """Scrape ViewPublisher HTML page for archived meetings beyond RSS cap."""
    start = cutoff_date.strftime("%Y-%m-%d") if cutoff_date else "2010-01-01"
    end = datetime.now().strftime("%Y-%m-%d")
    url = f"{base_url}/ViewPublisher.php?view_id={view_id}&clip_id=0&range=custom&start_date={start}&end_date={end}"

    resp = requests.get(url, timeout=120, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    items = []
    seen_ids = set()
    current_body = None

    for el in soup.find_all(["h2", "h3", "a"]):
        if el.name in ("h2", "h3"):
            text = el.get_text(strip=True)
            if text and text not in ("Upcoming Events", "Archived Meetings"):
                current_body = text
            continue

        href = el.get("href", "")
        if "AgendaViewer" not in href:
            continue

        mid = meeting_id_from_link(href)
        if mid in seen_ids:
            continue
        seen_ids.add(mid)

        # Rows are <li class="table-row"> on some sites and <tr><td class="listItem">
        # on others (San Marcos). The name is the first cell; the date cell has a
        # hidden epoch <span> followed by the visible text.
        row = el.find_parent("li", class_="table-row") or el.find_parent("tr")
        date_text = ""
        name_text = ""
        epoch = None
        dt = None
        if row:
            cells = row.find_all("td", recursive=False)
            date_cell = row.find("div", class_=re.compile(r"date")) if not cells else None
            name_cell = row.find("div", class_=re.compile(r"name")) if not cells else None
            if cells:
                name_cell = cells[0]
                date_cell = cells[1] if len(cells) > 1 else None
            if date_cell:
                span = date_cell.find("span", style=re.compile(r"display:\s*none"))
                if span and span.get_text(strip=True).isdigit():
                    epoch = int(span.get_text(strip=True))
                    span.extract()
                date_text = date_cell.get_text(" ", strip=True)
            if name_cell:
                name_text = name_cell.get_text(" ", strip=True)

        if date_text:
            date_text = re.sub(r'\s+', ' ', date_text.replace('\xa0', ' ')).strip()
            dt = parse_date_from_title(date_text)
        if dt is None and epoch:
            try:
                dt = datetime.fromtimestamp(epoch)
            except (OverflowError, OSError, ValueError):
                dt = None

        if cutoff_date and dt and dt < cutoff_date:
            continue

        link_url = href if href.startswith("http") else f"https:{href}"
        body = ""
        if name_text:
            body = extract_body_from_title(re.sub(r'\s+', ' ', name_text.replace('\xa0', ' ')))
        body = body or current_body or ""
        title = f"{body} - {date_text}" if body and date_text else body or date_text

        items.append({
            "title": title,
            "link": link_url,
            "guid": "",
            "date": dt,
            "body": body,
            "meeting_id": mid,
        })

    return items


# Agenda hosts that no longer resolve, mapped to their current location.
# San Diego's OnBase Agenda Online moved to Hyland Cloud; meeting IDs carry over.
HOST_REWRITES = {
    "https://onbase.sandiego.gov/OnBaseAgendaOnline": "https://sandiego.hylandcloud.com/211agendaonlinecouncil",
}


def _rewrite_host(url):
    for old, new in HOST_REWRITES.items():
        if url and url.startswith(old):
            return new + url[len(old):]
    return url


def _get_with_rewrites(url, max_hops=5):
    """GET that follows redirects itself so dead hosts can be rewritten mid-chain."""
    url = _rewrite_host(url)
    for _ in range(max_hops):
        resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT},
                            allow_redirects=False)
        if resp.is_redirect and resp.headers.get("Location"):
            url = _rewrite_host(urljoin(url, resp.headers["Location"]))
            continue
        return resp
    raise requests.TooManyRedirects(f"More than {max_hops} redirects for {url}")


def extract_pdf_links_from_agenda(agenda_url, base_url):
    """Scrape AgendaViewer page for PDF download links."""
    resp = _get_with_rewrites(agenda_url)
    resp.raise_for_status()

    pdfs = []
    text = resp.text
    from urllib.parse import unquote

    # Type 0: OnBase Agenda Online (San Diego). The meeting page is a JS shell;
    # the agenda itself is served as HTML from Documents/ViewAgenda.
    onbase = re.search(r'^(https?://[^?]+?)/Meetings/ViewMeeting\?id=(\d+)', resp.url)
    if onbase:
        root, meeting_id = onbase.groups()
        return [{"url": f"{root}/Documents/ViewAgenda?meetingId={meeting_id}&doctype=1",
                 "name": "agenda", "type": "agenda_text"}]

    # Type 0b: Redirected to Google Docs gview (San Marcos, some Encinitas committees).
    # The server-side redirect lands on docs.google.com/gview?url=<encoded PDF>.
    gview_matches = []
    if "docs.google.com/gview" in resp.url:
        gview_params = parse_qs(urlparse(resp.url).query)
        if "url" in gview_params:
            inner_url = gview_params["url"][0]
            fname = inner_url.rsplit("/", 1)[-1] if "/" in inner_url else "agenda.pdf"
            pdfs.append({"url": inner_url, "name": fname, "type": "agenda"})
            gview_matches.append(inner_url)
            return pdfs

    # Type 1a: Google Docs Viewer embed in page HTML → DocumentViewer.php?file=...
    # URL may be encoded (%3D for =, %26 for &, or double-encoded %253D)
    decoded = unquote(unquote(text))
    doc_matches = re.findall(r'DocumentViewer\.php\?file=([^&"\'\\]+\.pdf)', decoded)
    seen_docs = set()
    for fname in doc_matches:
        if fname in seen_docs:
            continue
        seen_docs.add(fname)
        url = f"{base_url}/DocumentViewer.php?file={fname}&view=1"
        pdfs.append({"url": url, "name": fname, "type": "agenda"})

    # Type 1b: gview embedded in page HTML (not a redirect)
    if not doc_matches:
        gview_in_page = re.findall(r'docs\.google\.com/gview\?[^"\']*url=([^&"\'\\]+)', decoded)
        for raw_url in gview_in_page:
            inner_url = unquote(raw_url)
            if inner_url in seen_docs:
                continue
            seen_docs.add(inner_url)
            gview_matches.append(inner_url)
            fname = inner_url.rsplit("/", 1)[-1] if "/" in inner_url else "agenda.pdf"
            pdfs.append({"url": inner_url, "name": fname, "type": "agenda"})

    # Type 2: MetaViewer.php links (archived meetings with individual docs)
    soup = BeautifulSoup(resp.text, "lxml")
    meta_links = soup.find_all("a", href=re.compile(r"MetaViewer\.php"))
    if meta_links:
        for link in meta_links:
            href = link.get("href", "")
            meta_m = re.search(r'meta_id=(\d+)', href)
            if meta_m:
                meta_id = meta_m.group(1)
                full_url = urljoin(base_url + "/", href)
                label = link.get_text(strip=True) or f"meta-{meta_id}"
                pdfs.append({"url": full_url, "name": label, "meta_id": meta_id, "type": "staff_report"})

    # If MetaViewer links but no main agenda PDF or gview, the page itself is the agenda
    if meta_links and not doc_matches and not gview_matches:
        pdfs.insert(0, {"url": resp.url, "name": "agenda_html", "type": "agenda_html",
                        "html": resp.text})

    return pdfs


def _norm_body(body):
    return re.sub(r"[^a-z0-9]+", " ", (body or "").lower()).strip()


def _bodies_match(a, b):
    a, b = _norm_body(a), _norm_body(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _mdate(date_str):
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def reconcile_superseded(slug, meetings_dir, docs_dir):
    """Mark evt-N rows superseded_by clip-M when an archived clip has the same
    date and body, and copy evt docs onto the clip if the clip has none."""
    import shutil
    rows = []
    for mdir in meetings_dir.iterdir():
        mf = mdir / "meeting.json"
        meta = load_json(mf) if mf.exists() else None
        if meta:
            rows.append((mdir.name, mf, meta))
    clips = [(i, mf, m) for i, mf, m in rows if i.startswith("clip-") and m.get("date")]
    marked = merged = 0
    for eid, emf, em in rows:
        if not eid.startswith("evt-") or em.get("superseded_by") or not em.get("date"):
            continue
        for cid, cmf, cm in clips:
            if _mdate(cm["date"]) != _mdate(em["date"]) or not _bodies_match(cm.get("body"), em.get("body")):
                continue
            em["superseded_by"] = cid
            save_json(emf, em)
            marked += 1
            clip_has_docs = any(f.name.startswith(cid + "-") and f.stat().st_size > 0
                                for f in docs_dir.iterdir())
            if not clip_has_docs:
                for f in list(docs_dir.iterdir()):
                    if f.name.startswith(eid + "-"):
                        dest = docs_dir / (cid + f.name[len(eid):])
                        if not dest.exists():
                            shutil.copy2(f, dest)
                            merged += 1
            break
    if marked:
        print(f"  Superseded: marked {marked} evt rows, merged {merged} doc files onto clips")
    return marked


def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    slug = args.agency
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    if cfg["platform"] != "granicus":
        print(f"{slug} is not a Granicus agency (platform: {cfg['platform']})")
        sys.exit(1)

    base_url = cfg["base_url"]
    view_id = cfg.get("view_id", 1)
    data_dir = agency_data_dir(slug)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    state_file = data_dir / "state.json"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}
    failed = state.setdefault("failed_fetches", {})
    now = datetime.now()

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching {cfg['name']} meetings (Granicus view_id={view_id})...")
    xml_text = fetch_rss(base_url, view_id)
    items = parse_rss(xml_text, cutoff_date=cutoff)
    print(f"  {len(items)} meetings in RSS feed (after {cutoff.strftime('%Y-%m-%d')} cutoff)")

    # RSS feeds are capped (~100 items). For deep historical fetches,
    # supplement with HTML scraping of the ViewPublisher archive page.
    rss_ids = {it["meeting_id"] for it in items}
    oldest_rss = min((it["date"] for it in items if it["date"]), default=None)
    if oldest_rss and cutoff < oldest_rss:
        print(f"  RSS oldest: {oldest_rss.strftime('%Y-%m-%d')}, cutoff: {cutoff.strftime('%Y-%m-%d')} — fetching HTML archive...")
        html_items = fetch_viewpublisher_html(base_url, view_id, cutoff_date=cutoff)
        new_from_html = [it for it in html_items if it["meeting_id"] not in rss_ids]
        print(f"  {len(html_items)} meetings in HTML archive, {len(new_from_html)} beyond RSS")
        items.extend(new_from_html)

    # Drop undated archive items: they cannot be windowed or matched to anything.
    undated = [it for it in items if not it["date"]]
    if undated:
        print(f"  Skipping {len(undated)} items with no parseable date")
        items = [it for it in items if it["date"]]

    # Retry list: failed fetches that have since left the RSS feed, plus any
    # ids forced with --retry. Entries expire RECHECK_DAYS after first failure.
    have = {it["meeting_id"] for it in items}
    retry_ids = list(getattr(args, "retry", None) or [])
    for rid, info in list(failed.items()):
        try:
            age = (now - datetime.fromisoformat(info["first_failure"])).days
        except (KeyError, ValueError):
            age = 0
        if age > RECHECK_DAYS:
            print(f"  Retry list: giving up on {rid} after {age} days ({info.get('url')})")
            del failed[rid]
        else:
            retry_ids.append(rid)
    for rid in retry_ids:
        if rid in have:
            continue
        mf = meetings_dir / rid / "meeting.json"
        meta = load_json(mf) if mf.exists() else None
        if not meta:
            print(f"  Retry: no meeting.json for {rid}, skipping")
            continue
        info = failed.get(rid, {})
        url = info.get("url") or meta.get("agenda_url")
        items.append({
            "title": meta.get("title", ""), "link": url, "guid": "",
            "date": datetime.strptime(meta["date"], "%m/%d/%Y") if _mdate(meta.get("date")) else None,
            "body": meta.get("body", ""), "meeting_id": rid, "forced": True,
        })
        have.add(rid)
        print(f"  RETRY: {rid} ({url})")

    new_count = 0
    doc_count = 0

    for item in items:
        mid = item["meeting_id"]
        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)

        meeting_file = mdir / "meeting.json"
        if not meeting_file.exists():
            meeting_meta = {
                "body": item["body"],
                "date": item["date"].strftime("%m/%d/%Y") if item["date"] else "",
                "title": item["title"],
                "agenda_url": item["link"],
                "id": mid,
                "source": "granicus",
                "agency": slug,
            }
            save_json(meeting_file, meeting_meta)
            new_count += 1
            print(f"  NEW: {item['title']}")
        else:
            # Heal rows written earlier without a date or body (old HTML parser).
            meta = load_json(meeting_file) or {}
            changed = False
            if not meta.get("date") and item["date"]:
                meta["date"] = item["date"].strftime("%m/%d/%Y")
                changed = True
            if not meta.get("body") and item["body"]:
                meta["body"] = item["body"]
                meta["title"] = item["title"]
                changed = True
            if changed:
                save_json(meeting_file, meta)

        if mid in state["meetings"] and not args.deep and not item.get("forced"):
            meeting_date = state["meetings"][mid].get("date", "") or (item["date"].strftime("%m/%d/%Y") if item["date"] else "")
            if not meeting_needs_recheck(mid, docs_dir, meeting_date, max_age_days=RECHECK_DAYS):
                continue
            print(f"  RECHECK: {item['title']} (no documents yet)")


        # Fetch agenda PDFs
        try:
            pdfs = extract_pdf_links_from_agenda(item["link"], base_url)
        except Exception as e:
            print(f"  Failed to fetch agenda page for {mid}: {e}")
            rec = failed.setdefault(mid, {"url": item["link"], "attempts": 0,
                                          "first_failure": now.isoformat()})
            rec["attempts"] += 1
            rec["last_error"] = str(e)[:200]
            rec["last_attempt"] = now.isoformat()
            continue
        agenda_failed = False

        has_agenda = any(p["type"] in ("agenda", "agenda_text") for p in pdfs)

        for i, pdf_info in enumerate(pdfs):
            if pdf_info["type"] == "agenda_html":
                dest = docs_dir / f"{mid}-agenda.txt"
                if not dest.exists() and pdf_info.get("html"):
                    agenda_text = BeautifulSoup(pdf_info["html"], "lxml").get_text("\n", strip=True)
                    if agenda_text:
                        dest.write_text(agenda_text)
                        doc_count += 1
                continue

            if pdf_info["type"] == "agenda_text":
                dest = docs_dir / f"{mid}-agenda.txt"
                if dest.exists():
                    continue
                try:
                    r = requests.get(pdf_info["url"], timeout=60, headers={"User-Agent": USER_AGENT})
                    r.raise_for_status()
                    agenda_text = BeautifulSoup(r.text, "lxml").get_text("\n", strip=True)
                except Exception as e:
                    print(f"  Failed to fetch HTML agenda for {mid}: {e}")
                    agenda_failed = True
                    rec = failed.setdefault(mid, {"url": item["link"], "attempts": 0,
                                                  "first_failure": now.isoformat()})
                    rec["attempts"] += 1
                    rec["last_error"] = str(e)[:200]
                    rec["last_attempt"] = now.isoformat()
                    continue
                if agenda_text:
                    dest.write_text(agenda_text)
                    doc_count += 1
                time.sleep(0.5)
                continue

            if pdf_info["type"] == "agenda":
                fname = f"{mid}-agenda.pdf"
            else:
                meta_id = pdf_info.get("meta_id", "")
                label = safe_filename(pdf_info["name"])
                fname = f"{mid}-{meta_id}-{label}.pdf" if meta_id else f"{mid}-{label}.pdf"

            dest = docs_dir / fname
            if dest.exists():
                continue

            # Without --deep: download agendas always; for clip_id meetings
            # (no main agenda PDF), download first 3 MetaViewer docs (agenda items)
            if not args.deep and pdf_info["type"] == "staff_report":
                if has_agenda:
                    continue
                meta_idx = len([p for p in pdfs[:i] if p["type"] == "staff_report"])
                if meta_idx >= 3:
                    continue

            if download_pdf(pdf_info["url"], dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.5)

        if agenda_failed:
            continue
        failed.pop(mid, None)
        state["meetings"][mid] = {
            "fetched": datetime.now().isoformat(),
            "title": item["title"],
            "date": item["date"].strftime("%m/%d/%Y") if item["date"] else "",
        }
        time.sleep(1)

    state["last_fetch"] = datetime.now().isoformat()
    reconcile_superseded(slug, meetings_dir, docs_dir)
    rebuild_doc_index(slug, state, docs_dir)
    save_json(state_file, state)
    log_discovery(slug, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(args.agency)


def main():
    parser = argparse.ArgumentParser(description="Granicus Publisher scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--agency", required=True)
    p_fetch.add_argument("--years", default="1")
    p_fetch.add_argument("--deep", action="store_true")
    p_fetch.add_argument("--retry", action="append", default=[],
                         help="force re-fetch of a meeting id (e.g. evt-8972); repeatable")

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
