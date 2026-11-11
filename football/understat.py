"""Understat download client. Understat has no official API: this reads the JSON its own
website loads. One request at a time, 1.5 s apart, with retries. Never run it in parallel.
"""

import gzip
import json
import time
import urllib.error
import urllib.request
import zlib

from . import config

BASE_URL = "https://understat.com"
RETRY_STATUS = {429, 500, 502, 503, 504}


class UnderstatError(Exception):
    """A request failed after every retry. The caller decides whether the run goes on."""


class BudgetSpent(Exception):
    """The run's request cap is reached. Not an error: the next run resumes."""


def decode_body(raw, encoding):
    """Decompress gzip/deflate responses (some servers compress even when not asked)."""
    encoding = (encoding or "").lower()
    if raw[:2] == b"\x1f\x8b" or "gzip" in encoding:
        return gzip.decompress(raw)
    if "deflate" in encoding:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    if "br" in encoding:
        raise ValueError("server used Brotli compression")
    return raw


class Client:
    def __init__(self, max_requests=config.MAX_REQUESTS, delay=config.REQUEST_DELAY,
                 retries=4, sleep=time.sleep):
        self.max_requests, self.delay, self.retries, self.sleep = max_requests, delay, retries, sleep
        self.used, self.last = 0, 0.0

    def _open(self, path):
        req = urllib.request.Request(f"{BASE_URL}/{path}", headers={
            "X-Requested-With": "XMLHttpRequest",           # Understat's data endpoints need it
            "User-Agent": "Mozilla/5.0 (compatible; football-db/2.0; personal use)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = decode_body(resp.read(), resp.headers.get("Content-Encoding", ""))
            try:
                return json.loads(body)
            except ValueError:
                # not JSON: say what came back instead (an HTML page, a block notice, nothing...)
                shown = body[:200].decode("utf-8", "replace").replace("\n", " ").strip() or "(empty)"
                raise ValueError(f"not JSON - HTTP {resp.status}, "
                                 f"{resp.headers.get('Content-Type', 'no content type')}, "
                                 f"{len(body)} bytes, starts with: {shown}")

    def get_json(self, path):
        """-> parsed JSON, or None when Understat has no such page (HTTP 404)."""
        if self.used >= self.max_requests:
            raise BudgetSpent()
        last_error = None
        for attempt in range(self.retries):
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                self.sleep(wait)
            self.last = time.time()
            self.used += 1
            try:
                return self._open(path)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last_error = f"HTTP {e.code}"
                if e.code not in RETRY_STATUS:
                    break                                   # 403 etc.: retrying won't help
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = f"network error: {getattr(e, 'reason', e)}"
            except ValueError as e:                         # not JSON / unreadable compression
                last_error = f"unreadable response: {e}"
            if attempt < self.retries - 1:
                self.sleep(min(15 * 2 ** attempt, 120))     # 15 s, 30 s, 60 s
        raise UnderstatError(f"{path}: {last_error}")

    def league(self, code, season):
        return self.get_json(f"getLeagueData/{code}/{season}")

    def match(self, match_id):
        return self.get_json(f"getMatchData/{match_id}")
