"""Pydantic models for ATG's racinginfo API.

Three payloads are read — the calendar day, the race, and the game — and the
models mirror their nesting: `Calendar` → `CalendarTrack` → `CalendarRace`, and
`Race` → `Start` → `Horse`/`Person`/`StartResult`. They validate what the
crawler archived; nothing here fetches.

Almost every field is Optional on purpose. Historical payloads are markedly
thinner than today's, and the service is not really versioned, so a required
field that one 2013 card omits costs the whole start — with its horse, its
connections and its result — rather than one column. Only the identifying
fields are required.

Two API facts the models are shaped around, both contradicting the endpoint
reference in docs/ and both verified live on 2026-08-26:

1. **The trainer hangs off the horse, not the start.** `start.driver` is a
   sibling of `start.horse`, but the trainer is `start.horse.trainer`.
2. **`place` is the prize-money position, not the finishing position.**
   `finishOrder` is the finishing order. `place >= 1` is a *paid* place, `0`
   means the horse completed the race outside the paid places, and *absent*
   means no classified finish — scratched or disqualified.
"""
from typing import Optional

from pydantic import BaseModel


URL = 'https://www.atg.se/services/racinginfo/v1/api'


class Track(BaseModel):
    id: int
    name: Optional[str] = None
    condition: Optional[str] = None
    countryCode: Optional[str] = None
    sportSystemCode: Optional[str] = None


# --- the calendar day: the discovery entry point ----------------------------

class CalendarRace(BaseModel):
    id: str                     # '2026-08-22_23_1'
    number: Optional[int] = None
    status: Optional[str] = None    # upcoming / bettable / results
    startTime: Optional[str] = None


class CalendarTrack(BaseModel):
    id: int
    name: Optional[str] = None
    countryCode: Optional[str] = None
    sport: Optional[str] = None     # trot / gallop
    startTime: Optional[str] = None
    races: list[CalendarRace] = []


class Calendar(BaseModel):
    date: Optional[str] = None
    tracks: list[CalendarTrack] = []
    # Keyed by game type (V75, V85, dd, vinnare …), each entry naming the race
    # ids it covers. Left as dicts: the crawl only reads `id` and `races`.
    games: dict[str, list[dict]] = {}


# --- the race: card and full-field result in one payload --------------------

class KmTime(BaseModel):
    """Either a time or a code, never both, and sometimes neither.

    A finisher gets minutes/seconds/tenths. A horse that broke or was pulled up
    gets a code instead — 'u' (utgått), '9', 'kub', '7', '8' have all been seen.
    """
    minutes: Optional[int] = None
    seconds: Optional[int] = None
    tenths: Optional[int] = None
    code: Optional[str] = None


class Record(BaseModel):
    code: Optional[str] = None          # 'aM', 'M' — start method + distance class
    startMethod: Optional[str] = None
    distance: Optional[str] = None      # 'medium', 'long', 'short'
    time: Optional[KmTime] = None


class ShoeState(BaseModel):
    hasShoe: Optional[bool] = None
    changed: Optional[bool] = None


class Shoes(BaseModel):
    reported: Optional[bool] = None
    front: Optional[ShoeState] = None
    back: Optional[ShoeState] = None


class SulkyType(BaseModel):
    code: Optional[str] = None          # VA vanlig / AM amerikansk
    text: Optional[str] = None
    changed: Optional[bool] = None


class Sulky(BaseModel):
    reported: Optional[bool] = None
    type: Optional[SulkyType] = None
    colour: Optional[dict] = None


class PedigreeParent(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    nationality: Optional[str] = None


class Pedigree(BaseModel):
    # `grandfather` is the damsire — the mother's sire — not the sire's sire.
    father: Optional[PedigreeParent] = None
    mother: Optional[PedigreeParent] = None
    grandfather: Optional[PedigreeParent] = None


class Party(BaseModel):
    """An owner or a breeder. Sometimes a syndicate, so `id` can be absent."""
    id: Optional[int] = None
    name: Optional[str] = None
    location: Optional[str] = None
    silks: Optional[str] = None


class Person(BaseModel):
    """A driver or a trainer. They appear to share one licence namespace, which
    is why archive_db keeps them in one `atg.person` table — `sjoden validate`
    reports any id carrying two different names, which is what would force a
    split.

    `statistics` rides along and is deliberately not modelled: it is an
    as-of-now snapshot, so persisting it would leak post-race information into
    anything built on this archive.
    """
    id: int
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    shortName: Optional[str] = None
    location: Optional[str] = None
    birth: Optional[int] = None
    homeTrack: Optional[Track] = None
    license: Optional[str] = None
    silks: Optional[str] = None


class Horse(BaseModel):
    """`money` is career earnings in öre, and it is the one embedded aggregate
    kept as a feature-bearing column (`atg.start.careerWinnings`) — on the
    understanding that it is the *pre-race* figure, which `sjoden validate`
    checks rather than assumes. `statistics` is not modelled, for the reason in
    `Person`.
    """
    id: int
    name: Optional[str] = None
    nationality: Optional[str] = None
    age: Optional[int] = None
    sex: Optional[str] = None
    color: Optional[str] = None
    money: Optional[int] = None
    record: Optional[Record] = None
    shoes: Optional[Shoes] = None
    sulky: Optional[Sulky] = None
    owner: Optional[Party] = None
    breeder: Optional[Party] = None
    pedigree: Optional[Pedigree] = None
    trainer: Optional[Person] = None
    homeTrack: Optional[Track] = None
    foreignOwned: Optional[bool] = None


class StartResult(BaseModel):
    """`finishOrder` is the finishing order — 1..N over everyone who completed,
    plus sentinel bands far above the field for the disqualified (~40s) and the
    scratched (~50s). `place` is the *prize-money* position and stops at the
    race's number of prize places, with 0 for a horse that finished outside
    them. Reading `place` as the finishing position silently truncates every
    field at its paid places.

    `scratched` has never been observed set: a scratching shows up only in
    `Race.result.scratchings`, which is why the parser derives it from there.
    """
    place: Optional[int] = None
    finishOrder: Optional[int] = None
    kmTime: Optional[KmTime] = None
    prizeMoney: Optional[int] = None    # öre, this race
    finalOdds: Optional[float] = None
    startNumber: Optional[int] = None
    galloped: Optional[bool] = None
    disqualified: Optional[bool] = None
    scratched: Optional[bool] = None


class Start(BaseModel):
    """`number` is the start number and the identity within the race;
    `postPosition` is *not* unique — in a volte handicap it restarts at 1 for
    each distance tier, and a scratched runner keeps its nominal post while a
    later runner also holds it.
    """
    number: int
    postPosition: Optional[int] = None
    distance: Optional[int] = None      # actual, differs per tier in a handicap
    horse: Horse
    driver: Optional[Person] = None
    result: Optional[StartResult] = None
    # Per-start pools, present only inside a /games/ payload:
    # {'V85': {'betDistribution': 82}, 'vinnare': {'odds': 3547}, …}
    pools: Optional[dict] = None


class RaceResult(BaseModel):
    victoryMargin: Optional[str] = None
    scratchings: list[int] = []         # start numbers


class Race(BaseModel):
    id: str
    number: Optional[int] = None
    date: Optional[str] = None
    name: Optional[str] = None
    distance: Optional[int] = None
    startMethod: Optional[str] = None   # auto / volte
    startTime: Optional[str] = None
    scheduledStartTime: Optional[str] = None
    prize: Optional[str] = None         # free text: 'Pris: 225.000-112.500-… kr'
    terms: list[str] = []
    sport: Optional[str] = None
    track: Optional[Track] = None
    status: Optional[str] = None
    result: Optional[RaceResult] = None
    starts: list[Start] = []
    pools: Optional[dict] = None        # race-level pools, inside /games/ only


# --- the game: pools, turnover and betting distribution ---------------------

class Game(BaseModel):
    """`pools` is keyed by bet type and the shapes differ per type (a marking
    bet has `systemCount` and `payouts`, a win pool does not), so it stays a
    dict — the parser reads the handful of scalars it wants and ignores the
    rest. `races` repeats the full race payload with pools attached.
    """
    id: str
    type: Optional[str] = None
    status: Optional[str] = None
    pools: dict[str, dict] = {}
    races: list[Race] = []
