"""Startup picker: shown when querymux runs without --db. Lists what this
machine can open (see discover.py) and returns the chosen (backend, target),
or None if the user quits. Runs before the API server starts, so it talks to
discover.py directly; it never opens a database itself.
"""
from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from . import discover, setup_test

# A choice is (backend, target). An empty DuckDB target means in-memory.
Choice = tuple[str, str]


def _short(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home) else s


class ConfirmSetup(ModalScreen[bool]):
    """What `s` will create, and a yes/no."""

    CSS = """
    ConfirmSetup { align: center middle; }
    #box { width: 84; height: auto; border: thick $accent; padding: 1 2; background: $surface; }
    #buttons { height: auto; margin-top: 1; }
    Button { margin-right: 2; }
    """
    BINDINGS = [Binding("escape", "no", "Cancel"), Binding("y", "yes", "Yes"), Binding("n", "no", "No")]

    def __init__(self, plan: str) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        u, p, d = setup_test.USER, setup_test.PASSWORD, setup_test.DBNAME
        lines = ["[b]Set up practice databases[/b]", "",
                 f"  SQLite   ~/.querymux/{d}.db",
                 f"  DuckDB   ~/.querymux/{d}.duckdb",
                 "  (tables people and orders, small random data)", ""]
        if self.plan == "server":
            lines.append(f"  Postgres role [b]{u}[/b] (password [b]{p}[/b]) owning database [b]{d}[/b],")
            lines.append("           created through your admin login. Existing ones are reused.")
        elif self.plan == "docker":
            lines.append("  Postgres no server is running, so start one in Docker:")
            lines.append(f"           {' '.join(setup_test.docker_command())}")
        else:
            lines.append("  Postgres skipped: no driver, or no server and no Docker")
        with Vertical(id="box"):
            yield Static("\n".join(lines))
            with Horizontal(id="buttons"):
                yield Button("Yes, set up (y)", variant="primary", id="yes")
                yield Button("Cancel (n)", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class SetupReport(ModalScreen[None]):
    """The results, with credentials and the psql command to copy."""

    CSS = """
    SetupReport { align: center middle; }
    #box { width: 100; height: 80%; border: thick $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("enter", "close", "Close")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="box"):
            yield Static("[b]Setup results[/b]  (Enter or Esc to close)\n")
            yield Static(self.text, markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


class PickerApp(App[Choice | None]):
    TITLE = "QueryMUX"
    SUB_TITLE = "choose a database"
    CSS = """
    #backends { height: auto; padding: 0 1; margin-bottom: 1; }
    #filter   { margin: 0 1; display: none; }
    #filter.visible { display: block; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("enter", "open", "Open", show=True, priority=True),
        Binding("slash", "filter", "Filter"),
        Binding("s", "setup", "Set up practice DBs"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("q", "quit_picker", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._scan()

    def _scan(self) -> None:
        self._statuses = discover.backend_statuses()
        self._choices: list[tuple[str, str, str, str]] = []   # backend, label, size, target
        available = {s.backend for s in self._statuses if s.available}
        for f in discover.find_databases():
            if f.backend in available:
                self._choices.append((f.backend, _short(f.path), f.size_label, str(f.path)))
        pg = next((s for s in self._statuses if s.backend == "postgres"), None)
        if pg and pg.databases:
            for name in pg.databases:
                self._choices.append(("postgres", name, "",
                                      discover.postgres_target(pg.base_conninfo, name)))
        if "duckdb" in available:
            self._choices.append(("duckdb", "(in memory: query CSV / Parquet files directly)", "", ""))
        self._visible = list(self._choices)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static(self._backend_summary(), id="backends")
            yield Input(placeholder="filter by name or backend", id="filter")
            yield DataTable(id="databases", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def _backend_summary(self) -> str:
        lines = ["[b]Backends[/b]"]
        for st in self._statuses:
            mark = "[green]✓[/green]" if st.available else "[red]✗[/red]"
            note = f"  [dim]{st.note}[/dim]" if st.note else ""
            lines.append(f"  {st.backend:<9} {mark} {st.version}{note}")
        if not self._choices:
            lines.append("")
            lines.append("[yellow]No databases found.[/yellow] Run querymux from a folder that has "
                         "some, set DATA_DIR, or add folders to ~/.querymux/config.toml:  "
                         'scan = ["~/data"]')
        return "\n".join(lines)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("backend", "database", "size")
        self._fill()
        table.focus()

    def _fill(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for backend, label, size, _ in self._visible:
            table.add_row(backend, label, size)

    def on_input_changed(self, event: Input.Changed) -> None:
        text = event.value.lower().strip()
        self._visible = [c for c in self._choices
                         if not text or text in c[0] or text in c[1].lower()]
        self._fill()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open()

    def action_open(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one(DataTable).focus()
            return
        table = self.query_one(DataTable)
        if not self._visible or table.cursor_row is None:
            return
        backend, _, _, target = self._visible[table.cursor_row]
        self.exit((backend, target))

    def action_filter(self) -> None:
        box = self.query_one(Input)
        box.add_class("visible")
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one(Input)
        box.value = ""
        box.remove_class("visible")
        self.query_one(DataTable).focus()

    def action_setup(self) -> None:
        if isinstance(self.focused, Input):
            return
        plan = setup_test.postgres_plan()

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.notify("Setting up practice databases...")
                self._run_setup(plan == "docker")

        self.push_screen(ConfirmSetup(plan), confirmed)

    @work(thread=True, exclusive=True)
    def _run_setup(self, use_docker: bool) -> None:
        text = setup_test.format_results(setup_test.run_setup(use_docker))
        self.call_from_thread(self._after_setup, text)

    def _after_setup(self, text: str) -> None:
        self._scan()
        self.query_one("#backends", Static).update(self._backend_summary())
        self._fill()
        self.push_screen(SetupReport(text))

    def action_quit_picker(self) -> None:
        if isinstance(self.focused, Input):
            return  # typing "q" in the filter box shouldn't quit
        self.exit(None)


def pick() -> Choice | None:
    return PickerApp().run()
