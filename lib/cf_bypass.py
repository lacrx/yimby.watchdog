"""Cloudflare challenge bypass using Playwright.

Provides a browser session that solves Cloudflare challenges once and
extracts cookies for use with plain requests. Falls back to direct
browser download when cookies alone don't work.

Usage:
    from lib.cf_bypass import CloudflareSession

    with CloudflareSession() as cf:
        # Get cookies after solving challenge on any page from this host
        cookies = cf.solve("https://escribemeetings.com/some/page")
        # Use cookies with requests
        resp = requests.get(url, cookies=cookies)

        # Or download a PDF directly through the browser
        pdf_bytes = cf.download(pdf_url)
"""

import atexit
import fcntl
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

_DATA_DIR = Path(os.environ.get("WATCHDOG_DATA_DIR",
                                str(Path(__file__).resolve().parent.parent / "data")))
LOCK_FILE = _DATA_DIR / ".cf_bypass.lock"
LOCK_WAIT_S = 10
_FALLBACK_UA_VERSION = "131.0.0.0"
_printed = set()
_busy_until = 0.0  # epoch; fail fast with BrowserBusy until then


class BrowserBusy(Exception):
    """Another process holds the cf_bypass browser lock."""


def _log_once(key, msg):
    if key not in _printed:
        _printed.add(key)
        print(msg, flush=True)


def find_chrome():
    """Path to a system Chrome/Chromium, or None to use Playwright's bundled one."""
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


class CloudflareSession:
    """Manages a Playwright browser session for Cloudflare bypass."""

    def __init__(self, headless=True, timeout_ms=30000):
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._cookies = {}  # domain -> {name: value}
        self._solved_domains = set()
        self._lock_fd = None
        self._prev_sigterm = None
        self.last_fail_challenge = False  # True if the last failed solve() hit a real challenge
        self.user_agent = None  # set from the real browser version at launch

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def invalidate(self, domain):
        """Forget cached cookies / solved state so the next solve() re-solves."""
        self._cookies.pop(domain, None)
        self._solved_domains.discard(domain)

    def _acquire_lock(self):
        if self._lock_fd is not None:
            return
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = open(LOCK_FILE, "w")
        deadline = time.time() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fd = fd
                return
            except OSError:
                if time.time() >= deadline:
                    fd.close()
                    raise BrowserBusy("browser lock held by another process")
                time.sleep(0.5)

    def _release_lock(self):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:
                pass
            self._lock_fd = None

    def _on_sigterm(self, signum, frame):
        self.close()
        prev = self._prev_sigterm
        if callable(prev):
            prev(signum, frame)
        raise SystemExit(128 + signum)

    def close(self):
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._release_lock()

    def _ensure_browser(self):
        if self._browser:
            return
        from playwright.sync_api import sync_playwright  # ImportError handled by caller
        global _busy_until
        if time.time() < _busy_until:
            raise BrowserBusy("browser lock recently held by another process")
        try:
            self._acquire_lock()
        except BrowserBusy:
            _busy_until = time.time() + 300
            raise
        try:
            self._pw = sync_playwright().start()
            kwargs = {}
            chrome = find_chrome()
            if chrome:
                kwargs["executable_path"] = chrome
            self._browser = self._pw.chromium.launch(
                headless=self._headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                **kwargs,
            )
            try:
                major = self._browser.version.split(".")[0]
                ver = f"{major}.0.0.0"
            except Exception:
                ver = _FALLBACK_UA_VERSION
            self.user_agent = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{ver} Safari/537.36"
            )
            self._context = self._browser.new_context(
                user_agent=self.user_agent,
                viewport={"width": 1280, "height": 720},
            )
        except Exception:
            self.close()
            raise
        atexit.register(self.close)
        try:
            self._prev_sigterm = signal.signal(signal.SIGTERM, self._on_sigterm)
        except ValueError:
            pass  # not in main thread

    def _domain(self, url):
        return urlparse(url).netloc

    def _probe(self, url, cookies):
        """GET url with requests + cookies. Returns (cleared, challenged).

        cleared: no cf-mitigated header and status < 500. challenged: the
        response carried cf-mitigated (a real challenge, not an outage).
        """
        try:
            r = requests.get(url, cookies=cookies, timeout=30, verify=False,
                             headers={"User-Agent": self.user_agent or ""},
                             allow_redirects=True)
        except Exception:
            return False, False
        challenged = "cf-mitigated" in r.headers
        return (not challenged and r.status_code < 500), challenged

    def solve(self, url):
        """Navigate to url, wait for Cloudflare challenge to clear, return cookies.

        Returns a dict of {name: value} cookies for use with requests, or {}
        (after logging a `cf_bypass: FAIL` line) if the challenge was not
        verifiably cleared. Success needs a cf_clearance cookie, or a probe GET
        with the cookies returning 200 without a cf-mitigated header.
        __cf_bm alone does not count.
        """
        domain = self._domain(url)
        if domain in self._solved_domains and domain in self._cookies:
            return self._cookies[domain]
        self.last_fail_challenge = False

        self._ensure_browser()
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            self._wait_for_challenge(page)

            cookies = {}
            for c in self._context.cookies():
                cd = c.get("domain", "")
                # Match exact domain or wildcard (.escribemeetings.com matches solanabeach.escribemeetings.com)
                if cd == domain or domain.endswith(cd) or cd.endswith(domain):
                    cookies[c["name"]] = c["value"]

            if "cf_clearance" not in cookies:
                # Probe the domain root: a document URL may legitimately 404
                scheme = urlparse(url).scheme or "https"
                cleared, challenged = self._probe(f"{scheme}://{domain}/", cookies)
                if not cleared:
                    self.last_fail_challenge = challenged
                    print(f"cf_bypass: FAIL {domain}: challenge not cleared "
                          f"(no cf_clearance, root probe "
                          f"{'challenged' if challenged else 'failed'})", flush=True)
                    return {}

            self._cookies[domain] = cookies
            self._solved_domains.add(domain)
            return cookies
        finally:
            page.close()

    def _wait_for_challenge(self, page, max_wait=25):
        """Wait for Cloudflare challenge to resolve."""
        for i in range(max_wait):
            try:
                title = page.title().lower()
            except Exception:
                time.sleep(1)
                continue

            # Cloudflare challenge pages have these markers
            if "just a moment" in title or "attention required" in title:
                time.sleep(1)
                continue

            url = page.url.lower()
            if "challenge" in url and "cloudflare" in url:
                time.sleep(1)
                continue

            # Check for cf_clearance cookie (__cf_bm alone is not proof of a solve)
            for c in page.context.cookies():
                if c["name"] == "cf_clearance":
                    return True

            # If page loaded and is past about:blank, we're through
            if page.url != "about:blank" and i >= 2:
                return True

            time.sleep(1)

        return False

    def download(self, url, dest_path=None):
        """Download a file through the browser. Returns bytes or writes to dest_path.

        Use when cookie-based requests still fail (rare, but some CDN
        paths require the full browser context).
        """
        self._ensure_browser()
        domain = self._domain(url)

        # Solve challenge first if we haven't for this domain
        if domain not in self._solved_domains:
            self.solve(url)

        page = self._context.new_page()
        tmp_name = None
        try:
            # Set up download handling
            with page.expect_download(timeout=60000) as download_info:
                page.goto(url, timeout=self._timeout_ms)
            download = download_info.value

            if dest_path:
                dest_path = Path(dest_path)
                fd, tmp_name = tempfile.mkstemp(dir=str(dest_path.parent),
                                                prefix=dest_path.name + ".", suffix=".part")
                os.close(fd)
                download.save_as(tmp_name)
                data = Path(tmp_name).read_bytes()
                if b"%PDF" in data[:1024]:
                    os.replace(tmp_name, dest_path)
                    tmp_name = None
                    return data
                return b""  # not a PDF (challenge page etc): never leave it at dest_path
            else:
                tmp = download.path()
                data = Path(tmp).read_bytes() if tmp else b""
                return data
        except Exception:
            # Fallback: some PDFs render in-browser instead of downloading
            resp = page.request.get(url)
            data = resp.body()
            if dest_path:
                if data and b"%PDF" in data[:1024]:
                    dest_path = Path(dest_path)
                    part = dest_path.with_name(dest_path.name + ".part")
                    part.write_bytes(data)
                    os.replace(part, dest_path)
                    return data
                return b""
            return data
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            page.close()

    def get_request_cookies(self, url):
        """Return cookies dict for use with requests.get(url, cookies=...).

        Solves challenge if needed. Returns empty dict if solve fails.
        """
        try:
            return self.solve(url)
        except (BrowserBusy, ImportError):
            raise
        except Exception as e:
            print(f"cf_bypass: FAIL {self._domain(url)}: solve error: {e}", flush=True)
            return {}
