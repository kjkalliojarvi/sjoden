# Win-Probability Agent for Harness Racing (Ravit)
## Data Specification & Development Roadmap

*Prepared for Kari — August 2026*

---

## 1. Frame the problem correctly first

The single most important design decision: **a horse race is a competition, not a set of independent events.** You are not predicting "will horse X win?" in isolation — you are predicting "which of these 8–16 horses wins *this* race?" The probabilities of all starters in a race must sum to 1 (minus a small adjustment if you model dead heats or mass disqualifications).

This has two practical consequences:

1. Your model should score each horse *relative to its opponents*, and the final probabilities should come from a **race-level normalization** (softmax over the horses in the race) or from a model family built for this, such as the **conditional (multinomial) logit / Plackett–Luce** model. This is the classic approach in the academic literature on racetrack modeling (Bolton & Chapman 1986, Benter 1994).
2. Your evaluation must also be race-level: "did the model put high probability on the actual winner of each race?" — measured with multiclass log loss per race — not per-horse binary accuracy.

A second framing decision: **what is the probability benchmark?** In any pari-mutuel market (Toto/Veikkaus in Finland, ATG in Sweden), the betting public already produces an implied probability for every horse (its odds, with the takeout removed). Decades of research show these are hard to beat. Even if your goal is not betting, the market odds are the yardstick: a model that can't match the public's log loss isn't yet extracting the signal in the data. Successful models (famously Benter's Hong Kong operation) combine their fundamental model *with* the market probability.

---

## 2. The data you need

### 2.1 Core entities and schema

Structure the data as a small relational schema. The central table is the **start** (one horse's participation in one race) — this is your training row.

**`races`** — one row per race

| Field | Notes |
|---|---|
| race_id, date, track | Track matters: sizes, surfaces and geometries differ (e.g., 800 m vs 1000 m ovals) |
| distance | 1600 / 2100 / 2600 / 3100 m typical in ravit |
| start_method | **Autostart vs. volt/tasoitusajo (handicap volt start)** — hugely important in trotting |
| race_class / conditions | Prize money, age/earnings restrictions, stakes vs. weekday race |
| monté flag | Ridden trot races behave differently from sulky races |
| track_condition, weather | Frozen/winter tracks in Finland shift km times a lot |
| field_size | Small fields (≤6) and full fields (12–16) behave differently |
| first_prize / total purse | Good proxy for class level |

**`starts`** — one row per horse per race (the training row)

| Field | Notes |
|---|---|
| race_id, horse_id, driver_id, trainer_id | Foreign keys |
| post_position / lane, and tier (front/second row in autostart) | Post bias is real and track-specific |
| handicap_distance | In volt starts, horses may give 20/40/60 m |
| finishing_position | Target-related |
| km_time | The core performance measure in trotting (min/km) |
| disqualification / gait info | **Laukka (break of gait), hylätty (DQ)** — trotting-specific and essential |
| margin, remarks | e.g., led throughout, parked outside, blocked |
| shoeing | **Barefoot front/hind vs. shod** — a major, well-known performance factor in Nordic trotting; changes are published pre-race |
| sulky/cart type | Regular vs. American ("jenkkikärry") — also published |
| final_odds and/or betting percentages | Both a benchmark and (later) a feature |
| scratched flag | Keep scratches; you need them to reconstruct fields correctly |

**`horses`** — id, name, registration number, sex, birth year, breed (**lämminverinen vs. suomenhevonen** — model them separately or with a breed feature; they race in separate races anyway), sire/dam (optional, useful for young horses with few starts).

**`drivers`** and **`trainers`** — id, name, plus you will *derive* rolling statistics (win %, top-3 %, starts volume) rather than store them.

### 2.2 How much history

As a rule of thumb you want **3–5+ seasons** of complete results. Finland runs roughly 500–600 race days a year (~5,000+ races, ~50,000–60,000 starts); Sweden about double. Two seasons is a workable minimum for a first model; more matters mainly for stable driver/trainer/track effects and for winter-vs-summer coverage. Critically, you also need each horse's starts *before* your modeling window, or the early rows in your window will have artificially empty form histories.

### 2.3 Where to get it

- **Finland — Suomen Hippos / Heppa.** All Finnish results, past performances, horse/driver/trainer records live in the public [Heppa system](https://heppa.hippos.fi/heppa/app) ([race results](https://heppa.hippos.fi/heppa/racing/RaceResults.html), plus a [mobile results/statistics interface](https://heppa.hippos.fi/mobiili/races/results) that is backed by JSON endpoints). Hippos does not advertise a public developer API, so the practical routes are: (a) ask Hippos for a data licence / research access — worth an email, they have granted data for research before; or (b) collect from the public pages, in which case check the ToS and be polite (cache everything, throttle requests).
- **Sweden — ATG / Svensk Travsport.** Sweden is attractive because volumes are larger and ATG exposes the JSON API that powers atg.se (`www.atg.se/services/racinginfo/...` — startlists, results, odds), which community projects have used for years (e.g. [this scraper client](https://github.com/Dotsonen/ATGScrapper/blob/master/src/main/java/com/company/AtgClient.java), [a multi-country harness scraping project](https://github.com/youreakim/Horses)). Svensk Travsport's sportapp/travsport.se has its own endpoints as well. For commercial arrangements there is an official partner route via [Swedish Horse Racing](https://www.swedishhorseracing.com/for-partners/offer-and-solution).
- **Commercial APIs** (e.g. [The Racing API](https://www.theracingapi.com/)) are mostly thoroughbred (UK/IRE/US) — not much help for ravit, but relevant if you later generalize.

Whatever the source, store the **raw responses immutably** (JSON/HTML files or a raw table) and build parsed tables from them. You will re-parse many times as you discover fields you missed.

### 2.4 Point-in-time discipline (the #1 silent killer)

Every feature must be computable **from information available before the race started.** Concretely:

- Derive form features only from starts with `date < race.date`.
- Career stats like "career win %" must be as-of-date snapshots, not today's values.
- Odds: only use odds that existed pre-race (final win odds are fine as a *benchmark*; if used as a *feature*, be aware you're building a hybrid model, which is legitimate but different).
- Beware silently updated master data (a horse's record, a driver's stats page) — this is why you snapshot raw data with timestamps.

---

## 3. Feature engineering (where the edge lives)

Trotting-specific features matter more than model choice. A solid starter set:

**Recent form.** Finishing positions and km times of the last N starts (N≈5–10), with recency weighting. Encode DNFs/gallops explicitly rather than dropping them.

**Speed figures.** Raw km times are not comparable across tracks, distances, start methods, seasons, or track conditions. Build a normalized km-time: regress km_time on (track, distance band, start method, month/track condition, class) and use the residual as the horse's "speed figure" per start. This one feature family typically carries a large share of the model's power.

**Break-of-gait propensity.** Share of recent starts with laukka, interacted with start method (volt starts cause more breaks) and distance. This is the trotting analogue of "risk."

**Class movement.** Prize-money level of today's race vs. the horse's recent races; earnings per start; whether the horse is stepping up or down.

**Rest and campaign.** Days since last start (with non-linear encoding — both very short and very long layoffs matter), starts in last 90 days, first start after a break flag.

**Post position × start method × track.** Estimate empirically from your own data (win rate by lane per track per start method) and feed the estimate in as a feature — don't hand-code assumptions.

**Driver and trainer.** Rolling win %/top-3 % with shrinkage toward the mean (a driver with 4 wins from 10 starts is not a 40% driver — use empirical Bayes / additive smoothing). Driver change vs. last start; "stable's first starter after relocation"-type signals come later.

**Equipment changes.** Barefoot on/off vs. previous start, sulky change. In Nordic trotting these are published pre-race and are among the strongest short-term signals.

**Handicap distance** (volt starts): meters conceded, relative to field.

**Age/sex/breed** basics, and horse's record at today's distance/track/start method.

Deliberately *excluded* at first: pedigree, sectional times, GPS/positioning data — real signal but poor effort-to-value for v1.

---

## 4. Modeling approach

**Stage 1 — Baseline: conditional logit (Plackett–Luce).** Each horse gets a linear score from its features; softmax within the race gives win probabilities. Fit by maximum likelihood on the winner (or on the full finishing order with rank-ordered logit). This is simple, well-calibrated by construction, interpretable, and a surprisingly strong baseline. `statsmodels` (ConditionalLogit), `choix`, or ~50 lines of PyTorch.

**Stage 2 — Gradient boosting.** LightGBM/XGBoost on the same rows. Two workable patterns: (a) binary "won/lost" objective, then normalize predicted scores within each race (divide by race sum, or better, softmax with a temperature fitted on validation data); or (b) LightGBM's `lambdarank`/listwise objectives with race as the query group, then calibrate scores to probabilities. GBMs usually beat the linear model once features are rich, at the cost of needing an explicit calibration step.

**Stage 3 — Hybrid with the market.** Fit a small logistic/conditional-logit combiner: `score = α·model_logit + β·market_logit`, calibrated on held-out races. If your α is meaningfully > 0 on out-of-sample data, your model adds information beyond the crowd — that's the real success criterion.

**Calibration and evaluation, non-negotiable:**

- **Temporal splits only.** Train on 2021–2024, validate on 2025, test on 2026. Never random splits — they leak form cycles and horses across the boundary.
- **Metrics:** mean per-race log loss (primary), calibration curves (predicted vs. actual win rate by probability bucket), and comparison against the market-implied probabilities with takeout removed.
- **If betting is a goal:** backtest with realistic assumptions — pari-mutuel pools mean *your own bets move the odds*, final odds aren't available at bet time, and Finnish pools are shallow. Fractional Kelly staking, and treat any backtest that beats the market by a wide margin as a bug until proven otherwise.

---

## 5. The "agent" architecture

Keep the agent thin and the pipeline deterministic:

1. **Ingestion** — scheduled jobs pulling startlists (pre-race) and results (post-race) into the raw store.
2. **Feature builder** — pure functions from (horse_id, as_of_date) → feature vector, shared *identically* between training and inference. This sharing is what prevents training/serving skew; a small feature-store-like module is enough.
3. **Model service** — loads the trained model, takes a startlist, emits per-horse probabilities + top feature attributions (SHAP) so you can sanity-check *why*.
4. **Monitoring & retraining** — log every prediction, compare weekly against outcomes and the market; retrain on a schedule (monthly is plenty) with the temporal-split evaluation as a gate.

An LLM-based agent layer, if you want one, sits *on top* of this — explaining picks, answering questions about a race, watching for scratches/driver changes and re-scoring. Don't ask an LLM to estimate the probabilities themselves; that's the statistical model's job.

---

## 6. Phased development plan

**Phase 0 — Data acquisition (1–2 weeks of calendar time, mostly waiting on collection).** Choose Finland (Heppa) or Sweden (ATG) as primary; contact Hippos about data access in parallel with building a throttled collector; land 2–5 seasons of raw results + startlists; build the parsed schema of §2.1. *Exit criterion: you can reconstruct any past race's startlist and result from your own database.*

**Phase 1 — Baseline model (1 week).** Minimal features (recent finish positions, normalized km time, driver win %, post position, days rest) → conditional logit → temporal-split log loss vs. two benchmarks: the uniform 1/n model and the market odds. *Exit criterion: comfortably beats uniform; you know exactly how far you are from the market.*

**Phase 2 — Feature depth (2–4 weeks, iterative).** Add the full §3 feature set, one family at a time, measuring the log-loss delta of each on validation. Move to LightGBM when features outgrow the linear model. This phase is where you'll spend most of your life; the measurement discipline is what makes it converge.

**Phase 3 — Calibration, hybrid, backtest (1–2 weeks).** Softmax temperature / isotonic calibration; market-hybrid model; if relevant, betting simulation with realistic frictions.

**Phase 4 — Productionize the agent.** Automate ingestion → predict on upcoming race cards → monitoring dashboard → scheduled retraining. Add the conversational/explanatory layer last.

---

## 7. Pitfalls checklist

- **Leakage** via career stats computed "as of today" instead of as of race date — the classic way to get a model that looks brilliant and is worthless.
- **Dropping gallops/DQs** — in trotting these are informative outcomes, not missing data.
- **Treating km times as comparable** across tracks/start methods/winter-summer without normalization.
- **Random train/test splits** instead of temporal ones.
- **Unshrunk small-sample rates** (driver with 3 starts at 67%).
- **Ignoring scratches** when reconstructing historical fields — post positions and pool percentages shift.
- **Mixing breeds/disciplines** (lämminveriset vs. suomenhevoset, monté vs. sulky) without features or separate models.
- **Overtrusting backtests** in shallow pari-mutuel pools.
- Legal/ToS: check the data source's terms, and if wagering is involved, note this is analysis tooling, not financial advice.

---

## 8. Suggested first-week stack

Python 3.11+, `polars` or `pandas`, `duckdb` or Postgres for storage, `httpx` + `tenacity` for collection, `statsmodels`/`choix` for the conditional logit, `lightgbm` + `scikit-learn` (calibration) for stage 2, `matplotlib` for calibration plots, and `pytest` around the feature builder (point-in-time correctness is very testable: assert that features for race R don't change when you add results dated after R).
