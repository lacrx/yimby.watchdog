#!/usr/bin/env python3
"""
Video transcription pipeline for civic meetings.

Downloads audio from YouTube/KOCT videos, transcribes with OpenAI Whisper API,
and stores transcripts alongside meeting data.

Usage:
    python transcribe.py <meeting_id> <youtube_url>   # single meeting
    python transcribe.py --batch batch.json            # batch from JSON file
    python transcribe.py --estimate <youtube_url>      # estimate cost without downloading
    python transcribe.py --list                        # list meetings with transcripts

batch.json format: [{"meeting_id": "1234", "url": "https://youtube.com/..."}, ...]

Requires: yt-dlp, ffmpeg, openai (pip install openai yt-dlp)
Env: OPENAI_API_KEY in .env or environment
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

DATA_DIR = Path(__file__).parent / "data"
AUDIO_DIR = DATA_DIR / "audio"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"

WHISPER_COST_PER_MINUTE = 0.006
WHISPER_MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB


def ensure_dirs():
    for d in [AUDIO_DIR, TRANSCRIPTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def check_dependencies():
    """Check that yt-dlp and ffmpeg are available."""
    missing = []
    for cmd in ["yt-dlp", "ffmpeg"]:
        try:
            subprocess.run([cmd, "--version"], capture_output=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            missing.append(cmd)
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print("Install with: pip install yt-dlp  (ffmpeg via system package manager)")
        sys.exit(1)


def get_video_duration(url):
    """Get video duration in seconds without downloading."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--print", "duration", "--no-download", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def download_audio(url, output_path):
    """Download audio from YouTube video as MP3."""
    if output_path.exists() and output_path.stat().st_size > 0:
        print(f"  Audio already downloaded: {output_path.name}")
        return True

    print(f"  Downloading audio from {url}...")
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-x", "--audio-format", "mp3",
                "--audio-quality", "5",  # lower quality = smaller file
                "-o", str(output_path.with_suffix(".%(ext)s")),
                url,
            ],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            print(f"  yt-dlp error: {result.stderr[:200]}")
            return False

        mp3_path = output_path.with_suffix(".mp3")
        if mp3_path.exists():
            if mp3_path != output_path:
                mp3_path.rename(output_path)
            for ext in [".webm", ".m4a", ".opus", ".wav"]:
                leftover = output_path.with_suffix(ext)
                if leftover.exists():
                    leftover.unlink()
            return True

        for ext in [".m4a", ".opus", ".webm", ".wav"]:
            alt = output_path.with_suffix(ext)
            if alt.exists():
                alt.rename(output_path)
                return True

        print(f"  No audio file produced")
        return False
    except subprocess.TimeoutExpired:
        print(f"  Download timed out")
        return False


def get_audio_duration_seconds(audio_path):
    """Get audio file duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(audio_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def split_audio(audio_path, max_size=WHISPER_MAX_FILE_SIZE):
    """Split audio into chunks if over Whisper's 25MB limit."""
    file_size = audio_path.stat().st_size
    if file_size <= max_size:
        return [audio_path]

    duration = get_audio_duration_seconds(audio_path)
    if not duration:
        print(f"  Cannot determine duration for splitting")
        return [audio_path]

    chunk_duration = int(duration * (max_size / file_size) * 0.9)  # 10% safety margin
    chunks = []
    offset = 0
    idx = 0

    while offset < duration:
        chunk_path = audio_path.parent / f"{audio_path.stem}_chunk{idx:03d}.mp3"
        if not chunk_path.exists():
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(audio_path),
                    "-ss", str(offset),
                    "-t", str(chunk_duration),
                    "-c:a", "libmp3lame", "-q:a", "5",
                    str(chunk_path),
                ],
                capture_output=True, timeout=120,
            )
        if chunk_path.exists() and chunk_path.stat().st_size > 0:
            chunks.append(chunk_path)
        offset += chunk_duration
        idx += 1

    return chunks if chunks else [audio_path]


def transcribe_audio(audio_path):
    """Transcribe audio file using OpenAI Whisper API."""
    try:
        from openai import OpenAI
    except ImportError:
        print("openai package not installed. Run: pip install openai")
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set. Add to .env or environment.")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    chunks = split_audio(audio_path)
    full_transcript = []

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  Transcribing chunk {i+1}/{len(chunks)}...")
        else:
            print(f"  Transcribing...")

        response = None
        for attempt in range(20):
            try:
                with open(chunk, "rb") as f:
                    response = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=f,
                        response_format="verbose_json",
                        timestamp_granularities=["segment"],
                    )
                break
            except Exception as e:
                err = str(e)
                if "429" in err or "rate" in err.lower() or "quota" in err.lower():
                    wait = min(60 * (2 ** attempt), 600)
                    print(f"  Rate limited/quota (attempt {attempt+1}/20). Waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        if response is None:
            print(f"  Failed after 20 retries on chunk {i}")
            return []

        if hasattr(response, "segments"):
            for seg in response.segments:
                full_transcript.append({
                    "start": seg.start + (i * len(chunks)),
                    "end": seg.end + (i * len(chunks)),
                    "text": seg.text,
                })
        elif hasattr(response, "text"):
            full_transcript.append({
                "start": 0,
                "end": 0,
                "text": response.text,
            })

    return full_transcript


def transcribe_meeting(meeting_id, url):
    """Full pipeline: download audio, transcribe, save transcript."""
    ensure_dirs()

    transcript_path = TRANSCRIPTS_DIR / f"{meeting_id}-transcript.json"
    transcript_text_path = TRANSCRIPTS_DIR / f"{meeting_id}-transcript.txt"

    if transcript_text_path.exists() and transcript_text_path.stat().st_size > 0:
        print(f"  Transcript already exists: {transcript_text_path.name}")
        return transcript_text_path.read_text()

    audio_path = AUDIO_DIR / f"{meeting_id}.mp3"
    if not download_audio(url, audio_path):
        return None

    duration = get_audio_duration_seconds(audio_path)
    if duration:
        cost = (duration / 60) * WHISPER_COST_PER_MINUTE
        print(f"  Audio: {duration/60:.1f} min, estimated Whisper cost: ${cost:.2f}")

    segments = transcribe_audio(audio_path)
    if not segments:
        print(f"  Transcription failed")
        return None

    transcript_path.write_text(json.dumps(segments, indent=2))

    full_text = "\n".join(seg["text"] for seg in segments)
    transcript_text_path.write_text(full_text)
    print(f"  Saved transcript: {len(full_text)} chars")

    return full_text


def cmd_transcribe(args):
    """Transcribe a single meeting."""
    check_dependencies()
    result = transcribe_meeting(args.meeting_id, args.url)
    if result:
        print(f"\nTranscription complete. {len(result)} chars.")
    else:
        print("\nTranscription failed.")


def cmd_batch(args):
    """Batch transcribe from JSON file."""
    check_dependencies()
    ensure_dirs()

    batch = json.loads(Path(args.batch).read_text())
    print(f"Batch: {len(batch)} meetings to transcribe")

    total_cost = 0
    for i, item in enumerate(batch):
        mid = item["meeting_id"]
        url = item["url"]
        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(batch)}] Meeting {mid}")
        print(f"{'='*60}")

        result = transcribe_meeting(mid, url)
        if result:
            audio_path = AUDIO_DIR / f"{mid}.mp3"
            duration = get_audio_duration_seconds(audio_path)
            if duration:
                total_cost += (duration / 60) * WHISPER_COST_PER_MINUTE

        time.sleep(1)

    print(f"\nBatch complete. Estimated total Whisper cost: ${total_cost:.2f}")


def cmd_estimate(args):
    """Estimate transcription cost without downloading."""
    duration = get_video_duration(args.url)
    if duration:
        cost = (duration / 60) * WHISPER_COST_PER_MINUTE
        print(f"Duration: {duration/60:.1f} minutes")
        print(f"Estimated Whisper API cost: ${cost:.2f}")
    else:
        print("Could not determine video duration.")


def cmd_list(args):
    """List meetings with transcripts."""
    ensure_dirs()
    transcripts = sorted(TRANSCRIPTS_DIR.glob("*-transcript.txt"))
    if not transcripts:
        print("No transcripts found.")
        return

    for t in transcripts:
        mid = t.stem.replace("-transcript", "")
        size = len(t.read_text())
        audio = AUDIO_DIR / f"{mid}.mp3"
        duration = get_audio_duration_seconds(audio) if audio.exists() else None
        dur_str = f"{duration/60:.1f} min" if duration else "?"
        print(f"  {mid}  {dur_str}  {size:,} chars")


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe civic meeting videos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p_one = sub.add_parser("one", help="Transcribe single meeting")
    p_one.add_argument("meeting_id", help="Meeting ID")
    p_one.add_argument("url", help="YouTube URL")

    p_batch = sub.add_parser("batch", help="Batch transcribe from JSON")
    p_batch.add_argument("batch", help="Path to batch JSON file")

    p_est = sub.add_parser("estimate", help="Estimate cost for a video")
    p_est.add_argument("url", help="YouTube URL")

    sub.add_parser("list", help="List meetings with transcripts")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "one": cmd_transcribe,
        "batch": cmd_batch,
        "estimate": cmd_estimate,
        "list": cmd_list,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
