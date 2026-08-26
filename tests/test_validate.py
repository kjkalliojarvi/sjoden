"""Each check, against an archive built to fail it.

Every check either reports counts that should be zero, or lists offending rows.
The tests here assert both directions: a clean archive is quiet, and a planted
fault is named.
"""
from conftest import AUTO_RACE, GAME, VOLTE_RACE, seed

from sjoden import archive_db
from sjoden.archive_db import db_ops, db_read
from sjoden.crawler import Manifest
from sjoden.parse import parse_all
from sjoden.validate import CHECKS

BY_TITLE = {c.title: c for c in CHECKS}


def run(db_path, title):
    with db_read(db_path) as conn:
        cur = conn.execute(BY_TITLE[title].sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return columns, rows


def archive(paths, races=(VOLTE_RACE, AUTO_RACE), games=()):
    raw_root, db_path = paths
    seed(raw_root, db_path, races=races, games=games)
    parse_all(db_path, raw_root)
    return db_path


def one(db_path, title):
    columns, rows = run(db_path, title)
    return dict(zip(columns, rows[0]))


def test_every_check_runs_on_an_empty_archive(paths):
    _, db_path = paths
    with db_ops(db_path) as conn:
        Manifest(conn).create()
        archive_db.create(conn)
    for check in CHECKS:
        run(db_path, check.title)      # must not raise


def test_coverage_counts_what_the_crawl_reached(paths):
    db_path = archive(paths)
    row = one(db_path, 'Coverage')
    assert row['races_fetched'] == 2
    assert row['fetched_but_unparsed'] == 0


def test_coverage_notices_a_fetched_race_that_never_parsed(paths):
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("DELETE FROM atg.race WHERE raceId = '2026-08-08_33_1'")
    assert one(db_path, 'Coverage')['fetched_but_unparsed'] == 1


def test_placings_are_quiet_on_a_real_card(paths):
    db_path = archive(paths)
    _, rows = run(db_path, 'Placings form a dead-heat-aware sequence from 1')
    assert rows == []


def test_a_dead_heat_is_not_reported_as_a_gap(paths):
    """The check that a naive version gets wrong.

    Two horses share second, so the places read 1, 2, 2, 4 — a gap at 3 that is
    entirely correct. Asserting a strictly consecutive sequence would fire on
    every dead heat in the archive.
    """
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""UPDATE atg.start SET place = 2
                        WHERE raceId = '2026-08-08_33_1' AND startNumber = 12""")
        conn.execute("""UPDATE atg.start SET place = 4
                        WHERE raceId = '2026-08-08_33_1' AND startNumber = 14""")
    _, rows = run(db_path, 'Placings form a dead-heat-aware sequence from 1')
    assert rows == []


def test_a_real_gap_in_the_placings_is_reported(paths):
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""UPDATE atg.start SET place = 9
                        WHERE raceId = '2026-08-08_33_1' AND startNumber = 13""")
    _, rows = run(db_path, 'Placings form a dead-heat-aware sequence from 1')
    assert [r[0] for r in rows] == ['broken sequence']


def test_a_short_field_is_reported(paths):
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""DELETE FROM atg.start
                        WHERE raceId = '2026-08-22_23_1' AND startNumber > 3""")
    _, rows = run(db_path, 'Placings form a dead-heat-aware sequence from 1')
    assert ('fewer than 4 runners', '2026-08-22_23_1', 3) in rows


def test_outcome_flags_agree_on_a_real_card(paths):
    db_path = archive(paths)
    row = one(db_path, 'Outcome flags are mutually consistent')
    assert row['placed_but_flagged'] == 0
    assert row['no_result_at_all'] == 0
    assert row['unpaid_finisher_ahead_of_a_paid_one'] == 0


def test_a_placed_horse_that_is_also_scratched_is_reported(paths):
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""UPDATE atg.start SET scratched = true
                        WHERE raceId = '2026-08-08_33_1' AND startNumber = 2""")
    assert one(db_path, 'Outcome flags are mutually consistent')['placed_but_flagged'] == 1


def test_an_unpaid_finisher_beating_a_paid_one_is_reported(paths):
    """The invariant that replaced "place = 0 means it galloped".

    That reading survived two fixtures and died against 376 real starts: 43 of
    them are place = 0 with no gallop at all, because `place` stops at the
    race's prize places and everyone behind them gets 0. What must still hold
    is the ordering — an unpaid finisher cannot have crossed the line ahead of
    a paid one.
    """
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""UPDATE atg.start SET finishOrder = 2
                        WHERE raceId = '2026-08-08_33_1' AND startNumber = 4""")
    row = one(db_path, 'Outcome flags are mutually consistent')
    assert row['unpaid_finisher_ahead_of_a_paid_one'] == 1


def test_a_gallop_that_was_placed_is_not_reported(paths):
    # Breaking gait and being placed are not contradictory, and a check that
    # said so would fire on thousands of legitimate rows.
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""UPDATE atg.start SET galloped = true
                        WHERE raceId = '2026-08-08_33_1' AND place = 1""")
    row = one(db_path, 'Outcome flags are mutually consistent')
    assert row['placed_but_flagged'] == 0 and row['no_result_at_all'] == 0


def test_atgs_other_convention_is_counted_not_faulted(paths):
    """ATG enumerates the unpaid finishers two different ways.

    Most races give them `place = 0`; some enumerate only the first three and
    leave the rest NULL. Both were seen on the same day at different tracks,
    and `finishOrder` is complete in both — so the second convention is counted
    and made visible, never reported as a missing result.
    """
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""UPDATE atg.start SET place = NULL
                        WHERE raceId = '2026-08-08_33_1' AND place >= 4""")
    row = one(db_path, 'Outcome flags are mutually consistent')
    assert row['finishers_left_unenumerated'] == 5
    assert row['no_result_at_all'] == 0


def test_a_start_with_no_result_at_all_is_reported(paths):
    # The genuine gap: no place, no finishing order, and no reason given.
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""UPDATE atg.start SET place = NULL, finishOrder = NULL
                        WHERE raceId = '2026-08-08_33_1' AND startNumber = 2""")
    assert one(db_path, 'Outcome flags are mutually consistent')['no_result_at_all'] == 1


def test_km_times_land_in_the_plausible_band(paths):
    db_path = archive(paths)
    row = one(db_path, 'Km times fall in the plausible band')
    assert row['with_time'] > 0
    assert row['faster_than_1_08'] == 0 and row['slower_than_1_50'] == 0


def test_an_impossible_km_time_is_flagged(paths):
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        conn.execute("""UPDATE atg.start SET kmTimeMs = 7180
                        WHERE raceId = '2026-08-08_33_1' AND startNumber = 2""")
    assert one(db_path, 'Km times fall in the plausible band')['faster_than_1_08'] == 1


def test_races_per_year_splits_the_two_cards(paths):
    db_path = archive(paths)
    _, rows = run(db_path, 'Races per year')
    #        year  races  days  tracks  trot  gallop  monte
    assert rows == [(2026, 2, 2, 2, 2, 0, 0)]


def test_bet_distribution_sums_to_ten_thousand(paths):
    db_path = archive(paths, races=(), games=(GAME,))
    row = one(db_path, 'Betting distribution sums to 10 000 per race')
    assert row['race_pools'] == 1 and row['near_10000'] == 1


def test_bet_distribution_check_is_empty_without_games(paths):
    db_path = archive(paths)
    assert one(db_path, 'Betting distribution sums to 10 000 per race')['race_pools'] == 0


def test_person_namespace_is_reported(paths):
    db_path = archive(paths)
    row = one(db_path, 'Driver and trainer ids share one namespace')
    assert row['persons'] > 0 and row['nameless'] == 0


def test_career_winnings_check_needs_a_second_start(paths):
    db_path = archive(paths)
    # The two fixture cards share no horse, so there is no consecutive pair to
    # compare — the check reports nothing rather than inventing a verdict.
    assert one(db_path, 'careerWinnings is the pre-race figure')['pairs'] == 0


def test_career_winnings_recognises_the_pre_race_pattern(paths):
    db_path = archive(paths)
    with db_ops(db_path) as conn:
        # One horse, two starts: it went in on 100 000 öre and won 25 000, so a
        # pre-race figure reads 125 000 at the next start.
        conn.execute("""UPDATE atg.start SET horseId = 999, careerWinnings = 100000,
                                             prizeMoney = 25000
                        WHERE raceId = '2026-08-08_33_1' AND startNumber = 2""")
        conn.execute("""UPDATE atg.start SET horseId = 999, careerWinnings = 125000
                        WHERE raceId = '2026-08-22_23_1' AND startNumber = 4""")
    row = one(db_path, 'careerWinnings is the pre-race figure')
    assert row['pairs'] == 1 and row['consistent'] == 1 and row['went_backwards'] == 0
