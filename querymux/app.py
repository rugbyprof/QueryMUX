"""The split-pane TUI. Talks to the FastAPI backend over HTTP only — see
Section 03 of the requirements doc for why it never imports a DB driver
directly."""
from __future__ import annotations

import httpx
import sqlparse
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static, TextArea
from textual.widgets.text_area import LanguageDoesNotExist


class HistoryScreen(ModalScreen[str | None]):
    """Ctrl+R overlay. Enter loads the selected query back into the editor
    (it does not re-run it); Escape closes with no change."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    CSS = """
    HistoryScreen {
        align: center middle;
    }
    #history-panel {
        width: 90%;
        height: 80%;
        border: round $accent;
        background: $surface;
    }
    """

    def __init__(self, api_url: str) -> None:
        # Called by QueryMuxApp.action_toggle_history() when it constructs
        # this screen, before push_screen() mounts it.
        super().__init__()
        self.api_url = api_url

    def compose(self) -> ComposeResult:
        # Textual lifecycle: called once, right after push_screen(), to
        # declare the widget tree. No data yet — the table is empty until
        # on_mount() below fills it in.
        with Vertical(id="history-panel"):
            yield Static(" Query history — Enter to load, Esc to close", id="history-title")
            yield DataTable(id="history-table")

    async def on_mount(self) -> None:
        # Textual lifecycle: fires automatically once compose() has mounted
        # the widgets above. Does the actual work of compose() can't (it's
        # sync) — fetches history from the backend and populates the table.
        table = self.query_one("#history-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("status", "rows", "ms", "query")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.api_url}/history", params={"limit": 100})
                resp.raise_for_status()
                entries = resp.json()
        except httpx.HTTPError as exc:
            self.query_one("#history-title", Static).update(f" Couldn't load history: {exc}")
            return

        self._entries = entries
        for e in entries:
            preview = " ".join(e["query_text"].split())[:70]
            duration = f'{e["duration_ms"]:.1f}' if e["duration_ms"] is not None else "-"
            table.add_row(e["status"], str(e["row_count"] or 0), duration, preview)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Textual event handler: auto-wired by the on_<widget>_<event> name
        # convention, fires when the user picks a row (Enter or click).
        # dismiss() closes the screen and hands the query text to whatever
        # callback was passed to push_screen() — see action_toggle_history().
        row_index = event.cursor_row
        if 0 <= row_index < len(self._entries):
            self.dismiss(self._entries[row_index]["query_text"])


class QueryMuxApp(App):
    """One connection, one adapter, chosen at launch (see 'Backend selection'
    in the requirements doc — no live switching in v1)."""

    ENABLE_COMMAND_PALETTE = False  # frees up Ctrl+P for query formatting

    CSS = """
    #panes {
        height: 1fr;
    }
    #query-col, #results-col {
        width: 1fr;
        height: 1fr;
    }
    #query-col {
        border: round $accent;
        border-title-color: $accent;
    }
    #results-col {
        border: round $accent;
        border-title-color: $accent;
    }
    #query-input {
        height: 1fr;
    }
    #status-line {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    #status-line.-error {
        color: $error;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("f5", "run_query", "Run"),
        Binding("ctrl+enter", "run_query", "Run", show=False),  # not recognized by every terminal — see README
        Binding("ctrl+p", "format_query", "Format"),
        Binding("ctrl+r", "toggle_history", "History"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, api_url: str, backend: str, target: str, editor_language: str = "sql") -> None:
        # Called once by __main__.main(), after it has started the FastAPI
        # backend subprocess and confirmed it's up — api_url is that
        # server's address, not something this class discovers itself.
        # editor_language comes from the active adapter's EDITOR_LANGUAGE
        # (via the backend's /health response), not from `backend` — the
        # TUI stays agnostic to which query syntax any given backend uses.
        super().__init__()
        self.api_url = api_url.rstrip("/")
        self.backend = backend
        self.target = target
        self.editor_language = editor_language

    def compose(self) -> ComposeResult:
        # Textual lifecycle: called once when .run() starts the app, to
        # declare the static widget tree (query editor, results table,
        # status line). No backend I/O happens here.
        yield Header()
        with Horizontal(id="panes"):
            with Vertical(id="query-col") as query_col:
                query_col.border_title = "QUERY"
                # editor_language is whatever the active adapter declared
                # (see Adapter.EDITOR_LANGUAGE) — fall back to plain text
                # rather than crashing if it names a grammar Textual
                # doesn't ship (e.g. a future Mongo/Redis adapter).
                try:
                    query_input = TextArea.code_editor("", language=self.editor_language, id="query-input")
                except LanguageDoesNotExist:
                    query_input = TextArea.code_editor("", language=None, id="query-input")
                yield query_input
            with Vertical(id="results-col") as results_col:
                results_col.border_title = "RESULTS"
                yield DataTable(id="results-table")
                yield Static("Ready.", id="status-line")
        yield Footer()

    def on_mount(self) -> None:
        # Textual lifecycle: fires once compose() has mounted the widgets
        # above. Purely cosmetic/local setup (titles, initial focus) — no
        # network calls, unlike HistoryScreen.on_mount().
        self.title = "QueryMUX"
        self.sub_title = f"backend: {self.backend} · {self.target}"
        self.query_one("#results-table", DataTable).cursor_type = "row"
        self.query_one("#query-input", TextArea).focus()

    def action_format_query(self) -> None:
        # Triggered by the Ctrl+P binding above. Entirely local/synchronous
        # (sqlparse) — never touches the backend, unlike action_run_query.
        text_area = self.query_one("#query-input", TextArea)
        formatted = sqlparse.format(text_area.text, reindent=True, keyword_case="upper")
        text_area.text = formatted

    def action_toggle_history(self) -> None:
        # Triggered by the Ctrl+R binding above. Pushes HistoryScreen (which
        # owns its own backend fetch, see its on_mount) and registers `loaded`
        # as the callback Textual invokes with HistoryScreen.dismiss()'s
        # argument once the user picks a row or closes with Escape.
        def loaded(query_text: str | None) -> None:
            if query_text is not None:
                self.query_one("#query-input", TextArea).text = query_text

        self.push_screen(HistoryScreen(self.api_url), loaded)

    async def action_run_query(self) -> None:
        # Triggered by the F5 / Ctrl+Enter binding above. The only method
        # here that talks to the backend: POSTs the query text, then renders
        # one of three outcomes below — transport error, query error, or a
        # result set (SELECT) / affected-row count (everything else).
        text = self.query_one("#query-input", TextArea).text
        status = self.query_one("#status-line", Static)
        table = self.query_one("#results-table", DataTable)

        if not text.strip():
            status.update("Nothing to run.")
            return

        status.remove_class("-error")
        status.update("Running…")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{self.api_url}/query", json={"text": text}, timeout=30)
                resp.raise_for_status()
                result = resp.json()
        except httpx.HTTPError as exc:
            status.add_class("-error")
            status.update(f"Request failed: {exc}")
            return

        table.clear(columns=True)

        if result["error"]:
            status.add_class("-error")
            status.update(result["error"])
            return

        if result["columns"]:
            styles = ["cyan", "magenta", "yellow", "green"]
            table.add_columns(*[
                Text(col, style=f"bold {styles[i % len(styles)]}")
                for i, col in enumerate(result["columns"])
            ])
            for row in result["rows"]:
                table.add_row(*[
                    Text(str(v), style=styles[i % len(styles)])
                    for i, v in enumerate(row)
                ])
            note = " (showing first {} — add LIMIT)".format(len(result["rows"])) if result.get("truncated") else ""
            status.update(f'{len(result["rows"])} rows · {result["duration_ms"]:.1f} ms{note}')
        else:
            status.update(f'OK · {result["row_count"]} row(s) affected · {result["duration_ms"]:.1f} ms')
