"""The widget behaviour that a query-layer test cannot reach.

`test_stats` covers the SQL and says the widgets are untested, which was true
until a click on a cleared breakdown panel crashed the app out of Textual's own
`DataTable._on_click`. That is a real failure of this module's code — the guard
against it lives in `ClickableTable` — so it gets a test, and only it.

No async plugin is involved: `App.run_test()` is an ordinary async context
manager, so `asyncio.run` drives it from a plain synchronous test and the suite
keeps its two dependencies.
"""
import asyncio

from conftest import AUTO_RACE, load, seed

from sjoden.parse import parse_all
from sjoden.tui import ClickableTable, StartsScreen, StatsApp

HORSE_NAME = 'vasterbo'


def archive(paths):
    raw_root, db_path = paths
    seed(raw_root, db_path, races=[load(AUTO_RACE)])
    parse_all(db_path, raw_root)
    return db_path


def drive(coroutine):
    """Run one piloted session, and let any app exception out."""
    return asyncio.run(coroutine)


def test_clicking_a_cleared_panel_does_not_crash(paths):
    """The panels are emptied whenever a search matches nothing.

    `DataTable._on_click` reads `ordered_columns[meta['column']]` for any click
    at row -1 without checking there are any columns, and its out-of-bounds
    guard is skipped whenever `cursor_type` is 'row' — which every table here
    uses. Overriding it is not an option: Textual dispatches to the handler in
    every class of the MRO, so the framework's runs regardless, which is why
    `ClickableTable.on_click` calls `prevent_default` instead.
    """
    db_path = archive(paths)

    async def session():
        app = StatsApp(db_path, 'zzzznothing')
        async with app.run_test(size=(160, 50)) as pilot:
            panel = app.query_one('#bd0', ClickableTable)
            assert not panel.columns and not panel.row_count
            for offset in ((0, 0), (10, 0), (30, 0)):
                await pilot.click('#bd0', offset=offset)
                await pilot.pause()
            assert app.is_running

    drive(session())


def test_clicking_a_filled_bucket_still_opens_its_starts(paths):
    """The other half of the same guard.

    `prevent_default` on an empty panel must not become `prevent_default` on a
    full one, and the fix had a way of going wrong that is invisible from the
    empty case: defining `_on_click` would have silently shadowed `on_click`,
    since each class contributes one or the other and the underscore wins.
    """
    db_path = archive(paths)

    async def session():
        app = StatsApp(db_path, HORSE_NAME)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = app.query_one('#bd1', ClickableTable)
            bucket, starts = panel.get_row_at(0)[:2]
            await pilot.click('#bd1', offset=(5, 1))
            await pilot.pause()
            assert isinstance(app.screen, StartsScreen), 'the bucket did not open'
            # The panel renders every cell through `_cell`, so its counts are text.
            assert app.screen.query_one('#starts').row_count == int(starts), bucket

    drive(session())
