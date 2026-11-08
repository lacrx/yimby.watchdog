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

import time
from pathlib import Path
from urllib.parse import urlparse


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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

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

    def _ensure_browser(self):
        if self._browser:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )

    def _domain(self, url):
        return urlparse(url).netloc

    def solve(self, url):
        """Navigate to url, wait for Cloudflare challenge to clear, return cookies.

        Returns a dict of {name: value} cookies for use with requests.
        """
        domain = self._domain(url)
        if domain in self._solved_domains and domain in self._cookies:
            return self._cookies[domain]

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

            # Check for cf_clearance or __cf_bm cookie
            for c in page.context.cookies():
                if c["name"] in ("cf_clearance", "__cf_bm"):
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
        try:
            # Set up download handling
            with page.expect_download(timeout=60000) as download_info:
                page.goto(url, timeout=self._timeout_ms)
            download = download_info.value

            if dest_path:
                download.save_as(str(dest_path))
                return Path(dest_path).read_bytes()
            else:
                tmp = download.path()
                data = Path(tmp).read_bytes() if tmp else b""
                return data
        except Exception:
            # Fallback: some PDFs render in-browser instead of downloading
            resp = page.request.get(url)
            data = resp.body()
            if dest_path and data and b"%PDF" in data[:10]:
                Path(dest_path).write_bytes(data)
            return data
        finally:
            page.close()

    def get_request_cookies(self, url):
        """Return cookies dict for use with requests.get(url, cookies=...).

        Solves challenge if needed. Returns empty dict if solve fails.
        """
        try:
            return self.solve(url)
        except Exception as e:
            print(f"  cf_bypass: challenge solve failed: {e}")
            return {}
