# ATG racinginfo API — Endpoint Reference & Swedish Past-Performance Collection Strategy

**Target:** `https://www.atg.se/services/racinginfo/v1/api`
**Purpose:** Build a past-performance dataset of Swedish trotting races for ML / predictive modeling
**Companion document:** `totodatacollectionstrategy.md` (Finnish side, Veikkaus toto-info + Heppa)
**Endpoints probed live:** 2026-08-09 and 2026-08-22

---

# Part I — Endpoint reference

Base URL:

```
https://www.atg.se/services/racinginfo/v1/api/
```

This is the JSON service that powers atg.se itself. It is **publicly reachable without authentication**, but it is *not an officially documented public API* — no published schema, no stability guarantees, and ATG's terms of service apply. For a production system, throttle politely, cache aggressively, identify yourself with a contact-bearing User-Agent, and consider the official partner route via [Swedish Horse Racing](https://www.swedishhorseracing.com/for-partners/offer-and-solution).

Unlike veikkaus.fi, **atg.se's `robots.txt` did not block probing from this environment**, which is why every endpoint below carries a live verdict rather than a community-sourced guess.

## 1. `GET /calendar/day/{YYYY-MM-DD}` — the discovery entry point

Returns everything happening on a given date.

- `date`
- `tracks[]` — `id` (numeric), `name`, `startTime` (ISO 8601), `sport` (`trot`/`gallop`), `countryCode` (SE, NO, DK, FI, FR, US, CA, GB…), `biggestGameType`, `trackChanged`, and `races[]` with `id`, `number`, `status`, `startTime`, `mergedPools`.
- `games` — keyed by game type (`V75`, `V86`, `V85`, `V65`, `V64`, `V5`, `V4`, `V3`, `dd`, `ld`, `trio`, `komb`, `tvilling`, `vp`, `vinnare`), each entry with `id`, constituent race IDs, `returnToPlayer`.

**ID formats** — all other calls are constructed from these:

| Object | Format | Example |
|---|---|---|
| Race | `YYYY-MM-DD_{trackId}_{raceNumber}` | `2026-08-08_33_1` |
| Game | `{gameType}_YYYY-MM-DD_{trackId}_{raceNumber}` | `V75_2012-06-16_11_8` |

**Race `status` values observed:** `upcoming` (future date), `bettable` (open for wagering), `results` (finished).

**Historical reach — bisected 2026-08-22:**

| Date probed | Result |
|---|---|
| 2026-08-23 (future) | ✅ `upcoming` |
| 2017-04-15 | ✅ full results |
| 2014-06-14 | ✅ full results |
| 2012-06-16 | ✅ full results |
| 2011-06-18 | ✅ full results |
| 2010-09-25 | ❌ 404 |
| 2010-06-12 | ❌ 404 |
| 2005-05-21 | ❌ 404 |

**The archive begins somewhere in 2011.** Two separate 2010 dates 404, three separate 2011–2012 dates succeed. Bisect the remaining 2010-09→2011-06 gap in Phase 0 if the exact boundary matters; for a 3–5 year modelling window it does not. Roughly **15 years of history is available**, three times the Veikkaus reach that the Finnish project settled for.

`GET /calendar/month/{YYYY-MM}` **does not exist** (404). Day granularity only — one calendar call per date.

## 2. `GET /races/{raceId}` — full race card *and* full results

Race-level: `id`, `name`, `date`, `number`, `distance` (m), `startMethod` (`volte`/auto), `startTime` + `scheduledStartTime`, `prize` (purse text), `terms[]` (eligibility conditions), `sport`, `track {id, name, condition, countryCode}`, `status`, `mediaId`, `result {victoryMargin, scratchings[]}`.

Each element of `starts[]`:

- **`horse`** — `id` (**stable numeric ATG horse id**), `name`, `nationality`, `age`, `sex`, `color`, `money` (career earnings), `record {code, startMethod, distance, time}`, **`shoes`** (front/back + changed flag), **`sulky`** (`VA` vanlig / `AM` amerikansk, + colour), `owner`, `breeder`, `pedigree` (sire/dam/damsire ids + names), `foreignOwned`, `statistics` (by year and `life`).
- **`driver`** and **`trainer`** — `id`, `firstName`, `lastName`, `shortName`, `location`, `birth`, `homeTrack`, `license`, `silks`, `statistics`.
- **`result`** — `place`, `finishOrder`, `kmTime {minutes, seconds, tenths}` (+ `code` flags), `prizeMoney`, `finalOdds`, `startNumber` (post position), `galloped`, `disqualified`, `scratched`, `lastFiveStarts {averageOdds}`, `videos[]`.

**Verified full-field completeness** (race `2026-08-08_33_1`, 14 starts):

| Outcome | Count | Data present |
|---|---|---|
| Placed 1st–8th | 8 | `place` 1…8, `kmTime` |
| Galloped / disqualified | 5 | `place: 0`, `kmTime` present or coded (`u`, `9`, `kub`) |
| Scratched | 1 | no `place`, no `kmTime` |

Every runner is accounted for, and `place` runs to the back of the field. **This single fact is what makes the Swedish plan simpler than the Finnish one** — see Part II §1.

## 3. `GET /races/{raceId}/extended` — card + each horse's past starts

Everything in §2, plus per horse a `results.records[]` array — previous starts with `date`, `link`, `kmTime`, `odds`, `place`, `galloped`, `disqualified`, `mediaId`, `race {…}`, `track {…}`, `start {distance, postPosition, driver, horse}` — plus `hasMoreRecords`.

**Confirmed limitation:** the nested `start` object in a historical record carries `driver` but **no `trainer`**. Trainer appears only on the current start. Trainer history must come from crawling the historical race cards themselves (Part II §6).

## 4. `GET /races/{raceId}/start/{startNumber}` — single-start drill-down

Same `results[]` past-start records plus richer `statistics` (`winPercentage`, `placePercentage`, `earningsPerStart`, best records by distance/start-method category such as `aK`, `M`). No free-text trip comments were found in any payload probed.

## 5. `GET /games/{gameId}` — pools, turnover, and betting distribution

Top-level `status`, `pools` with `turnover` (öre), `systemCount`, `payouts` keyed by number of correct picks, `addOns`. Embedded `races[]` repeat the race data and add per-start `pools.{gameType}.betDistribution` and `trend`, plus per-race `vinnare`/`plats` pools with their own turnovers.

**Betting distribution survives historically** — verified on two independent past games:

| Game | Status | Turnover | Example `betDistribution` |
|---|---|---|---|
| `V75_2014-06-14_11_7` | `results` | 7 597 862 200 | `302` |
| `V75_2012-06-16_11_8` | `results` | 9 094 677 600 | `1227` |

This is a material difference from Veikkaus, where betting percentages and odds had to be collected *forward* because history was unavailable. On the Swedish side **the market signal is backfillable**.

`betDistribution` appears to be **hundredths of a percent** (`1227` = 12.27%; `302` = 3.02%) — the same convention as Veikkaus's `probable`/`winOdd`. It cannot be per-mille, since 1227‰ is impossible. Confirm in Phase 0 with the obvious check: **the values for one race's non-scratched starts should sum to ≈ 10 000.**

## 6. `GET /horses/{horseId}` — horse master record

Exists. Returns `id`, `name`, `nationality`, `age`, `sex`, `money`, `owner`, `breeder`, `statistics`, `pedigree`, `foreignOwned`.

**It contains no start history and no trainer**, and its `statistics` are as-of-now. It is therefore *not* a shortcut to past performances, and its aggregates must never be used as features (they leak post-race information — the same reasoning that put Heppa's `/horse/{id}/stats` out of scope in the Finnish project). Its legitimate uses are pedigree and identity resolution, and even those are largely redundant because `horse.id` is already embedded in every start.

## 7. Endpoint summary

| Endpoint | Purpose | Verdict |
|---|---|---|
| `/calendar/day/{date}` | Discovery: tracks, races, games | ✅ back to 2011; **crawl driver** |
| `/calendar/month/{ym}` | — | ❌ 404, does not exist |
| `/races/{raceId}` | Card **+ full-field results** | ✅ **the core payload** |
| `/races/{raceId}/extended` | Card + per-horse form lines | ✅ live inference; no trainer |
| `/races/{raceId}/start/{n}` | Single-start drill-down | ✅ rarely needed |
| `/games/{gameId}` | Pools, turnover, `betDistribution` | ✅ **including historically** |
| `/horses/{horseId}` | Horse master record | ✅ pedigree only; as-of-now stats |

---

# Part II — Collection strategy for Swedish past performances

This part is the ATG counterpart to `totodatacollectionstrategy.md`. It deliberately reuses that document's architecture — raw zone first, manifest-driven resumable crawler, DuckDB archive, incremental parse — because those decisions were validated the hard way on the Finnish side and none of the reasoning behind them is Finland-specific. What changes is everything downstream of one structural fact.

## 1. The headline: five Finnish problems that do not exist here

The Finnish project spent most of its effort working around gaps in the Veikkaus payload. Every one of those gaps is filled by ATG's own race endpoint.

| Problem on the Finnish side | Cost there | On the Swedish side |
|---|---|---|
| **Results not on the runner.** `/race/{id}/runners` never carries this race's result; `/race/{id}/results` pays out top three only | 195 690 of 268 864 starts had no placing; required crawling Heppa as a whole second source (~28 000 requests, 16 h) | **Solved.** `/races/{id}` returns `place` for the entire field, plus `galloped`/`disqualified`/`scratched` flags and coded km times for non-finishers |
| **No stable horse ID.** `horse_key = normalize(name)+birthYear`; 182 horses split across 365 keys | Needed Heppa `horseId`, then a 14 050-request registry crawl, then a `canonicalKey` resolution layer | **Solved.** `horse.id` (e.g. `809423`) is on every start. `driver.id` and `trainer.id` likewise |
| **No trainer in history.** `prevStarts` carries `driver` only | `prev_start.coachName` back-filled from `archive.start`, NULL wherever the crawl had not reached | **Partly solved.** `/extended` records have the same gap, but `trainer` **is** on every current start — so crawling cards yields point-in-time trainer for free (§6) |
| **No per-race prize money.** `runner.prize` is career earnings | Only Heppa had it | **Solved.** `result.prizeMoney` is per start, per race |
| **No historical market data.** Odds/percentages forward-collection only | 269 010 odds snapshots and 414 457 betting percentages exist only from the day polling started | **Solved.** `finalOdds` on every start in the race payload; `betDistribution` recoverable from `/games/{id}` back to 2011 (§5, Part I) |

The practical consequence: **there is no Heppa-equivalent second source to build.** Svensk Travsport (`api.travsport.se/webapi/raceinfo/startlists/organisation/TROT/sourceofdata/SPORT/racedayid/{id}`, surfaced in a [community userscript](https://greasyfork.org/en/scripts/477970-travsport-redirect/code)) exists and is the registry of record, but on present evidence it would be a cross-validation source, not a dependency. A guessed `/webapi/horses/...` path 404'd; if a registry crawl is ever wanted, its endpoint set needs its own Phase 0. **Do not build it speculatively** — the Finnish experience was that Heppa became necessary only after the primary source was measured and found short, and here the primary source is not short.

## 2. Why this is worth doing even though Swedish data already flows in

The Finnish document notes that a daily `fi_se` dump already writes to the `main` tables — Sweden is present today *via Veikkaus*, which sells Toto on Swedish cards. That path inherits every Veikkaus limitation above, and one more that the Finnish archive already quantified:

> 5 232 of the 5 235 horses with no registry id race only on the Swedish simulcast and combination-pool cards.

Those horses are unidentifiable and largely unplaced in the current archive, because Heppa is the *Finnish* registry and has no record of them. **An ATG crawl closes that gap directly**: every one of those horses has an ATG `horse.id`, a full finishing position, a per-race prize, and a trainer. This is the single most concrete payoff and it is worth stating first when justifying the work.

There is a symmetric bonus. ATG's calendar carries Finnish races too (track `59` "Finland" appears on 2011, 2012 and 2014 dates; Kaustinen on 2017-04-15). Those give an **independent third opinion** on a slice of Finnish races, which the existing `veikkaus crosscheck` machinery can consume with no new concepts.

## 3. Volume estimate and crawl budget

Observed Swedish tracks per day across the probes: 2 (2012-06-16), 2 (2014-06-14), 3 (2026-08-23), at 7–15 races each. Taking ~2.5 SE track-days/day and ~10 races each:

| Object | Per year | 5 years (2021→) | 15 years (2011→) |
|---|---|---|---|
| SE cards (track-days) | ~900 | ~4 500 | ~13 500 |
| Races | ~9 500 | ~48 000 | ~140 000 |
| Starts | ~105 000 | ~525 000 | ~1 550 000 |
| HTTP requests (calendar + races) | ~9 900 | ~50 000 | ~145 000 |

**The request count is dominated by races, and it is one request per race — not three.** ATG collapses Veikkaus's card→races→runners chain into calendar→race, and the race payload already contains results. A 5-year Swedish backfill is therefore ~50 000 requests: at the Finnish project's 2 s base delay, **~28 hours** — almost exactly the Finnish figure, for twice the races.

**Storage is the one place Sweden is more expensive.** ATG race payloads embed full `statistics` blocks for the horse, driver *and* trainer on every start, so they are fat — budget on the order of 150–300 KB each. Five years is roughly 10 GB raw / 1.5–2 GB gzipped; fifteen years is 25–40 GB raw. Measure a real sample in Phase 0 before committing to the 15-year window, and consider that the marginal modelling value of 2011–2015 data is low compared to its cost.

Add `/games/{id}` only if `betDistribution` is wanted beyond `finalOdds` — roughly one call per multi-leg game (each covering 4–8 races) plus one per remaining race, so at most a ~60% uplift, and far less if only the V-game legs matter.

**Crawl off-peak and newest-first.** Swedish racing runs afternoons and evenings; the small hours put load where the API is not serving live betting. Newest-first banks the most valuable seasons early, exactly as §6 of the Finnish plan argued.

## 4. Architecture: unchanged

Keep the Finnish architecture verbatim — it is source-agnostic and it earned its keep.

```
raw/atg/
  2024-03-15/
    calendar.json.gz                   ← /calendar/day/2024-03-15
    race_2024-03-15_11_1.json.gz       ← /races/{raceId}
    game_V75_2024-03-15_11_5.json.gz   ← /games/{gameId}
```

- **Every successful response written verbatim and gzipped before parsing.** Parser bugs stay recoverable without re-crawling.
- **Manifest-driven and resumable** — `(endpointType, entityId, url, status, httpCode, fetchedAt, parsedAt, rawPath, meetDate)`. Reuse `archive.manifest` with ATG-specific `endpointType` values (`atg_calendar`, `atg_race`, `atg_game`) so that `--refetch-from` (§7d of the Finnish doc) restricts correctly to the calling source, and `parsedAt`-based incremental parse (§7c) composes for free.
- **Enumeration:** insert one `atg_calendar` row per date in the window → parsing each calendar inserts one `atg_race` row per race on an SE track (and `atg_game` rows if enabled) → parsing races upserts the tables in §5.

**One ATG-specific crawl-order rule, inherited from the Finnish §7b lesson.** A race that has not run returns HTTP 200 with `status: "upcoming"`/`"bettable"` and no results — so crawling a card too early would mark it `done` and lose the placings permanently, exactly as premature Veikkaus crawls did. But here the fix is cheap and self-healing rather than dependent on a second source: **have the race parser refuse to stamp a task `done` unless `status == "results"`, leaving it `pending` instead.** Add that guard before the first backfill run. Continue to lag `--to` by two days regardless, as belt and braces.

## 5. Schema

Mirror the Finnish `archive` tables in a sibling `atg` schema inside the same DuckDB file, so the two sources stay comparable and the existing `main`-table dump layer can union them.

```sql
CREATE TABLE atg.race (
  raceId        TEXT PRIMARY KEY,       -- '2026-08-08_33_1'
  meetDate      DATE NOT NULL,
  trackId       INTEGER NOT NULL,
  trackName     TEXT,
  countryCode   TEXT NOT NULL,          -- SE filter decision explicit
  sport         TEXT,                   -- trot / gallop
  raceNumber    INTEGER NOT NULL,
  name          TEXT,
  distance      INTEGER,                -- metres
  startMethod   TEXT,                   -- volte / auto
  trackCondition TEXT,
  startTime     TIMESTAMP,
  scheduledStartTime TIMESTAMP,
  prizeText     TEXT,
  terms         TEXT,                   -- JSON array as-is
  monte         BOOLEAN,                -- derive from name/terms
  status        TEXT,
  victoryMargin TEXT
);

-- One row per (race, horse) = one START = one past-performance line.
CREATE TABLE atg.start (
  raceId        TEXT NOT NULL REFERENCES atg.race(raceId),
  startNumber   INTEGER NOT NULL,       -- post position
  horseId       BIGINT NOT NULL REFERENCES atg.horse(horseId),
  driverId      BIGINT,
  trainerId     BIGINT,                 -- point-in-time: trainer AS OF this race
  distance      INTEGER,                -- actual, for volt handicaps
  shoesFront    BOOLEAN,
  shoesBack     BOOLEAN,
  shoesChanged  BOOLEAN,
  sulkyType     TEXT,                   -- VA / AM
  -- results
  place         INTEGER,                -- 0 = galloped/DQ, NULL = scratched
  finishOrder   INTEGER,
  kmTimeMs      BIGINT,
  kmTimeCode    TEXT,                   -- 'u', '9', 'kub', …
  galloped      BOOLEAN,
  disqualified  BOOLEAN,
  scratched     BOOLEAN,
  prizeMoney    BIGINT,
  finalOdds     DOUBLE,
  careerWinnings BIGINT,                -- horse.money AS OF this race
  PRIMARY KEY (raceId, startNumber)
);

CREATE TABLE atg.horse (
  horseId     BIGINT PRIMARY KEY,       -- stable ATG id — no key-guessing needed
  name        TEXT NOT NULL,
  nationality TEXT,
  sex         TEXT,
  color       TEXT,
  birthYear   INTEGER,                  -- derive from age + meetDate; see §6
  sireId BIGINT, sireName TEXT,
  damId  BIGINT, damName  TEXT,
  damsireId BIGINT, damsireName TEXT,
  ownerId BIGINT, ownerName TEXT,
  breederId BIGINT, breederName TEXT,
  foreignOwned BOOLEAN
);

CREATE TABLE atg.person (                -- drivers and trainers share a namespace
  personId    BIGINT PRIMARY KEY,
  firstName TEXT, lastName TEXT, shortName TEXT,
  location TEXT, birth INTEGER, homeTrack TEXT, license TEXT
);

CREATE TABLE atg.bet_distribution (
  raceId      TEXT NOT NULL,
  startNumber INTEGER NOT NULL,
  gameType    TEXT NOT NULL,            -- V75, vinnare, …
  distribution INTEGER,                 -- hundredths of a percent — verify (§5 Part I)
  trend       INTEGER,
  PRIMARY KEY (raceId, startNumber, gameType)
);

CREATE TABLE atg.pool (
  gameId      TEXT PRIMARY KEY,
  gameType    TEXT, meetDate DATE, trackId INTEGER,
  turnover    BIGINT,                   -- öre
  systemCount BIGINT,
  payouts     TEXT                      -- JSON, keyed by correct-pick count
);

CREATE INDEX idx_atg_start_horse ON atg.start(horseId);
CREATE INDEX idx_atg_race_date   ON atg.race(meetDate);
```

Note what is **absent** relative to the Finnish schema and deliberately so: no `prev_start` table, no registry table, no `canonicalKey`, no `resultSource`. `atg.start` is authoritative on its own, so "past performances of horse X before date D" is one indexed join of `atg.start ⋈ atg.race` — the same query shape the Finnish pipeline reaches through two merged sources.

`atg.person` merges drivers and trainers into one table because the same licence-holder is frequently both, and ATG's `driver.id` / `trainer.id` appear to draw on one licence namespace. **Verify that assumption in Phase 0** — find a person who both drives and trains on the same card and check the ids match. If they do not, split the table.

## 6. Point-in-time correctness — the ATG-specific traps

The Finnish document's leakage discipline applies unchanged, and ATG adds its own hazards precisely *because* it is generous with denormalised data.

**The embedded `statistics` blocks are as-of-now, not as-of-race-day.** Every start carries a full horse/driver/trainer statistics block, and it is tempting to use them directly as features. They are almost certainly served from the current registry state, not snapshotted at race time — the same trap as Heppa's `/horse/{id}/stats`. **Do not persist them as features.** Derive rolling form, win rates and earnings-per-start from `atg.start` with an explicit `< race.meetDate` predicate. The one field worth keeping is `horse.money` as `careerWinnings`, and only if a spot check confirms it is the pre-race figure (the Finnish `careerWinnings` is; ATG's needs its own check — compare a horse's `money` across two consecutive races against the `prizeMoney` it won in between).

**Trainer history is a crawl-depth function, exactly as on the Finnish side.** Because `/extended` records carry no trainer, the only point-in-time trainer is the `trainer` object on the current start of each historical race — which the crawl captures as `atg.start.trainerId`. Trainer-change features are then a self-join over `atg.start` ordered by date. Coverage begins where the crawl begins; treat "trainer unknown" as an explicit category rather than dropping rows, and never back-fill a horse's earlier starts from a later race's trainer — that silently rewrites history whenever a horse changes yards.

**`birthYear` must be derived, and derived carefully.** ATG gives `age`, not birth year. Age is relative to the racing year, so compute `birthYear = year(meetDate) - age` and expect the value to be stable across a horse's starts within a year but to need a mode-vote across years. Since `horseId` is authoritative this only affects display and pedigree joins, never identity — which is the whole reason the Finnish `horse_key` problem does not recur.

**`place: 0` is three different things.** Galloped, disqualified, and other non-completions all collapse to `0`. Keep `galloped`, `disqualified` and `kmTimeCode` as separate columns and never treat `0` as a finishing position. For modelling, gait breaks are informative outcomes, not missing data.

**Scratchings appear in two places** — `result.scratched` on the start and `race.result.scratchings[]`. Reconcile them and keep scratched runners as rows; field size and post positions depend on knowing who was declared.

**km times need normalisation before use**, per the roadmap document: regress on track, distance band, start method, season and class, and model the residual.

**Dead heats.** Two horses can share a `place`. Assert per race that non-zero places are unique *or* explained by a shared value, rather than asserting strict uniqueness — a validation check written the naive way will fire constantly.

## 7. Incremental cycle

Mirror the Finnish cycle, pinning `--from` and lagging `--to`:

```bash
LAST=$(date -d '2 days ago' +%F)
uv run atg backfill --from 2021-01-01 --to "$LAST"
uv run atg parse
```

With the `status == "results"` guard from §4 in place, a prematurely crawled race stays `pending` and self-heals on the next run — so the Swedish source gets the property that Veikkaus lacked and Heppa had to supply.

Two things worth collecting **forward only**, which no backfill can recover:

- **Pre-race entry state.** Fetch upcoming cards the morning of, and again shortly before post time, to capture declared shoeing and sulky changes, driver changes and late scratchings as they were known pre-race. This is the state your inference pipeline will see in production, so training on it keeps train/serve symmetry.
- **Odds trajectories.** `finalOdds` and closing `betDistribution` backfill fine, but their *movement* does not. If drift features are wanted, poll the win pool at roughly T−30, T−10 and T−2 minutes.

## 8. Validation

Structural checks after backfill, in the spirit of the Finnish §8:

1. Every calendar day in the window has at least one `atg_calendar` task `done`; every SE race on those calendars has a `race` row.
2. Every `status == 'results'` race has ≥ 4 starts, and its non-zero places form a gap-free sequence from 1 (allowing shared values for dead heats).
3. Places, `galloped`, `disqualified` and `scratched` are mutually consistent: `place > 0` implies none of the flags.
4. km times fall within 1:08–1:50 per km; flag outliers rather than dropping them.
5. `betDistribution` values per race sum to ≈ 10 000 — this simultaneously validates the unit assumption from Part I §5.
6. Per-year race counts are stable and near the ~9 500 estimate; a step change means a track vocabulary or filter bug, which is precisely how the Finnish `Hr`/`Hr2` gap was eventually caught.
7. **Cross-source check against the existing archive.** Swedish cards already present via Veikkaus should agree with ATG on placings, km times and final odds where both have them. Expect the same benign disagreement classes the Finnish crosscheck found: post-race disqualifications where one source records payout order and the other official order, km times differing by a tenth on rounding convention, and late scratchings recorded by only one side. Expect name mismatches too — country tags are handled differently between sources, which is exactly why this bridge, like the Heppa one, must be positional (`meetDate`, track, race number, start number) and never name-based.

## 9. Phased plan

| Phase | Work | Effort |
|---|---|---|
| **0 — Probe** | Confirm the 2010→2011 boundary; measure real payload sizes and SE cards/week over four sampled weeks; verify `betDistribution` units sum to 10 000; verify the driver/trainer id namespace; verify `horse.money` is pre-race; record rate-limit behaviour at 1–2 s | ½ day |
| **1 — Build** | ATG fetcher + parsers reusing the existing manifest/raw-zone/incremental-parse machinery; the `status == 'results'` stamping guard; unit tests for km-time codes, `place: 0` semantics, dead heats, `birthYear` derivation | 2–3 days |
| **2 — Backfill** | Newest→oldest, 2021-01-01 → now, SE tracks. ~50 000 requests, ~28 h at 2 s. Then §8 checks | ~1–2 days wall-clock |
| **2b — Games** | `/games/{id}` for `betDistribution` and pool turnover, if the market signal is wanted beyond `finalOdds` | ~½–1 day wall-clock |
| **2c — Extend** | Optionally deepen to 2011 once payload-size measurements are in and the modelling value is judged worth the storage | ~2–3 days wall-clock |
| **3 — Incremental** | Daily cron; forward-only pre-race snapshots and optional odds polling | ½ day |
| **4 — Merge & features** | Union SE into the feature layer; reconcile with the Veikkaus-sourced Swedish rows; close the 5 232 unidentified-horse gap (§2) | ongoing |

Phases 2b and 2c are genuinely optional and should be decided on measurements from Phase 0, not up front.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Unofficial API restructured or closed to scraping | Raw zone + early backfill, exactly as for the 2027 Finnish licensing risk. Svensk Travsport is the fallback registry, and the official partner route exists if the project becomes commercial |
| ToS friction — public backend, unofficial use | Single-threaded ≥ 1–2 s with jitter, contact-bearing User-Agent, off-peak, exponential backoff and a circuit breaker. Ask ATG for sanctioned access if this becomes more than research |
| Silent schema drift (`/v1/` is not really versioned) | Schema-validate on ingest, log unknown fields loudly, keep raw for re-parse. Expect historical payloads to be *thinner* than live ones, as on the Finnish side |
| Storage growth from fat payloads | Measure in Phase 0; prefer the 5-year window; gzip everything; consider dropping the embedded `statistics` blocks at parse time since they are unusable as features anyway |
| Treating embedded `statistics` as point-in-time | §6. The single highest-value rule in this document |
| Assuming ATG and Veikkaus agree | Positional bridge only; `crosscheck` before merging; coalesce rather than overwrite so disagreements stay visible |

---

*Part I is live-verified. Part II is a plan: its architecture is transplanted from a validated Finnish implementation, its ATG-specific claims rest on the Part I probes, and the items flagged for Phase 0 are exactly those that could not be settled by probing alone.*
