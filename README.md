# QueryMUX

Type a query on the left, see the rows or the error on the right — a split-screen
terminal tool for comparing how different database engines behave.

## Status

Phase 1: SQLite backend. See `docs/requirements.md` (or the published requirements
artifact) for the full spec, rollout phases, and open questions.

## Architecture

    Textual TUI  -->  FastAPI backend  -->  DB adapter (translate + execute)

<a href="https://res.cloudinary.com/cqd6nje4/image/upload/v1789159668/Screenshot_2026-09-11_at_3.45.18_PM_fkyc1c.png"><img src="https://res.cloudinary.com/cqd6nje4/image/upload/v1789159668/Screenshot_2026-09-11_at_3.45.18_PM_fkyc1c.png" width="400"></a>

The TUI never talks to a database driver directly. It POSTs raw query text to a
local FastAPI server, which runs it through the active adapter's `translate()`
(cosmetic/parsing step — a no-op for SQL engines, real work for Mongo/Redis later)
and `execute()` (runs it, returns rows or an error) and logs the attempt to
`~/.querymux/history.db`.

## Installing it

QueryMUX is a Python CLI, so the recommended way to install it is
[pipx](https://pipx.pypa.io) — it gives `querymux` its own isolated
environment and puts it on your `PATH` without touching your system Python.

If you don't have pipx yet:

    python3 -m pip install --user pipx
    pipx ensurepath

Then, to install QueryMUX straight from GitHub (no clone needed):

    pipx install git+https://github.com/rugbyprof/QueryMUX.git

Or, from a local clone:

    git clone git@github.com:rugbyprof/QueryMUX.git
    cd QueryMUX
    pipx install .

Either way, once installed you can run `querymux` from any shell, in any
directory:

    `querymux --backend sqlite --db ./sample.db`

Where:

- `querymux` is the command
- `--backend sqlite` chooses what adapter (db backend) to use
- `--db ./sample.db` chooses the sqlite db file

To upgrade later: `pipx upgrade querymux` (or `pipx reinstall querymux` after
a fresh `git pull` if you installed from a local clone). To remove it:
`pipx uninstall querymux`.

### Developing it

If you're working on QueryMUX itself, install it editable in a venv instead
so code changes take effect without reinstalling:

    git clone git@github.com:rugbyprof/QueryMUX.git
    cd QueryMUX
    python3 -m venv .venv && source .venv/bin/activate
    pip install -e .
    querymux --backend sqlite --db ./sample.db

## Known gotchas

Point `--db` at a SQLite file that lives in a synced folder (Dropbox, OneDrive,
iCloud Drive) and you can hit a bare `disk I/O error` — SQLite's file locking
doesn't always get along with how those services sync in the background.
Use a plain local path.

## Keybindings

| Key             | Action                                                                          |
| --------------- | ------------------------------------------------------------------------------- |
| F5 / Ctrl+Enter | Run the current query (Ctrl+Enter depends on terminal support — see note below) |
| Ctrl+P          | Reformat the query (uppercase keywords, one clause per line)                    |
| Ctrl+R          | Toggle query history                                                            |
| Tab             | Switch focus between panes                                                      |
| Ctrl+Q          | Quit                                                                            |

**Note on Ctrl+Enter:** many terminal emulators can't distinguish Ctrl+Enter from
plain Enter in raw input mode (no extended keyboard protocol), so it may silently
just insert a newline instead of running the query. `F5` is bound to the same
action and works everywhere — treat it as the reliable default and Ctrl+Enter as
a bonus on terminals that support it (Kitty, WezTerm, some iTerm2 configs).
