#!/usr/bin/env python3
"""
Local transcription using faster-whisper (no API cost).

Drop-in replacement for transcribe.py's Whisper API calls.
Uses CTranslate2-optimized Whisper models locally.

Usage:
    python transcribe_local.py <meeting_id> <youtube_url>
    python transcribe_local.py --batch batch.json
    python transcribe_local.py --list

Requires: faster-whisper, yt-dlp, ffmpeg
Install:  pip install faster-whisper yt-dlp
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
AUDIO_DIR = DATA_DIR / "audio"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"

MODEL_SIZE = "large-v3"
DEVICE = "auto"  # auto-detects GPU; falls back to CPU
COMPUTE_TYPE = "int8"  # good balance of speed/quality on CPU; use float16 on GPU


def ensure_dirs():
    for d in [AUDIO_DIR, TRANSCRIPTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def download_audio(url, output_path):
    """Download audio from YouTube/KOCT video as MP3."""
    if output_path.exists() and output_path.stat().st_size > 0:
        print(f"  Audio already downloaded: {output_path.name}")
        return True

    print(f"  Downloading audio from {url}...")
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-x", "--audio-format", "mp3",
                "--audio-quality", "5",
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


def transcribe_audio_local(audio_path):
    """Transcribe audio using faster-whisper (local, no API cost)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper not installed. Run: pip install faster-whisper")
        sys.exit(1)

    print(f"  Loading {MODEL_SIZE} model ({DEVICE}/{COMPUTE_TYPE})...")
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)

    print(f"  Transcribing {audio_path.name}...")
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        language="en",
        vad_filter=True,
    )

    print(f"  Detected language: {info.language} (probability {info.language_probability:.2f})")
    print(f"  Duration: {info.duration:.0f}s")

    transcript = []
    for seg in segments:
        transcript.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        })

    return transcript


def transcribe_meeting(meeting_id, url):
    """Full pipeline: download audio, transcribe locally, save transcript."""
    ensure_dirs()

    transcript_path = TRANSCRIPTS_DIR / f"{meeting_id}-transcript.json"
    transcript_text_path = TRANSCRIPTS_DIR / f"{meeting_id}-transcript.txt"

    if transcript_text_path.exists() and transcript_text_path.stat().st_size > 0:
        print(f"  Transcript already exists: {transcript_text_path.name}")
        return transcript_text_path

    audio_path = AUDIO_DIR / f"{meeting_id}.mp3"
    if not download_audio(url, audio_path):
        return None

    transcript = transcribe_audio_local(audio_path)
    if not transcript:
        print(f"  Transcription failed")
        return None

    transcript_path.write_text(json.dumps(transcript, indent=2))

    full_text = "\n".join(seg["text"] for seg in transcript)
    transcript_text_path.write_text(full_text)

    print(f"  Saved transcript: {len(transcript)} segments, {len(full_text)} chars")
    return transcript_text_path


def cmd_transcribe(args):
    """Transcribe a single meeting."""
    result = transcribe_meeting(args.meeting_id, args.url)
    if result:
        print(f"\nTranscript saved: {result}")
    else:
        print("\nTranscription failed.")
        sys.exit(1)


def cmd_batch(args):
    """Batch transcribe from JSON file."""
    batch = json.loads(Path(args.batch_file).read_text())
    results = {"success": [], "failed": []}

    for item in batch:
        mid = item["meeting_id"]
        url = item["url"]
        print(f"\n{'='*60}")
        print(f"Meeting {mid}: {url}")
        result = transcribe_meeting(mid, url)
        if result:
            results["success"].append(mid)
        else:
            results["failed"].append(mid)

    print(f"\n{'='*60}")
    print(f"Done. {len(results['success'])} succeeded, {len(results['failed'])} failed.")
    if results["failed"]:
        print(f"Failed: {', '.join(results['failed'])}")


def cmd_list(args):
    """List meetings with transcripts."""
    ensure_dirs()
    transcripts = sorted(TRANSCRIPTS_DIR.glob("*-transcript.txt"))
    if not transcripts:
        print("No transcripts found.")
        return
    for t in transcripts:
        size = t.stat().st_size
        mid = t.stem.replace("-transcript", "")
        print(f"  {mid}  ({size:,} chars)")


def main():
    parser = argparse.ArgumentParser(description="Local transcription with faster-whisper")
    sub = parser.add_subparsers(dest="command")

    p_transcribe = sub.add_parser("transcribe", help="Transcribe a single meeting")
    p_transcribe.add_argument("meeting_id", help="Meeting ID")
    p_transcribe.add_argument("url", help="YouTube/KOCT video URL")

    p_batch = sub.add_parser("batch", help="Batch transcribe from JSON")
    p_batch.add_argument("batch_file", help="JSON file with meeting_id + url pairs")

    sub.add_parser("list", help="List existing transcripts")

    args = parser.parse_args()

    if not args.command:
        if len(sys.argv) >= 3 and not sys.argv[1].startswith("-"):
            args.meeting_id = sys.argv[1]
            args.url = sys.argv[2]
            cmd_transcribe(args)
        else:
            parser.print_help()
        return

    {"transcribe": cmd_transcribe, "batch": cmd_batch, "list": cmd_list}[args.command](args)


if __name__ == "__main__":
    main()
