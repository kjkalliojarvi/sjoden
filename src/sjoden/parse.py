"""Walk the raw zone into the `atg` tables. Idempotent.

Fetching and parsing are separate halves of the pipeline on purpose: a parsing
bug never costs a re-crawl, and the raw responses survive even if the endpoint
is closed to scraping. Every upsert is `INSERT OR REPLACE`, so re-running this
over the same payloads changes nothing.

By default only payloads fetched since they were last loaded — a nightly cycle
adds a dozen race days and should not pay for re-validating the whole archive.
`--full` reloads everything, which is what a change to any `*_record()` builder
or scalar parser requires: this tracks what has been *parsed*, not what the
parser would now produce.
"""
import json

from pydantic import ValidationError

from . import archive_db
from .archive_db import ArchiveDb, db_ops
from .crawler import Manifest
from .fetcher import read_raw
from .models import Game, Horse, KmTime, Person, Race, Start

FLUSH = 5000

# Swedish spells ridden trot `monté`, and the accent is what makes matching on
# it safe — a bare 'monte' would catch sponsor and horse names.
MONTE_MARKER = 'monté'


def km_time_ms(km_time: KmTime | None) -> int | None:
    """Milliseconds per kilometre, or None where the payload gave a code.

    ATG sends minutes/seconds/tenths as separate integers, so 1:11.8 arrives as
    {minutes: 1, seconds: 11, tenths: 8} = 71 800 ms.
    """
    if km_time is None or km_time.seconds is None:
        return None
    minutes = km_time.minutes or 0
    tenths = km_time.tenths or 0
    return (minutes * 60 + km_time.seconds) * 1000 + tenths * 100


def is_monte(race: Race) -> bool:
    text = ' '.join([race.name or '', *(race.terms or [])]).lower()
    return MONTE_MARKER in text


def meet_date(race: Race) -> str | None:
    """The race's own date, falling back to the one inside its id.

    `2026-08-08_33_1` is date_track_number, so the id always carries it even on
    a payload thin enough to omit the field.
    """
    if race.date:
        return race.date
    parts = race.id.split('_')
    return parts[0] if parts else None


def race_record(race: Race) -> tuple:
    track = race.track
    result = race.result
    return (race.id,
            meet_date(race),
            track.id if track else None,
            track.name if track else None,
            track.condition if track else None,
            track.countryCode if track else None,
            race.sport,
            race.number,
            race.name,
            race.distance,
            race.startMethod,
            race.startTime,
            race.scheduledStartTime,
            race.prize,
            json.dumps(race.terms, ensure_ascii=False),
            is_monte(race),
            race.status,
            result.victoryMargin if result else None,
            json.dumps(result.scratchings if result else []),
            len(race.starts))


def scratched_numbers(race: Race) -> set[int]:
    """Which start numbers were withdrawn.

    `result.scratched` on the start has never been observed set — a scratching
    shows up only in `race.result.scratchings` — so reading the start alone
    loses every one of them. Both are consulted anyway; the flag costs nothing
    and would matter the day it starts arriving.
    """
    listed = set(race.result.scratchings) if race.result else set()
    flagged = {s.number for s in race.starts
               if s.result is not None and s.result.scratched}
    return listed | flagged


def start_record(race: Race, start: Start, scratchings: set[int]) -> tuple:
    horse = start.horse
    result = start.result
    shoes = horse.shoes
    front = shoes.front if shoes else None
    back = shoes.back if shoes else None
    sulky = horse.sulky.type if horse.sulky else None
    km = result.kmTime if result else None
    return (race.id,
            start.number,
            start.postPosition,
            horse.id,
            start.driver.id if start.driver else None,
            horse.trainer.id if horse.trainer else None,
            horse.age,
            start.distance,
            shoes.reported if shoes else None,
            front.hasShoe if front else None,
            back.hasShoe if back else None,
            front.changed if front else None,
            back.changed if back else None,
            sulky.code if sulky else None,
            sulky.changed if sulky else None,
            result.place if result else None,
            result.finishOrder if result else None,
            km_time_ms(km),
            km.code if km else None,
            result.galloped if result else None,
            result.disqualified if result else None,
            start.number in scratchings,
            result.prizeMoney if result else None,
            result.finalOdds if result else None,
            horse.money)


def horse_record(horse: Horse) -> tuple:
    pedigree = horse.pedigree
    sire = pedigree.father if pedigree else None
    dam = pedigree.mother if pedigree else None
    # `grandfather` is the damsire — the mother's sire — not the sire's sire.
    damsire = pedigree.grandfather if pedigree else None
    return (horse.id,
            horse.name,
            horse.nationality,
            horse.sex,
            horse.color,
            None,                      # birthYear: recomputed after loading
            sire.id if sire else None,
            sire.name if sire else None,
            dam.id if dam else None,
            dam.name if dam else None,
            damsire.id if damsire else None,
            damsire.name if damsire else None,
            horse.owner.id if horse.owner else None,
            horse.owner.name if horse.owner else None,
            horse.breeder.id if horse.breeder else None,
            horse.breeder.name if horse.breeder else None,
            horse.homeTrack.id if horse.homeTrack else None,
            horse.homeTrack.name if horse.homeTrack else None,
            horse.foreignOwned)


def person_name(person: Person) -> str:
    return ' '.join(filter(None, (person.firstName, person.lastName)))


def person_record(person: Person) -> tuple:
    return (person.id,
            person.firstName,
            person.lastName,
            person.shortName,
            person.location,
            person.birth,
            person.homeTrack.id if person.homeTrack else None,
            person.homeTrack.name if person.homeTrack else None,
            person.license)


def _flush(store, rows, force=False):
    if rows and (force or len(rows) >= FLUSH):
        store(rows)
        rows.clear()


def _read(raw_root: str, task):
    payload = read_raw(raw_root, task.rawPath)
    if payload is None:
        print(f'missing raw file: {task.rawPath}')
    return payload


def is_race_card(payload: dict) -> bool:
    """Whether this payload is a race, rather than a market ATG files as one.

    A start whose horse carries no `horse.id` is not a runner from the racing
    registry, and one is enough to disqualify the payload. Two shapes reach
    here, both under trackId 47 and neither of them a race:

    - an equestrian card — show jumping, tagged `sport: 'gallop'` — where no
      horse is registered and there is nothing raceworthy at all;
    - an ante-post market, which lists a real field beside a synthetic 'Övriga
      Hästar' bucket for the rest of it. These carry `finalOdds` and nothing
      else: no place, no finishing order, no time, no prize money. The race
      they price runs at a real track and is already in the archive under its
      own id, so keeping the market would duplicate every horse in it and pin
      a `careerWinnings` figure that is weeks stale onto a start that never
      happened.

    Excluding these is also what keeps the caller's ValidationError branch
    meaningful: what remains there is a shape nobody has seen before.
    """
    return all((s.get('horse') or {}).get('id') is not None
               for s in payload.get('starts') or [])


def _each_payload(manifest: Manifest, raw_root: str, endpoint_type: str,
                  unparsed_only: bool = True):
    """Yield (task, payload) for the archived responses of one endpoint type.

    Stamping is the caller's job: it appends a task to its own `consumed` list
    once the payload has been loaded — or deliberately excluded — so that a
    payload the parser could not handle stays unparsed and is retried on the
    next run. See Manifest.mark_parsed.
    """
    for task in manifest.done(endpoint_type, unparsed_only):
        payload = _read(raw_root, task)
        if payload is None:
            continue
        yield task, payload


def _parse_races(manifest: Manifest, raw_root: str, db: ArchiveDb,
                 unparsed_only: bool) -> tuple[int, int]:
    """The core phase: one payload is a card *and* its full-field result."""
    races, starts, horses, persons = [], [], [], []
    consumed = []
    n_races = n_starts = 0
    n_not_a_race = 0
    # atg.person merges drivers and trainers on the assumption that both roles
    # draw on one licence namespace. This is where that assumption is actually
    # tested: the table's primary key would swallow a collision silently, so
    # every id is checked against the name it was last seen with, across both
    # roles and every payload in the run. A non-empty report means the table
    # has to be split by role.
    seen_names: dict[int, str] = {}
    clashes: dict[int, set[str]] = {}
    for task, payload in _each_payload(manifest, raw_root, 'atg_race',
                                       unparsed_only):
        if not is_race_card(payload):
            # Excluded on purpose, so it is stamped and not reconsidered.
            n_not_a_race += 1
            consumed.append(task)
            continue
        try:
            race = Race.model_validate(payload)
        except ValidationError as e:
            print(f'{task.entityId}: {e.error_count()} validation errors, skipped')
            continue
        races.append(race_record(race))
        n_races += 1
        scratchings = scratched_numbers(race)
        for start in race.starts:
            starts.append(start_record(race, start, scratchings))
            horses.append(horse_record(start.horse))
            for person in (start.driver, start.horse.trainer):
                if person is None:
                    continue
                persons.append(person_record(person))
                name = person_name(person)
                if name and seen_names.setdefault(person.id, name) != name:
                    clashes.setdefault(person.id, {seen_names[person.id]}).add(name)
            n_starts += 1
        consumed.append(task)
        _flush(db.store_races, races)
        _flush(db.store_starts, starts)
        _flush(db.store_horses, horses)
        _flush(db.store_persons, persons)
    # Horses and persons before the starts that reference them is not required
    # — DuckDB does not enforce the relationship — but it keeps the archive
    # readable at every point a crash could leave it.
    _flush(db.store_horses, horses, force=True)
    _flush(db.store_persons, persons, force=True)
    _flush(db.store_races, races, force=True)
    _flush(db.store_starts, starts, force=True)
    manifest.mark_parsed(consumed)
    if n_not_a_race:
        print(f'{n_not_a_race} payloads excluded — not race cards '
              '(show jumping, ante-post markets)')
    if clashes:
        print(f'{len(clashes)} person ids carry more than one name — '
              'atg.person may need splitting by role:')
        for person_id, names in list(clashes.items())[:10]:
            print(f'  {person_id}: {", ".join(sorted(names))}')
    return n_races, n_starts


def _pool_record(pool: dict, game_id: str, game_type: str, task, race_id: str | None):
    """One pool row, or None where the payload only left a stub.

    A multi-leg game's legs each carry an entry for the game's own bet type
    with no id and no turnover — the leg's status, nothing more. Manufacturing
    an id for it (`{gameType}_{raceId}`) reproduces the id of the game pool
    itself, whose turnover it then overwrites with NULLs. So a pool without an
    id is not a pool.
    """
    if not pool.get('id'):
        return None
    payouts = pool.get('payouts')
    return (pool['id'],
            game_id,
            game_type,
            task.meetDate,
            task.trackId,
            race_id,
            pool.get('turnover'),
            pool.get('systemCount'),
            json.dumps(payouts, ensure_ascii=False) if payouts is not None else None)


def _parse_games(manifest: Manifest, raw_root: str, db: ArchiveDb,
                 unparsed_only: bool) -> tuple[int, int]:
    """Pools, turnover and the betting distribution — crawled only with --games.

    This is the market signal that no forward-only collection could recover:
    unlike the Finnish side, ATG serves `betDistribution` for finished games as
    far back as the calendar reaches.
    """
    pools, distributions = [], []
    consumed = []
    n_pools = n_dist = 0
    for task, payload in _each_payload(manifest, raw_root, 'atg_game',
                                       unparsed_only):
        try:
            game = Game.model_validate(payload)
        except ValidationError as e:
            print(f'{task.entityId}: {e.error_count()} validation errors, skipped')
            continue
        candidates = [(game_type, pool, None) for game_type, pool in game.pools.items()]
        candidates += [(game_type, pool, race.id) for race in game.races
                       for game_type, pool in (race.pools or {}).items()]
        for game_type, pool, race_id in candidates:
            record = _pool_record(pool, game.id, game_type, task, race_id)
            if record is not None:
                pools.append(record)
                n_pools += 1
        for race in game.races:
            for start in race.starts:
                for game_type, start_pool in (start.pools or {}).items():
                    if 'betDistribution' not in start_pool:
                        continue
                    distributions.append((race.id, start.number, game_type,
                                          start_pool['betDistribution']))
                    n_dist += 1
        consumed.append(task)
        _flush(db.store_pools, pools)
        _flush(db.store_bet_distributions, distributions)
    _flush(db.store_pools, pools, force=True)
    _flush(db.store_bet_distributions, distributions, force=True)
    manifest.mark_parsed(consumed)
    return n_pools, n_dist


def parse_all(db_name: str, raw_root: str, full: bool = False) -> dict:
    """Load the raw zone into the atg tables. Idempotent.

    The recompute pass always runs over the whole table. It costs a fraction of
    a second, and being whole-table is exactly what makes it deterministic.
    """
    unparsed = not full
    with db_ops(db_name) as conn:
        manifest = Manifest(conn)
        manifest.create()
        archive_db.create(conn)
        db = ArchiveDb(conn)
        races, starts = _parse_races(manifest, raw_root, db, unparsed)
        pools, distributions = _parse_games(manifest, raw_root, db, unparsed)
        db.recompute_birth_years()
    return {'races': races, 'starts': starts,
            'pools': pools, 'bet distributions': distributions}


def parse(args):
    """CLI handler: parse the raw zone into the atg tables."""
    counts = parse_all(args.db, args.raw, getattr(args, 'full', False))
    for name, count in counts.items():
        print(f'{count} {name} parsed into {args.db}.')
    if not any(counts.values()):
        print('Nothing new to parse. Use --full to reload the whole raw zone.')
