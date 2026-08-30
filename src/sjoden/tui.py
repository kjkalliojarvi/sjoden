"""`sjoden stats` — browse one horse's, driver's or trainer's ATG starts.

A reader over `atg.start`: search on the left, and the right pane counts the
starts and the first three placings nine ways — overall, by shoeing, by sulky,
by distance band, by start method, by post position, by the gap since the
previous start, by track, and by the counterpart role. All the SQL, and the
reasoning behind it, is in `stats`; this module is the widgets and nothing else.

**Which subject is being counted is app state, not a third app.** `t` cycles
horse → trainer → driver; the widget tree does not change, because
`stats.breakdowns` is always `stats.AXIS_COUNT` long and only the last panel's
title differs. See `stats.Subject`.

Read-only, on a connection opened per query rather than per session, so a
browsing session cannot hold the archive against a concurrent `parse`. Nine
breakdowns over the busiest driver in the archive measure 69 ms, so the queries
run on the event-loop thread rather than in a worker. See `archive_db.db_read`.

Clicking a bucket row — or pressing Enter on it — opens the individual starts
behind it, filtered by the same expression that labelled the bucket.

    uv run sjoden stats
    uv run sjoden stats 'järvsöodin'
"""
import duckdb
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from . import stats
from .archive_db import DEFAULT_DB, db_read

HINT = 'Type part of a {} name and press Enter.'

# A space, not '', so the spacer column keeps a width of its own: DataTable
# sizes a column from its widest cell, and nothing is not a width.
GAP = ' '

# The vocabulary the tables use, glossed once rather than in nine headers.
#
# The layoff line is the one caveat that changes meaning with the subject: the
# gap is always the horse's own, so on a driver or a trainer it answers 'how do
# the horses I get go off a break'. Its unknown bucket is then one row per horse
# rather than one row. See `stats._LAYOFF`.
LEGEND = ("shoes: front / rear, as ATG reported them — 'unknown' means it did not report\n"
          "plc: the finishing order · 'dq <code>' a disqualification · '-' no classified "
          "finish\n"
          "post position is counted per start method: the inside is a shorter trip off an "
          "auto and traffic off a volte\n"
          "days since previous start is the horse's own gap, whoever drove or trained it — "
          "'unknown' means no earlier start is known, not a zero gap\n"
          "gallop and dq overlap the placings rather than adding to them: a horse can "
          "gallop and still win\n"
          "odds are the final win odds, prize money is this race's, in kronor\n"
          "gallop cards are excluded throughout, and scratched starts with them\n"
          "sulky: monté is ridden and has none — 'unknown' is a sulky that went "
          "unreported, which is a different thing")


class ClickableTable(DataTable):
    """A breakdown table that opens the starts behind the row you click.

    A subclass, and both halves of that are forced. `DataTable._on_click` calls
    `event.stop()`, so an app-level `@on(events.Click)` never sees the click;
    and a single click posts only `RowHighlighted`, because `RowSelected` wants
    a second click on the same cell. Defining `on_click` here fires on the
    first one, and `event.style.meta['row']` is the row that was clicked — the
    header is -1, a click below the last row has no `row` at all, and
    `self.cursor_row` is still the *old* row at this point, which is why the
    metadata is what we read rather than the cursor.

    It posts its own message rather than reposting `RowSelected`, and that is
    the load-bearing part. `_on_click` posts `RowSelected` itself whenever the
    clicked cell already holds the cursor — which a freshly filled table's
    (0, 0) always does — so a `RowSelected` handler would open two panels for
    one click on the first row, and only on that row. Enter is bound here for
    the same reason: this binding replaces DataTable's own `enter`, since
    bindings merge with the most-derived class winning per key, leaving exactly
    one path per input method.

    `axis` is reassigned rather than fixed at construction, because cycling the
    subject swaps the last panel's axis under a widget that stays put. See
    `StatsApp.show_subject`.
    """

    BINDINGS = [Binding('enter', 'open_bucket', 'Show starts')]

    class BucketSelected(Message):
        """A bucket row was chosen. `bucket` is None on Overall, which has none."""

        def __init__(self, axis, bucket: str | None):
            super().__init__()
            self.axis = axis
            self.bucket = bucket

    def __init__(self, axis, id: str):
        super().__init__(id=id, cursor_type='row')
        self.axis = axis

    def on_click(self, event: events.Click) -> None:
        """Open the clicked bucket, or stop a click on a cleared panel.

        `prevent_default` rather than a plain return, and it is load-bearing.
        Textual dispatches an event to the handler in *every* class of the MRO,
        so `DataTable._on_click` runs after this one and cannot be overridden
        away — defining `_on_click` here would not replace it, and would also
        kill this method, since each class contributes `_on_click` *or*
        `on_click` and the underscore wins.

        That matters because `DataTable._on_click` treats any click at row -1 as
        a header click and reads `ordered_columns[meta['column']]` without
        checking there are any columns, so a click anywhere in a panel that
        `clear(columns=True)` emptied raises IndexError out of the framework.
        Its own out-of-bounds guard does not catch it: that guard is skipped
        whenever `cursor_type` is 'row', which is what every table here uses.

        `prevent_default` sets the flag the dispatch loop breaks on, so this
        runs first and the framework's handler never does. An empty panel has
        nothing to select in any case.
        """
        if not self.columns:
            event.prevent_default()
            return
        self._open(event.style.meta.get('row', -1))

    def action_open_bucket(self) -> None:
        self._open(self.cursor_row)

    def _open(self, row_index: int) -> None:
        """The bucket rides in the row key, as the hit list's identity does.

        One guard covers the header (-1), the empty space below the last row
        (no `row` in the metadata, so -1 again) and Enter on an empty table.
        """
        if 0 <= row_index < self.row_count:
            key = self.coordinate_to_cell_key(Coordinate(row_index, 0)).row_key
            self.post_message(self.BucketSelected(self.axis, key.value))


class StartsScreen(ModalScreen[None]):
    """The starts behind one bucket.

    DEFAULT_CSS rather than CSS, which would apply to the whole app: this is the
    one place in the file where the distinction bites.
    """

    DEFAULT_CSS = """
    StartsScreen { align: center middle; }
    StartsScreen > Vertical { width: 90%; height: 80%; border: round $accent;
                              background: $surface; padding: 0 1; }
    StartsScreen DataTable { height: 1fr; }
    """

    # `q` closes rather than quitting: it is the reflex the main screen trains,
    # and losing the session to it would be the app's most annoying keystroke.
    BINDINGS = [('escape', 'dismiss', 'Close'), ('q', 'dismiss', 'Close')]

    def __init__(self, heading: str, columns, rows):
        super().__init__()
        self.heading = heading
        self.columns = columns
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical():
            yield DataTable(id='starts', cursor_type='row')

    def on_mount(self) -> None:
        box = self.query_one(Vertical)
        box.border_title = self.heading
        box.border_subtitle = 'escape: close'
        table = self.query_one('#starts', DataTable)
        table.add_columns(*self.columns)
        # add_rows, not a row at a time: Overall on the busiest driver is 10,416
        # starts over twelve columns. Nothing here needs row keys — a start list
        # is the end of the drill-down, not something to click through.
        table.add_rows([[_cell(v) for v in row] for row in self.rows])


class StatsApp(App):
    """Search a horse, a trainer or a driver, count its ATG starts."""

    CSS = """
    #body { height: 1fr; }
    #hits-pane { width: 46; }
    #hits { height: 1fr; }
    #stats-pane { padding: 0 1; }
    #subject { padding: 0 0 1 0; text-style: bold; }
    .bd-title { color: $text-muted; }
    #legend { padding: 1 0 0 0; color: $text-muted; }
    """

    TITLE = 'ATG starts'

    BINDINGS = [
        ('/', 'search', 'Search'),
        ('escape', 'search', 'Search'),
        ('t', 'cycle_subject', 'Horse/trainer/driver'),
        ('q', 'quit', 'Quit'),
    ]

    def __init__(self, db: str = DEFAULT_DB, name: str | None = None):
        super().__init__()
        self.db = db
        self.subject = stats.HORSE
        # Which subject this is counting, and which archive it is looking at:
        # both are worth having on screen, since --db defaults to a path that
        # may or may not be the one that was just crawled.
        self.sub_title = f'{self.subject.name} · {db}'
        self.prefill = name or ''
        self.key = None         # the identity the breakdowns are showing
        self.label = None       # what to call it, since a personId is six digits
        self.labels = {}        # identity -> display name, filled by the search

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id='body'):
            with Vertical(id='hits-pane'):
                yield Input(value=self.prefill,
                            placeholder=f'{self.subject.name} name…', id='search')
                yield DataTable(id='hits', cursor_type='row')
            with VerticalScroll(id='stats-pane'):
                yield Static(HINT.format(self.subject.name), id='subject')
                # Built from the opening subject's axes, and never rebuilt: every
                # subject has stats.AXIS_COUNT of them, so a cycle retitles the
                # last panel rather than changing the tree. See `show_subject`.
                for i, axis in enumerate(stats.breakdowns(self.subject)):
                    yield Label(axis.title, classes='bd-title', id=f'bt{i}')
                    yield ClickableTable(axis, id=f'bd{i}')
                yield Static(LEGEND, id='legend')
        yield Footer()

    def on_mount(self) -> None:
        # A prefilled term leaves the focus on its results, so the arrow keys
        # walk the hits straight away; without one there is nothing to walk.
        if self.prefill:
            self.search(self.prefill)
        else:
            self.query_one('#search', Input).focus()

    def action_search(self) -> None:
        """Start a new search, rather than editing the last one.

        Clearing is the point: the box keeps its text after Enter, so without
        this the next term is typed onto the end of the old one.
        """
        search = self.query_one('#search', Input)
        search.clear()
        search.focus()

    def action_cycle_subject(self) -> None:
        """Move to the next subject, and re-run the term in it.

        Re-running rather than clearing is what makes `sjoden stats 'ohlsson'`
        work: it reports no horse, and one keypress turns that into the drivers.
        A term that matches nothing in the new subject says so, which is an
        answer too.
        """
        nxt = (stats.SUBJECTS.index(self.subject) + 1) % len(stats.SUBJECTS)
        self.subject = stats.SUBJECTS[nxt]
        self.key = self.label = None
        self.labels = {}
        self.sub_title = f'{self.subject.name} · {self.db}'
        search = self.query_one('#search', Input)
        search.placeholder = f'{self.subject.name} name…'
        if search.value:
            self.search(search.value)
        else:
            self.query_one('#hits', DataTable).clear(columns=True)
            self._message(HINT.format(self.subject.name))
            self._retitle()
            search.focus()

    @on(Input.Submitted, '#search')
    def _submitted(self, event: Input.Submitted) -> None:
        self.search(event.value)

    @on(DataTable.RowHighlighted, '#hits')
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            key = event.row_key.value
            self.show_subject(key, self.labels.get(key, key))

    def search(self, term: str) -> None:
        """Fill the hit list, and load the top hit."""
        hits = self._query(stats.search, self.subject, term)
        table = self.query_one('#hits', DataTable)
        table.clear(columns=True)
        if hits is None:
            return
        names, rows = hits
        if not rows:
            self._message(f'no {self.subject.name} matching {term!r}')
            self._retitle()
            return
        # The last column is the identity — a horseId or a personId. It is the
        # row's identity rather than something to read, so it becomes the row
        # key rather than a column, and the first column is what to display.
        self.labels = {row[-1]: _cell(row[0]) for row in rows}
        table.add_columns(*names[:-1])
        for row in rows:
            table.add_row(*(_cell(v) for v in row[:-1]), key=row[-1])
        # RowHighlighted does not necessarily fire for a first row added under a
        # cursor already sitting at 0, so the top hit is loaded outright.
        self.show_subject(rows[0][-1], _cell(rows[0][0]))
        table.focus()

    def show_subject(self, key, label: str) -> None:
        """Refresh the nine breakdown tables for one horse, driver or trainer.

        Each breakdown runs on its own inside one connection, so a column an
        older archive does not have costs that one panel rather than the whole
        screen — a read-only reader cannot migrate the archive, by design, and
        eight ninths of an answer beats none.

        The panels are also rebound to the current subject's axes here rather
        than in the cycle handler, because this runs on every path that changes
        what is on screen and the cycle handler does not.
        """
        self.key, self.label = key, label
        try:
            with db_read(self.db) as conn:
                heading, failed = key, []
                for i, axis in enumerate(stats.breakdowns(self.subject)):
                    self.query_one(f'#bt{i}', Label).update(axis.title)
                    table = self.query_one(f'#bd{i}', ClickableTable)
                    table.axis = axis
                    table.clear(columns=True)
                    try:
                        names, rows = stats.fetch(
                            conn, axis.breakdown(self.subject), [key])
                    except duckdb.Error as exc:
                        failed.append(f'{axis.title}: {_first_line(exc)}')
                        continue
                    table.add_columns(*_spaced(names, GAP))
                    for row in rows:
                        # The bucket rides on the row key, which is what a click
                        # or an Enter hands back. Overall has no bucket.
                        table.add_row(*_spaced([_cell(v) for v in row]),
                                      key=row[0] if axis.label else None)
                    if not i:
                        heading = _heading(label, key, rows)
        except duckdb.Error as exc:
            self._message(f'query failed: {exc}')
            return
        self.query_one('#subject', Static).update(
            heading + (f'   (query failed — {"; ".join(failed)})' if failed else ''))

    @on(ClickableTable.BucketSelected)
    def _drill(self, event: ClickableTable.BucketSelected) -> None:
        """Show the starts behind the chosen bucket.

        The query goes through `_query` like every other read, so a failure is
        a message on the main screen rather than an empty panel.
        """
        if self.key is None:
            return
        axis, label = event.axis, event.bucket
        result = self._query(stats.bucket_starts, self.subject, axis, self.key, label)
        if result is None:
            return
        columns, rows = result
        bucket = f'{axis.title}: {label}' if label else axis.title
        count = f'{len(rows)} start' + ('' if len(rows) == 1 else 's')
        self.push_screen(StartsScreen(f'{self.label or self.key} · {bucket} · {count}',
                                      columns, rows))

    def _query(self, fn, *args):
        """Any read outside `show_subject`: a browser must not die on an error.

        One `except` covers the ways a real archive refuses to be read — another
        process holding the write lock, a WAL a reader cannot replay, a schema
        older than the query. `show_subject` handles its own, per panel.
        """
        try:
            with db_read(self.db) as conn:
                return fn(conn, *args)
        except duckdb.Error as exc:
            self._message(f'query failed: {exc}')
            return None

    def _message(self, text: str) -> None:
        """Every message and empty state goes in one place."""
        self.query_one('#subject', Static).update(text)
        for i in range(stats.AXIS_COUNT):
            self.query_one(f'#bd{i}', DataTable).clear(columns=True)

    def _retitle(self) -> None:
        """Point the empty panels at the current subject's axes.

        `show_subject` does this as it fills them, which covers every path that
        puts something on screen. This covers the two that do not: a cycle with
        an empty search box, and a term that matches nothing.
        """
        for i, axis in enumerate(stats.breakdowns(self.subject)):
            self.query_one(f'#bt{i}', Label).update(axis.title)
            self.query_one(f'#bd{i}', ClickableTable).axis = axis


def _heading(label: str, key, overall) -> str:
    """`name · identity — N starts`, or the honest 'no starts'.

    Both halves earn their place. The name is what you searched for; the
    identity is what every other tool in this repo speaks — a horseId is what
    `validate` and the raw payloads carry — so dropping it would mean looking up
    what is already on screen.

    'No starts' is not an edge case worth hiding: `atg.horse` is built from the
    starts, so a horse in it normally has some, but a scratching-only career
    leaves a row with nothing to count, and that has to read as an answer rather
    than as a blank screen.
    """
    starts = overall[0][0] if overall else 0
    who = f'{label} · {key}' if label and label != str(key) else str(key)
    if not starts:
        return f'{who} — no starts in atg.start'
    return f'{who} — {starts} starts'


def _spaced(values, filler: str = '') -> list:
    """The row, with a blank column in front of its last two cells.

    Every breakdown ends in the gallop and dq counts, and those overlap the
    placings rather than adding to them — a horse can gallop and still win. A
    blank column says so at a glance, which beats a caption explaining that
    `1st` and `gallop` can count the same start.
    """
    return [*values[:-2], filler, *values[-2:]]


def _first_line(exc) -> str:
    """DuckDB's binder errors carry a multi-line list of candidate columns."""
    return str(exc).splitlines()[0]


def _cell(value) -> str:
    """None renders as empty — never as 'None'."""
    return '' if value is None else str(value)


def stats_tui(args):
    """CLI handler: browse a horse's, driver's or trainer's starts in a TUI.

    Always opens on horses; `t` cycles. There is no --driver flag because the
    cycle re-runs the term, so one keypress does the same job.
    """
    StatsApp(args.db, args.name).run()


if __name__ == '__main__':
    from argparse import Namespace
    stats_tui(Namespace(db=DEFAULT_DB, name=None))
