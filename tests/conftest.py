"""Fixtures built from real payloads captured from the live API on 2026-08-26.

The two race fixtures are chosen for what they contain rather than for being
typical:

- `race_2026-08-22_23_1` — a 10-horse auto start where four horses are both
  galloped *and* disqualified, so `place` is absent on all four and `kmTime`
  arrives as a bare code.
- `race_2026-08-08_33_1` — a 14-horse volte handicap over two distance tiers,
  with a scratching, two disqualifications and three completed gallops. It is
  the one fixture that exercises every `place` state at once, and the repeated
  `postPosition` that makes the start number the only identity in a race.

The embedded `statistics` blocks were stripped when the fixtures were saved:
nothing reads them, and they were most of the bulk.
"""
import gzip
import json
import os
from pathlib import Path

import pytest

from sjoden import archive_db
from sjoden.crawler import Manifest, game_task, race_task

FIXTURES = Path(__file__).parent / 'data'

CALENDAR = 'calendar_2026-08-22.json'
AUTO_RACE = 'race_2026-08-22_23_1.json'
VOLTE_RACE = 'race_2026-08-08_33_1.json'
GAME = 'game_V85_2026-08-22_23_5.json'


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


@pytest.fixture
def paths(tmp_path):
    """A raw zone and a database path, both under the test's own directory.

    The database is named after the real default and not, say, `atg.duckdb`:
    DuckDB names the catalog after the file, so that one collides with the
    schema and every query fails with 'Ambiguous reference to catalog or
    schema "atg"'.
    """
    return str(tmp_path / 'raw'), str(tmp_path / 'atg_data.duckdb')


def _store(raw_root: str, raw_path: str, payload: dict):
    full = os.path.join(raw_root, raw_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with gzip.open(full, 'wt', encoding='utf-8') as f:
        json.dump(payload, f)


def seed(raw_root: str, db_path: str, races=(), games=()):
    """Write payloads into the raw zone and enqueue them as fetched tasks.

    Stands in for a crawl: `parse` walks the manifest's `done` rows, so a test
    that wants to parse a payload has to put it there the way the crawler
    would.
    """
    tasks = []
    for name in races:
        payload = load(name)
        meet_date = payload['id'].split('_')[0]
        task = race_task(meet_date, payload['track']['id'], payload['id'])
        _store(raw_root, task.rawPath, payload)
        tasks.append(task)
    for name in games:
        payload = load(name)
        parts = payload['id'].split('_')
        task = game_task(parts[1], int(parts[2]), payload['id'])
        _store(raw_root, task.rawPath, payload)
        tasks.append(task)
    with archive_db.db_ops(db_path) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue(tasks)
        for task in tasks:
            manifest.mark(task, 'done', 200, None)
    return tasks
