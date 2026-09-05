from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .streaming import (
    DEFAULT_GEO_NEWS_URL,
    StreamSpec,
    ingestion_doctor,
    run_live_probe,
    write_json_report,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="news-stream",
        description="Phase 2 native YouTube Live ingestion diagnostics",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="check local FFmpeg and yt-dlp dependencies")

    probe = subparsers.add_parser(
        "probe", help="resolve a live source, capture frames, and write a metrics-only report"
    )
    probe.add_argument("--url", default=os.getenv("GEO_NEWS_URL", DEFAULT_GEO_NEWS_URL))
    probe.add_argument("--channel", default="Geo News")
    probe.add_argument("--stream-id", default="geo-news")
    probe.add_argument("--duration", type=float, default=120.0)
    probe.add_argument("--fps", type=float, default=float(os.getenv("STREAM_CAPTURE_FPS", "2.0")))
    probe.add_argument(
        "--target-height", type=int, default=int(os.getenv("STREAM_TARGET_HEIGHT", "1080"))
    )
    probe.add_argument(
        "--queue-capacity", type=int, default=int(os.getenv("STREAM_QUEUE_CAPACITY", "32"))
    )
    probe.add_argument("--output", default=None)
    return parser


def default_report_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT_DIR / "artifacts" / "phase2" / f"geo_news_probe_{stamp}.json"


async def run_probe(args: argparse.Namespace) -> int:
    spec = StreamSpec(
        stream_id=args.stream_id,
        channel_name=args.channel,
        youtube_url=args.url,
        capture_fps=args.fps,
        target_height=args.target_height,
    )
    output = args.output or default_report_path()
    try:
        report = await run_live_probe(
            spec,
            duration_seconds=args.duration,
            queue_capacity=args.queue_capacity,
        )
    except Exception as exc:
        report = {
            "status": "failed",
            "failure_stage": "stream_resolution_or_capture",
            "error": f"{exc.__class__.__name__}: {exc}",
            "stream": {
                "stream_id": spec.stream_id,
                "channel_name": spec.channel_name,
                "youtube_url": spec.youtube_url,
            },
            "configuration": {
                "duration_seconds": args.duration,
                "capture_fps": args.fps,
                "target_height": args.target_height,
                "queue_capacity": args.queue_capacity,
                "hardware_acceleration": "none",
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    path = write_json_report(report, output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport: {path}")
    return 0 if report["status"] == "passed" else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "doctor":
        report = ingestion_doctor()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 1
    if args.command == "probe":
        try:
            return asyncio.run(run_probe(args))
        except KeyboardInterrupt:
            print("Probe stopped by user.", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"Probe failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            return 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
