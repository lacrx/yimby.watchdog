#!/usr/bin/env python3
"""
Historical backfill of intel sources back to 2020.

Discovers article URLs via sitemaps (preferred) or HTML pagination,
then runs each through the same two-tier detection pipeline as intel_feed.py.

Runs through the catch-up cron job, processes incrementally with --stop-at.

Usage:
    python intel_backfill.py                    # backfill all sources
    python intel_backfill.py --source "CalHDF"  # backfill one source
    python intel_backfill.py --stats            # show backfill progress
    python intel_backfill.py --stop-at 7        # stop at 7am
    python intel_backfill.py --dry-run          # show what would be crawled
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

DATA_DIR = Path(__file__).parent / "data"
INTEL_DIR = DATA_DIR / "intel"
BACKFILL_DIR = INTEL_DIR / "backfill"
STATE_FILE = BACKFILL_DIR / "backfill-state.json"
FEED_STATE_FILE = INTEL_DIR / "feed-state.json"

UA = "Mozilla/5.0 (X11; Linux x86_64) civics-intel/1.0"
MIN_DATE = "2020-01-01"

sys.path.insert(0, str(Path(__file__).parent))
from intel_feed import (
    DIRECT_KEYWORDS,
    PATTERN_KEYWORDS,
    matches_keywords,
    check_relevance_claude,
    item_hash,
)

# ═══════════════════════════════════════════
# SOURCE DEFINITIONS
# ═══════════════════════════════════════════

ARCHIVE_SOURCES = [
    # ── Sitemap sources ──
    {
        "name": "Voice of San Diego — Housing",
        "tier": "journalism",
        "discovery": "rss_paginated",
        "feed_url": "https://voiceofsandiego.org/category/housing/feed/",
        "max_pages": 13,
    },
    {
        "name": "CalMatters — Housing",
        "tier": "journalism",
        "discovery": "sitemap",
        "sitemap_url": "https://calmatters.org/sitemap_index.xml",
        "filter_path": "/housing/",
    },
    {
        "name": "CalHDF News",
        "tier": "enforcement",
        "discovery": "sitemap",
        "sitemap_url": "https://calhdf.org/wp-sitemap.xml",
    },
    {
        "name": "California YIMBY Blog",
        "tier": "enforcement",
        "discovery": "sitemap",
        "sitemap_url": "https://cayimby.org/wp-sitemap.xml",
    },
    {
        "name": "YIMBY Law Press",
        "tier": "enforcement",
        "discovery": "sitemap",
        "sitemap_url": "https://www.yimbylaw.org/sitemap.xml",
    },
    {
        "name": "Circulate SD Blog",
        "tier": "enforcement",
        "discovery": "sitemap",
        "sitemap_url": "https://www.circulatesd.org/sitemap.xml",
    },
    # ── HTML pagination sources ──
    {
        "name": "AG Press Releases",
        "tier": "state",
        "discovery": "html",
        "base_url": "https://oag.ca.gov",
        "pages": [
            "https://oag.ca.gov/housing",
            "https://oag.ca.gov/media/news",
        ],
        "link_pattern": r'/news/press-releases/[^"\']+',
        "filter_keywords": ["housing", "hcd", "zoning", "density", "builder",
                            "rent", "eviction", "tenant", "landlord", "homelessness"],
    },
    {
        "name": "LAO Publications",
        "tier": "state",
        "discovery": "html_paginated",
        "base_url": "https://lao.ca.gov",
        "page_url_template": "https://lao.ca.gov/Publications?page={page}",
        "max_pages": 330,
        "link_pattern": r'/Publications/Report/\d+',
        "filter_keywords": ["housing", "land use", "zoning", "density",
                            "infrastructure", "transit", "homelessness"],
    },
    # ── Holland & Knight: RSS-only, no sitemap, no archive ──
    # Nightly RSS captures new posts; historical archive is behind a 403 wall.
    # Skipped for backfill — we get ongoing coverage from the daily feed.
    #
    # ── Cox Castle: domain unreachable (landuse.coxcastle.com DNS failure) ──
    # Likely discontinued or migrated. Skipped.
]


# ═══════════════════════════════════════════
# URL DISCOVERY
# ═══════════════════════════════════════════

def discover_sitemap(source):
    """Parse sitemap/sitemap index and return list of (url, date) tuples."""
    urls = _parse_sitemap_recursive(
        source["sitemap_url"],
        filter_path=source.get("filter_path"),
    )
    # URL slug pre-filter (for large sitemaps without path-based categories)
    slug_keywords = source.get("slug_keywords")
    if slug_keywords and urls:
        before = len(urls)
        urls = [(u, d) for u, d in urls if any(kw in u.lower() for kw in slug_keywords)]
        print(f"  Sitemap: {before} total, {len(urls)} after slug filter (post-2020)")
    else:
        print(f"  Sitemap: {len(urls)} URLs found (post-2020)")
    return urls


def _parse_sitemap_recursive(url, filter_path=None):
    """Recursively parse sitemap/index, return [(url, date)]."""
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": UA})
        if resp.status_code != 200:
            print(f"  Sitemap HTTP {resp.status_code}: {url}")
            return []

        root = ET.fromstring(resp.content)
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        # Sitemap index → recurse
        sitemaps = root.findall("s:sitemap", ns)
        if sitemaps:
            all_urls = []
            for sm in sitemaps:
                loc = sm.find("s:loc", ns)
                if loc is None:
                    continue
                sub_url = loc.text
                if filter_path and filter_path not in sub_url and "post" not in sub_url.lower():
                    continue
                all_urls.extend(_parse_sitemap_recursive(sub_url, filter_path))
            return all_urls

        # Regular sitemap
        urls = []
        for url_elem in root.findall("s:url", ns):
            loc = url_elem.find("s:loc", ns)
            lastmod = url_elem.find("s:lastmod", ns)
            if loc is None:
                continue
            u = loc.text
            date = lastmod.text[:10] if lastmod is not None and lastmod.text else ""

            if filter_path and filter_path not in u:
                continue
            if date and date < MIN_DATE:
                continue

            urls.append((u, date))

        return urls

    except Exception as e:
        print(f"  Sitemap parse error: {e}")
        return []


def discover_html(source):
    """Scrape one or more HTML pages for article links."""
    urls = []
    seen = set()
    pattern = source["link_pattern"]
    base = source.get("base_url", "")

    for page_url in source["pages"]:
        try:
            resp = requests.get(page_url, timeout=20, headers={"User-Agent": UA})
            if resp.status_code != 200:
                continue

            matches = re.findall(pattern, resp.text)
            for m in matches:
                full_url = m if m.startswith("http") else base + m
                if full_url not in seen:
                    seen.add(full_url)
                    urls.append((full_url, ""))
        except Exception as e:
            print(f"  HTML scrape error for {page_url}: {e}")

    print(f"  HTML: {len(urls)} URLs found")
    return urls


def discover_html_paginated(source):
    """Scrape paginated HTML archive."""
    urls = []
    seen = set()
    pattern = source["link_pattern"]
    base = source.get("base_url", "")
    max_pages = source.get("max_pages", 100)

    for page_num in range(0, max_pages):
        page_url = source["page_url_template"].format(page=page_num)
        try:
            resp = requests.get(page_url, timeout=20, headers={"User-Agent": UA})
            if resp.status_code != 200:
                break

            matches = re.findall(pattern, resp.text)
            if not matches:
                break

            new_count = 0
            for m in matches:
                full_url = m if m.startswith("http") else base + m
                if full_url not in seen:
                    seen.add(full_url)
                    urls.append((full_url, ""))
                    new_count += 1

            if new_count == 0:
                break

            if (page_num + 1) % 50 == 0:
                print(f"  Page {page_num + 1}: {len(urls)} URLs so far...")

        except Exception as e:
            print(f"  HTML page {page_num} error: {e}")
            break

    print(f"  HTML paginated: {len(urls)} URLs across {page_num + 1} pages")
    return urls


def discover_rss_paginated(source):
    """Paginate through RSS feed, return articles with content already included."""
    import feedparser

    feed_url = source["feed_url"]
    max_pages = source.get("max_pages", 20)
    articles = []
    seen = set()

    for page in range(1, max_pages + 1):
        url = f"{feed_url}?paged={page}" if page > 1 else feed_url
        feed = feedparser.parse(url)

        if not feed.entries:
            break

        for entry in feed.entries:
            link = entry.get("link", "")
            if link in seen:
                continue
            seen.add(link)

            published = entry.get("published", "")
            if "2019" in published or "2018" in published or "2017" in published:
                continue

            title = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))
            # Strip HTML from summary
            content = re.sub(r'<[^>]+>', ' ', summary)
            content = re.sub(r'\s+', ' ', content).strip()

            articles.append((link, published[:16], title, content))

    print(f"  RSS paginated: {len(articles)} articles across {page} pages")
    return articles


def discover_urls(source):
    """Route to the right discovery method."""
    method = source["discovery"]
    if method == "sitemap":
        return discover_sitemap(source)
    elif method == "html":
        return discover_html(source)
    elif method == "html_paginated":
        return discover_html_paginated(source)
    elif method == "rss_paginated":
        return discover_rss_paginated(source)
    else:
        print(f"  Unknown discovery method: {method}")
        return []


# ═══════════════════════════════════════════
# CONTENT FETCHING
# ═══════════════════════════════════════════

def fetch_page_text(url):
    """Fetch page and extract title + text content."""
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": UA})
        if resp.status_code != 200:
            return None

        html = resp.text

        # Extract title
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else ""

        # Extract date from meta tags or URL
        date = ""
        date_match = re.search(
            r'(?:datePublished|article:published_time|date)["\s:]+["\s]*(\d{4}-\d{2}-\d{2})',
            html
        )
        if date_match:
            date = date_match.group(1)
        else:
            url_date = re.search(r'/(\d{4})/(\d{2})/', url)
            if url_date:
                date = f"{url_date.group(1)}-{url_date.group(2)}-01"

        # Strip HTML tags for text content
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if len(text) < 100:
            return None

        return {
            "title": title,
            "date": date,
            "content": text[:5000],
            "url": url,
        }

    except Exception:
        return None


# ═══════════════════════════════════════════
# DETECTION (same two-tier as intel_feed.py)
# ═══════════════════════════════════════════

def process_article(article, source, feed_state):
    """Run article through two-tier detection."""
    url = article.get("url", "")
    title = article.get("title", "")
    content = article.get("content", "")
    date = article.get("date", "")

    h = item_hash(title, url)

    if h in feed_state.get("seen", {}):
        return None

    if date and date < MIN_DATE:
        feed_state.setdefault("seen", {})[h] = True
        return None

    text = f"{title} {content}"

    # Source-level keyword pre-filter (AG, LAO)
    if source.get("filter_keywords"):
        text_lower = text.lower()
        if not any(kw in text_lower for kw in source["filter_keywords"]):
            feed_state.setdefault("seen", {})[h] = True
            return None

    # Tier 1: direct mention
    direct = matches_keywords(text, DIRECT_KEYWORDS)
    if direct:
        hit = {
            "source": source["name"],
            "tier": source["tier"],
            "title": title,
            "url": url,
            "summary": content[:2000],
            "published": date,
            "hash": h,
            "detection": "direct",
            "matched_keywords": direct,
            "relevance_score": 10,
            "relevance_reason": f"Direct mention: {', '.join(direct[:3])}",
            "action_items": [],
            "backfilled": True,
        }
        feed_state.setdefault("seen", {})[h] = True
        return hit

    # Tier 2: pattern match + claude relevance check
    pattern = matches_keywords(text, PATTERN_KEYWORDS)
    if pattern:
        relevance = check_relevance_claude(title, content[:3000], source["name"])
        if relevance and relevance.get("relevant"):
            hit = {
                "source": source["name"],
                "tier": source["tier"],
                "title": title,
                "url": url,
                "summary": content[:2000],
                "published": date,
                "hash": h,
                "detection": "pattern",
                "matched_keywords": pattern,
                "relevance_score": relevance.get("relevance_score", 5),
                "relevance_reason": relevance.get("reason", ""),
                "action_items": relevance.get("action_items", []),
                "backfilled": True,
            }
            feed_state.setdefault("seen", {})[h] = True
            return hit

    feed_state.setdefault("seen", {})[h] = True
    return None


# ═══════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════

def load_state():
    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"sources": {}, "processed_urls": {}, "started": datetime.now().isoformat()}


def save_state(state):
    state["last_updated"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def load_feed_state():
    if FEED_STATE_FILE.exists():
        return json.loads(FEED_STATE_FILE.read_text())
    return {"seen": {}}


def save_feed_state(feed_state):
    FEED_STATE_FILE.write_text(json.dumps(feed_state, indent=2, default=str))


def save_hit(hit):
    """Append hit to daily intel file."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    intel_file = INTEL_DIR / f"intel-{date_str}.json"
    existing = []
    if intel_file.exists():
        try:
            existing = json.loads(intel_file.read_text())
        except json.JSONDecodeError:
            pass
    existing.append(hit)
    intel_file.write_text(json.dumps(existing, indent=2, default=str))


# ═══════════════════════════════════════════
# BACKFILL LOGIC
# ═══════════════════════════════════════════

def backfill_source(source, state, feed_state, stop_hour=None):
    """Backfill a single source."""
    name = source["name"]
    source_state = state["sources"].setdefault(name, {
        "status": "pending",
        "urls_discovered": 0,
        "urls_processed": 0,
        "hits": 0,
    })

    if source_state["status"] == "complete":
        print(f"  {name}: already complete, skipping.")
        return True

    # Phase 1: Discover URLs/articles (if not already done)
    urls_key = f"urls_{name}"
    articles_key = f"articles_{name}"
    is_rss = source.get("discovery") == "rss_paginated"

    if urls_key not in state:
        print(f"  Discovering URLs...")
        results = discover_urls(source)
        if is_rss:
            # RSS returns (url, date, title, content) tuples — cache articles
            state[urls_key] = [r[0] for r in results]
            state[articles_key] = {r[0]: {"url": r[0], "date": r[1], "title": r[2], "content": r[3]} for r in results}
        else:
            state[urls_key] = [u for u, d in results]
        source_state["urls_discovered"] = len(results)
        source_state["status"] = "discovered"
        save_state(state)
    else:
        print(f"  Using {len(state[urls_key])} cached URLs")

    urls = state[urls_key]
    rss_articles = state.get(articles_key, {})

    # Phase 2: Process each URL
    processed = state.get("processed_urls", {})
    remaining = [u for u in urls if u not in processed]

    print(f"  {len(remaining)} remaining of {len(urls)}")

    for i, url in enumerate(remaining):
        if stop_hour and datetime.now().hour >= stop_hour:
            print(f"  Stopping at {datetime.now().strftime('%H:%M')} ({len(remaining) - i} remaining)")
            save_state(state)
            save_feed_state(feed_state)
            return False

        if url in rss_articles:
            article = rss_articles[url]
        else:
            article = fetch_page_text(url)

        if not article:
            processed[url] = "failed"
            source_state["urls_processed"] += 1
            continue

        hit = process_article(article, source, feed_state)

        if hit:
            save_hit(hit)
            source_state["hits"] += 1
            print(f"  [{i+1}/{len(remaining)}] HIT: {hit['title'][:60]} (score: {hit['relevance_score']})")
        else:
            if (i + 1) % 50 == 0:
                print(f"  [{i+1}/{len(remaining)}] processing... ({source_state['hits']} hits so far)")

        processed[url] = "done"
        source_state["urls_processed"] += 1

        # Save periodically
        if (i + 1) % 25 == 0:
            save_state(state)
            save_feed_state(feed_state)

    source_state["status"] = "complete"
    save_state(state)
    save_feed_state(feed_state)
    print(f"  {name}: complete. {source_state['hits']} hits found.")
    return True


def cmd_stats(state):
    """Show backfill progress."""
    print("Intel Backfill Progress")
    print("=" * 70)
    total_discovered = 0
    total_processed = 0
    total_hits = 0

    for source in ARCHIVE_SOURCES:
        name = source["name"]
        ss = state["sources"].get(name, {})
        status = ss.get("status", "pending")
        discovered = ss.get("urls_discovered", 0)
        processed = ss.get("urls_processed", 0)
        hits = ss.get("hits", 0)

        total_discovered += discovered
        total_processed += processed
        total_hits += hits

        icon = "done" if status == "complete" else "..." if status in ("discovered", "crawled") else "   "
        pct = f"{processed}/{discovered}" if discovered else "—"
        print(f"  [{icon:4s}] {name:40s} {pct:>12s}  {hits:3d} hits")

    print(f"\nTotal: {total_processed}/{total_discovered} processed, {total_hits} hits")
    remaining = total_discovered - total_processed
    if remaining > 0:
        print(f"Remaining: {remaining} URLs")


def main():
    parser = argparse.ArgumentParser(description="Backfill intel sources back to 2020")
    parser.add_argument("--source", help="Backfill only this source (by name)")
    parser.add_argument("--stats", action="store_true", help="Show backfill progress")
    parser.add_argument("--stop-at", type=int, metavar="HOUR", help="Stop at this hour (0-23)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be crawled")
    args = parser.parse_args()

    state = load_state()

    if args.stats:
        cmd_stats(state)
        return

    if args.dry_run:
        for source in ARCHIVE_SOURCES:
            ss = state["sources"].get(source["name"], {})
            status = ss.get("status", "pending")
            method = source["discovery"]
            url = source.get("sitemap_url", source.get("base_url", source.get("page_url_template", "?")))
            print(f"  [{method:15s}] {source['name']:40s} [{status}]")
            print(f"                   {url}")
        return

    feed_state = load_feed_state()

    sources = ARCHIVE_SOURCES
    if args.source:
        sources = [s for s in sources if args.source.lower() in s["name"].lower()]
        if not sources:
            print(f"No source matching '{args.source}'")
            return

    print(f"Backfilling {len(sources)} sources...")
    all_complete = True

    for source in sources:
        print(f"\n{'='*60}")
        print(f"Source: {source['name']} ({source['discovery']})")
        print(f"{'='*60}")
        completed = backfill_source(source, state, feed_state, stop_hour=args.stop_at)
        if not completed:
            all_complete = False

        if args.stop_at and datetime.now().hour >= args.stop_at:
            print(f"\nStopping at {datetime.now().strftime('%H:%M')}")
            all_complete = False
            break

    if all_complete:
        print("\n" + "=" * 60)
        print("ALL SOURCES BACKFILLED.")
        print("=" * 60)

    save_state(state)
    save_feed_state(feed_state)


if __name__ == "__main__":
    main()
