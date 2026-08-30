"""The stats layer's SQL, against an archive built to exercise it.

Two invariants carry most of the weight, and both are properties rather than
examples: every uncapped breakdown sums back to the overall start count, and
every bucket opens exactly the count it shows. They are asserted across all
three subjects and all nine axes, so a new `Axis` is covered the day it is
added rather than the day someone writes a test for it.

The widgets are not tested, here or in the sibling repo: Textual's `run_test`
needs an async plugin that is not a dependency, and `tui.py` holds no SQL.

The fixture card is a happy accident worth knowing about — its first start is
disqualified with `finishOrder` 38 and km time code '8', which is exactly the
shape that makes a naive placing column print '38'. See `test_a_disqualification
_shows_its_code_and_not_its_sentinel`.
"""
import copy

import pytest
from conftest import AUTO_RACE, load, seed

from sjoden import stats
from sjoden.archive_db import db_ops, db_read
from sjoden.parse import parse_all

# The three identities the fixture card carries on its first start. One card
# re-dated gives all three the same career, which is what lets one archive
# answer for every subject.
HORSE_ID = 797284       # Västerbo Sparkly
DRIVER_ID = 527494      # Robin Bakker
TRAINER_ID = 647636     # Paul J P Hagoort


def card(date: str, number: int = 1, scratched: tuple[int, ...] = ()) -> dict:
    """The auto-start fixture re-dated, so one horse gets a career to measure.

    Same helper as `test_parse._card`, and for the same reason: no captured
    fixture holds one horse's repeated starts, and a career is what every axis
    here is a way of slicing.
    """
    payload = copy.deepcopy(load(AUTO_RACE))
    payload['id'] = f'{date}_23_{number}'
    payload['date'] = date
    payload['number'] = number
    payload['result']['scratchings'] = list(scratched)
    return payload


def archive(paths, races):
    raw_root, db_path = paths
    seed(raw_root, db_path, races=races)
    parse_all(db_path, raw_root)
    return db_path


def career(paths, *, scratched=()):
    """Three starts a fortnight apart, the last optionally scratched."""
    return archive(paths, [card('2026-08-01'), card('2026-08-15'),
                           card('2026-08-22', scratched=scratched)])


@pytest.fixture(params=[(stats.HORSE, HORSE_ID), (stats.TRAINER, TRAINER_ID),
                        (stats.DRIVER, DRIVER_ID)],
                ids=['horse', 'trainer', 'driver'])
def who(request):
    """Every generic test, asked once per subject.

    One start row carries a horseId, a driverId and a trainerId, so the same
    archive answers for all three and a subject that breaks one axis cannot hide
    behind the other two.
    """
    return request.param


def overall(conn, subject, key) -> dict:
    names, rows = stats.fetch(conn, stats.COMMON[0].breakdown(subject), [key])
    return dict(zip(names, rows[0]))


def rows_of(conn, subject, axis, key):
    return stats.fetch(conn, axis.breakdown(subject), [key])[1]


def axis(title: str):
    """A breakdown by the start of its title, so a cap can be renamed freely."""
    return next(a for a in stats.COMMON if a.title.startswith(title))


# --- the two invariants -----------------------------------------------------

def test_every_uncapped_breakdown_sums_back_to_overall(paths, who):
    """A bucket that drops starts is worse than no bucket.

    The capped axes are exempt by construction — that is what the cap is — but
    still bounded, so a cap that silently stopped capping would be caught.
    """
    subject, key = who
    db_path = career(paths)
    with db_read(db_path) as conn:
        starts = overall(conn, subject, key)['starts']
        assert starts == 3
        for one in stats.breakdowns(subject):
            if one.label is None:
                continue
            counted = sum(row[1] for row in rows_of(conn, subject, one, key))
            if one.limit is None:
                assert counted == starts, one.title
            else:
                assert counted <= starts, one.title


def test_every_bucket_opens_exactly_the_count_it_shows(paths, who):
    """The one invariant `Axis` exists to hold.

    The label expression is written once, so the aggregate's bucket and the
    drill-down's filter cannot drift; this is the assertion that says so. A cap
    must not reach the drill-down, or a bucket on screen would open short.
    """
    subject, key = who
    db_path = career(paths)
    with db_read(db_path) as conn:
        for one in stats.breakdowns(subject):
            if one.label is None:
                continue
            for bucket, count, *_ in rows_of(conn, subject, one, key):
                _, opened = stats.bucket_starts(conn, subject, one, key, bucket)
                assert len(opened) == count, (one.title, bucket)


def test_overall_opens_every_start(paths, who):
    """Overall has no bucket, so clicking it lists the whole career."""
    subject, key = who
    db_path = career(paths)
    with db_read(db_path) as conn:
        _, opened = stats.bucket_starts(conn, subject, stats.COMMON[0], key)
        assert len(opened) == overall(conn, subject, key)['starts']


def test_no_bucket_label_is_ever_null(paths, who):
    """`NULL = ?` is never true, so a NULL label opens an empty list.

    Every label expression coalesces for this reason; the test is what keeps a
    new axis from forgetting.
    """
    subject, key = who
    db_path = career(paths)
    with db_read(db_path) as conn:
        for one in stats.breakdowns(subject):
            if one.label is None:
                continue
            assert all(row[0] is not None for row in rows_of(conn, subject, one, key))


# --- scratchings ------------------------------------------------------------

def test_a_scratching_is_not_a_start(paths, who):
    """A withdrawn horse carries shoes and a post, but it did not race.

    Counting one invents a shoe combination it never started in and dilutes
    every rate — 28,659 rows of it on the live archive.
    """
    subject, key = who
    db_path = career(paths, scratched=(1,))
    with db_read(db_path) as conn:
        assert overall(conn, subject, key)['starts'] == 2
        _, opened = stats.bucket_starts(conn, subject, stats.COMMON[0], key)
        assert len(opened) == 2


def test_a_scratching_is_not_a_point_on_the_layoff_timeline(paths):
    """The gap is measured across a scratching, from the last race actually run.

    So the scratched row keeps NULL and never appears as its own unknown bucket
    — which is what lets the unknown bucket mean one thing. See `stats._LAYOFF`.
    """
    db_path = archive(paths, [card('2026-08-01'), card('2026-08-15', scratched=(1,)),
                              card('2026-08-22')])
    with db_read(db_path) as conn:
        rows = rows_of(conn, stats.HORSE, axis('Days since previous'), HORSE_ID)
        assert dict((r[0], r[1]) for r in rows) == {
            'unknown (no earlier start known)': 1, '15-30 days': 1}


# --- the placing column -----------------------------------------------------

def test_a_disqualification_shows_its_code_and_not_its_sentinel(paths):
    """`finishOrder` on a disqualified row is a sentinel in the 40s.

    Reading it before the disqualification flag prints '38' for this start, as
    if it had finished thirty-eighth in a field of ten. 3,036 live rows would
    read '41'. The code says why: '8' is the gallop code.
    """
    db_path = career(paths)
    with db_read(db_path) as conn:
        names, rows = stats.bucket_starts(conn, stats.HORSE, stats.COMMON[0], HORSE_ID)
        assert all(row[names.index('plc')] == 'dq 8' for row in rows)


def test_a_finisher_shows_its_finishing_order(paths):
    """The other arm: a clean result renders as its number.

    Driver 479879 wins the fixture card, so this also pins that the placings
    read `finishOrder` — `place` is the prize-money position and would agree
    here, which is exactly why the two are easy to confuse.
    """
    db_path = career(paths)
    with db_read(db_path) as conn:
        names, rows = stats.bucket_starts(conn, stats.DRIVER, stats.COMMON[0], 479879)
        assert {row[names.index('plc')] for row in rows} == {'1'}
        assert overall(conn, stats.DRIVER, 479879)['1st'] == 3


def test_prize_money_is_shown_in_kronor_not_ore(paths):
    """Every money column in this archive is öre. 1500 öre is 15 kronor."""
    db_path = career(paths)
    with db_read(db_path) as conn:
        names, rows = stats.bucket_starts(conn, stats.HORSE, stats.COMMON[0], HORSE_ID)
        assert rows[0][names.index('prize kr')] == 15


# --- the layoff axis --------------------------------------------------------

def test_the_layoff_buckets_sort_by_length_not_alphabetically(paths):
    """As text they come out '15-30', '31-60', '<= 14', '> 60'.

    Boundaries at 14/15, 30/31 and 60/61, one start either side of each.
    """
    dates = ['2026-01-01',                      # first start, unknown
             '2026-01-15', '2026-01-30',        # 14 days, then 15
             '2026-03-01', '2026-04-01',        # 30, then 31
             '2026-05-31', '2026-07-31']        # 60, then 61
    db_path = archive(paths, [card(d, n) for n, d in enumerate(dates, 1)])
    with db_read(db_path) as conn:
        rows = rows_of(conn, stats.HORSE, axis('Days since previous'), HORSE_ID)
        assert [r[0] for r in rows] == ['<= 14 days', '15-30 days', '31-60 days',
                                        '> 60 days',
                                        'unknown (no earlier start known)']
        assert [r[1] for r in rows] == [1, 2, 2, 1, 1]


# --- search -----------------------------------------------------------------

def test_a_search_folds_accents(paths, who):
    """The archive is Swedish and a keyboard is not.

    'vasterbo' finds `Västerbo Sparkly`, and 'hagoort' the trainer whose card
    entry is plain — the fold has to leave an unaccented term alone too.
    """
    subject, key = who
    db_path = career(paths)
    term = {'horse': 'vasterbo', 'trainer': 'hagoort', 'driver': 'bakker'}[subject.name]
    with db_read(db_path) as conn:
        _, rows = stats.search(conn, subject, term)
        assert [r[-1] for r in rows] == [key]


def test_a_search_finds_an_identity_pasted_back_in(paths, who):
    """Every other tool in this repo speaks ids, so pasting one has to work."""
    subject, key = who
    db_path = career(paths)
    with db_read(db_path) as conn:
        _, rows = stats.search(conn, subject, str(key))
        assert [r[-1] for r in rows] == [key]


def test_a_search_puts_the_identity_last(paths, who):
    """The UI reads the last column as the row key and the first as its label.

    That contract is what lets `search()` stay subject-agnostic, so it is worth
    pinning rather than leaving to the three query strings to agree on.
    """
    subject, key = who
    db_path = career(paths)
    with db_read(db_path) as conn:
        names, rows = stats.search(conn, subject, str(key))
        assert names[-1] in ('horseId', 'personId')
        assert names[0] == subject.name
        assert 'starts' in names


def test_a_pasted_id_still_excludes_scratchings(paths):
    """The parentheses around the OR in the person search.

    Without them the scratched filter binds to the id arm alone, and pasting an
    id lists withdrawn entries — the one arm nobody reaches by typing a name.
    """
    db_path = career(paths, scratched=(1,))
    with db_read(db_path) as conn:
        names, rows = stats.search(conn, stats.DRIVER, str(DRIVER_ID))
        assert rows[0][names.index('starts')] == 2


def test_a_horse_with_no_starts_left_is_still_a_hit(paths):
    """'Found it, it has nothing here' has to read as an answer.

    A horse whose only appearance was scratched keeps its `atg.horse` row, and
    the LEFT JOIN plus the FILTER inside count() is what reports it as zero
    rather than dropping it or counting the scratching.
    """
    db_path = archive(paths, [card('2026-08-01', scratched=(1,))])
    with db_read(db_path) as conn:
        names, rows = stats.search(conn, stats.HORSE, 'vasterbo')
        assert [r[-1] for r in rows] == [HORSE_ID]
        assert rows[0][names.index('starts')] == 0


def test_a_search_matching_nothing_is_empty_rather_than_an_error(paths, who):
    subject, _ = who
    db_path = career(paths)
    with db_read(db_path) as conn:
        assert stats.search(conn, subject, 'zzzznothing')[1] == []


# --- subjects ---------------------------------------------------------------

def test_every_subject_answers_the_same_number_of_axes():
    """The UI builds its panels once and only retitles them.

    A subject with a different count would leave a stale panel on screen, so the
    invariant belongs here rather than in the widget code.
    """
    assert all(len(stats.breakdowns(s)) == stats.AXIS_COUNT for s in stats.SUBJECTS)


def test_the_last_axis_asks_about_the_counterpart_role():
    """Asking a driver which drivers drove it returns one row equal to Overall."""
    assert stats.breakdowns(stats.DRIVER)[-1] is stats.TRAINER_AXIS
    assert stats.breakdowns(stats.TRAINER)[-1] is stats.DRIVER_AXIS
    assert stats.breakdowns(stats.HORSE)[-1] is stats.DRIVER_AXIS


def test_a_person_start_list_names_the_horse_first(paths):
    """A driver's or a trainer's question is 'which horse ran'."""
    db_path = career(paths)
    with db_read(db_path) as conn:
        for subject in (stats.DRIVER, stats.TRAINER):
            key = DRIVER_ID if subject is stats.DRIVER else TRAINER_ID
            names, _ = stats.bucket_starts(conn, subject, stats.COMMON[0], key)
            assert names[0] == 'horse'
        names, _ = stats.bucket_starts(conn, stats.HORSE, stats.COMMON[0], HORSE_ID)
        assert names[0] == 'date'


def test_two_people_sharing_a_name_are_counted_apart(paths):
    """The identity is the id, never the name.

    On the live archive `atg.person` holds names that repeat; grouping a career
    on the name would report one person's record for two.
    """
    db_path = career(paths)
    with db_ops(db_path) as conn:
        conn.execute("UPDATE atg.person SET firstName = 'Robin', lastName = 'Bakker' "
                     'WHERE personId = 740606')
    with db_read(db_path) as conn:
        names, rows = stats.search(conn, stats.DRIVER, 'bakker')
        assert sorted(r[-1] for r in rows) == [527494, 740606]
        assert all(r[names.index('starts')] == 3 for r in rows)


# --- the empty archive ------------------------------------------------------

def test_every_query_runs_against_an_archive_with_no_starts(paths, who):
    """A fresh archive must render as empty rather than raising."""
    subject, key = who
    _, db_path = paths
    with db_ops(db_path) as conn:
        __import__('sjoden.archive_db', fromlist=['create']).create(conn)
    with db_read(db_path) as conn:
        assert stats.search(conn, subject, 'anything')[1] == []
        for one in stats.breakdowns(subject):
            stats.fetch(conn, one.breakdown(subject), [key])   # must not raise
