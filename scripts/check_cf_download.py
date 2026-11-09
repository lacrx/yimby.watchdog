#!/usr/bin/env python3
"""Check CloudflareSession.download() against a local server, no network needed.

The browser-download tier is hard to exercise against real agency sites: it only
runs when the in-page fetch fails, and whether an endpoint sends an attachment
or an inline PDF is not ours to choose. So this serves both shapes locally.

Cases:
  1. attachment PDF            -> bytes come back via the download event
  2. attachment PDF + dest     -> file written and validates as a PDF
  3. non-PDF attachment        -> rejected, nothing written
  4. inline PDF                -> Chrome's viewer fires no download event, so
                                  this must give up on the timeout, not hang
  5. unsolved domain, case 1   -> download() must survive a solve() that fails
  6. solve() on an attachment  -> the navigation aborts with "Download is
                                  starting", so solve() has to fall back to the
                                  origin root to see a challenge at all. This is
                                  the path get_request_cookies() uses, where a
                                  raise degrades silently to "no cookies".

Usage: python3 scripts/check_cf_download.py [--port 8099]
Exit status is 0 only if every case passes.
"""
import argparse
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the check's output readable

    def handle_one_request(self):
        # Chrome abandons connections when a navigation turns into a download.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send(self, body, ctype, disposition=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/attach.pdf":
            self._send(MINIMAL_PDF, "application/pdf", 'attachment; filename="a.pdf"')
        elif self.path == "/inline.pdf":
            self._send(MINIMAL_PDF, "application/pdf")
        elif self.path == "/attach.txt":
            self._send(b"not a pdf at all", "text/plain", 'attachment; filename="a.txt"')
        else:
            self._send(b"<html><body>origin</body></html>", "text/html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()

    from lib.cf_bypass import CloudflareSession, find_chrome

    if not find_chrome():
        print("SKIP: no chrome binary found")
        return 0

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{args.port}"
    results = []

    def case(label, path, want_bytes, dest=None, force_event=True, solved=True):
        session = CloudflareSession(headless=True, timeout_ms=15000)
        try:
            session._ensure_browser()
            url = f"{base}{path}"
            if solved:
                session._solved_domains.add(session._domain(url))
            if force_event:
                # What download() sees when the in-page fetch throws, which is
                # the only way to reach the download-event tier on purpose.
                session._fetch_in_page = lambda page, u: (b"", None)
            started = time.monotonic()
            try:
                data = session.download(url, dest_path=dest)
            except Exception as e:
                results.append((label, "FAIL", f"raised {type(e).__name__}: "
                                f"{str(e).splitlines()[0]}", time.monotonic() - started))
                return
            elapsed = time.monotonic() - started
            got = bool(data)
            detail = f"{len(data)} bytes" if got else 'b""'
            if dest is not None:
                written = Path(dest).exists() and Path(dest).stat().st_size > 0
                detail += f", dest={'written' if written else 'empty'}"
                if want_bytes and not written:
                    got = False
            results.append((label, "PASS" if got == want_bytes else "FAIL",
                            detail, elapsed))
        finally:
            closer = getattr(session, "close", None)
            if callable(closer):
                closer()

    def case_solve_attachment(label):
        session = CloudflareSession(headless=True, timeout_ms=15000)
        try:
            session._ensure_browser()
            url = f"{base}/attach.pdf"
            started = time.monotonic()
            try:
                session.solve(url)
            except Exception as e:
                results.append((label, "FAIL", f"raised {type(e).__name__}: "
                                f"{str(e).splitlines()[0]}", time.monotonic() - started))
                return
            elapsed = time.monotonic() - started
            solved = session._domain(url) in session._solved_domains
            results.append((label, "PASS" if solved else "FAIL",
                            "domain marked solved" if solved else "domain not solved",
                            elapsed))
        finally:
            closer = getattr(session, "close", None)
            if callable(closer):
                closer()

    tmpdir = tempfile.mkdtemp(prefix="cf-download-check-")
    case("attachment PDF -> bytes", "/attach.pdf", True)
    case("attachment PDF -> dest file", "/attach.pdf", True,
         dest=os.path.join(tmpdir, "out.pdf"))
    case("non-PDF attachment rejected", "/attach.txt", False)
    case("inline PDF gives up on timeout", "/inline.pdf", False)
    case("unsolved domain, attachment URL", "/attach.pdf", True,
         force_event=False, solved=False)
    case_solve_attachment("solve() on attachment URL")

    server.shutdown()
    print()
    for label, verdict, detail, elapsed in results:
        print(f"  {verdict:4}  {label:34}  {detail:34}  {elapsed:5.1f}s")
    failures = [r for r in results if r[1] != "PASS"]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
