"""One horse's — or one driver's, or one trainer's — starts, counted every way
the TUI shows them.

The query layer behind `sjoden stats`. Read-only, and deliberately free of any
UI import: every function takes an open connection, so the SQL is testable
against a temporary archive the way `test_validate` tests `validate`'s checks.

Four traps are handled here once rather than in each caller, because each one
is a way the ATG schema reads plausibly and answers wrongly:

- **Scratched starts are excluded everywhere.** A scratching carries shoes, a
  sulky and a post position — entry data is what the card has at that point —
  but the horse did not race. Counting one invents a shoe combination it never
  started in and dilutes every rate. 28,659 rows of it.
- **`scratched` and `disqualified` are the filters, not `finishOrder`.** The
  sentinel bands overlap the real field: disqualified rows run 1-58 and
  non-disqualified rows run 1-57, so no numeric cut separates them. The boolean
  columns are the fact; the number is a sort order.
- **A disqualification is tested before the finishing order.** `finishOrder` on
  a DQ row is a sentinel in the 40s, so a `coalesce(cast(finishOrder …))` prints
  `41` where the result was a disqualification — 3,036 rows of it. See `_PLC`.
- **A NULL `startInterval` is its own bucket.** The column is NULL both for a
  scratching and for a horse's earliest known start. Scratchings are filtered
  above, so what survives means exactly one thing — no earlier start known —
  and bucketing it with a `<= 14` predicate would file every horse's first
  start as a quick turnaround.

Each breakdown is an `Axis`, and the label expression that names its buckets is
written once: the aggregate selects it, and the drill-down behind a clicked
bucket compares against it. See `Axis`.

**Whose starts are being counted is a `Subject`, and almost nothing else
varies.** A horse, a driver and a trainer are the same questions asked of the
same table; a second module per subject would be a second definition of what
'<= 14 days' means, free to drift. See `Subject`.

Run it directly to iterate on the SQL without starting Textual:

    uv run python -m sjoden.stats 'järvsö'
    uv run python -m sjoden.stats 'ohlsson' driver
    uv run python -m sjoden.stats 'untersteiner' trainer
"""
from typing import NamedTuple

from .archive_db import DEFAULT_DB, db_read


# Scratched rows are filtered in every query. coalesce(), not a bare NOT: the
# column is non-NULL on the whole live archive, but a card the parser reads
# without a `result.scratchings` list leaves it NULL, and `NOT NULL` is NULL —
# which would filter out every such row instead of keeping it.
NOT_SCRATCHED = 'NOT coalesce(s.scratched, false)'

# One join for every query, so a label expression can reach race, person and
# horse columns without each Subject having to know which of them it needs.
#
# The two `atg.person` joins are inner, which is a claim about the data: zero of
# the archive's 516,775 starts lack a driver or a trainer row. `atg.horse` is
# LEFT because it is a derived table — `recompute_birth_years` builds it from
# the starts — and a start whose horse never made it in must still be counted.
#
# It costs about twice a bare start-race join: nine breakdowns over the busiest
# driver in the archive (Ulf Ohlsson, 10,416 starts) measure 69 ms against 30,
# and a typical horse is 6. One FROM for every query is worth that.
JOINS = ('FROM atg.start s JOIN atg.race r USING (raceId) '
         'JOIN atg.person d ON d.personId = s.driverId '
         'JOIN atg.person t ON t.personId = s.trainerId '
         'LEFT JOIN atg.horse h ON h.horseId = s.horseId')

# Quoted identifiers so '1st'/'2nd'/'3rd' survive as table headers.
#
# The placings read `finishOrder`, not `place`: `place` is the prize-money
# position, and it is 0 for a start that finished outside the money and NULL for
# a finisher ATG chose not to enumerate, so a top-three count off it undercounts.
# `finishOrder` is the order across the line. Only 9 rows archive-wide are both
# in the first three and disqualified or scratched, so no extra guard is needed
# here — the FROM has already dropped the scratchings.
#
# `gallop` and `dq` are orthogonal to the placings and do not subtract from
# them: a horse can gallop and still win. They overlap the three columns before
# them by design — 'how often did it break' and 'how often was it taken down',
# not two more outcomes.
PLACINGS = """count(*)                                  AS starts,
    count(*) FILTER (WHERE s.finishOrder = 1) AS "1st",
    count(*) FILTER (WHERE s.finishOrder = 2) AS "2nd",
    count(*) FILTER (WHERE s.finishOrder = 3) AS "3rd",
    count(*) FILTER (WHERE s.galloped)        AS gallop,
    count(*) FILTER (WHERE s.disqualified)    AS dq"""

# Barefoot is the fact worth seeing in Swedish harness racing, and ATG reports
# it as two booleans behind a third that says whether anyone reported at all.
# `shoesReported` false is 'not reported' rather than a third kind of shoeing,
# so it stays its own bucket rather than folding into shod: 43,371 starts say
# so. coalesce on the flag, because `||` propagates a NULL and a NULL label is a
# bucket that cannot be opened. See `Axis`.
_SHOES = """CASE WHEN NOT coalesce(s.shoesReported, false) THEN 'unknown'
            ELSE (CASE WHEN s.shoesFront THEN 'shod' ELSE 'barefoot' END) || ' / '
              || (CASE WHEN s.shoesBack  THEN 'shod' ELSE 'barefoot' END) END"""

# ATG's two sulky codes, spelled out. NULL is 'unknown' and stays visible on
# 66,314 starts — mostly older cards that did not carry the field.
_SULKY = """CASE s.sulkyType WHEN 'VA' THEN 'VA standard'
                             WHEN 'AM' THEN 'AM american'
                             ELSE 'unknown' END"""

# Banded, not grouped raw. The archive holds distances from 640 to 4,800 m and
# 2140 alone is half of them, so grouping the column produces a handful of real
# buckets and a long tail of one-row ones. The bands follow the Swedish classes:
# kort around 1,640, medel around 2,140, lång around 2,640.
#
# There is no unknown band because `s.distance` is non-NULL on all 516,775 rows,
# and this is one of the few places that is construction rather than luck: the
# column is the *actual* distance for the tier, which a volte handicap varies
# per horse, so a card that names a race distance always names this one too.
_DISTANCE = """CASE WHEN s.distance <  1800 THEN 'sprint (< 1800 m)'
                    WHEN s.distance <= 2400 THEN 'middle (1800-2400 m)'
                    WHEN s.distance <= 3000 THEN 'long (2401-3000 m)'
                                            ELSE 'stayer (> 3000 m)' END"""

# Ordered short to long, because the label sorts wrong: alphabetically these
# come out long, middle, sprint, stayer.
_DISTANCE_ORDER = """CASE WHEN s.distance <  1800 THEN 0
                          WHEN s.distance <= 2400 THEN 1
                          WHEN s.distance <= 3000 THEN 2
                                                  ELSE 3 END"""

# Off the race, because the start method is a property of the race and not of
# the horse. NULL is 'unknown' and is not noise: it is the 2,677 gallop races,
# which have no trot start method at all.
_METHOD = "coalesce(r.startMethod, 'unknown')"

# The post, 1-16. cast to text so the bucket label is a string like every other
# axis — `Axis.starts` compares the label against a `?` bound from a table cell,
# which is text by the time it comes back out of the UI.
_POST = "coalesce(cast(s.postPosition AS varchar), 'unknown')"

# Numerically, or the labels sort 1, 10, 11, 12, …, 2. 99 puts the 22,024
# unknowns last rather than first, which a NULL would do in DuckDB's default
# ordering.
_POST_ORDER = 'coalesce(s.postPosition, 99)'

# The layoff buckets sort by length, so the label cannot be the sort key: as
# text they come out '15-30', '31-60', '<= 14', '> 60'. A second CASE gives the
# numeric key, and DuckDB takes it in GROUP BY and ORDER BY without it reaching
# the SELECT list, so the sort key never shows up in the table.
#
# `startInterval` is a stored column, filled over the whole table by
# `ArchiveDb.recompute_start_intervals` rather than per record, so there is no
# window function to run here.
#
# **Its NULL means one thing, and only because the FROM already dropped the
# scratchings.** The column is NULL for both a scratching and a horse's earliest
# known start; with 30,811 scratched rows filtered out, the 28,249 that remain
# are first starts — a horse whose real previous run fell before the crawl
# window or abroad included. Never read it as a zero gap.
_LAYOFF = """CASE WHEN s.startInterval IS NULL THEN 'unknown (no earlier start known)'
                  WHEN s.startInterval <= 14   THEN '<= 14 days'
                  WHEN s.startInterval <= 30   THEN '15-30 days'
                  WHEN s.startInterval <= 60   THEN '31-60 days'
                                               ELSE '> 60 days' END"""

_LAYOFF_ORDER = """CASE WHEN s.startInterval IS NULL THEN 4
                        WHEN s.startInterval <= 14   THEN 0
                        WHEN s.startInterval <= 30   THEN 1
                        WHEN s.startInterval <= 60   THEN 2
                                                     ELSE 3 END"""

# The full name rather than the id, which is what a bucket label is for, and
# rather than `shortName`, which is 'Ul Ohl' and collides. Both are non-NULL on
# every start, so no coalesce is needed — see the note on JOINS.
_DRIVER_NAME = "d.firstName || ' ' || d.lastName"
_TRAINER_NAME = "t.firstName || ' ' || t.lastName"

# A disqualification is tested first, and that ordering is the whole point.
#
# `finishOrder` on a disqualified row is a sentinel in the 40s, so reading it
# first prints '40' for 3,304 starts and '41' for 3,036 more, as if they had
# finished fortieth in a field of twelve. `kmTimeCode` carries why — 'u' for
# utesluten, a number for the gallop code — so the cell reads 'dq u' or 'dq 10'.
#
# The finishing arm is bounded to 1-20 rather than taking any positive number:
# the largest field in the archive is 16, and 25 non-scratched non-disqualified
# rows carry a stray sentinel above 20 that no flag explains. They render '-',
# which is honest, rather than a finishing position that never happened.
_PLC = """CASE WHEN s.disqualified THEN coalesce('dq ' || s.kmTimeCode, 'dq')
               WHEN s.finishOrder BETWEEN 1 AND 20 THEN cast(s.finishOrder AS varchar)
               ELSE '-' END"""

# kmTimeMs is milliseconds per kilometre — 67100 is a 1:07,1 kilometre rate.
# Swedish decimal comma and tenths, which is how a result page prints it.
# Integer division throughout, because DuckDB's `/` returns a double and printf
# %d on one truncates unpredictably at the boundary.
#
# `kmTimeCode` is the fallback and it is not a missing value: it says why there
# is no time — 'u' excluded, a digit for the gallop code. 465,087 rows have a
# time, 51,688 have a code instead, and '-' covers neither.
_KM_TIME = ("coalesce(printf('%d:%02d,%d', s.kmTimeMs // 60000, "
            "(s.kmTimeMs % 60000) // 1000, (s.kmTimeMs % 1000) // 100), "
            "s.kmTimeCode, '-')")

# The individual starts behind a bucket. Compact on purpose — the identity of
# the start, the conditions it ran under, how it went, and who was involved.
#
# nullif on the odds because 1.01 is the floor of a real win price, so a stored
# 0 is 'not reported' rather than a price of nothing; printf rather than round(),
# which returns a double and prints 2.6 for a price of 2.60, and which would
# turn the 1,344 NULL rows into the string 'nan'. printf keeps a NULL NULL, so
# those stay blank.
#
# prizeMoney is in öre like every money column in this archive, so it is divided
# by 100 here and named for the unit it ends up in. 0 is what an unplaced start
# won — a fact, not a gap.
START_COLUMNS = f"""r.meetDate AS date, r.trackName AS track, r.raceNumber AS race,
    s.distance AS dist, s.postPosition AS post,
    {_PLC} AS plc,
    {_KM_TIME} AS "km time",
    printf('%.2f', nullif(s.finalOdds, 0)) AS odds,
    s.prizeMoney // 100 AS "prize kr",
    {_DRIVER_NAME} AS driver, {_TRAINER_NAME} AS trainer"""

# Every name search folds accents, and it is not a nicety: the archive is
# Swedish, and typing what a keyboard offers finds nothing without it.
# 'kihlstrom' matches zero rows against `Örjan Kihlström`, and 'jarvso' zero
# against the twelve `Järvsö` horses. strip_accents() on both sides, so a term
# pasted back out of the UI with its diacritics intact still matches.
#
# Neither table has a name index, so this is a full scan — of 28,640 horses and
# 6,888 persons, measured at 5 and 8 ms. Far below anything a typist notices.
_FOLDED = "lower(strip_accents({})) LIKE '%' || lower(strip_accents(?)) || '%'"

# One stage, unlike the equivalent in veikkaus_bot: there is no canonicalisation
# to resolve here, because `horseId` is ATG's own stable identity and one id is
# one horse.
#
# LEFT JOIN because a horse exists in `atg.horse` whether or not any of its
# starts survived the crawl window, and 'found it, nothing here' is an answer
# search has to be able to render. The FILTER inside count() is what keeps that
# honest — count(s.raceId) alone would count a horse's scratchings.
#
# `born` and `sex` are the disambiguators: 21 horses match 'raja', and the year
# is how you tell Raja Piraya from Raja Knight.
SQL_SEARCH_HORSES = f"""
    SELECT h.name AS horse, h.birthYear AS born, h.sex AS sex,
           count(s.raceId) FILTER (WHERE NOT coalesce(s.scratched, false)) AS starts,
           h.horseId AS horseId
    FROM atg.horse h
    LEFT JOIN atg.start s ON s.horseId = h.horseId
    WHERE {_FOLDED.format('h.name')} OR cast(h.horseId AS varchar) = ?
    GROUP BY h.horseId, h.name, h.birthYear, h.sex
    ORDER BY starts DESC, horse
    LIMIT ?
"""

# Drivers and trainers share `atg.person` and differ only in which column of
# `atg.start` points at them, so one template builds both searches.
#
# **The parentheses around the OR are load-bearing.** Without them the scratched
# filter binds to the id arm alone, and a pasted id lists withdrawn entries.
#
# `cast(personId AS varchar) = ?` rather than a LIKE on the number: pasting an
# id means that exact id, and it keeps the parameters (term, term, limit)
# identical to the horse search, so `search()` needs to know nothing about which
# subject it is running.
#
# `location` and `horses` are the disambiguators, the way `born` is for a horse.
# count(DISTINCT horseId), because the id is stable where a name is a string.
_SQL_SEARCH_PERSON = """
    SELECT {name} AS {role}, p.location AS location,
           count(*) AS starts, count(DISTINCT s.horseId) AS horses,
           p.personId AS personId
    FROM atg.person p
    JOIN atg.start s ON s.{column} = p.personId
    WHERE ({folded} OR cast(p.personId AS varchar) = ?)
      AND NOT coalesce(s.scratched, false)
    GROUP BY p.personId, {role}, p.location
    ORDER BY starts DESC, {role}
    LIMIT ?
"""


def _person_search(role: str, column: str) -> str:
    """The driver or the trainer search — same query, different start column."""
    name = "p.firstName || ' ' || p.lastName"
    return _SQL_SEARCH_PERSON.format(
        name=name, role=role, column=column, folded=_FOLDED.format(name))


class Axis(NamedTuple):
    """One way of grouping a subject's starts, and both queries about it.

    `label` is the SQL expression that produces the bucket label, and it is
    written **once**: `breakdown` selects it as the group column and `starts`
    compares a clicked label against it. Two copies would be two definitions of
    what '<= 14 days' means, free to drift apart, and the count a bucket shows
    would stop being the count it opens.

    Every label is non-NULL by construction, and that is a requirement rather
    than a nicety: `NULL = ?` is never true, so a NULL label would be a blank
    bucket row that opens an empty list. Hence the `coalesce` around `_POST` and
    the guard in `_SHOES`, where `||` propagates a NULL.

    `Overall` has no label. It groups nothing and filters nothing, so clicking
    it lists every start the subject has.

    `limit` caps the **breakdown** and nothing else. `starts` stays uncapped, so
    a bucket that is on screen still opens exactly the count it shows; capping
    both would break the one invariant this class exists to hold. A capped axis
    therefore does not sum back to the overall start count, which is why every
    one that has a limit says so in its title.
    """

    title: str
    column: str | None = None
    label: str | None = None
    order: str = 'starts DESC'
    sort: str | None = None      # a numeric sort key, where the label sorts wrong
    limit: int | None = None     # keep only the busiest N buckets

    def breakdown(self, subject: 'Subject') -> str:
        """Starts, placings, gallops and disqualifications per bucket."""
        if self.label is None:
            return f'SELECT {PLACINGS} {subject.frm}'
        group = f'GROUP BY 1, {self.sort}' if self.sort else 'GROUP BY 1'
        cap = f' LIMIT {self.limit}' if self.limit else ''
        return (f'SELECT {self.label} AS "{self.column}", {PLACINGS} '
                f'{subject.frm} {group} ORDER BY {self.order}{cap}')

    def starts(self, subject: 'Subject') -> str:
        """The individual starts behind one bucket, newest first.

        startNumber breaks the ties, because a trainer or a driver can have two
        horses in one race and a two-column order over (date, race) is no longer
        total. A no-op for a horse, which cannot be in one race twice.
        """
        bucket = '' if self.label is None else f'AND {self.label} = ?'
        return (f'SELECT {subject.columns} {subject.frm} {bucket} '
                f'ORDER BY r.meetDate DESC, r.raceNumber DESC, s.startNumber')


# The counterpart-role axis, which is the one breakdown that depends on who is
# being counted: a horse and a trainer want to know which drivers, and a driver
# wants to know which trainers. Asking a driver about drivers returns one row
# equal to Overall.
#
# Capped, and the cap is **named in the title** because these are the axes that
# do not sum back to Overall. The `, name` tiebreak is what makes the cut
# deterministic rather than whichever equal row DuckDB happened to return.
DRIVER_AXIS = Axis('Driver (top 3 by starts)', 'driver', _DRIVER_NAME,
                   'starts DESC, driver', limit=3)
TRAINER_AXIS = Axis('Trainer (top 3 by starts)', 'trainer', _TRAINER_NAME,
                    'starts DESC, trainer', limit=3)

# The axes every subject answers, in the order the UI stacks them. One entry per
# panel: an extra breakdown is one line here and no widget code.
COMMON = (
    Axis('Overall'),
    Axis('Shoes (front / rear)', 'shoes', _SHOES, 'starts DESC, shoes'),
    Axis('Sulky', 'sulky', _SULKY, 'starts DESC, sulky'),
    Axis('Distance', 'distance', _DISTANCE, f'{_DISTANCE_ORDER}, distance',
         _DISTANCE_ORDER),
    Axis('Start method', 'start method', _METHOD, 'starts DESC, "start method"'),
    Axis('Post position', 'post', _POST, _POST_ORDER, _POST_ORDER),
    Axis('Days since previous start', 'days since previous', _LAYOFF,
         _LAYOFF_ORDER, _LAYOFF_ORDER),
    Axis('Track (top 5 by starts)', 'track', 'r.trackName', 'starts DESC, track',
         limit=5),
)

# Constant across subjects, which is what lets the UI build its panels once and
# never rebuild them when the subject changes. See `breakdowns`.
AXIS_COUNT = len(COMMON) + 1


class Subject(NamedTuple):
    """Whose starts a breakdown counts — a horse, a trainer or a driver.

    `COMMON` says how to group a set of `atg.start` rows; a Subject says *which*
    rows, what a drill-down lists behind one bucket, and which counterpart role
    its last axis asks about.

    `frm` is the contract, and it is three things at once. The tables are
    aliased the way every shared expression above spells them — `s`, `r`, `d`,
    `t`, `h`. It takes exactly one `?`, the identity, so `bucket_starts` can
    build the parameters without knowing the subject. And it *ends* in a WHERE,
    so `Axis.starts` can hang `AND <label> = ?` off it.
    """

    name: str        # the noun the UI puts in its prompt and its messages
    frm: str         # JOINS … WHERE <identity> = ? AND NOT scratched
    columns: str     # the drill-down's SELECT list
    search: str      # (display…, identity last), given (term, term, limit)
    partner: Axis    # the counterpart-role axis, which the other two ask about


def _frm(column: str) -> str:
    return f'{JOINS} WHERE {column} = ? AND {NOT_SCRATCHED}'


HORSE = Subject('horse', _frm('s.horseId'), START_COLUMNS,
                SQL_SEARCH_HORSES, DRIVER_AXIS)

# The horse column the horse view has no use for: a person's start list is
# 'which horse ran', so it goes first and the shared columns follow unchanged.
_WITH_HORSE = f'h.name AS horse, {START_COLUMNS}'

TRAINER = Subject('trainer', _frm('s.trainerId'), _WITH_HORSE,
                  _person_search('trainer', 'trainerId'), DRIVER_AXIS)

DRIVER = Subject('driver', _frm('s.driverId'), _WITH_HORSE,
                 _person_search('driver', 'driverId'), TRAINER_AXIS)

# The order `t` cycles through in the UI.
SUBJECTS = (HORSE, TRAINER, DRIVER)

SEARCH_LIMIT = 50


def breakdowns(subject: Subject) -> tuple:
    """The axes for one subject: the shared eight, then its counterpart role.

    Always `AXIS_COUNT` long, whichever subject it is, so the UI's panels are
    built once and only ever retitled.
    """
    return COMMON + (subject.partner,)


def fetch(conn, sql: str, params=()):
    """(column names, rows) — headers off the cursor, as `validate` does.

    Keeping the names on the result rather than in the caller means the SQL is
    the only place a column is named.
    """
    rows = conn.execute(sql, list(params)).fetchall()
    return [d[0] for d in conn.description], rows


def search(conn, subject: Subject, term: str, limit: int = SEARCH_LIMIT):
    """Subjects whose name or identity contains `term`, most starts first.

    The last column is the identity — a horseId or a personId — for the caller
    to pass back to `Axis.breakdown`; the first is what to display for it.
    """
    return fetch(conn, subject.search, [term, term, limit])


def bucket_starts(conn, subject: Subject, axis: Axis, key, label: str | None = None):
    """The individual starts behind one row of `axis.breakdown(subject)`.

    `label` is that row's bucket, and it is ignored for `Overall`, which has no
    label expression to compare it against.
    """
    params = [key] if axis.label is None else [key, label]
    return fetch(conn, axis.starts(subject), params)


if __name__ == '__main__':
    import sys

    term = sys.argv[1] if len(sys.argv) > 1 else ''
    role = sys.argv[2][0].lower() if len(sys.argv) > 2 else 'h'
    who = {'t': TRAINER, 'd': DRIVER}.get(role, HORSE)
    with db_read(DEFAULT_DB) as connection:
        names, hits = search(connection, who, term)
        count = names.index('starts')
        print(f'{len(hits)} {who.name}(s) for {term!r}: '
              + ', '.join(f'{r[0]} ({r[count]})' for r in hits[:5]))
        if hits:
            key = hits[0][-1]
            for one in breakdowns(who):
                columns, rows = fetch(connection, one.breakdown(who), [key])
                print(f'\n=== {key} — {one.title} ===')
                print('  ' + '  '.join(str(c) for c in columns))
                for row in rows:
                    print('  ' + '  '.join('' if v is None else str(v) for v in row))
