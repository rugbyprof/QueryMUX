# QueryMUX

Type a query on the left, see the rows or the error on the right — a split-screen
terminal tool for comparing how different database engines behave.

## Status

Phase 1: SQLite backend. See `docs/requirements.md` (or the published requirements
artifact) for the full spec, rollout phases, and open questions.

## Architecture

    Textual TUI  -->  FastAPI backend  -->  DB adapter (translate + execute)

The TUI never talks to a database driver directly. It POSTs raw query text to a
local FastAPI server, which runs it through the active adapter's `translate()`
(cosmetic/parsing step — a no-op for SQL engines, real work for Mongo/Redis later)
and `execute()` (runs it, returns rows or an error) and logs the attempt to
`~/.querymux/history.db`.

## Running it

    pip install -e .
    querymux --backend sqlite --db ./sample.db

## Known gotchas

Point `--db` at a SQLite file that lives in a synced folder (Dropbox, OneDrive,
iCloud Drive) and you can hit a bare `disk I/O error` — SQLite's file locking
doesn't always get along with how those services sync in the background.
Use a plain local path.

## Keybindings

| Key         | Action                                   |
|-------------|-------------------------------------------|
| F5 / Ctrl+Enter | Run the current query (Ctrl+Enter depends on terminal support — see note below) |
| Ctrl+P      | Reformat the query (uppercase keywords, one clause per line) |
| Ctrl+R      | Toggle query history                      |
| Tab         | Switch focus between panes                |
| Ctrl+Q      | Quit                                      |

**Note on Ctrl+Enter:** many terminal emulators can't distinguish Ctrl+Enter from
plain Enter in raw input mode (no extended keyboard protocol), so it may silently
just insert a newline instead of running the query. `F5` is bound to the same
action and works everywhere — treat it as the reliable default and Ctrl+Enter as
a bonus on terminals that support it (Kitty, WezTerm, some iTerm2 configs).
