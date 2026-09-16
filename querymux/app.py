"""The split-pane TUI. Talks to the FastAPI backend over HTTP only — see
Section 03 of the requirements doc for why it never imports a DB driver
directly."""
from __future__ import annotations

import logging
import subprocess
import threading

import httpx
import sqlparse
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static, TextArea
from textual.widgets.text_area import LanguageDoesNotExist

logger = logging.getLogger(__name__)


class _RichLogHandler(logging.Handler):
    """Bridges logging.debug()/info()/etc calls made *during* the TUI's own
    runtime into the debug pane live, same as print() gets via on_print
    below. Only attached when --debug is passed (see QueryMuxApp.on_mount)
    — logging.basicConfig(filename=...) in __main__.py already sends every
    record to disk regardless, this is purely for the on-screen view.

    Must handle both threads that can call logging.debug(): the app's own
    (safe to touch the widget directly) and _pump_backend_output's (not —
    Textual widgets aren't thread-safe, same reason that method itself uses
    call_from_thread rather than writing to the widget directly)."""

    def __init__(self, log_widget: RichLog) -> None:
        super().__init__()
        self._log_widget = log_widget
        self._app_thread = threading.current_thread()  # constructed in on_mount, on the app's own thread

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        if threading.current_thread() is self._app_thread:
            self._log_widget.write(message)
        else:
            self._log_widget.app.call_from_thread(self._log_widget.write, message)


class HistoryTable(DataTable):
    """Inline history panel — lives in the same bottom stack as the debug
    pane (see QueryMuxApp.compose), not a popup, so it shares that pane's
    position/sizing behavior: hidden by default, toggled by Ctrl+R, and
    when both it and the debug pane are visible the debug pane renders
    above it (compose() yields debug_log first). Persists for the app's
    whole lifetime — toggling only flips .display — so columns are added
    once in on_mount and rows are refreshed by re-running load()."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("d", "delete_selected", "Delete"),
        Binding("delete", "delete_selected", "Delete", show=False),
    ]

    def __init__(self, api_url: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.api_url = api_url
        self._entries: list[dict] = []

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.border_title = "HISTORY (Enter: load · d: delete · Esc/Ctrl+R: close)"
        self.add_columns("status", "rows", "ms", "query")

    async def load(self) -> None:
        # Called by QueryMuxApp.action_toggle_history whenever the panel is
        # opened, and again after a delete, so it always reflects what's
        # actually in ~/.querymux/history.db rather than a stale snapshot.
        logger.debug("HistoryTable.load: fetching history from %s", self.api_url)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.api_url}/history", params={"limit": 100})
                resp.raise_for_status()
                entries = resp.json()
        except httpx.HTTPError as exc:
            logger.debug("HistoryTable.load: failed: %s", exc)
            return

        self._entries = entries
        self.clear()  # rows only — columns were added once in on_mount
        for e in entries:
            preview = " ".join(e["query_text"].split())[:70]
            duration = f'{e["duration_ms"]:.1f}' if e["duration_ms"] is not None else "-"
            self.add_row(e["status"], str(e["row_count"] or 0), duration, preview)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Textual event handler: auto-wired by the on_<widget>_<event> name
        # convention, fires when the user picks a row (Enter or click).
        row_index = event.cursor_row
        if 0 <= row_index < len(self._entries):
            query_text = self._entries[row_index]["query_text"]
            logger.debug("HistoryTable: row %d selected, loading query (%d chars)", row_index, len(query_text))
            self.app.query_one("#query-input", TextArea).text = query_text
            self.action_close()

    async def action_delete_selected(self) -> None:
        # Triggered by the d / Delete bindings above. Re-fetches afterward
        # rather than removing the row locally — simpler than keeping row
        # keys and self._entries in sync, and history lists are small.
        if not (0 <= self.cursor_row < len(self._entries)):
            return
        entry_id = self._entries[self.cursor_row]["id"]
        logger.debug("HistoryTable.action_delete_selected: deleting id=%s", entry_id)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(f"{self.api_url}/history/{entry_id}")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("HistoryTable.action_delete_selected: delete failed: %s", exc)
            return
        await self.load()

    def action_close(self) -> None:
        # Triggered by the Escape binding above, and after a row is picked.
        # Mirrors QueryMuxApp.action_toggle_history's "hide" branch since
        # this can also close itself without going through that action.
        self.display = False
        self.app.query_one("#query-input", TextArea).focus()


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
    #debug-log {
        height: 12;
        border: round $warning;
        border-title-color: $warning;
        background: $surface;
    }
    #history-table {
        height: 12;
        border: round $accent;
        border-title-color: $accent;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("f5", "run_query", "Run"),
        Binding("ctrl+enter", "run_query", "Run", show=False),  # not recognized by every terminal — see README
        Binding("ctrl+p", "format_query", "Format"),
        Binding("ctrl+r", "toggle_history", "History"),
        # priority=True so it works regardless of which widget has focus —
        # ctrl+d, f6 and f7 were all tried first and are already claimed
        # internally by TextArea (delete-char, select-line, select-all).
        Binding("f9", "toggle_debug", "Debug", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        api_url: str,
        backend: str,
        target: str,
        editor_language: str = "sql",
        backend_proc: subprocess.Popen | None = None,
        debug: bool = False,
    ) -> None:
        # Called once by __main__.main(), after it has started the FastAPI
        # backend subprocess and confirmed it's up — api_url is that
        # server's address, not something this class discovers itself.
        # editor_language comes from the active adapter's EDITOR_LANGUAGE
        # (via the backend's /health response), not from `backend` — the
        # TUI stays agnostic to which query syntax any given backend uses.
        # backend_proc is only set when __main__ spawned the backend itself
        # (not --no-server) — its stdout/stderr get piped into the debug
        # pane; see _pump_backend_output. debug mirrors --debug: whether
        # __main__ already configured file logging (see DEBUG_LOG_PATH)
        # decides whether logging.debug() calls go anywhere at all, this
        # only controls the on-screen extras (pane open by default, plus
        # the live logging bridge — see on_mount).
        super().__init__()
        self.api_url = api_url.rstrip("/")
        self.backend = backend
        self.target = target
        self.editor_language = editor_language
        self.backend_proc = backend_proc
        self.querymux_debug = debug

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
        # debug_log and history_table both span the full width below both
        # panes — siblings of #panes, not inside it, so Horizontal's
        # left/right split doesn't apply to them. Both are hidden by
        # default (unless --debug) and just toggle .display rather than
        # being created/destroyed, so #panes (height: 1fr) automatically
        # grows or shrinks in the y axis as they're shown or hidden.
        # Declared in this order — debug_log first — so when both are
        # visible, history renders below the debug pane, not above it.
        debug_log = RichLog(id="debug-log", markup=True, wrap=True)
        debug_log.border_title = "DEBUG"
        debug_log.display = self.querymux_debug
        yield debug_log
        history_table = HistoryTable(self.api_url, id="history-table")
        history_table.display = False
        yield history_table
        yield Footer()

    def on_mount(self) -> None:
        # Textual lifecycle: fires once compose() has mounted the widgets
        # above. Sets up cosmetic/local state (titles, initial focus) plus
        # the debug pane: this process's own print()/stderr are captured
        # directly (on_print below), and if we own the backend subprocess,
        # a background thread pumps its stdout/stderr in too — see
        # _pump_backend_output.
        self.title = "QueryMUX"
        self.sub_title = f"backend: {self.backend} · {self.target}"
        self.query_one("#results-table", DataTable).cursor_type = "row"
        self.query_one("#query-input", TextArea).focus()

        self.begin_capture_print(self, stdout=True, stderr=True)
        if self.querymux_debug:
            # Only needed for the live view — logging.debug() calls always
            # reach DEBUG_LOG_PATH regardless (see __main__._configure_debug_logging),
            # this additionally mirrors them into the pane as they happen.
            handler = _RichLogHandler(self.query_one("#debug-log", RichLog))
            handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
            logging.getLogger().addHandler(handler)
            logger.debug("QueryMuxApp.on_mount: backend=%s target=%s api_url=%s", self.backend, self.target, self.api_url)
        if self.backend_proc is not None and self.backend_proc.stdout is not None:
            threading.Thread(target=self._pump_backend_output, daemon=True).start()

    def on_print(self, event: events.Print) -> None:
        # Textual event handler: fires for anything printed in this process
        # once begin_capture_print() is active (set up in on_mount above).
        # Without this capture, print() output would corrupt the terminal —
        # Textual owns the screen while the app is running.
        self.query_one("#debug-log", RichLog).write(event.text.rstrip("\n"))

    def _pump_backend_output(self) -> None:
        # Runs on its own thread (started from on_mount), since reading a
        # subprocess pipe line-by-line blocks. Forwards the FastAPI
        # backend's stdout/stderr (server.py, adapters — a separate OS
        # process, so on_print above can't see it) into the same debug pane.
        # call_from_thread is required: Textual widgets aren't thread-safe
        # to touch directly from outside the app's own event loop.
        assert self.backend_proc is not None and self.backend_proc.stdout is not None
        logger.debug("_pump_backend_output: thread started, pid=%s", self.backend_proc.pid)
        for line in self.backend_proc.stdout:
            try:
                self.call_from_thread(
                    self.query_one("#debug-log", RichLog).write, f"[backend] {line.rstrip()}"
                )
            except Exception:
                # The app has already shut down (e.g. Ctrl+Q terminated the
                # backend, which is still emitting its own shutdown lines)
                # — query_one() raises NoMatches on a torn-down widget tree,
                # and it runs right here, before call_from_thread even gets
                # invoked, not inside its deferred callback. Nothing left to
                # write to; stop rather than raising on a background thread.
                break

    def action_format_query(self) -> None:
        # Triggered by the Ctrl+P binding above. Entirely local/synchronous
        # (sqlparse) — never touches the backend, unlike action_run_query.
        logger.debug("action_format_query")
        text_area = self.query_one("#query-input", TextArea)
        formatted = sqlparse.format(text_area.text, reindent=True, keyword_case="upper")
        text_area.text = formatted

    def action_toggle_debug(self) -> None:
        # Triggered by the F9 binding above. Just flips visibility —
        # capture (on_print) and the backend pump keep running either way,
        # so toggling this on shows whatever was already logged, not just
        # what's printed from this point on.
        debug_log = self.query_one("#debug-log", RichLog)
        debug_log.display = not debug_log.display

    async def action_toggle_history(self) -> None:
        # Triggered by the Ctrl+R binding above. Just flips visibility, same
        # as action_toggle_debug — HistoryTable persists for the app's whole
        # lifetime rather than being pushed/popped, it just refetches (load)
        # each time it's shown so it reflects the current history.db, not a
        # stale snapshot from last time it was open.
        logger.debug("action_toggle_history")
        history_table = self.query_one("#history-table", HistoryTable)
        history_table.display = not history_table.display
        if history_table.display:
            history_table.focus()
            await history_table.load()
        else:
            self.query_one("#query-input", TextArea).focus()

    async def action_run_query(self) -> None:
        # Triggered by the F5 / Ctrl+Enter binding above. The only method
        # here that talks to the backend: POSTs the query text, then renders
        # one of three outcomes below — transport error, query error, or a
        # result set (SELECT) / affected-row count (everything else).
        text = self.query_one("#query-input", TextArea).text
        status = self.query_one("#status-line", Static)
        table = self.query_one("#results-table", DataTable)

        if not text.strip():
            logger.debug("action_run_query: empty query, nothing to send")
            status.update("Nothing to run.")
            return

        status.remove_class("-error")
        status.update("Running…")
        logger.debug("action_run_query: POST %s/query, %d chars", self.api_url, len(text))

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{self.api_url}/query", json={"text": text}, timeout=30)
                resp.raise_for_status()
                result = resp.json()
        except httpx.HTTPError as exc:
            logger.debug("action_run_query: request failed: %s", exc)
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
