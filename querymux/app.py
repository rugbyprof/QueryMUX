"""The split-pane TUI. Talks to the FastAPI backend over HTTP only — see
Section 03 of the requirements doc for why it never imports a DB driver
directly."""
from __future__ import annotations

import httpx
import sqlparse
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static, TextArea


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
        super().__init__()
        self.api_url = api_url

    def compose(self) -> ComposeResult:
        with Vertical(id="history-panel"):
            yield Static(" Query history — Enter to load, Esc to close", id="history-title")
            yield DataTable(id="history-table")

    async def on_mount(self) -> None:
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

    def __init__(self, api_url: str, backend: str, target: str) -> None:
        super().__init__()
        self.api_url = api_url.rstrip("/")
        self.backend = backend
        self.target = target

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            with Vertical(id="query-col") as query_col:
                query_col.border_title = "QUERY"
                yield TextArea.code_editor("", language="sql", id="query-input")
            with Vertical(id="results-col") as results_col:
                results_col.border_title = "RESULTS"
                yield DataTable(id="results-table")
                yield Static("Ready.", id="status-line")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "QueryMUX"
        self.sub_title = f"backend: {self.backend} · {self.target}"
        self.query_one("#results-table", DataTable).cursor_type = "row"
        self.query_one("#query-input", TextArea).focus()

    def action_format_query(self) -> None:
        text_area = self.query_one("#query-input", TextArea)
        formatted = sqlparse.format(text_area.text, reindent=True, keyword_case="upper")
        text_area.text = formatted

    def action_toggle_history(self) -> None:
        def loaded(query_text: str | None) -> None:
            if query_text is not None:
                self.query_one("#query-input", TextArea).text = query_text

        self.push_screen(HistoryScreen(self.api_url), loaded)

    async def action_run_query(self) -> None:
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
            table.add_columns(*result["columns"])
            for row in result["rows"]:
                table.add_row(*[str(v) for v in row])
            note = " (showing first {} — add LIMIT)".format(len(result["rows"])) if result.get("truncated") else ""
            status.update(f'{len(result["rows"])} rows · {result["duration_ms"]:.1f} ms{note}')
        else:
            status.update(f'OK · {result["row_count"]} row(s) affected · {result["duration_ms"]:.1f} ms')
