"""FotMob download client. FotMob has no official API: this reads the JSON its own website
loads (www.fotmob.com/api/data/...). One request at a time, 1.5 s apart, with retries.
Never run it in parallel, and expect the paths to change without notice.
"""

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

try:   # use the computer's own certificate store (fixes SSL errors on Windows / macOS / company networks)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

BASE_URL = "https://www.fotmob.com/api/data"
RETRY_STATUS = {429, 500, 502, 503, 504}
HEADERS = {                     # the headers the probe of 11 Nov 2026 was answered with
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "application/json", "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip", "Referer": "https://www.fotmob.com/",
}


class FotMobError(Exception):
    """A request failed after every retry. The caller decides whether the run goes on."""


class BudgetSpent(Exception):
    """The run's request cap is reached. Not an error: the next run resumes."""


class Client:
    def __init__(self, max_requests=config.MAX_REQUESTS, delay=config.REQUEST_DELAY,
                 retries=4, sleep=time.sleep):
        self.max_requests, self.delay, self.retries, self.sleep = max_requests, delay, retries, sleep
        self.used, self.last = 0, 0.0

    def _open(self, path):
        req = urllib.request.Request(f"{BASE_URL}/{path}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            try:
                return json.loads(body)
            except ValueError:
                # not JSON: say what came back instead (the website's HTML, a block notice, nothing...)
                shown = body[:200].decode("utf-8", "replace").replace("\n", " ").strip() or "(empty)"
                raise ValueError(f"not JSON - HTTP {resp.status}, "
                                 f"{resp.headers.get('Content-Type', 'no content type')}, "
                                 f"{len(body)} bytes, starts with: {shown}")

    def get_json(self, path):
        """-> parsed JSON, or None when FotMob has no such page (HTTP 404)."""
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
                if "CERTIFICATE_VERIFY_FAILED" in last_error:
                    last_error += " - run: python -m pip install truststore"
                    break                                   # retrying won't help
            except ValueError as e:                         # not JSON
                last_error = f"unreadable response: {e}"
            if attempt < self.retries - 1:
                self.sleep(min(15 * 2 ** attempt, 120))     # 15 s, 30 s, 60 s
        raise FotMobError(f"{path}: {last_error}")

    def league(self, code, season):
        """One league-season: every match with its status and score, and the league table."""
        season = urllib.parse.quote(config.fotmob_season(season), safe="")
        return self.get_json(f"leagues?id={config.FOTMOB_IDS[code]}&season={season}")

    def match(self, match_id):
        """One match: team stats, lineups, player stats, shots."""
        return self.get_json(f"matchDetails?matchId={match_id}")
