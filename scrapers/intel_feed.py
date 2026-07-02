#!/usr/bin/env python3
"""
Intel feed monitor — checks RSS feeds and web pages for items relevant
to local housing advocacy. Two-tier detection:

  Tier 1 (direct): Mentions primary city, transit agencies, council members, or local projects
  Tier 2 (pattern): Legal theories, enforcement actions, or policy developments
         that affect the jurisdiction even if another city is named

Stores new items in data/intel/, summarizes hits via claude -p.
Feed sources, keywords, and relevance context are configurable via config.local.yaml / SSM.

Usage:
    python intel_feed.py                # check all feeds, flag relevant items
    python intel_feed.py --dry-run      # show what would be checked
    python intel_feed.py --stats        # show intel feed stats
    python intel_feed.py --force        # re-check all items (ignore seen cache)
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import feedparser
import requests
from civic_utils import DATA_DIR
import config

INTEL_DIR = DATA_DIR / "intel"
STATE_FILE = INTEL_DIR / "feed-state.json"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"

# ═══════════════════════════════════════════
# All feed sources, keywords, and relevance context
# are loaded from config (SSM or config.local.yaml).
# Defaults below are used if config is missing.
# ═══════════════════════════════════════════

_DEFAULT_RSS_FEEDS = [
    {"name": "CalMatters — Housing", "url": "https://calmatters.org/category/housing/feed/", "tier": "journalism"},
    {"name": "LAist", "url": "https://laist.com/rss-feed", "tier": "journalism"},
    {"name": "CalHDF News", "url": "https://calhdf.org/category/news/feed/", "tier": "enforcement"},
    {"name": "California YIMBY Blog", "url": "https://cayimby.org/blog/feed/", "tier": "enforcement"},
]

_DEFAULT_WEB_PAGES = [
    {"name": "AG Housing Enforcement", "url": "https://oag.ca.gov/housing", "tier": "state"},
    {"name": "HCD Enforcement", "url": "https://www.hcd.ca.gov/planning-and-community-development/housing-open-data-tools/housing-element-implementation-and-apr", "tier": "state"},
    {"name": "YIMBY Law Press", "url": "https://www.yimbylaw.org/press", "tier": "enforcement"},
]

RSS_FEEDS = config.get("feeds/rss_feeds", _DEFAULT_RSS_FEEDS)
WEB_PAGES = config.get("feeds/web_pages", _DEFAULT_WEB_PAGES)
DIRECT_KEYWORDS = config.get("feeds/direct_keywords", [])
PATTERN_KEYWORDS = config.get("feeds/pattern_keywords", [])

_primary_city = config.get("identity/primary_city", "the jurisdiction")
_state = config.get("identity/state", "")
_relevance_context = config.get("feeds/relevance_context", "")

RELEVANCE_PROMPT = f"""You are a housing policy analyst monitoring news and enforcement actions for an advocate in {_primary_city}, {_state}.

{_relevance_context}

Does this item affect {_primary_city}'s legal exposure, advocacy strategy, or political landscape? Consider:
- Does it establish legal precedent that applies to {_primary_city}'s situation?
- Does it signal enforcement action that could extend to {_primary_city}?
- Does it involve a city using the same obstruction tactics {_primary_city} uses?
- Does it change the political calculus for housing law compliance in the region?

Respond with ONLY a JSON object:
{{
  "relevant": true/false,
  "relevance_score": 1-10,
  "reason": "one sentence explaining why this matters or doesn't",
  "action_items": ["what the advocate should do in response"] or []
}}

Item to evaluate:
"""


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}, "last_check": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def item_hash(title, url=""):
    return hashlib.md5(f"{title}|{url}".encode()).hexdigest()[:12]


def matches_keywords(text, keywords):
    """Check if text matches any keywords. Returns list of matched keywords."""
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


def check_relevance_claude(title, summary, source):
    """Ask claude -p whether a pattern-matched item is relevant."""
    text = f"Source: {source}\nTitle: {title}\nContent: {summary[:3000]}"
    prompt = RELEVANCE_PROMPT + text

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True, text=True, timeout=120,
            env=env,
        )
        if result.returncode != 0:
            return None

        output = result.stdout.strip()
        if output.startswith("```"):
            output = output.split("\n", 1)[1] if "\n" in output else output
            if output.endswith("```"):
                output = output[:-3].strip()

        return json.loads(output)
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def fetch_rss_feeds():
    """Fetch all RSS feeds and return new items."""
    items = []
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"], agent=USER_AGENT)
            for entry in feed.entries[:20]:
                title = entry.get("title", "")
                link = entry.get("link", "")
                summary = entry.get("summary", entry.get("description", ""))
                published = entry.get("published", entry.get("updated", ""))

                items.append({
                    "source": feed_info["name"],
                    "tier": feed_info["tier"],
                    "title": title,
                    "url": link,
                    "summary": summary,
                    "published": published,
                    "hash": item_hash(title, link),
                })
        except Exception as e:
            print(f"  Error fetching {feed_info['name']}: {e}")

    return items


def fetch_web_pages(state):
    """Fetch web pages and detect new content via hash comparison."""
    items = []
    for page in WEB_PAGES:
        try:
            resp = requests.get(page["url"], timeout=30, headers={"User-Agent": USER_AGENT})
            if resp.status_code != 200:
                continue

            content = resp.text
            page_hash = hashlib.md5(content.encode()).hexdigest()[:16]
            prev_hash = state.get("page_hashes", {}).get(page["url"])

            if prev_hash and prev_hash == page_hash:
                continue

            state.setdefault("page_hashes", {})[page["url"]] = page_hash

            items.append({
                "source": page["name"],
                "tier": page["tier"],
                "title": f"[Page updated] {page['name']}",
                "url": page["url"],
                "summary": content[:5000],
                "published": datetime.now().isoformat(),
                "hash": item_hash(page["name"], page_hash),
                "is_page_update": True,
            })
        except Exception as e:
            print(f"  Error checking {page['name']}: {e}")

    return items


def process_items(items, state, args):
    """Filter items through two-tier detection."""
    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    new_items = []
    direct_hits = []
    pattern_hits = []
    irrelevant = 0

    for item in items:
        h = item["hash"]
        if h in state["seen"] and not args.force:
            continue
        new_items.append(item)

    if not new_items:
        print("No new items to process.")
        return

    print(f"Processing {len(new_items)} new items...")

    for item in new_items:
        text = f"{item['title']} {item['summary']}"

        # Tier 1: Direct mention
        direct = matches_keywords(text, DIRECT_KEYWORDS)
        if direct:
            item["detection"] = "direct"
            item["matched_keywords"] = direct
            item["relevance_score"] = 10
            direct_hits.append(item)
            state["seen"][item["hash"]] = {
                "title": item["title"][:100],
                "detection": "direct",
                "date": datetime.now().isoformat(),
            }
            continue

        # Tier 2: Pattern match → claude -p relevance check
        patterns = matches_keywords(text, PATTERN_KEYWORDS)
        if patterns:
            if args.dry_run:
                item["detection"] = "pattern_candidate"
                item["matched_keywords"] = patterns
                pattern_hits.append(item)
                continue

            print(f"  Checking relevance: {item['title'][:80]}...")
            assessment = check_relevance_claude(item["title"], item["summary"], item["source"])

            if assessment and assessment.get("relevant"):
                item["detection"] = "pattern"
                item["matched_keywords"] = patterns
                item["relevance_score"] = assessment.get("relevance_score", 5)
                item["relevance_reason"] = assessment.get("reason", "")
                item["action_items"] = assessment.get("action_items", [])
                pattern_hits.append(item)
                state["seen"][item["hash"]] = {
                    "title": item["title"][:100],
                    "detection": "pattern",
                    "score": item["relevance_score"],
                    "date": datetime.now().isoformat(),
                }
            else:
                irrelevant += 1
                state["seen"][item["hash"]] = {
                    "title": item["title"][:100],
                    "detection": "irrelevant",
                    "date": datetime.now().isoformat(),
                }

            time.sleep(1)
        else:
            state["seen"][item["hash"]] = {
                "title": item["title"][:100],
                "detection": "no_match",
                "date": datetime.now().isoformat(),
            }

    # Save hits
    all_hits = direct_hits + pattern_hits
    if all_hits:
        today = datetime.now().strftime("%Y-%m-%d")
        out_path = INTEL_DIR / f"intel-{today}.json"

        existing = []
        if out_path.exists():
            existing = json.loads(out_path.read_text())

        for hit in all_hits:
            hit.pop("summary", None)
            existing.append(hit)

        out_path.write_text(json.dumps(existing, indent=2, default=str))

    # Report
    print(f"\nResults:")
    print(f"  New items checked: {len(new_items)}")
    print(f"  Direct mentions (Tier 1): {len(direct_hits)}")
    print(f"  Pattern relevant (Tier 2): {len(pattern_hits)}")
    print(f"  Irrelevant: {irrelevant}")

    if direct_hits:
        print(f"\n  DIRECT HITS:")
        for hit in direct_hits:
            print(f"    [{hit['source']}] {hit['title'][:80]}")
            print(f"      Keywords: {', '.join(hit['matched_keywords'][:5])}")

    if pattern_hits:
        print(f"\n  PATTERN HITS:")
        for hit in pattern_hits:
            print(f"    [{hit['source']}] {hit['title'][:80]}")
            if hit.get("relevance_reason"):
                print(f"      Why: {hit['relevance_reason']}")
            if hit.get("action_items"):
                for ai in hit["action_items"]:
                    print(f"      → {ai}")


def cmd_stats(args):
    """Show intel feed statistics."""
    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    seen = state.get("seen", {})
    by_detection = {"direct": 0, "pattern": 0, "irrelevant": 0, "no_match": 0}
    for h, info in seen.items():
        d = info.get("detection", "unknown")
        by_detection[d] = by_detection.get(d, 0) + 1

    print(f"Total items seen: {len(seen)}")
    for d, count in sorted(by_detection.items(), key=lambda x: -x[1]):
        print(f"  {d}: {count}")

    intel_files = sorted(INTEL_DIR.glob("intel-*.json"))
    total_hits = 0
    for f in intel_files:
        hits = json.loads(f.read_text())
        total_hits += len(hits)

    print(f"\nIntel files: {len(intel_files)}")
    print(f"Total hits stored: {total_hits}")
    print(f"Last check: {state.get('last_check', 'never')}")

    print(f"\nFeeds configured: {len(RSS_FEEDS)} RSS + {len(WEB_PAGES)} web pages")


def main():
    parser = argparse.ArgumentParser(description="Intel feed monitor for civic housing advocacy")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be checked")
    parser.add_argument("--force", action="store_true", help="Re-check all items")
    parser.add_argument("--stats", action="store_true", help="Show intel feed stats")
    parser.add_argument("--stop-at", type=int, metavar="HOUR", help="Stop at this hour")

    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
        return

    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    if args.dry_run:
        print(f"Would check {len(RSS_FEEDS)} RSS feeds and {len(WEB_PAGES)} web pages")
        print(f"\nRSS feeds:")
        for f in RSS_FEEDS:
            print(f"  [{f['tier']}] {f['name']}")
        print(f"\nWeb pages:")
        for p in WEB_PAGES:
            print(f"  [{p['tier']}] {p['name']}")
        print(f"\nDirect keywords: {len(DIRECT_KEYWORDS)}")
        print(f"Pattern keywords: {len(PATTERN_KEYWORDS)}")
        return

    print(f"Fetching RSS feeds ({len(RSS_FEEDS)})...")
    rss_items = fetch_rss_feeds()
    print(f"  Got {len(rss_items)} items from RSS")

    print(f"Checking web pages ({len(WEB_PAGES)})...")
    web_items = fetch_web_pages(state)
    print(f"  Got {len(web_items)} updated pages")

    all_items = rss_items + web_items
    process_items(all_items, state, args)

    state["last_check"] = datetime.now().isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
