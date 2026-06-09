#!/usr/bin/env python3
"""
Auto-discover meeting video URLs from YouTube playlists and NCTD meeting data.

Matches YouTube videos to meeting IDs by date, populates transcribe-batch.json
for the transcription pipeline.

Sources:
  - Oceanside City Council: YouTube playlist PLUunlla2QsxHz0ZCuVvsi-xwpm6tZiGMN
  - Oceanside Planning Commission: YouTube playlist PLUunlla2QsxGU8lQ-GUqresB-pzfcXYhA
  - NCTD Board: video_url field in meeting.json (scraped from gonctd.com)

Usage:
    python discover_videos.py                # discover new videos, update batch file
    python discover_videos.py --dry-run      # show what would be added
    python discover_videos.py --stats        # show video coverage stats
    python discover_videos.py --force        # re-check all meetings, not just untranscribed

Requires: yt-dlp
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
MEETINGS_DIR = DATA_DIR / "meetings"
NCTD_MEETINGS_DIR = DATA_DIR / "nctd" / "meetings"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
BATCH_FILE = DATA_DIR / "transcribe-batch.json"

PLAYLISTS = {
    "City Council": "PLUunlla2QsxHz0ZCuVvsi-xwpm6tZiGMN",
    "Planning Commission": "PLUunlla2QsxGU8lQ-GUqresB-pzfcXYhA",
}

DATE_PATTERNS = [
    r"(\w+ \d{1,2},? \d{4})",          # "May 20, 2026" or "May 20 2026"
    r"(\d{1,2}/\d{1,2}/\d{4})",        # "5/20/2026"
    r"(\d{4}-\d{2}-\d{2})",            # "2026-05-20"
    r"(\d{1,2}\.\d{1,2}\.\d{4})",      # "5.20.2026"
]


def parse_date_from_title(title):
    """Extract a date from a YouTube video title."""
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, title)
        if match:
            datestr = match.group(1)
            for fmt in ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                        "%m/%d/%Y", "%Y-%m-%d", "%m.%d.%Y"]:
                try:
                    return datetime.strptime(datestr, fmt)
                except ValueError:
                    continue
    return None


def fetch_playlist_videos(playlist_id):
    """Fetch video URLs and titles from a YouTube playlist using yt-dlp."""
    try:
        result = subprocess.run(
            [
                "yt-dlp", "--flat-playlist",
                "--print", "%(id)s\t%(title)s\t%(upload_date)s",
                f"https://www.youtube.com/playlist?list={playlist_id}",
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"  yt-dlp error: {result.stderr[:200]}")
            return []

        videos = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                vid_id = parts[0]
                title = parts[1]
                upload_date = parts[2] if len(parts) > 2 else ""
                videos.append({
                    "id": vid_id,
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "title": title,
                    "upload_date": upload_date,
                })
        return videos
    except FileNotFoundError:
        print("  yt-dlp not found. Install: pip install yt-dlp")
        return []
    except subprocess.TimeoutExpired:
        print("  yt-dlp timed out")
        return []


def load_meetings():
    """Load all meeting metadata with dates parsed."""
    meetings = {}

    for meetings_dir, agency in [(MEETINGS_DIR, "oceanside"), (NCTD_MEETINGS_DIR, "nctd")]:
        if not meetings_dir.exists():
            continue
        for mdir in meetings_dir.iterdir():
            mf = mdir / "meeting.json"
            if not mf.exists():
                continue
            m = json.loads(mf.read_text())
            date_str = m.get("date", "")
            dt = None
            for fmt in ["%m/%d/%Y", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(
                        date_str.split("T")[0] if "T" in date_str else date_str, fmt
                    )
                    break
                except ValueError:
                    pass

            meetings[mdir.name] = {
                "id": mdir.name,
                "date": dt,
                "date_str": date_str,
                "body": m.get("body", ""),
                "agency": agency,
                "video_url": m.get("video_url"),
            }

    return meetings


def already_transcribed(meeting_id):
    """Check if a meeting already has a transcript."""
    txt = TRANSCRIPTS_DIR / f"{meeting_id}-transcript.txt"
    return txt.exists() and txt.stat().st_size > 0


def load_existing_batch():
    """Load existing batch file entries."""
    if BATCH_FILE.exists():
        return json.loads(BATCH_FILE.read_text())
    return []


def match_video_to_meeting(video, meetings, body_filter):
    """Match a YouTube video to a meeting by date and body."""
    video_date = parse_date_from_title(video["title"])
    if not video_date:
        if video.get("upload_date") and len(video["upload_date"]) == 8:
            try:
                video_date = datetime.strptime(video["upload_date"], "%Y%m%d")
            except ValueError:
                pass

    if not video_date:
        return None

    best_match = None
    for mid, m in meetings.items():
        if not m["date"]:
            continue
        if body_filter and body_filter.lower() not in m["body"].lower():
            continue
        if m["date"].date() == video_date.date():
            best_match = m
            break
        delta = abs((m["date"] - video_date).days)
        if delta <= 1 and (not best_match or delta < abs((best_match["date"] - video_date).days)):
            best_match = m

    return best_match


def discover_all(args):
    """Discover new video URLs and update the batch file."""
    meetings = load_meetings()
    existing_batch = load_existing_batch()
    existing_ids = {e["meeting_id"] for e in existing_batch}
    new_entries = []

    # YouTube playlists (Council + Planning Commission)
    for body, playlist_id in PLAYLISTS.items():
        print(f"\nFetching {body} playlist...")
        videos = fetch_playlist_videos(playlist_id)
        print(f"  Found {len(videos)} videos")

        matched = 0
        for video in videos:
            match = match_video_to_meeting(video, meetings, body)
            if not match:
                continue

            mid = match["id"]
            if mid in existing_ids:
                continue
            if not args.force and already_transcribed(mid):
                continue

            entry = {
                "meeting_id": mid,
                "date": match["date_str"],
                "body": match["body"],
                "url": video["url"],
                "title": video["title"],
            }
            new_entries.append(entry)
            existing_ids.add(mid)
            matched += 1

        print(f"  Matched {matched} new videos to meetings")

    # NCTD (video URLs already in meeting.json)
    print(f"\nChecking NCTD meeting videos...")
    nctd_new = 0
    for mid, m in meetings.items():
        if m["agency"] != "nctd":
            continue
        if not m.get("video_url"):
            continue
        if mid in existing_ids:
            continue
        if not args.force and already_transcribed(mid):
            continue

        entry = {
            "meeting_id": mid,
            "date": m["date_str"],
            "body": m["body"],
            "url": m["video_url"],
        }
        new_entries.append(entry)
        existing_ids.add(mid)
        nctd_new += 1

    print(f"  Found {nctd_new} new NCTD videos")

    if not new_entries:
        print("\nNo new videos to add.")
        return

    if args.dry_run:
        print(f"\nWould add {len(new_entries)} entries to {BATCH_FILE}:")
        for e in new_entries:
            print(f"  {e['meeting_id']} — {e.get('body', '?')} {e.get('date', '?')}")
            print(f"    {e['url']}")
        return

    # Append to batch file
    all_entries = existing_batch + new_entries
    BATCH_FILE.write_text(json.dumps(all_entries, indent=2))
    print(f"\nAdded {len(new_entries)} entries → {BATCH_FILE} ({len(all_entries)} total)")


def cmd_stats(args):
    """Show video coverage statistics."""
    meetings = load_meetings()

    bodies = {}
    for mid, m in meetings.items():
        body = m["body"] or "Unknown"
        if body not in bodies:
            bodies[body] = {"total": 0, "has_video": 0, "transcribed": 0}
        bodies[body]["total"] += 1
        if m.get("video_url") or already_transcribed(mid):
            bodies[body]["has_video"] += 1
        if already_transcribed(mid):
            bodies[body]["transcribed"] += 1

    existing_batch = load_existing_batch()
    batch_ids = {e["meeting_id"] for e in existing_batch}
    for mid in batch_ids:
        if mid in meetings:
            body = meetings[mid]["body"] or "Unknown"
            if body in bodies:
                bodies[body]["has_video"] = max(bodies[body]["has_video"],
                                                 bodies[body].get("has_video", 0))

    print(f"{'Body':35s} {'Meetings':>9s} {'Videos':>7s} {'Transcribed':>12s}")
    print("-" * 65)
    for body in sorted(bodies.keys()):
        b = bodies[body]
        print(f"{body:35s} {b['total']:9d} {b['has_video']:7d} {b['transcribed']:12d}")

    print(f"\nBatch file: {len(existing_batch)} entries in {BATCH_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Auto-discover meeting video URLs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be added")
    parser.add_argument("--force", action="store_true", help="Include already-transcribed meetings")
    parser.add_argument("--stats", action="store_true", help="Show video coverage stats")

    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
    else:
        discover_all(args)


if __name__ == "__main__":
    main()
