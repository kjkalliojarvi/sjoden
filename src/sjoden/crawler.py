"""Manifest-driven, resumable backfill crawler.

The manifest is a ledger of every planned fetch. The crawl loop is just "take
the next pending row, fetch it, store the raw response, mark it done" — and
parsing a fetched response enqueues its children. Kill the process at any
point; restarting resumes exactly where it stopped.

Ordering is newest date first: if access breaks mid-crawl, the most valuable
seasons are already banked.
"""
from collections import namedtuple
from datetime import date, datetime, timedelta
import json

from .archive_db import CREATE_SCHEMA, db_ops
from .fetcher import CircuitOpen, Fetcher


# Stages order the work within one meet date: the calendar before the races it
# names, and the games last. next_pending() sorts by (meetDate DESC, stage ASC).
CALENDAR, RACE, GAME = range(3)

ATG_TYPES = ('atg_calendar', 'atg_race', 'atg_game')

# A race that has not run answers 200 with status 'upcoming'/'bettable' and no
# results, so marking it done would lose the placings permanently. `crawl`
# stamps such a row EARLY instead, and `backfill` resets EARLY to pending on
# every run — a distinct status rather than leaving it pending, because
# next_pending() would otherwise hand the same row straight back and spin.
EARLY = 'early'

Task = namedtuple('Task', 'endpointType entityId url rawPath meetDate trackId stage')

CREATE_MANIFEST_TABLE = """
    CREATE TABLE IF NOT EXISTS atg.manifest(
        endpointType TEXT,
        entityId TEXT,
        url TEXT,
        rawPath TEXT,
        meetDate TEXT,
        trackId BIGINT,
        stage BIGINT,
        status TEXT,
        httpCode BIGINT,
        fetchedAt TEXT,
        attempts BIGINT,
        error TEXT,
        parsedAt TEXT,           -- when this payload was last loaded; NULL = never
        PRIMARY KEY (endpointType, entityId));
"""

# Never clobbers a row that is already done — re-enqueueing is a no-op.
INSERT_TASK = """
    INSERT OR IGNORE INTO atg.manifest
    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0, NULL, NULL);
"""


class Manifest:
    """The fetch ledger. Wraps an open DuckDB connection."""

    def __init__(self, conn):
        self.conn = conn

    def create(self):
        self.conn.execute(CREATE_SCHEMA)
        self.conn.execute(CREATE_MANIFEST_TABLE)

    def enqueue(self, tasks: list[Task]):
        if tasks:
            self.conn.executemany(INSERT_TASK, [tuple(t) for t in tasks])

    def next_pending(self, limit: int, types: tuple = ATG_TYPES) -> list[Task]:
        placeholders = ', '.join('?' * len(types))
        rows = self.conn.execute(
            f"""SELECT endpointType, entityId, url, rawPath, meetDate, trackId, stage
                FROM atg.manifest
                WHERE status = 'pending' AND endpointType IN ({placeholders})
                ORDER BY meetDate DESC, stage ASC LIMIT ?""",
            (*types, limit)).fetchall()
        return [Task(*row) for row in rows]

    def mark(self, task: Task, status: str, http_code, error):
        """Record the outcome of a fetch, and forget that it was ever parsed.

        Clearing `parsedAt` is what makes a re-fetch authoritative: the payload
        on disk has just been replaced, so whatever was loaded from it is stale
        by definition. Leaving it to the `parsedAt < fetchedAt` comparison
        alone would lose a re-fetch landing in the same second as the parse
        before it, both stamps being second-resolution.
        """
        self.conn.execute(
            """UPDATE atg.manifest
               SET status = ?, httpCode = ?, fetchedAt = ?, attempts = attempts + 1,
                   error = ?, parsedAt = NULL
               WHERE endpointType = ? AND entityId = ?""",
            (status, http_code, datetime.now().isoformat(timespec='seconds'), error,
             task.endpointType, task.entityId))

    def retry_failed(self, types: tuple = ATG_TYPES) -> int:
        return self._reset('failed', types)

    def reset_early(self, types: tuple = ATG_TYPES) -> int:
        """Make the not-yet-run races fetchable again.

        Called at the top of every backfill, which is what makes a prematurely
        crawled card self-heal without a second source and without a --to lag.
        """
        return self._reset(EARLY, types)

    def _reset(self, status: str, types: tuple) -> int:
        placeholders = ', '.join('?' * len(types))
        where = f'status = ? AND endpointType IN ({placeholders})'
        params = (status, *types)
        before = self.conn.execute(
            f'SELECT count(*) FROM atg.manifest WHERE {where}', params).fetchone()[0]
        self.conn.execute(
            f"UPDATE atg.manifest SET status = 'pending' WHERE {where}", params)
        return before

    def counts(self) -> list[tuple]:
        return self.conn.execute(
            """SELECT endpointType, status, count(*) FROM atg.manifest
               GROUP BY endpointType, status ORDER BY endpointType, status""").fetchall()

    def done(self, endpoint_type: str, unparsed_only: bool = False) -> list[Task]:
        """Every successfully fetched row of one endpoint type, oldest date first.

        `unparsed_only` narrows it to what a parse still owes work on. A
        re-fetch clears `parsedAt` outright (see `mark`), so the NULL branch is
        what normally catches a row put back through the crawl by
        `reset_window()`. The timestamp comparison is the backstop.
        """
        unparsed = 'AND (parsedAt IS NULL OR parsedAt < fetchedAt)' if unparsed_only else ''
        rows = self.conn.execute(
            f"""SELECT endpointType, entityId, url, rawPath, meetDate, trackId, stage
                FROM atg.manifest
                WHERE endpointType = ? AND status = 'done' {unparsed}
                ORDER BY meetDate ASC""", (endpoint_type,)).fetchall()
        return [Task(*row) for row in rows]

    def mark_parsed(self, tasks: list[Task]):
        """Record that these payloads have been loaded.

        Called once per phase, *after* its final flush: a crash between the two
        re-does the phase, which is safe because every upsert is idempotent,
        whereas stamping first would lose the rows silently.
        """
        if tasks:
            now = datetime.now().isoformat(timespec='seconds')
            self.conn.executemany(
                """UPDATE atg.manifest SET parsedAt = ?
                   WHERE endpointType = ? AND entityId = ?""",
                [(now, t.endpointType, t.entityId) for t in tasks])

    def reset_window(self, first: str, last: str, types: tuple = ATG_TYPES) -> int:
        """Make a date range fetchable again. Returns the number of rows reset.

        `missing` and `failed` are reset alongside `done`: recovering a
        mis-timed crawl is the point, and 'nothing there' is exactly what an
        early fetch can look like.
        """
        placeholders = ', '.join('?' * len(types))
        rows = self.conn.execute(
            f"""UPDATE atg.manifest SET status = 'pending'
                WHERE meetDate BETWEEN ? AND ?
                  AND endpointType IN ({placeholders})
                  AND status <> 'pending'
                RETURNING 1""", (first, last, *types)).fetchall()
        return len(rows)


def dates(start: date, end: date) -> list[date]:
    """Every date in [start, end], newest first."""
    span = (end - start).days
    return [end - timedelta(days=n) for n in range(span + 1)]


def calendar_task(day: date) -> Task:
    d = day.isoformat()
    return Task('atg_calendar', d, f'/calendar/day/{d}', f'{d}/calendar.json.gz',
                d, None, CALENDAR)


def race_task(meet_date: str, track_id: int, race_id: str) -> Task:
    return Task('atg_race', race_id, f'/races/{race_id}',
                f'{meet_date}/race_{race_id}.json.gz', meet_date, track_id, RACE)


def game_task(meet_date: str, track_id: int, game_id: str) -> Task:
    return Task('atg_game', game_id, f'/games/{game_id}',
                f'{meet_date}/game_{game_id}.json.gz', meet_date, track_id, GAME)


def selected_tracks(payload: dict, countries: tuple) -> list[dict]:
    return [t for t in payload.get('tracks', []) if t.get('countryCode') in countries]


def expand(task: Task, payload, countries: tuple, with_games: bool) -> list[Task]:
    """The children a fetched response implies.

    Only the calendar has any: a race payload is a leaf, and so is a game.
    """
    if task.endpointType != 'atg_calendar':
        return []
    tracks = selected_tracks(payload, countries)
    children = [race_task(task.meetDate, track['id'], race['id'])
                for track in tracks for race in track.get('races', [])]
    if with_games:
        # A game names the race ids it covers; the leg's track id is embedded in
        # each. Keep a game if any leg runs at a selected track — a multi-leg
        # game never spans two, but filtering on membership costs nothing and
        # does not assume it.
        wanted = {race['id'] for track in tracks for race in track.get('races', [])}
        by_track = {race['id']: track['id']
                    for track in tracks for race in track.get('races', [])}
        for games in (payload.get('games') or {}).values():
            for game in games:
                legs = [r for r in game.get('races', []) if r in wanted]
                if legs:
                    children.append(game_task(task.meetDate, by_track[legs[0]], game['id']))
    return children


def is_final(task: Task, payload) -> bool:
    """Has this payload's racing actually been run?

    A calendar day is always final — it is a listing, and a listing of an
    empty future date is still the truth about that date. A race or a game is
    final only once it says `results`; anything else is a card crawled too
    early, which the EARLY status sends back round.
    """
    if task.endpointType == 'atg_calendar':
        return True
    return isinstance(payload, dict) and payload.get('status') == 'results'


def crawl(manifest: Manifest, fetcher: Fetcher, expander, finality=is_final,
          types: tuple = ATG_TYPES, limit: int | None = None) -> tuple[int, int]:
    """Drain the manifest. Returns (fetched, early).

    `expander` is the crawl graph — `(task, payload) -> list[Task]`. The loop
    itself knows nothing about the API.
    """
    fetched = early = 0
    while True:
        batch = manifest.next_pending(50, types)
        if not batch:
            return fetched, early
        for task in batch:
            if limit is not None and fetched + early >= limit:
                return fetched, early
            result = fetcher.fetch(task.url)
            if result.body is None:
                status = 'missing' if result.error is None else 'failed'
                manifest.mark(task, status, result.httpCode, result.error)
                continue
            fetcher.store_raw(task.rawPath, result.body)
            try:
                payload = json.loads(result.body)
                children = expander(task, payload)
            except (ValueError, KeyError) as e:
                manifest.mark(task, 'failed', result.httpCode, f'expand: {e}')
                continue
            manifest.enqueue(children)
            if not finality(task, payload):
                # Raw is kept: it is a legitimate pre-race snapshot, and the
                # re-fetch will overwrite it once the racing is done.
                manifest.mark(task, EARLY, result.httpCode,
                              f"status: {payload.get('status')}")
                early += 1
                continue
            manifest.mark(task, 'done', result.httpCode, None)
            fetched += 1
            if fetched % 100 == 0:
                print(f'{fetched} fetched, at {task.meetDate} ({task.endpointType})',
                      flush=True)


def refetch_window(args, manifest: Manifest, types: tuple = ATG_TYPES):
    """Apply --refetch-from/--refetch-to, if given.

    Deliberately a separate window from --from/--to: the recommended update
    cycle pins --from at the start of the archive, so a flag reusing that
    window would quietly re-crawl years.
    """
    if not getattr(args, 'refetch_start', None):
        return
    first = args.refetch_start
    last = args.refetch_end or first
    if first > last:
        print('--refetch-from must not be after --refetch-to.')
        return
    count = manifest.reset_window(first, last, types)
    print(f'{count} manifest rows in {first}..{last} reset to pending.')


def backfill(args):
    """CLI handler: enqueue a date window and crawl it, newest date first."""
    start = datetime.strptime(args.start, '%Y-%m-%d').date()
    end = datetime.strptime(args.end, '%Y-%m-%d').date() if args.end else date.today()
    if start > end:
        print('--from must not be after --to.')
        return
    countries = tuple(c.strip().upper() for c in args.country.split(',') if c.strip())
    fetcher = Fetcher(args.raw, args.delay)
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([calendar_task(d) for d in dates(start, end)])
        reset = manifest.reset_early()
        if reset:
            print(f'{reset} rows crawled before their racing was final reset to pending.')
        refetch_window(args, manifest)
        if args.retry_failed:
            print(f'{manifest.retry_failed()} failed rows reset to pending.')
        try:
            fetched, early = crawl(
                manifest, fetcher,
                lambda t, p: expand(t, p, countries, args.games),
                limit=args.limit)
            print(f'{fetched} responses fetched into {args.raw}.')
            if early:
                print(f'{early} not yet run — they stay {EARLY} and are retried next run.')
        except CircuitOpen as e:
            print(f'Crawl paused: {e}\nRerun the same command to resume.')
        for row in manifest.counts():
            print('  {:<13} {:<8} {}'.format(*row))


def status(args):
    """CLI handler: what the manifest knows."""
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        rows = manifest.counts()
    if not rows:
        print('Manifest is empty.')
        return
    for row in rows:
        print('{:<13} {:<8} {}'.format(*row))
