"""The crawl graph, and the guard that stops an early crawl losing a result."""
import json

from conftest import AUTO_RACE, CALENDAR, load

from sjoden.archive_db import db_ops
from sjoden.crawler import (ATG_TYPES, EARLY, Manifest, calendar_task, crawl,
                            dates, expand, is_final, race_task)
from sjoden.fetcher import FetchResult

from datetime import date


class FakeFetcher:
    """Answers from a dict of url -> payload, and remembers what it stored."""

    def __init__(self, responses):
        self.responses = responses
        self.stored = {}
        self.requested = []

    def fetch(self, path):
        self.requested.append(path)
        if path not in self.responses:
            return FetchResult(404, None, None)
        return FetchResult(200, json.dumps(self.responses[path]), None)

    def store_raw(self, raw_path, body):
        self.stored[raw_path] = body
        return raw_path


def manifest_for(tmp_path):
    return db_ops(str(tmp_path / 'atg_data.duckdb'))


def test_dates_run_newest_first():
    # If access breaks mid-crawl, the most valuable seasons are already banked.
    assert dates(date(2021, 1, 1), date(2021, 1, 3)) == [
        date(2021, 1, 3), date(2021, 1, 2), date(2021, 1, 1)]


def test_expand_keeps_only_the_selected_countries():
    task = calendar_task(date(2026, 8, 22))
    children = expand(task, load(CALENDAR), ('SE',), with_games=False)
    assert children and all(c.endpointType == 'atg_race' for c in children)
    # Romme (23) and Vaggeryd (43) are the Swedish cards that day; the same
    # calendar also carries NO, DK, US, CA, FI and FR.
    assert {c.trackId for c in children} == {23, 43}


def test_expand_can_take_more_than_one_country():
    task = calendar_task(date(2026, 8, 22))
    children = expand(task, load(CALENDAR), ('SE', 'FI'), with_games=False)
    assert 59 in {c.trackId for c in children}     # Kouvola


def test_games_are_opt_in():
    task = calendar_task(date(2026, 8, 22))
    without = expand(task, load(CALENDAR), ('SE',), with_games=False)
    with_games = expand(task, load(CALENDAR), ('SE',), with_games=True)
    assert not any(c.endpointType == 'atg_game' for c in without)
    games = [c for c in with_games if c.endpointType == 'atg_game']
    assert games and all(c.entityId.startswith(('V85_', 'V5_')) for c in games)


def test_a_race_is_a_leaf():
    task = race_task('2026-08-22', 23, '2026-08-22_23_1')
    assert expand(task, load(AUTO_RACE), ('SE',), with_games=True) == []


def test_is_final_only_accepts_a_run_race():
    task = race_task('2026-08-22', 23, '2026-08-22_23_1')
    assert is_final(task, {'status': 'results'})
    assert not is_final(task, {'status': 'upcoming'})
    assert not is_final(task, {'status': 'bettable'})
    # A calendar is a listing: a listing of an empty future day is still true.
    assert is_final(calendar_task(date(2030, 1, 1)), {'tracks': []})


def test_an_early_race_is_not_marked_done_and_self_heals(tmp_path):
    """The failure this guard exists to prevent.

    A race that has not run answers 200 with no results. Marking it done would
    retire the task forever and lose the placings permanently — which is what
    premature crawls cost the Finnish archive. Here it is parked as `early` and
    the next backfill picks it up again.
    """
    upcoming = dict(load(AUTO_RACE), status='bettable')
    fetcher = FakeFetcher({'/races/2026-08-22_23_1': upcoming})
    task = race_task('2026-08-22', 23, '2026-08-22_23_1')
    with manifest_for(tmp_path) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([task])
        fetched, early = crawl(manifest, fetcher, lambda t, p: [])
        assert (fetched, early) == (0, 1)
        status = conn.execute(
            'SELECT status FROM atg.manifest').fetchone()[0]
        assert status == EARLY
        # The raw response is kept — it is a legitimate pre-race snapshot.
        assert task.rawPath in fetcher.stored

        # Now the racing is over, and a second run reaches it again.
        assert manifest.reset_early() == 1
        fetcher.responses['/races/2026-08-22_23_1'] = load(AUTO_RACE)
        fetched, early = crawl(manifest, fetcher, lambda t, p: [])
        assert (fetched, early) == (1, 0)
        assert conn.execute('SELECT status FROM atg.manifest').fetchone()[0] == 'done'


def test_crawl_does_not_spin_on_an_early_row(tmp_path):
    # The reason `early` is its own status rather than a row left pending:
    # next_pending() would otherwise hand the same task back forever.
    fetcher = FakeFetcher({'/races/2026-08-22_23_1': dict(load(AUTO_RACE),
                                                          status='upcoming')})
    with manifest_for(tmp_path) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([race_task('2026-08-22', 23, '2026-08-22_23_1')])
        crawl(manifest, fetcher, lambda t, p: [])
    assert len(fetcher.requested) == 1


def test_a_calendar_enqueues_its_races_and_the_crawl_drains_them(tmp_path):
    calendar = load(CALENDAR)
    responses = {'/calendar/day/2026-08-22': calendar}
    for track in calendar['tracks']:
        if track['countryCode'] != 'SE':
            continue
        for race in track['races']:
            responses[f"/races/{race['id']}"] = dict(load(AUTO_RACE),
                                                     id=race['id'], status='results')
    fetcher = FakeFetcher(responses)
    with manifest_for(tmp_path) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([calendar_task(date(2026, 8, 22))])
        fetched, early = crawl(manifest, fetcher,
                               lambda t, p: expand(t, p, ('SE',), False))
        # One calendar plus the 19 Swedish races on it.
        assert fetched == 20 and early == 0
        assert conn.execute(
            "SELECT count(*) FROM atg.manifest WHERE status = 'done'").fetchone()[0] == 20


def test_enqueue_never_reopens_finished_work(tmp_path):
    task = race_task('2026-08-22', 23, '2026-08-22_23_1')
    with manifest_for(tmp_path) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([task])
        manifest.mark(task, 'done', 200, None)
        manifest.enqueue([task])
        assert conn.execute('SELECT status FROM atg.manifest').fetchone()[0] == 'done'


def test_a_missing_date_is_recorded_not_retried_forever(tmp_path):
    # Before the archive begins, a calendar day 404s. That is an answer, not a
    # failure, and it must not spend the retry budget.
    fetcher = FakeFetcher({})
    with manifest_for(tmp_path) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([calendar_task(date(2005, 5, 21))])
        crawl(manifest, fetcher, lambda t, p: [])
        assert conn.execute('SELECT status FROM atg.manifest').fetchone()[0] == 'missing'
        assert manifest.retry_failed() == 0


def test_reset_window_reopens_a_date_range(tmp_path):
    task = race_task('2026-08-22', 23, '2026-08-22_23_1')
    with manifest_for(tmp_path) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([task])
        manifest.mark(task, 'done', 200, None)
        assert manifest.reset_window('2026-08-01', '2026-08-31', ATG_TYPES) == 1
        assert manifest.next_pending(10)


def test_marking_a_refetch_forgets_that_it_was_parsed(tmp_path):
    # The payload on disk has just been replaced, so whatever was loaded from
    # it is stale by definition.
    task = race_task('2026-08-22', 23, '2026-08-22_23_1')
    with manifest_for(tmp_path) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([task])
        manifest.mark(task, 'done', 200, None)
        manifest.mark_parsed([task])
        assert not manifest.done('atg_race', unparsed_only=True)
        manifest.mark(task, 'done', 200, None)
        assert manifest.done('atg_race', unparsed_only=True)
