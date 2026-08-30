# sjoden

Builds a past-performance dataset of Swedish harness racing — horses, drivers and
trainers — from ATG's `racinginfo` API (`https://www.atg.se/services/racinginfo/v1/api`),
crawling it into a gzipped raw archive and parsing that into DuckDB.

`TARGET.md` states the goal; `docs/atgracinginfoapireference.md` is the collection
strategy, and `docs/harnesswinprobabilityroadmap.md` the modelling roadmap this archive
is meant to feed. **Read "API facts" below before changing anything that touches the
API** — it records where the live service contradicts the reference document.

Requires Python >= 3.14, managed with `uv`.

## Commands

```bash
uv sync                                          # install deps into .venv (make install)
uv run sjoden backfill --from 2021-01-01         # crawl into data/raw/ (resumable)
uv run sjoden backfill --from D --to D --games   # ...also the pools and bet distribution
uv run sjoden parse                              # raw archive -> atg.* tables
uv run sjoden status                             # crawl manifest progress
uv run sjoden validate                           # structural checks over the archive
uv run pytest tests                              # run tests (make run-tests)
```

The entry point is `sjoden` (`[project.scripts]` → `sjoden.__main__:sjoden`).
`SJODEN_CONTACT` sets the contact address in the `User-Agent`.

Incremental cycle — safe to run daily, and safe to kill at any point:

```bash
uv run sjoden backfill --from 2021-01-01 && uv run sjoden parse
```

## Architecture

One pipeline in two halves that never run together: **fetching and parsing are
deliberately separate**, so a parsing bug never costs a re-crawl and the raw responses
survive even if the endpoint is closed to scraping.

- `fetcher.py` — `Fetcher` does one request at a time: 2 s base delay ±30 % jitter,
  retries 429/5xx/timeouts through 30 s → 2 min → 10 min, and raises `CircuitOpen` after
  5 consecutive failures. 400/404 mean "nothing there", not an error. `store_raw()` /
  `read_raw()` are the gzipped raw zone, laid out as `data/raw/{meetDate}/…`.
- `crawler.py` — `Manifest` is the fetch ledger (`atg.manifest`, PK
  `(endpointType, entityId)`); `enqueue` uses `INSERT OR IGNORE` so re-running a window
  never re-fetches finished work. `next_pending()` orders by `meetDate DESC, stage ASC`
  — newest season first, calendars before the races they name. `crawl()` takes the crawl
  graph as an `expander` callable and knows nothing about the API; `expand()` is that
  graph. Three endpoint types: `atg_calendar` → `atg_race` (+ `atg_game` with `--games`).
- `models.py` — Pydantic models for the three payloads. Almost every field is Optional
  on purpose: historical payloads are thinner than today's, and a required field one
  2013 card omits costs the whole start rather than one column.
- `archive_db.py` — the `atg` schema, its upserts, and the shared DuckDB helpers
  (`db_ops`, `db_read`, `_insert_many`). Tables: `race`, `start`, `horse`, `person`,
  `bet_distribution`, `pool`, `manifest`.
- `parse.py` — walks the manifest's `done` rows, reads the raw files, upserts.
  Idempotent, and incremental by default (`--full` reloads everything, which is what a
  change to any `*_record()` builder or scalar parser requires).
- `validate.py` — read-only structural checks. Most report counts rather than a verdict,
  because several of the questions cannot be answered by the check itself.

### The `early` status — why a premature crawl is not destructive here

A race that has not run answers **HTTP 200** with `status: "upcoming"`/`"bettable"` and
no results. Marking that `done` would retire the task forever and lose the placings
permanently, which is exactly what premature crawls cost the Finnish archive.

So `crawl()` takes an `is_final()` predicate. A race that is not yet `results` is stored
(it is a legitimate pre-race snapshot) but marked **`early`**, and every `backfill` resets
`early` → `pending` before it starts. The strategy document suggests leaving such rows
`pending` instead; that spins, because `next_pending()` hands the same row straight back
within the run. A distinct status self-heals across runs without the loop — so no `--to`
lag is needed, though lagging two days costs nothing.

## API facts the schema is shaped around

All verified live on 2026-08-26 and again over 931 parsed starts. Several contradict
`docs/atgracinginfoapireference.md`, which was written from a narrower probe.

- **`finishOrder` is the finishing order; `place` is the prize-money position.** They are
  not the same column twice, and confusing them is the easiest way to build a wrong
  target variable. `finishOrder` runs 1..N gap-free over everyone who completed, with
  sentinel bands far above the field for the disqualified (~40s) and the scratched
  (~50s). `place` stops at the race's number of prize places — the count matches the
  `N prisplacerade` in the prize text wherever both are present.
- **ATG uses two conventions for the unpaid finishers, and does not say which.** Most
  races give them `place = 0`; some enumerate only the first three and leave the rest
  NULL. Both were observed on the same day at different tracks. `finishOrder` is complete
  in both, which is why it is the column to model on and `place` is a convenience.
- **The trainer hangs off the horse**: `start.horse.trainer`, not `start.trainer`. Only
  `driver` is a sibling of `horse`.
- **`result.scratched` has never been observed set.** A scratching appears only in
  `race.result.scratchings[]`, a list of start numbers. Reading the start alone loses
  every withdrawal. Scratched runners are kept as rows — field size and everyone else's
  post positions depend on knowing who was declared.
- **`postPosition` is not unique within a race.** In a volte handicap it restarts at 1 for
  each distance tier (1–9 at 2140 m, 1–5 at 2160 m in one observed race) and a scratched
  runner keeps its nominal post while a later runner also holds it. The identity is
  `(raceId, startNumber)`; per-start `distance` carries the tier.
- **`galloped` is orthogonal to the outcome.** A horse can break gait and still be placed,
  so it is never a fourth placing.
- **Km times arrive as three integers** — `{minutes, seconds, tenths}` — or as a code
  (`u`, `9`, `kub`, `vänd`) where there is no time. Never as a string.
- **`betDistribution` is hundredths of a percent.** Verified: 21 of 21 race pools summed
  to between 9 999 and 10 002. The reference also lists a `trend` field beside it; no
  probed payload carried one, so there is no column for it.
- **`horse.money` is the pre-race figure**, which is what makes it safe to keep as
  `start.careerWinnings`. Verified: on 27 of 27 consecutive-start pairs, the next start's
  figure exceeds this one's by exactly what the horse won here.
- **`startInterval` is days since the horse's previous start**, derived after loading
  rather than read from the API. A scratched horse did not start, so a scratching is not
  a point on the timeline: the gap is measured across it and the scratched row keeps
  NULL. **NULL is not a zero gap** — it means no earlier start is known, which is also
  the case for a horse whose real previous start fell before the crawl window or abroad.
  Zero *is* a real value: a heat and its final put a horse in two races on one date.
- **Drivers and trainers share one licence namespace.** 144 ids appeared in both roles
  with no id ever carrying two different names, which is why `atg.person` is one table.
  `parse` reports a clash if one ever appears; that is what would force a split.
- **Money is in öre** everywhere: `horse.money`, `result.prizeMoney`, pool `turnover`.
- **Gallop racing populates `place` more sparsely** than trotting. ATG's calendar carries
  both sports, and `race.sport` separates them.
- **Payloads are ~30 KB raw / ~6 KB gzipped**, an order of magnitude under the reference
  document's 150–300 KB estimate. Five years is roughly 1.5–2 GB raw, ~300 MB gzipped.
- The calendar reaches back to at least **2013-03-05**; `/calendar/month/{ym}` does not
  exist. One call per date.
- **Do not name the database `atg.duckdb`.** DuckDB names the catalog after the file, so
  that collides with the `atg` schema and every query fails with "Ambiguous reference to
  catalog or schema".

## Point-in-time discipline

**The embedded `statistics` blocks are not persisted, and that is the single
highest-value rule here.** Every start carries a full horse/driver/trainer statistics
block, served from current registry state rather than snapshotted at race time. Storing
one would leak that race's own result — and every result after it — into anything trained
on this archive. Rolling form, win rates and earnings-per-start are derived from
`atg.start` with an explicit `< race.meetDate` predicate instead.

The one embedded aggregate kept is `horse.money`, as `start.careerWinnings`, and
`validate` re-checks that it is the pre-race figure rather than assuming it.

`start.startInterval` is the one derived feature materialised on a start, and it is safe
precisely because it is a backward-only window over `atg.start` — it can see the date of
an earlier start and nothing else, so no result later than this race can reach it.

`trainerId` on a start is the trainer **as of that race**, so trainer-change features are
a self-join over `atg.start` ordered by date. Coverage begins where the crawl begins;
treat "trainer unknown" as a category and never back-fill a horse's earlier starts from a
later race's trainer.

## Not built

Feature engineering and modelling; a stats TUI; forward-only pre-race and odds-drift
snapshots (worth adding once the archive exists — no backfill can recover them); any
Svensk Travsport second source, which the strategy document argues against building
speculatively.
