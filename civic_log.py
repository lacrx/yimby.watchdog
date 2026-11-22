#!/usr/bin/env python3
"""CLI face of civic_utils.log_failure, for the shell orchestrator.

civic-pipeline is bash and the record format lives in Python. Rather than keep
a second JSON writer in shell and let the two drift, the orchestrator shells
out to this. Failures are rare, so a process per failure is cheap.

    python3 -m civic_log fail --agency carlsbad --phase fetch --rc 3
    python3 -m civic_log fail --agency vista --phase fetch --rc 124 --timeout

Always exits 0. Recording a failure must never become a second failure.
"""

import argparse
import sys

import civic_utils


def main(argv=None):
    parser = argparse.ArgumentParser(prog="civic_log")
    sub = parser.add_subparsers(dest="cmd")
    fail = sub.add_parser("fail", help="append one record to errors.jsonl")
    fail.add_argument("--agency", default="")
    fail.add_argument("--platform")
    fail.add_argument("--phase", required=True)
    fail.add_argument("--rc", type=int)
    fail.add_argument("--timeout", action="store_true",
                      help="the phase hit run_with_timeout, not a scraper error")
    fail.add_argument("--msg")
    fail.add_argument("--url")
    fail.add_argument("--fixture")

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0

    if args.cmd != "fail":
        return 0

    try:
        civic_utils.log_failure(
            args.agency,
            args.phase,
            platform=args.platform,
            rc=args.rc,
            timed_out=True if args.timeout else None,
            msg=args.msg,
            url=args.url,
            fixture=args.fixture,
            source="orchestrator",
        )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
