"""The crawl-derived past-performance dataset.

The tables live in a DuckDB schema named `atg`, keeping the crawl-derived
dataset distinct from anything else the database file might hold — and named so
that attaching this file alongside the Finnish `archive` schema stays
unambiguous.

Column names mirror the API's camelCase. Two DuckDB rules hold throughout:
every id and epoch-millisecond value is `BIGINT` (`INTEGER` is 32-bit and the
öre amounts overflow it), and every table upserts with `INSERT OR REPLACE` —
the SQLite `ON CONFLICT` clause inside a `PRIMARY KEY` definition is not
supported.

**Money is in öre**, everywhere it appears: `horse.money`, `result.prizeMoney`
and pool turnover. Stored as the API sends it, divided only for display.

**What is deliberately absent: the embedded `statistics` blocks.** Every start
carries a full horse/driver/trainer statistics block and it is tempting to use
as a feature. They are served from current registry state, not snapshotted at
race time, so persisting them would leak each race's own result — and every
result after it — into anything trained on this archive. Rolling form, win
rates and earnings-per-start are derived from `atg.start` with an explicit
`< race.meetDate` predicate instead. The one embedded aggregate kept is
`horse.money`, as `start.careerWinnings`, and `sjoden validate` checks it is
the pre-race figure rather than assuming it.
"""
import os
from contextlib import contextmanager

import duckdb


# Alongside the raw zone (`data/raw`), so that both halves of the pipeline
# default into `data/` and a bare `parse` finds the manifest a bare `backfill`
# wrote.
# Not `atg.duckdb`: DuckDB names the catalog after the file, so a database
# called `atg` collides with the schema of that name and every query fails with
# 'Ambiguous reference to catalog or schema'.
DEFAULT_DB = 'data/atg_data.duckdb'

CREATE_SCHEMA = 'CREATE SCHEMA IF NOT EXISTS atg;'

CREATE_RACE_TABLE = """
    CREATE TABLE IF NOT EXISTS atg.race(
        raceId TEXT,             -- '2026-08-08_33_1'
        meetDate DATE,
        trackId BIGINT,
        trackName TEXT,
        trackCondition TEXT,
        countryCode TEXT,        -- the SE filter, recorded rather than implied
        sport TEXT,              -- trot / gallop
        raceNumber BIGINT,
        name TEXT,
        distance BIGINT,         -- metres
        startMethod TEXT,        -- auto / volte
        startTime TIMESTAMP,
        scheduledStartTime TIMESTAMP,
        prizeText TEXT,          -- free text: 'Pris: 225.000-112.500-… kr'
        terms TEXT,              -- JSON array, verbatim
        monte BOOLEAN,           -- derived from the name and terms
        status TEXT,
        victoryMargin TEXT,
        scratchings TEXT,        -- JSON array of start numbers, verbatim
        fieldSize BIGINT,        -- starts declared, scratchings included
        PRIMARY KEY (raceId));
"""
INSERT_RACE = ('INSERT OR REPLACE INTO atg.race '
               'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
RACE_KEY = (0,)  # raceId

# One row per (race, horse) = one START = one past-performance line. This table
# is authoritative on its own: the whole field is here, results included, so
# "past performances of horse X before date D" is one indexed join against
# atg.race and needs no second source.
#
# **`finishOrder` is the finishing order; `place` is the prize-money position.**
# They are not the same column read twice, and mistaking one for the other is
# the single easiest way to build a wrong target variable here.
#
#   finishOrder  1..N over everyone who completed, gap-free, plus sentinel
#                bands far above the field for the disqualified (~40s) and the
#                scratched (~50s). This is the finishing order.
#   place >= 1   a *paid* position: 1..k where k is the race's number of prize
#                places ('6 prisplacerade' in the prize text). Verified: the
#                count of these equals that number on every race where the two
#                can be compared, or falls short of it where fewer horses
#                finished than there were prize places.
#   place = 0    completed the race, outside the prize places. A real result,
#                with a real km time and a real finishOrder — not missing data.
#   place NULL   either no classified finish (scratched, disqualified) *or* a
#                finisher ATG chose not to enumerate — see below.
#
# **ATG uses two conventions for the unpaid finishers and does not say which.**
# Most races give them `place = 0`; some give them `place` NULL, enumerating
# only the first three. Both were observed on the same day at different tracks.
# In both, `finishOrder` is complete and gap-free over everyone who finished.
# So: **model on `finishOrder`, and treat `place` as a convenience column.**
# A NULL `place` is evidence of nothing on its own; separate the two cases by
# whether `finishOrder` is inside the field or up in a sentinel band.
#
# `galloped` is orthogonal to all of it: a horse can break gait and still be
# placed, so it must never be read as a fourth outcome.
CREATE_START_TABLE = """
    CREATE TABLE IF NOT EXISTS atg.start(
        raceId TEXT,
        startNumber BIGINT,      -- the identity within the race
        postPosition BIGINT,     -- NOT unique: restarts per tier in a handicap
        horseId BIGINT,
        driverId BIGINT,
        trainerId BIGINT,        -- point-in-time: the trainer AS OF this race
        horseAge BIGINT,         -- age at this race; also feeds horse.birthYear
        distance BIGINT,         -- actual, differs per tier in a volte handicap
        shoesReported BOOLEAN,
        shoesFront BOOLEAN,
        shoesBack BOOLEAN,
        shoesFrontChanged BOOLEAN,
        shoesBackChanged BOOLEAN,
        sulkyType TEXT,          -- VA vanlig / AM amerikansk
        sulkyChanged BOOLEAN,
        place BIGINT,            -- PRIZE-MONEY position; 0 and NULL are not places
        finishOrder BIGINT,      -- the finishing order; sentinel bands past it
        kmTimeMs BIGINT,
        kmTimeCode TEXT,         -- 'u', '9', 'kub', … where there is no time
        galloped BOOLEAN,
        disqualified BOOLEAN,
        scratched BOOLEAN,       -- from race.result.scratchings, not from the start
        prizeMoney BIGINT,       -- öre, this race
        finalOdds DOUBLE,
        careerWinnings BIGINT,   -- öre; horse.money AS OF this race
        PRIMARY KEY (raceId, startNumber));
"""
INSERT_START = ('INSERT OR REPLACE INTO atg.start '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                '?, ?, ?, ?, ?);')
START_KEY = (0, 1)  # raceId, startNumber

# `horseId` is the stable ATG id and it is on every start, so there is no
# key-guessing and no name normalisation anywhere in this pipeline — the whole
# identity layer the Finnish archive needs does not exist here.
CREATE_HORSE_TABLE = """
    CREATE TABLE IF NOT EXISTS atg.horse(
        horseId BIGINT,
        name TEXT,
        nationality TEXT,
        sex TEXT,
        color TEXT,
        birthYear BIGINT,        -- derived: mode of year(meetDate) - horseAge
        sireId BIGINT, sireName TEXT,
        damId BIGINT, damName TEXT,
        damsireId BIGINT, damsireName TEXT,
        ownerId BIGINT, ownerName TEXT,
        breederId BIGINT, breederName TEXT,
        homeTrackId BIGINT, homeTrackName TEXT,
        foreignOwned BOOLEAN,
        PRIMARY KEY (horseId));
"""
INSERT_HORSE = ('INSERT OR REPLACE INTO atg.horse '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
HORSE_KEY = (0,)  # horseId

# Drivers and trainers share one table because the same licence-holder is
# frequently both, and their ids appear to draw on one namespace. That is an
# assumption, not a verified fact: `sjoden validate` reports any personId
# carrying two different names, which is what would force a split.
CREATE_PERSON_TABLE = """
    CREATE TABLE IF NOT EXISTS atg.person(
        personId BIGINT,
        firstName TEXT,
        lastName TEXT,
        shortName TEXT,
        location TEXT,
        birth BIGINT,
        homeTrackId BIGINT,
        homeTrackName TEXT,
        license TEXT,
        PRIMARY KEY (personId));
"""
INSERT_PERSON = 'INSERT OR REPLACE INTO atg.person VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);'
PERSON_KEY = (0,)  # personId

# Populated only under `backfill --games`. `distribution` is hundredths of a
# percent — verified live, a V85 race's ten starts summed to 10 001 — which is
# what check 6 of `validate` re-tests over the whole archive.
#
# The endpoint reference also lists a `trend` field beside it. No probed payload
# carried one, so there is no column for it; add one if it reappears.
CREATE_BETDISTRIBUTION_TABLE = """
    CREATE TABLE IF NOT EXISTS atg.bet_distribution(
        raceId TEXT,
        startNumber BIGINT,
        gameType TEXT,           -- V85, V75, vinnare, …
        distribution BIGINT,     -- hundredths of a percent
        PRIMARY KEY (raceId, startNumber, gameType));
"""
INSERT_BETDISTRIBUTION = 'INSERT OR REPLACE INTO atg.bet_distribution VALUES (?, ?, ?, ?);'
BETDISTRIBUTION_KEY = (0, 1, 2)  # raceId, startNumber, gameType

# One row per pool, whether it is the multi-leg game's own pool or one of the
# per-race pools riding inside the same payload — hence `raceId` NULL on the
# former. `payouts` is JSON keyed by number of correct picks.
CREATE_POOL_TABLE = """
    CREATE TABLE IF NOT EXISTS atg.pool(
        poolId TEXT,
        gameId TEXT,
        gameType TEXT,
        meetDate DATE,
        trackId BIGINT,
        raceId TEXT,             -- NULL for a multi-leg game's own pool
        turnover BIGINT,         -- öre
        systemCount BIGINT,
        payouts TEXT,            -- JSON
        PRIMARY KEY (poolId));
"""
INSERT_POOL = 'INSERT OR REPLACE INTO atg.pool VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);'
POOL_KEY = (0,)  # poolId

CREATE_INDEXES = (
    'CREATE INDEX IF NOT EXISTS idx_atg_start_horse ON atg.start(horseId);',
    'CREATE INDEX IF NOT EXISTS idx_atg_start_driver ON atg.start(driverId);',
    'CREATE INDEX IF NOT EXISTS idx_atg_start_trainer ON atg.start(trainerId);',
    'CREATE INDEX IF NOT EXISTS idx_atg_race_date ON atg.race(meetDate);',
)

# ATG gives `age`, not birth year, and age is relative to the racing year — so
# the value is stable across a horse's starts within a year but can disagree
# across years. A mode-vote over every start settles it deterministically.
# Identity never depends on this: `horseId` is authoritative, so a wrong vote
# costs a display column and a pedigree join, never a merged horse.
RECOMPUTE_HORSE_BIRTHYEAR = """
    UPDATE atg.horse h SET birthYear = v.birthYear
    FROM (SELECT horseId, birthYear FROM (
              SELECT s.horseId AS horseId,
                     year(r.meetDate) - s.horseAge AS birthYear,
                     row_number() OVER (
                         PARTITION BY s.horseId
                         ORDER BY count(*) DESC, year(r.meetDate) - s.horseAge DESC
                     ) AS rn
              FROM atg.start s JOIN atg.race r USING (raceId)
              WHERE s.horseAge IS NOT NULL AND r.meetDate IS NOT NULL
              GROUP BY s.horseId, year(r.meetDate) - s.horseAge)
          WHERE rn = 1) v
    WHERE h.horseId = v.horseId;
"""


@contextmanager
def db_ops(db_name):
    # DuckDB will not create a missing parent directory, and the default path
    # is under data/, which a fresh clone does not have.
    os.makedirs(os.path.dirname(db_name) or '.', exist_ok=True)
    conn = duckdb.connect(db_name)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_read(db_name):
    """A read-only connection, for the commands that only ever query.

    Two things `db_ops` does that a reader must not. It `makedirs` the parent
    and hands DuckDB a path, which mints a database for whatever it is given —
    the failure `require_db()` exists to stop. And a read-write connection
    holds the archive against a concurrent `parse` for as long as it is open.
    """
    conn = duckdb.connect(db_name, read_only=True)
    try:
        yield conn
    finally:
        conn.close()


def _insert_many(cur, statement, rows, key):
    """Insert rows, at most one per primary key.

    Every table has a primary key, so the database already refuses a second row
    for the same key across calls, files and runs. This collapses duplicates
    *within* one batch as well, so the winning row is decided here rather than
    by DuckDB's per-statement conflict handling — last row for a key wins,
    matching `INSERT OR REPLACE`. Keep each `*_KEY` in sync with its table's
    `PRIMARY KEY` when columns move.
    """
    unique = {}
    for row in rows:
        unique[tuple(row[i] for i in key)] = row
    # DuckDB's executemany rejects an empty parameter list.
    if unique:
        cur.executemany(statement, list(unique.values()))


def create(conn):
    conn.execute(CREATE_SCHEMA)
    for statement in (CREATE_RACE_TABLE, CREATE_START_TABLE, CREATE_HORSE_TABLE,
                      CREATE_PERSON_TABLE, CREATE_BETDISTRIBUTION_TABLE,
                      CREATE_POOL_TABLE, *CREATE_INDEXES):
        conn.execute(statement)


class ArchiveDb:
    """Batched upserts into the atg schema."""

    def __init__(self, conn):
        self.conn = conn

    def store_races(self, rows):
        _insert_many(self.conn, INSERT_RACE, rows, RACE_KEY)

    def store_starts(self, rows):
        _insert_many(self.conn, INSERT_START, rows, START_KEY)

    def store_horses(self, rows):
        _insert_many(self.conn, INSERT_HORSE, rows, HORSE_KEY)

    def store_persons(self, rows):
        _insert_many(self.conn, INSERT_PERSON, rows, PERSON_KEY)

    def store_bet_distributions(self, rows):
        _insert_many(self.conn, INSERT_BETDISTRIBUTION, rows, BETDISTRIBUTION_KEY)

    def store_pools(self, rows):
        _insert_many(self.conn, INSERT_POOL, rows, POOL_KEY)

    def recompute_birth_years(self):
        self.conn.execute(RECOMPUTE_HORSE_BIRTHYEAR)
