"""Polite HTTP fetcher and the gzipped raw archive.

Every successful response is written verbatim to the raw zone before anything
is parsed, so a parser bug never costs a re-crawl and the payloads survive even
if ATG closes the endpoint. The crawl stays inside polite-crawler territory —
single-threaded, >= 1 s between requests with jitter, an identifying
User-Agent, exponential backoff, and a circuit breaker.

`racinginfo` is the JSON service that powers atg.se itself. It is publicly
reachable without authentication but it is *not* an officially documented
public API, so the politeness here is the whole licence to use it.
"""
from collections import namedtuple
import gzip
import json
import os
import random
import time

import requests

from .models import URL as API_URL


CONTACT = os.environ.get('SJODEN_CONTACT', 'https://github.com/kjkalliojarvi/sjoden')
HEADERS = {'Accept': 'application/json',
           'Accept-Encoding': 'gzip',
           'User-Agent': f'sjoden/0.1 (personal research; {CONTACT})'}

# 2 s base (0.5 req/s before jitter). Swedish racing runs afternoons and
# evenings, so an off-peak backfill puts load where the API is not serving live
# betting; the pace is the only thing limiting what it carries.
DEFAULT_DELAY = 2.0          # base seconds between requests, before jitter
JITTER = 0.3                 # +- 30 %
BACKOFF = (30, 120, 600)     # 30 s -> 2 min -> 10 min on 429/5xx/timeout
MAX_CONSECUTIVE_FAILURES = 5

# body is the raw response text on success, None otherwise.
FetchResult = namedtuple('FetchResult', 'httpCode body error')


class CircuitOpen(Exception):
    """Too many consecutive failures — the run pauses and can be resumed later."""


class Fetcher:
    """One host at a time. The rate limit, backoff and circuit breaker are per
    instance, so a second source added later never spends this one's politeness
    budget.
    """

    def __init__(self, raw_root: str, delay: float = DEFAULT_DELAY,
                 base_url: str = API_URL):
        self.raw_root = raw_root
        self.delay = delay
        self.base_url = base_url
        self.consecutive_failures = 0
        self._last_request = 0.0

    def _wait(self):
        target = self.delay * random.uniform(1 - JITTER, 1 + JITTER)
        elapsed = time.monotonic() - self._last_request
        if elapsed < target:
            time.sleep(target - elapsed)
        self._last_request = time.monotonic()

    def fetch(self, path: str) -> FetchResult:
        """GET one endpoint, retrying transient failures with backoff."""
        result = FetchResult(None, None, 'not attempted')
        for pause in (0, *BACKOFF):
            if pause:
                time.sleep(pause)
            self._wait()
            try:
                resp = requests.get(f'{self.base_url}{path}', headers=HEADERS, timeout=30)
            except requests.RequestException as e:
                result = FetchResult(None, None, str(e))
                continue
            if resp.status_code == 200:
                self.consecutive_failures = 0
                return FetchResult(200, resp.text, None)
            if resp.status_code in (400, 404):
                # Nothing there — a date before the archive begins, a race id
                # the calendar named but the service no longer serves.
                self.consecutive_failures = 0
                return FetchResult(resp.status_code, None, None)
            result = FetchResult(resp.status_code, None, resp.text[:200])
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise CircuitOpen(
                f'{self.consecutive_failures} consecutive failures, last: {result.error}')
        return result

    def store_raw(self, raw_path: str, body: str) -> str:
        full = os.path.join(self.raw_root, raw_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with gzip.open(full, 'wt', encoding='utf-8') as f:
            f.write(body)
        return full


def read_raw(raw_root: str, raw_path: str):
    """Read one archived response back. Returns None if the file is gone."""
    full = os.path.join(raw_root, raw_path)
    if not os.path.exists(full):
        return None
    with gzip.open(full, 'rt', encoding='utf-8') as f:
        return json.load(f)
