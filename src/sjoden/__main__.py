import argparse
import os
import signal
import sys

from .archive_db import DEFAULT_DB
from .crawler import backfill, status
from .fetcher import DEFAULT_DELAY
from .parse import parse
from .tui import stats_tui
from .validate import validate

DEFAULT_RAW = 'data/raw'
DEFAULT_COUNTRY = 'SE'
# Five seasons. The endpoint reference measured the archive back to at least
# 2013, but the marginal modelling value of the older years is low against
# their crawl time.
DEFAULT_START = '2021-01-01'

PACKAGE_NAME = 'sjoden'


def require_db(args):
    """Refuse a --db path that is not already there.

    DuckDB creates a database for whatever path it is handed, so a mistyped
    --db does not fail — it mints an empty archive, and the command then
    reports zero of everything as though the work had never been done.

    The crawl and the parse do have to create the database the first time, so
    they take --create-db and say so. `status`, `validate` and `stats` only ever
    read one, so for them there is nothing to opt into.
    """
    if os.path.exists(args.db) or getattr(args, 'create_db', False):
        return
    hint = ' Pass --create-db to start a new one.' if hasattr(args, 'create_db') else ''
    sys.exit(f'{PACKAGE_NAME}: no database at {args.db}.{hint}')


def sigterm_exit(_signum=None, _frame=None):
    """Leave quietly, whether SIGTERM sent us here or the dispatch tail did.

    Both parameters exist because `signal.signal` calls a handler with (signum,
    frame) and this is also called directly with neither. With only one, a real
    SIGTERM would raise TypeError *inside the handler*, so instead of exiting
    the crawl would blow up wherever it stood — typically in `time.sleep` —
    and take `db_ops`'s `conn.close()` down with it.
    """
    sys.exit(0)


def sjoden():
    signal.signal(signal.SIGTERM, sigterm_exit)

    sys.argv[0] = PACKAGE_NAME
    parser = argparse.ArgumentParser(
        description="Collect Swedish harness racing past performances from ATG")
    subparser = parser.add_subparsers(title='Commands', dest='command')

    p_backfill = subparser.add_parser(
        'backfill', help='Crawl the race calendar into the raw archive (resumable)')
    p_backfill.add_argument('--from', dest='start', default=DEFAULT_START,
                            help=f'First meet date to crawl (yyyy-mm-dd, '
                                 f'default: {DEFAULT_START})')
    p_backfill.add_argument('--to', dest='end', default=None,
                            help='Last meet date to crawl (yyyy-mm-dd, default: today)')
    p_backfill.add_argument('--country', default=DEFAULT_COUNTRY,
                            help='Comma-separated track country filter '
                                 f'(default: {DEFAULT_COUNTRY}). ATG also carries NO, '
                                 'DK, FI, FR, US and others')
    p_backfill.add_argument('--raw', default=DEFAULT_RAW,
                            help=f'Raw archive directory (default: {DEFAULT_RAW})')
    p_backfill.add_argument('--db', default=DEFAULT_DB,
                            help=f'DuckDB database holding the manifest '
                                 f'(default: {DEFAULT_DB})')
    p_backfill.add_argument('--delay', type=float, default=DEFAULT_DELAY,
                            help=f'Base seconds between requests (default: {DEFAULT_DELAY})')
    p_backfill.add_argument('--games', action='store_true',
                            help='Also crawl the pools, for turnover and the betting '
                                 'distribution (roughly a 60 %% uplift in requests)')
    p_backfill.add_argument('--limit', type=int, default=None,
                            help='Stop after N fetches (for a trial run)')
    p_backfill.add_argument('--retry-failed', action='store_true',
                            help='Reset failed manifest rows to pending before crawling')
    p_backfill.add_argument('--refetch-from', dest='refetch_start', default=None,
                            help='Re-fetch this date even if already crawled')
    p_backfill.add_argument('--refetch-to', dest='refetch_end', default=None,
                            help='Last date of the re-fetch window (default: --refetch-from)')
    p_backfill.add_argument('--create-db', action='store_true',
                            help='Create the database if it is not there yet '
                                 '(otherwise a missing --db is an error)')
    p_backfill.set_defaults(func=backfill)

    p_parse = subparser.add_parser(
        'parse', help='Parse the raw archive into the atg.* tables')
    p_parse.add_argument('--raw', default=DEFAULT_RAW,
                         help=f'Raw archive directory (default: {DEFAULT_RAW})')
    p_parse.add_argument('--db', default=DEFAULT_DB,
                         help=f'DuckDB database file (default: {DEFAULT_DB})')
    p_parse.add_argument('--full', action='store_true',
                         help='Reload every archived payload, not just the ones fetched '
                              'since the last parse. Needed after changing a parser')
    p_parse.add_argument('--create-db', action='store_true',
                         help='Create the database if it is not there yet')
    p_parse.set_defaults(func=parse)

    p_status = subparser.add_parser('status', help='Show crawl manifest progress')
    p_status.add_argument('--db', default=DEFAULT_DB,
                          help=f'DuckDB database file (default: {DEFAULT_DB})')
    p_status.set_defaults(func=status)

    p_validate = subparser.add_parser(
        'validate', help='Structural checks over the parsed archive')
    p_validate.add_argument('--db', default=DEFAULT_DB,
                            help=f'DuckDB database file (default: {DEFAULT_DB})')
    p_validate.set_defaults(func=validate)

    p_stats = subparser.add_parser(
        'stats',
        help="Browse a horse's, driver's or trainer's starts in a terminal UI")
    p_stats.add_argument('name', nargs='?', default=None,
                         help='Prefill the search box with this name')
    p_stats.add_argument('--db', default=DEFAULT_DB,
                         help=f'DuckDB database file (default: {DEFAULT_DB})')
    p_stats.set_defaults(func=stats_tui)

    args, _ = parser.parse_known_args()
    if not args.command:
        parser.print_help()
        sigterm_exit(None)

    require_db(args)

    try:
        args.func(args)
    except (KeyboardInterrupt, SystemExit):
        sigterm_exit(None)


if __name__ == '__main__':
    sjoden()
