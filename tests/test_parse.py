"""The parsing rules that the endpoint reference gets wrong or leaves implicit."""
import copy

import pytest

from conftest import AUTO_RACE, GAME, VOLTE_RACE, load, seed

from sjoden.archive_db import db_read
from sjoden.models import KmTime, Race
from sjoden.parse import (is_monte, km_time_ms, meet_date, parse_all,
                          scratched_numbers, start_record)


# --- scalar parsers ---------------------------------------------------------

def test_km_time_reads_the_three_integers():
    # 1:11.8 per kilometre arrives as three fields, never as a string.
    assert km_time_ms(KmTime(minutes=1, seconds=11, tenths=8)) == 71800


def test_km_time_without_a_minute():
    assert km_time_ms(KmTime(seconds=59, tenths=0)) == 59000


@pytest.mark.parametrize('km_time', [None, KmTime(code='u'), KmTime(code='kub')])
def test_km_time_is_none_where_the_payload_gave_a_code(km_time):
    # A horse that broke or was pulled up has a code and no time. Coercing that
    # to 0 would put it at the front of any speed ordering.
    assert km_time_ms(km_time) is None


def test_meet_date_falls_back_to_the_race_id():
    # date_track_number, so a payload thin enough to omit `date` still has one.
    assert meet_date(Race(id='2013-03-05_7_4')) == '2013-03-05'


def test_monte_needs_the_accent():
    # 'Monte' unaccented is a sponsor or a horse; 'monté' is the discipline.
    assert is_monte(Race(id='x', name='Svenskt Monté-Derby'))
    assert not is_monte(Race(id='x', name='Monte Carlo Loppet'))


# --- the outcome columns ----------------------------------------------------

@pytest.fixture
def volte():
    return Race.model_validate(load(VOLTE_RACE))


def test_scratchings_come_from_the_race_not_the_start(volte):
    # `result.scratched` on the start has never been observed set. Reading the
    # start alone would lose every withdrawal.
    assert scratched_numbers(volte) == {3}
    assert all(s.result is None or not s.result.scratched for s in volte.starts)


def test_place_is_the_prize_money_position_not_the_finishing_one(volte):
    """The distinction the endpoint reference misses, and the one most likely
    to produce a wrong target variable.

    This card pays eight places, so `place` stops at 8 — and the three horses
    that finished ninth, tenth and eleventh get 0, not 9, 10, 11. Their real
    order is in `finishOrder`.
    """
    place = {s.number: (s.result.place if s.result else None) for s in volte.starts}
    order = {s.number: (s.result.finishOrder if s.result else None) for s in volte.starts}
    assert '8 prisplacerade' in volte.prize
    assert sorted(p for p in place.values() if p and p >= 1) == list(range(1, 9))
    # Finished, unpaid — a real result with a real position behind the paid ones.
    assert [place[n] for n in (4, 9, 11)] == [0, 0, 0]
    assert sorted(order[n] for n in (4, 9, 11)) == [9, 10, 11]
    # No classified finish: 3 was scratched, 5 was disqualified.
    assert place[3] is None and place[5] is None


def test_a_gallop_can_still_be_placed(volte):
    # `galloped` is orthogonal to the outcome: breaking gait is not a fourth
    # placing, and a horse that breaks can still be paid.
    galloped = {s.number for s in volte.starts if s.result and s.result.galloped}
    assert galloped, 'fixture should contain gallops'
    assert 10 in galloped and volte.starts[9].result.disqualified


def test_finish_order_uses_sentinel_bands(volte):
    orders = {s.number: s.result.finishOrder for s in volte.starts if s.result}
    # Everyone who completed is ranked 1..11, gap-free...
    assert sorted(o for o in orders.values() if o <= 11) == list(range(1, 12))
    # ...while the disqualified and the scratched sit in bands far above the
    # field, so sorting on finishOrder without excluding them invents an order.
    assert orders[5] > 40 and orders[10] > 40 and orders[3] > 50


def test_post_position_is_not_unique_but_the_start_number_is(volte):
    posts = [s.postPosition for s in volte.starts]
    numbers = [s.number for s in volte.starts]
    assert len(set(numbers)) == len(numbers) == 14
    assert len(set(posts)) < len(posts)
    # Because the field runs over two distance tiers, and the post restarts.
    assert {s.distance for s in volte.starts} == {2140, 2160}


def test_start_record_takes_the_trainer_from_the_horse(volte):
    start = volte.starts[0]
    record = start_record(volte, start, scratched_numbers(volte))
    trainer_id = record[5]
    assert trainer_id == start.horse.trainer.id
    # The endpoint reference lists `trainer` beside `driver`; it is not there.
    assert not hasattr(start, 'trainer')


# --- end to end through the tables ------------------------------------------

def test_parse_loads_the_whole_field(paths):
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[VOLTE_RACE])
    counts = parse_all(db_path, raw_root)
    assert counts['races'] == 1 and counts['starts'] == 14
    with db_read(db_path) as conn:
        rows = conn.execute(
            """SELECT startNumber, place, scratched, galloped, disqualified, kmTimeMs,
                      kmTimeCode, prizeMoney
               FROM atg.start WHERE raceId = '2026-08-08_33_1'
               ORDER BY startNumber""").fetchall()
    assert len(rows) == 14
    by_number = {r[0]: r for r in rows}
    # The scratched runner is kept as a row: field size and the post positions
    # of everyone else depend on knowing who was declared.
    assert by_number[3][2] is True and by_number[3][1] is None
    # An unpaid finisher keeps its km time; a disqualification keeps its code.
    assert by_number[4][1] == 0 and by_number[4][5] == 92600
    assert by_number[5][5] is None and by_number[5][6] == 'kub'


def test_parse_is_idempotent(paths):
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[VOLTE_RACE, AUTO_RACE])
    parse_all(db_path, raw_root, full=True)
    parse_all(db_path, raw_root, full=True)
    with db_read(db_path) as conn:
        assert conn.execute('SELECT count(*) FROM atg.start').fetchone()[0] == 24
        assert conn.execute('SELECT count(*) FROM atg.race').fetchone()[0] == 2


def test_incremental_parse_skips_what_it_has_loaded(paths):
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[VOLTE_RACE])
    assert parse_all(db_path, raw_root)['races'] == 1
    # Second pass has nothing outstanding — this is what keeps a nightly cycle
    # from re-validating the whole archive.
    assert parse_all(db_path, raw_root)['races'] == 0
    assert parse_all(db_path, raw_root, full=True)['races'] == 1


def test_birth_year_is_derived_from_age_and_meet_date(paths):
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[AUTO_RACE])
    parse_all(db_path, raw_root)
    with db_read(db_path) as conn:
        rows = conn.execute(
            """SELECT h.birthYear, s.horseAge FROM atg.horse h
               JOIN atg.start s USING (horseId) WHERE s.raceId = '2026-08-22_23_1'""").fetchall()
    assert rows and all(year == 2026 - age for year, age in rows)


# --- startInterval ----------------------------------------------------------

HORSE = 797284      # the first start of the auto-start fixture


def _card(date: str, number: int, scratched: tuple[int, ...] = ()) -> dict:
    """The auto-start fixture re-dated, so one horse gets a career to measure."""
    payload = copy.deepcopy(load(AUTO_RACE))
    payload['id'] = f'{date}_23_{number}'
    payload['date'] = date
    payload['number'] = number
    payload['result']['scratchings'] = list(scratched)
    return payload


def _intervals(db_path: str) -> dict[str, int | None]:
    """Every start of HORSE, its meet date against its interval."""
    with db_read(db_path) as conn:
        return dict(conn.execute(
            """SELECT CAST(r.meetDate AS TEXT), s.startInterval
               FROM atg.start s JOIN atg.race r USING (raceId)
               WHERE s.horseId = ? ORDER BY r.meetDate, r.raceNumber""",
            [HORSE]).fetchall())


def test_start_interval_is_the_days_since_the_previous_start(paths):
    raw_root, db_path = paths
    seed(raw_root, db_path,
         races=[_card('2026-08-01', 1), _card('2026-08-15', 1), _card('2026-08-22', 1)])
    parse_all(db_path, raw_root)
    assert _intervals(db_path) == {'2026-08-01': None, '2026-08-15': 14,
                                   '2026-08-22': 7}


def test_the_earliest_start_in_the_archive_is_null_not_a_sentinel(paths):
    """NULL, not days-since-1970.

    The Finnish archive's older column stamps an epoch sentinel there, which has
    to be filtered by a magnitude threshold and reads as a 20 000-day layoff to
    anything that forgets. A nullable column has somewhere to put 'unknowable'.
    """
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[_card('2026-08-01', 1)])
    parse_all(db_path, raw_root)
    assert _intervals(db_path) == {'2026-08-01': None}


def test_a_scratching_is_not_a_point_on_the_timeline(paths):
    """The horse did not start, so the next gap is measured across it."""
    raw_root, db_path = paths
    seed(raw_root, db_path,
         races=[_card('2026-08-01', 1),
                _card('2026-08-08', 1, scratched=(1,)),   # HORSE is start number 1
                _card('2026-08-15', 1)])
    parse_all(db_path, raw_root)
    assert _intervals(db_path) == {'2026-08-01': None, '2026-08-08': None,
                                   '2026-08-15': 14}


def test_two_starts_on_one_date_give_a_real_zero_gap(paths):
    """A heat and its final. Zero is a value here, not a missing one."""
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[_card('2026-08-01', 1), _card('2026-08-01', 5)])
    parse_all(db_path, raw_root)
    with db_read(db_path) as conn:
        gaps = [row[0] for row in conn.execute(
            """SELECT s.startInterval FROM atg.start s JOIN atg.race r USING (raceId)
               WHERE s.horseId = ? ORDER BY r.raceNumber""", [HORSE]).fetchall()]
    assert gaps == [None, 0]


def test_recompute_clears_a_gap_that_stops_qualifying(paths):
    """`UPDATE ... FROM` only reaches the rows it joins to, hence the reset.

    Re-parsing the middle card with the horse scratched must move the last
    start's gap from 7 to 14 *and* blank the scratched one — not leave either
    holding what the first run wrote.
    """
    raw_root, db_path = paths
    seed(raw_root, db_path,
         races=[_card('2026-08-01', 1), _card('2026-08-08', 1), _card('2026-08-15', 1)])
    parse_all(db_path, raw_root)
    assert _intervals(db_path) == {'2026-08-01': None, '2026-08-08': 7,
                                   '2026-08-15': 7}
    seed(raw_root, db_path, races=[_card('2026-08-08', 1, scratched=(1,))])
    parse_all(db_path, raw_root, full=True)
    assert _intervals(db_path) == {'2026-08-01': None, '2026-08-08': None,
                                   '2026-08-15': 14}


def test_connections_land_in_one_person_table(paths):
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[AUTO_RACE])
    parse_all(db_path, raw_root)
    with db_read(db_path) as conn:
        drivers, trainers = conn.execute(
            """SELECT count(DISTINCT driverId), count(DISTINCT trainerId)
               FROM atg.start""").fetchone()
        known = conn.execute(
            """SELECT count(*) FROM atg.start s
               WHERE EXISTS (SELECT 1 FROM atg.person p WHERE p.personId = s.driverId)
                 AND EXISTS (SELECT 1 FROM atg.person p WHERE p.personId = s.trainerId)"""
        ).fetchone()[0]
    assert drivers and trainers and known == 10


def test_statistics_blocks_are_not_persisted(paths):
    # The single highest-value rule in the strategy: those blocks are as-of-now,
    # so a column holding one would leak every future result into a feature.
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[AUTO_RACE])
    parse_all(db_path, raw_root)
    with db_read(db_path) as conn:
        columns = [row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'atg'").fetchall()]
    assert not any('statistic' in c.lower() or 'winpercent' in c.lower() for c in columns)


def test_games_give_bet_distribution_and_pools(paths):
    raw_root, db_path = paths
    seed(raw_root, db_path, games=[GAME])
    counts = parse_all(db_path, raw_root)
    assert counts['bet distributions'] > 0 and counts['pools'] > 0
    with db_read(db_path) as conn:
        total = conn.execute(
            """SELECT sum(distribution) FROM atg.bet_distribution
               WHERE gameType = 'V85'""").fetchone()[0]
        turnover = conn.execute(
            "SELECT turnover FROM atg.pool WHERE poolId = 'V85_2026-08-22_23_5'"
        ).fetchone()[0]
    # Hundredths of a percent — this is the check that establishes the unit.
    assert 9900 <= total <= 10100
    assert turnover > 0


# --- payloads that are not race cards ---------------------------------------

def _parsed_at(db_path: str, race_id: str):
    with db_read(db_path) as conn:
        return conn.execute(
            "SELECT parsedAt FROM atg.manifest WHERE entityId = ?",
            [race_id]).fetchone()[0]


def test_a_card_of_unregistered_horses_is_excluded(paths):
    """Show jumping, which ATG files under trackId 47 tagged `sport: gallop`.

    None of the horses are in the racing registry, so no start carries
    `horse.id` or `trainer.id`. Nothing here can be joined to a horse's career,
    so the card is dropped on purpose — and stamped, so it is not reconsidered.
    """
    raw_root, db_path = paths
    payload = load(AUTO_RACE)
    for start in payload['starts']:
        del start['horse']['id']
        start['horse']['trainer'].pop('id', None)
    seed(raw_root, db_path, races=[payload])
    assert parse_all(db_path, raw_root)['races'] == 0
    assert _parsed_at(db_path, payload['id']) is not None


def test_one_unregistered_start_excludes_the_whole_payload(paths):
    """The ante-post shape: a real field beside an 'Övriga Hästar' bucket.

    Tempting to keep by dropping just the synthetic start — but these carry
    `finalOdds` and no result at all, and the race they price is already in the
    archive under its own id. Keeping one would duplicate every horse in it and
    give each a start that never ran, carrying stale careerWinnings.
    """
    raw_root, db_path = paths
    payload = load(AUTO_RACE)
    del payload['starts'][-1]['horse']['id']
    seed(raw_root, db_path, races=[payload])
    counts = parse_all(db_path, raw_root)
    assert counts['races'] == 0 and counts['starts'] == 0
    with db_read(db_path) as conn:
        assert conn.execute('SELECT count(*) FROM atg.start').fetchone()[0] == 0
    assert _parsed_at(db_path, payload['id']) is not None


def test_a_payload_that_fails_validation_stays_unparsed(paths):
    """The skip has to leave `parsedAt` NULL.

    Stamping it made the loss permanent: an incremental parse never looks at a
    stamped task again, so a payload skipped by a parser bug was gone until
    somebody thought to run `--full`.
    """
    raw_root, db_path = paths
    payload = load(AUTO_RACE)
    del payload['starts'][0]['number']      # required, and unrelated to horse.id
    seed(raw_root, db_path, races=[payload])
    assert parse_all(db_path, raw_root)['races'] == 0
    assert _parsed_at(db_path, payload['id']) is None
    # Still outstanding, so a later run retries it without --full.
    assert parse_all(db_path, raw_root)['races'] == 0
