"""Read-only structural checks over the parsed archive.

Every check is a query and nothing here writes, so it is safe to run against an
archive a crawl is still filling. Most report *counts* rather than a verdict:
several of the questions below — whether km times outside the plausible band
are corrupt or just a slow monté field, whether a horse's career earnings jump
because the archive is missing its start abroad — cannot be answered by the
check itself, and a pass/fail would be lying about that.

Two of them exist to settle assumptions the schema rests on rather than to find
bugs: the betting distribution's unit (check 6), and whether `horse.money` is
the pre-race figure (check 8). Both were flagged as unverifiable by probing
alone.
"""
from collections import namedtuple

from .archive_db import db_read

Check = namedtuple('Check', 'title note sql')

CHECKS = (
    Check('Coverage',
          'What the crawl reached. `early` is a race that had not run when it was '
          'fetched; it is retried on the next backfill.',
          """SELECT
                 count(*) FILTER (WHERE endpointType = 'atg_calendar'
                                    AND status = 'done')          AS calendar_days,
                 count(*) FILTER (WHERE endpointType = 'atg_calendar'
                                    AND status <> 'done')         AS calendar_days_missing,
                 count(*) FILTER (WHERE endpointType = 'atg_race'
                                    AND status = 'done')          AS races_fetched,
                 count(*) FILTER (WHERE endpointType = 'atg_race'
                                    AND status = 'early')         AS races_not_yet_run,
                 count(*) FILTER (WHERE endpointType = 'atg_race'
                                    AND status IN ('failed', 'missing')) AS races_unreachable,
                 (SELECT count(*) FROM atg.manifest m
                   LEFT JOIN atg.race r ON r.raceId = m.entityId
                   WHERE m.endpointType = 'atg_race' AND m.status = 'done'
                     AND r.raceId IS NULL)                        AS fetched_but_unparsed
             FROM atg.manifest"""),

    Check('Placings form a dead-heat-aware sequence from 1',
          'Two horses may share a place, so the next place is the previous one plus '
          'the number of horses on it — not the previous one plus 1. Rows here are '
          'races where that does not hold. Small fields are listed alongside.',
          """WITH placed AS (
                 SELECT raceId, place, count(*) AS n
                 FROM atg.start WHERE place >= 1 GROUP BY raceId, place),
             seq AS (
                 SELECT raceId, place, n,
                        1 + coalesce(sum(n) OVER (PARTITION BY raceId ORDER BY place
                                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0)
                            AS expected
                 FROM placed)
             SELECT 'broken sequence' AS problem, raceId, place AS detail
             FROM seq WHERE place <> expected
             UNION ALL
             SELECT 'fewer than 4 runners', r.raceId, count(*) FILTER (WHERE NOT s.scratched)
             FROM atg.race r JOIN atg.start s USING (raceId)
             WHERE r.status = 'results'
             GROUP BY r.raceId HAVING count(*) FILTER (WHERE NOT s.scratched) < 4
             ORDER BY 1, 2 LIMIT 25"""),

    Check('Outcome flags are mutually consistent',
          'The first three must be 0. The fourth is not an error — it counts the '
          'starts ATG left unenumerated under its second convention (see below), and '
          'it is here so that the split between the two stays visible.',
          """WITH paid AS (
                 SELECT raceId, max(finishOrder) AS lastPaid
                 FROM atg.start WHERE place >= 1 GROUP BY raceId)
             SELECT
                 count(*) FILTER (WHERE s.place >= 1
                                    AND (s.scratched OR coalesce(s.disqualified, false)))
                     AS placed_but_flagged,
                 count(*) FILTER (WHERE s.place IS NULL AND s.finishOrder IS NULL
                                    AND NOT s.scratched
                                    AND NOT coalesce(s.disqualified, false))
                     AS no_result_at_all,
                 count(*) FILTER (WHERE s.place = 0 AND s.finishOrder < p.lastPaid)
                     AS unpaid_finisher_ahead_of_a_paid_one,
                 count(*) FILTER (WHERE s.place IS NULL AND NOT s.scratched
                                    AND NOT coalesce(s.disqualified, false)
                                    AND s.finishOrder <= r.fieldSize)
                     AS finishers_left_unenumerated
             FROM atg.start s JOIN atg.race r USING (raceId)
             LEFT JOIN paid p USING (raceId)
             WHERE r.status = 'results'"""),

    Check('Km times fall in the plausible band',
          'Roughly 1:08 to 1:50 per kilometre (68 000-110 000 ms). Outliers are '
          'flagged, never dropped — a monté field or a heavy winter track is slow '
          'for real reasons.',
          """SELECT count(*)                                       AS with_time,
                    count(*) FILTER (WHERE kmTimeMs < 68000)       AS faster_than_1_08,
                    count(*) FILTER (WHERE kmTimeMs > 110000)      AS slower_than_1_50,
                    min(kmTimeMs)                                  AS fastest,
                    max(kmTimeMs)                                  AS slowest
             FROM atg.start WHERE kmTimeMs IS NOT NULL"""),

    Check('Races per year',
          'A step change means a track filter or a calendar vocabulary bug, not a '
          'quiet season.',
          """SELECT year(meetDate)          AS year,
                    count(*)                AS races,
                    count(DISTINCT meetDate) AS race_days,
                    count(DISTINCT trackId)  AS tracks,
                    count(*) FILTER (WHERE sport = 'trot')   AS trot,
                    count(*) FILTER (WHERE sport = 'gallop') AS gallop,
                    count(*) FILTER (WHERE monte)            AS monte
             FROM atg.race GROUP BY 1 ORDER BY 1"""),

    Check('Betting distribution sums to 10 000 per race',
          'This is what establishes the unit: hundredths of a percent. Empty unless '
          'the crawl ran with --games.',
          """WITH totals AS (
                 SELECT raceId, gameType, sum(distribution) AS total
                 FROM atg.bet_distribution GROUP BY 1, 2)
             SELECT count(*)                                            AS race_pools,
                    count(*) FILTER (WHERE total BETWEEN 9900 AND 10100) AS near_10000,
                    min(total)                                          AS lowest,
                    max(total)                                          AS highest
             FROM totals"""),

    Check('Driver and trainer ids share one namespace',
          'atg.person merges the two roles on that assumption. Ids seen in both roles '
          'is the evidence for it; `parse` is what reports an id arriving with two '
          'different names, which is what would force the table to be split.',
          """SELECT (SELECT count(*) FROM atg.person)              AS persons,
                    (SELECT count(*) FROM (
                        SELECT driverId FROM atg.start WHERE driverId IS NOT NULL
                        INTERSECT
                        SELECT trainerId FROM atg.start WHERE trainerId IS NOT NULL))
                                                                   AS ids_in_both_roles,
                    (SELECT count(*) FROM atg.person
                      WHERE firstName IS NULL AND lastName IS NULL) AS nameless"""),

    Check('careerWinnings is the pre-race figure',
          "If it is, a horse's earnings at its next start exceed those at this one by "
          "exactly what it won here. Disagreements are expected wherever the archive "
          "is missing a start in between — a race abroad, or one outside the country "
          "filter — so read the rate, not the residue.",
          """WITH ordered AS (
                 SELECT s.horseId, s.careerWinnings, s.prizeMoney,
                        lead(s.careerWinnings) OVER (PARTITION BY s.horseId
                                                     ORDER BY r.meetDate, r.raceId)
                            AS nextWinnings
                 FROM atg.start s JOIN atg.race r USING (raceId)
                 WHERE s.careerWinnings IS NOT NULL AND NOT s.scratched
                   AND r.status = 'results')
             SELECT count(*)                                                AS pairs,
                    count(*) FILTER (WHERE nextWinnings - careerWinnings = prizeMoney)
                                                                            AS consistent,
                    count(*) FILTER (WHERE nextWinnings < careerWinnings)   AS went_backwards
             FROM ordered
             WHERE nextWinnings IS NOT NULL AND prizeMoney IS NOT NULL"""),
)


def _print(check: Check, columns: list[str], rows: list[tuple]):
    print(f'\n== {check.title}')
    print(f'   {check.note}')
    if not rows:
        print('   (nothing)')
        return
    if len(rows) == 1:
        width = max(len(c) for c in columns)
        for name, value in zip(columns, rows[0]):
            print(f'   {name:<{width}}  {value}')
        return
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows))
              for i, c in enumerate(columns)]
    print('   ' + '  '.join(str(c).ljust(w) for c, w in zip(columns, widths)))
    for row in rows:
        print('   ' + '  '.join(str(v).ljust(w) for v, w in zip(row, widths)))


def validate(args):
    """CLI handler: run every structural check against the archive."""
    with db_read(args.db) as conn:
        for check in CHECKS:
            cur = conn.execute(check.sql)
            columns = [d[0] for d in cur.description]
            _print(check, columns, cur.fetchall())
    print()
