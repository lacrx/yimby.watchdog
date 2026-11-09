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
from urllib.parse import quote
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


class AgendaNotPublished(Exception):
    """Granicus says the agenda for this clip is not published (video-only clip)."""


_SESSION = None


def _session():
    """Shared session: Laserfiche WebLink bounces cookieless clients between
    DocView.aspx and itself forever, so redirect hops must carry cookies."""
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers["User-Agent"] = USER_AGENT
    return _SESSION


LASERFICHE_DOCVIEW = re.compile(
    r'^(https?://[^/]+/Portal)/DocView\.aspx\?(?=.*\bid=(\d+))(?=.*\brepo=([\w-]+))', re.I)
LASERFICHE_MAX_BYTES = 400 * 1024 * 1024  # rendered page-image packets reach ~270MB; refuse runaways


class PermanentFetchError(Exception):
    """Fetch can never succeed (record it in failed_fetches as permanent, don't retry)."""


def fetch_laserfiche_pdf(doc_url, dest, poll_seconds=2, max_wait=300):
    """Download a Laserfiche WebLink document as PDF.

    Tries ElectronicFile.aspx (native PDF); if the entry is page images only
    ("electronic file is empty"), asks the portal to render a PDF
    (GeneratePDF10.aspx + PDFTransition polling + PDF10/<key>/<id>).
    Requires a session cookie obtained from DocView.aspx first.
    """
    m = LASERFICHE_DOCVIEW.match(doc_url)
    if not m:
        raise ValueError(f"not a Laserfiche DocView url: {doc_url}")
    base, doc_id, repo = m.groups()
    s = _session()
    s.get(doc_url, timeout=30)  # session cookie
    r = s.get(f"{base}/ElectronicFile.aspx?docid={doc_id}&repo={repo}", timeout=120)
    if r.content[:5] == b"%PDF-":
        dest.write_bytes(r.content)
        return True
    hdrs = {"Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest", "Referer": doc_url}
    info = s.post(f"{base}/DocumentService.aspx/GetBasicDocumentInfo",
                  json={"repoName": repo, "entryId": int(doc_id)}, headers=hdrs, timeout=30)
    pages = (info.json().get("data") or {}).get("pageCount") or 0
    if not pages:
        raise PermanentFetchError("Laserfiche entry has no electronic file and no pages")
    r = s.post(f"{base}/GeneratePDF10.aspx?key={doc_id}&PageRange={quote(f'1 - {pages}')}"
               f"&Watermark=0&repo={repo}", data="{}", headers=hdrs, timeout=60)
    key = r.text.split("\n", 1)[0].strip()
    if r.status_code != 200 or not key:
        raise RuntimeError(f"Laserfiche PDF render refused ({r.status_code})")
    deadline = time.time() + max_wait
    while True:
        st = s.post(f"{base}/DocumentService.aspx/PDFTransition", json={"Key": key},
                    headers=hdrs, timeout=30).json().get("data") or {}
        if st.get("finished"):
            if not st.get("success"):
                raise RuntimeError(f"Laserfiche PDF render failed: {st.get('errMsg')}")
            break
        if time.time() > deadline:
            raise RuntimeError(f"Laserfiche PDF render timed out at {st.get('completion')}%")
        time.sleep(poll_seconds)
    with s.get(f"{base}/PDF10/{key}/{doc_id}", timeout=300, stream=True) as resp:
        resp.raise_for_status()
        total = 0
        first = True
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    if first:
                        if chunk[:5] != b"%PDF-":
                            raise RuntimeError("Laserfiche render returned non-PDF")
                        first = False
                    total += len(chunk)
                    if total > LASERFICHE_MAX_BYTES:
                        raise PermanentFetchError(
                            f"rendered PDF exceeds {LASERFICHE_MAX_BYTES // (1 << 20)}MB "
                            f"({pages} page-image scan, no electronic file); skipped")
                    f.write(chunk)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
    return True


def _get_with_rewrites(url, max_hops=8):
    """GET that follows redirects itself so dead hosts can be rewritten mid-chain."""
    url = _rewrite_host(url)
    for _ in range(max_hops):
        resp = _session().get(url, timeout=30, allow_redirects=False)
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
    if "This agenda is not currently published" in text:
        raise AgendaNotPublished(agenda_url)

    # Type 00: Laserfiche WebLink document (Encinitas). Needs the session cookie.
    if LASERFICHE_DOCVIEW.match(resp.url):
        return [{"url": resp.url, "name": "agenda", "type": "agenda", "laserfiche": True}]
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
        if not eid.startswith("evt-") or not em.get("date"):
            continue
        same_day = [c for c in clips if _mdate(c[2]["date"]) == _mdate(em["date"])]
        # Prefer a clip whose body is identical; fall back to containment.
        exact = [c for c in same_day if _norm_body(c[2].get("body")) == _norm_body(em.get("body"))]
        loose = [c for c in same_day if _bodies_match(c[2].get("body"), em.get("body"))]
        pick = (exact or loose or [None])[0]
        if pick is None:
            continue
        cid, cmf, cm = pick
        cur = em.get("superseded_by")
        if cur == cid:
            continue
        if cur and (cur in [c[0] for c in exact] or not exact):
            continue  # existing mark is already as good as the best candidate
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
    if marked:
        print(f"  Superseded: marked {marked} evt rows, merged {merged} doc files onto clips")
    return marked


def _heal_row(meta, item):
    """Fill a missing date/body on a meeting row from a listing item. Returns True if changed."""
    changed = False
    if not meta.get("date") and item.get("date"):
        meta["date"] = item["date"].strftime("%m/%d/%Y")
        changed = True
    if not meta.get("body") and item.get("body"):
        meta["body"] = item["body"]
        meta["title"] = item["title"]
        changed = True
    return changed


FILENAME_DATE_PATTERNS = [
    # 1349_A_City_Council_26-09-08_Regular_Agenda.pdf  (yy-mm-dd)
    (r"(?<!\d)(\d{2})-(\d{2})-(\d{2})(?!\d)", "ymd2"),
    (r"(?<!\d)(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})(?!\d)", "ymd4"),
    (r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](20\d{2})(?!\d)", "mdy4"),
]


def date_from_filename(name):
    """Best-effort meeting date from a document filename; None if nothing plausible."""
    for pat, kind in FILENAME_DATE_PATTERNS:
        for m in re.finditer(pat, name):
            a, b, c = (int(x) for x in m.groups())
            try:
                if kind == "ymd2":
                    dt = datetime(2000 + a, b, c)
                elif kind == "ymd4":
                    dt = datetime(a, b, c)
                else:
                    dt = datetime(c, a, b)
            except ValueError:
                continue
            if 2005 <= dt.year <= datetime.now().year + 1:
                return dt
    return None


def cmd_backfill_dates(args):
    """Date meeting rows that have none (rows older than the listing window).

    1. Live ViewPublisher archive (whole history, one request): clip id -> date/body.
    2. Dates embedded in document filenames.
    3. Otherwise left undated.
    """
    agencies = load_agencies(enabled_only=False)
    slug = args.agency
    cfg = agencies.get(slug)
    if not cfg or cfg["platform"] != "granicus":
        print(f"ERROR: {slug} is not a Granicus agency")
        sys.exit(1)
    data_dir = agency_data_dir(slug)
    meetings_dir = data_dir / "meetings"
    docs_dir = data_dir / "documents"
    undated = []
    for mdir in sorted(meetings_dir.iterdir()):
        mf = mdir / "meeting.json"
        meta = load_json(mf) if mf.exists() else None
        if meta and not meta.get("date"):
            undated.append((mdir.name, mf, meta))
    print(f"{cfg['name']}: {len(undated)} undated meeting rows")
    if not undated:
        return

    try:
        archive = fetch_viewpublisher_html(cfg["base_url"], cfg.get("view_id", 1))
    except Exception as e:
        print(f"ERROR: {slug}: archive fetch failed: {e}")
        sys.exit(2)
    if not archive:
        print(f"ERROR: {slug}: archive parsed 0 meetings")
        sys.exit(3)
    by_id = {it["meeting_id"]: it for it in archive if it.get("date")}
    print(f"  archive: {len(archive)} meetings, {len(by_id)} dated")

    doc_names = sorted(f.name for f in docs_dir.iterdir()) if docs_dir.exists() else []
    via_archive = via_filename = left = 0
    for mid, mf, meta in undated:
        item = by_id.get(mid)
        if item and _heal_row(meta, item):
            meta["date_source"] = "archive"
            if not args.dry_run:
                save_json(mf, meta)
            via_archive += 1
            continue
        dts = [d for d in (date_from_filename(n) for n in doc_names if n.startswith(mid + "-")) if d]
        if dts:
            meta["date"] = min(dts).strftime("%m/%d/%Y")
            meta["date_source"] = "filename"
            if not args.dry_run:
                save_json(mf, meta)
            via_filename += 1
            continue
        left += 1
    tag = " (dry run, nothing written)" if args.dry_run else ""
    print(f"Backfilled dates{tag}: {via_archive} from archive, {via_filename} from filenames, {left} left undated")


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
        if info.get("permanent"):
            continue
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
            if _heal_row(meta, item):
                save_json(meeting_file, meta)

        if mid in state["meetings"] and not args.deep and not item.get("forced"):
            meeting_date = state["meetings"][mid].get("date", "") or (item["date"].strftime("%m/%d/%Y") if item["date"] else "")
            if not meeting_needs_recheck(mid, docs_dir, meeting_date, max_age_days=RECHECK_DAYS):
                continue
            print(f"  RECHECK: {item['title']} (no documents yet)")

        if failed.get(mid, {}).get("permanent") and not item.get("forced"):
            continue


        # Fetch agenda PDFs
        try:
            pdfs = extract_pdf_links_from_agenda(item["link"], base_url)
        except AgendaNotPublished:
            # Video-only clip: nothing to fetch. Flag so the doctor skips it;
            # the flag is cleared below if an agenda is ever published.
            meta = load_json(meeting_file) or {}
            has_docs = any(f.name.startswith(mid + "-") and f.stat().st_size > 0
                           for f in docs_dir.iterdir())
            if not has_docs and not meta.get("no_assets"):
                meta["no_assets"] = True
                meta["no_assets_reason"] = "granicus: agenda not published (video-only clip)"
                save_json(meeting_file, meta)
                print(f"  NO ASSETS: {mid} agenda not published (video-only)")
            failed.pop(mid, None)
            state["meetings"][mid] = {
                "fetched": datetime.now().isoformat(), "title": item["title"],
                "date": item["date"].strftime("%m/%d/%Y") if item["date"] else "",
            }
            continue
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                # Granicus redirects a clip with no agenda to its NotFound
                # page, so a 404 here is a permanent answer, not a flaky
                # fetch. Retrying it for RECHECK_DAYS cannot help and leaves
                # the row reading as a coverage gap the whole time.
                cur = load_json(meeting_file) or {}
                has_docs = any(f.name.startswith(mid + "-") and f.stat().st_size > 0
                               for f in docs_dir.iterdir())
                if not has_docs and not cur.get("no_assets"):
                    cur["no_assets"] = True
                    cur["no_assets_reason"] = f"granicus: agenda page returns {status}"
                    save_json(meeting_file, cur)
                    print(f"  NO ASSETS: {mid} agenda page returns {status}")
                failed.pop(mid, None)
                continue
            print(f"  Failed to fetch agenda page for {mid}: {e}")
            rec = failed.setdefault(mid, {"url": item["link"], "attempts": 0,
                                          "first_failure": now.isoformat()})
            rec["attempts"] += 1
            rec["last_error"] = str(e)[:200]
            rec["last_attempt"] = now.isoformat()
            continue
        agenda_failed = False
        if pdfs:
            cur = load_json(meeting_file) or {}
            if cur.get("no_assets") and str(cur.get("no_assets_reason", "")).startswith("granicus:"):
                cur.pop("no_assets", None)
                cur.pop("no_assets_reason", None)
                save_json(meeting_file, cur)
        else:
            # An agenda page that lists no PDFs is as empty as one that was
            # never published; AgendaNotPublished only covers the latter. Left
            # unflagged, the row reads as a coverage gap on every run and
            # nothing ever resolves it. The flag clears above if PDFs appear.
            cur = load_json(meeting_file) or {}
            has_docs = any(f.name.startswith(mid + "-") and f.stat().st_size > 0
                           for f in docs_dir.iterdir())
            if not has_docs and not cur.get("no_assets"):
                cur["no_assets"] = True
                cur["no_assets_reason"] = "granicus: agenda page listed no documents"
                save_json(meeting_file, cur)
                print(f"  NO ASSETS: {mid} agenda page listed no documents")

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

            if pdf_info.get("laserfiche"):
                try:
                    ok = fetch_laserfiche_pdf(pdf_info["url"], dest)
                except Exception as e:
                    permanent = isinstance(e, PermanentFetchError)
                    print(f"  Failed to fetch Laserfiche doc for {mid}: {e}")
                    agenda_failed = True
                    rec = failed.setdefault(mid, {"url": item["link"], "attempts": 0,
                                                  "first_failure": now.isoformat()})
                    rec["attempts"] += 1
                    rec["last_error"] = str(e)[:200]
                    rec["last_attempt"] = now.isoformat()
                    if permanent:
                        rec["permanent"] = True
                        meta = load_json(meeting_file) or {}
                        meta["no_assets"] = True
                        meta["no_assets_reason"] = f"laserfiche: {e}"[:200]
                        save_json(meeting_file, meta)
                    continue
                if ok and extract_text(dest):
                    doc_count += 1
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
            if failed.get(mid, {}).get("permanent"):
                state["meetings"][mid] = {
                    "fetched": datetime.now().isoformat(), "title": item["title"],
                    "date": item["date"].strftime("%m/%d/%Y") if item["date"] else "",
                }
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

    p_bf = sub.add_parser("backfill-dates", aliases=["--backfill-dates"],
                          help="date undated meeting rows from the live archive, then doc filenames")
    p_bf.add_argument("--agency", required=True)
    p_bf.add_argument("--dry-run", action="store_true")

    p_list = sub.add_parser("list")
    p_list.add_argument("--agency", required=True)

    if "--backfill-dates" in sys.argv[1:]:
        sys.argv = [a for a in sys.argv if a != "--backfill-dates"]
        sys.argv.insert(1, "backfill-dates")
    args = parser.parse_args()
    if args.command == "backfill-dates" or args.command == "--backfill-dates":
        cmd_backfill_dates(args)
    elif args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
