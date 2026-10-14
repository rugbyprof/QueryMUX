"""MongoDB adapter. Unlike the SQL adapters, `translate()` does real work here:
the query pane holds mongosh-style shell text, and translate() parses it into
a small "operation" that execute() runs with pymongo. No JavaScript engine is
involved; the parser understands the subset of mongosh that queries use:

    db.purchases.find({ customer_id: 42 }, { amount: 1 }).sort({ purchase_date: -1 }).limit(10)
    db.purchases.findOne({ _id: 7 })
    db.purchases.aggregate([ { $group: { _id: "$state_code", n: { $sum: 1 } } } ])
    db.purchases.countDocuments({ department: "Games" })
    db.purchases.estimatedDocumentCount()
    db.purchases.distinct("department", { amount: { $gt: 100 } })
    db.purchases.insertOne({...})   insertMany([...])
    db.purchases.updateOne(filter, update)   updateMany(...)   replaceOne(...)
    db.purchases.deleteOne(filter)  deleteMany(filter)
    db.purchases.createIndex({ customer_id: 1 })   dropIndex("customer_id_1")   getIndexes()
    db.purchases.find({...}).explain()          ... .explain("executionStats")
    db.purchases.aggregate([...]).explain()
    db.getSiblingDB("idx_1m").purchases.find()  db.getCollection("weird name").find()
    db.runCommand({ find: "purchases", filter: {...} })
    show dbs     show collections     use idx_1m
    { "find": "purchases", "limit": 5 }        a raw command document, as JSON

Values can be written as in mongosh: unquoted keys, 'single' or "double"
quoted strings, ISODate("2025-03-01"), new Date("..."), NumberDecimal("9.99"),
NumberInt(5), NumberLong(5), ObjectId("..."), true/false/null, and // or
/* */ comments. Several statements separated by ';' run in order; the last
one's result is shown (the same convention as the SQL adapters).

The target is a MongoDB connection string. Its path names the database:

    mongodb://localhost:27017/idx_10k?directConnection=true

An empty target means $MONGO_URL, else mongodb://localhost:27017 with the
`test` database, like bare mongosh. `use <db>` switches database for the rest
of the session.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bson import ObjectId
from bson.decimal128 import Decimal128
from bson.int64 import Int64
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from .base import Adapter, QueryResult, MAX_ROWS

DEFAULT_URL = "mongodb://localhost:27017/?directConnection=true"


# --------------------------------------------------------------------------- #
# Parsing mongosh-style text
# --------------------------------------------------------------------------- #

class ParseError(ValueError):
    pass


_TOKEN = re.compile(r"""
      (?P<ws>\s+|//[^\n]*|/\*.*?\*/)
    | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
    | (?P<name>[$A-Za-z_][$\w]*)
    | (?P<punct>[{}\[\]():,.;])
""", re.VERBOSE | re.DOTALL)


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            snippet = text[pos:pos + 20].splitlines()[0]
            raise ParseError(f"can't read this part of the query: {snippet!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind != "ws":
            out.append((kind, m.group()))
    return out


def _unquote(s: str) -> str:
    body = s[1:-1]
    if s[0] == "'":
        body = body.replace('\\"', '"').replace("\\'", "'").replace('"', '\\"')
    return json.loads(f'"{body}"')


def _iso_date(text: str) -> datetime:
    t = text.strip().replace("Z", "+00:00")
    if len(t) == 10:
        t += "T00:00:00+00:00"
    d = datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


_CONSTRUCTORS = {
    "ISODate": lambda *a: _iso_date(a[0]) if a else datetime.now(timezone.utc),
    "Date": lambda *a: _iso_date(a[0]) if a else datetime.now(timezone.utc),
    "NumberDecimal": lambda a: Decimal128(Decimal(str(a))),
    "Decimal128": lambda a: Decimal128(Decimal(str(a))),
    "NumberInt": lambda a: int(a),
    "NumberLong": lambda a: Int64(int(a)),
    "ObjectId": lambda *a: ObjectId(a[0]) if a else ObjectId(),
}


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.t = tokens
        self.i = 0

    # helpers ---------------------------------------------------------------
    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        j = self.i + offset
        return self.t[j] if j < len(self.t) else None

    def take(self, value: str | None = None, kind: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        wanted = repr(value) if value is not None else ("a name" if kind == "name" else kind)
        if tok is None:
            raise ParseError(f"the query ended early; expected {wanted}")
        if (value is not None and tok[1] != value) or (kind is not None and tok[0] != kind):
            raise ParseError(f"expected {wanted}, found {tok[1]!r}")
        self.i += 1
        return tok

    def at(self, value: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[1] == value

    # values ----------------------------------------------------------------
    def value(self) -> Any:
        tok = self.peek()
        if tok is None:
            raise ParseError("the query ended early; expected a value")
        kind, text = tok
        if text == "{":
            return self.obj()
        if text == "[":
            return self.arr()
        if kind == "str":
            self.i += 1
            return _unquote(text)
        if kind == "num":
            self.i += 1
            return float(text) if any(c in text for c in ".eE") else int(text)
        if kind == "name":
            self.i += 1
            if text in ("true", "false"):
                return text == "true"
            if text in ("null", "undefined"):
                return None
            if text == "new":
                text = self.take(kind="name")[1]
            if text in _CONSTRUCTORS:
                args = self.args()
                try:
                    return _CONSTRUCTORS[text](*args)
                except Exception as exc:
                    raise ParseError(f"{text}({', '.join(map(repr, args))}): {exc}") from exc
            raise ParseError(f"unknown name {text!r} (quote it if it's a string)")
        raise ParseError(f"unexpected {text!r}")

    def obj(self) -> dict:
        self.take("{")
        out: dict = {}
        while not self.at("}"):
            kind, text = self.take()
            if kind == "str":
                key = _unquote(text)
            elif kind in ("name", "num"):
                key = text
            else:
                raise ParseError(f"expected a field name, found {text!r}")
            self.take(":")
            out[key] = self.value()
            if not self.at("}"):
                self.take(",")
        self.take("}")
        return out

    def arr(self) -> list:
        self.take("[")
        out = []
        while not self.at("]"):
            out.append(self.value())
            if not self.at("]"):
                self.take(",")
        self.take("]")
        return out

    def args(self) -> list:
        self.take("(")
        out = []
        while not self.at(")"):
            out.append(self.value())
            if not self.at(")"):
                self.take(",")
        self.take(")")
        return out


@dataclass
class Op:
    """One parsed statement, ready for execute()."""
    kind: str                                   # "collection", "db", "show", "use", "command"
    database: str | None = None                 # getSiblingDB(...) override
    collection: str | None = None
    method: str = ""
    args: list = field(default_factory=list)
    chain: list[tuple[str, list]] = field(default_factory=list)   # .sort(), .limit(), .explain(), ...


_SHOW_USE = re.compile(r"^\s*(show|use)\s+([\w.$-]+)\s*;?\s*$", re.IGNORECASE)


def parse_statement(text: str) -> Op:
    m = _SHOW_USE.match(text)
    if m:
        return Op(kind=m.group(1).lower(), method=m.group(2))
    toks = _tokens(text)
    if not toks:
        raise ParseError("empty statement")
    p = _Parser(toks)

    if toks[0][1] == "{":                       # a raw command document
        cmd = p.obj()
        if p.peek() is not None:
            raise ParseError(f"unexpected {p.peek()[1]!r} after the command document")
        return Op(kind="command", args=[cmd])

    p.take("db")
    op = Op(kind="collection")
    p.take(".")
    name = p.take(kind="name")[1]
    if name == "getSiblingDB":
        op.database = str(p.args()[0])
        p.take(".")
        name = p.take(kind="name")[1]
    if name == "getCollection":
        op.collection = str(p.args()[0])
    elif name in ("runCommand", "adminCommand", "getCollectionNames", "stats", "dropDatabase"):
        op.kind, op.method, op.args = "db", name, p.args()
    elif p.at("("):
        raise ParseError(f"db.{name}() isn't supported here")
    else:
        op.collection = name
    if op.kind == "collection":
        p.take(".")
        op.method = p.take(kind="name")[1]
        op.args = p.args()
    while p.at("."):
        p.take(".")
        method = p.take(kind="name")[1]
        op.chain.append((method, p.args() if p.at("(") else []))
    if p.peek() is not None:
        raise ParseError(f"unexpected {p.peek()[1]!r} at the end of the statement")
    return op


def split_statements(text: str) -> list[str]:
    """Split on ';' outside strings, brackets, and comments."""
    out, depth, start, i, quote = [], 0, 0, 0, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
        elif text.startswith("/*", i):
            i = text.find("*/", i + 2) + 1
            if i == 0:
                break
        elif c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        elif c == ";" and depth == 0:
            out.append(text[start:i])
            start = i + 1
        i += 1
    out.append(text[start:])
    return [s for s in out if _tokens_or_keyword(s)]


def _tokens_or_keyword(s: str) -> bool:
    stripped = re.sub(r"//[^\n]*|/\*.*?\*/", "", s, flags=re.DOTALL).strip()
    return bool(stripped)


# --------------------------------------------------------------------------- #
# Turning documents into a results grid
# --------------------------------------------------------------------------- #

def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Decimal128):
        return str(v.to_decimal())
    if isinstance(v, datetime):                 # BSON dates are always UTC
        text = v.isoformat()
        return text.replace("+00:00", "Z") if v.tzinfo else text + "Z"
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=lambda x: _cell(x) if not isinstance(x, (dict, list)) else str(x))
    return str(v)                               # ObjectId, Int64, bytes, ...


def _grid(docs: list[dict]) -> QueryResult:
    docs = docs[:MAX_ROWS]
    columns: list[str] = []
    seen: set[str] = set()
    for d in docs:
        for k in d:
            if k not in seen:
                seen.add(k)
                columns.append(k)
    rows = [[_cell(d.get(c)) for c in columns] for d in docs]
    return QueryResult(columns=columns, rows=rows, row_count=len(rows))


def _one_column(name: str, values: list) -> QueryResult:
    values = values[:MAX_ROWS]
    return QueryResult(columns=[name], rows=[[_cell(v)] for v in values], row_count=len(values))


def _explain_rows(explain: dict) -> QueryResult:
    """The useful part of an explain document, one line per row."""
    lines: list[str] = []
    stages = explain.get("stages")
    cursor = stages[0]["$cursor"] if stages and "$cursor" in stages[0] else explain
    planner = cursor.get("queryPlanner", {})
    winning = planner.get("winningPlan", {})

    def walk(node: dict, depth: int) -> None:
        if not node:
            return
        bits = [node.get("stage", "?")]
        if "indexName" in node:
            bits.append(node["indexName"])
            if node.get("indexBounds"):
                bits.append(json.dumps(node["indexBounds"], default=str))
        if node.get("filter"):
            bits.append("filter " + json.dumps(node["filter"], default=str))
        if "sortPattern" in node:
            bits.append("by " + json.dumps(node["sortPattern"]))
        lines.append("  " * depth + "  ".join(bits))
        for child in [node.get("inputStage")] + node.get("inputStages", []):
            if child:
                walk(child, depth + 1)

    walk(winning.get("queryPlan", winning), 0)
    st = cursor.get("executionStats")
    if st:
        lines.append(f"examined: {st.get('totalKeysExamined', 0):,} index keys, "
                     f"{st.get('totalDocsExamined', 0):,} documents -> returned "
                     f"{st.get('nReturned', 0):,} ({st.get('executionTimeMillis', 0)} ms)")
    for s in (stages or [])[1:]:
        name = next((k for k in s if k.startswith("$")), "?")
        extra = f"  returned {s['nReturned']:,}" if "nReturned" in s else ""
        lines.append(f"then {name}{extra}")
    if not lines:
        lines = json.dumps(explain, indent=2, default=str).splitlines()
    return _one_column("plan", lines)


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #

def _display_target(url: str) -> str:
    """Connection string minus the password (title bar, history.db)."""
    parts = urlsplit(url)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
        return urlunsplit(parts._replace(netloc=netloc))
    return url


def _database_from_url(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    return path or "test"


_FIND_OPTIONS = {"sort", "limit", "skip", "projection", "hint", "collation", "maxTimeMS", "batchSize"}


class MongoAdapter(Adapter):
    EDITOR_LANGUAGE = "javascript"

    def __init__(self, url: str):
        import os
        self.url = url or os.environ.get("MONGO_URL", "") or DEFAULT_URL
        self.target = _display_target(self.url)
        self.database = _database_from_url(self.url)
        self._client: MongoClient | None = None

    def client(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(self.url, serverSelectionTimeoutMS=5000)
        return self._client

    def translate(self, query_text: str) -> list[Op]:
        # Parse errors become an "error" op rather than an exception, so the
        # TUI shows the message itself, not a Python class name.
        statements = split_statements(query_text)
        if not statements:
            return []
        try:
            return [parse_statement(s) for s in statements]
        except ParseError as exc:
            first = statements[0].lstrip().split(None, 1)[0].lower() if statements[0].strip() else ""
            hint = ""
            if first in ("select", "insert", "update", "delete", "with", "create"):
                hint = " This is MongoDB, not SQL: write db.<collection>.find({...}) or .aggregate([...])."
            return [Op(kind="error", args=[f"Couldn't parse the query: {exc}.{hint}"])]

    def execute(self, native_query: list[Op]) -> QueryResult:
        if not native_query:
            return QueryResult(error="Empty query.")
        if native_query[0].kind == "error":
            return QueryResult(error=native_query[0].args[0])
        result = QueryResult()
        try:
            for op in native_query:
                result = self._run(op)
        except PyMongoError as exc:
            msg = str(exc).split(", full error:")[0]
            return QueryResult(error=f"MongoDB: {msg}")
        except (ParseError, ValueError, TypeError, IndexError) as exc:
            return QueryResult(error=str(exc))
        return result

    # --------------------------------------------------------------------- #

    def _run(self, op: Op) -> QueryResult:
        client = self.client()
        if op.kind == "use":
            self.database = op.method
            return _one_column("switched to db", [self.database])
        if op.kind == "show":
            what = op.method.lower()
            if what in ("dbs", "databases"):
                return _grid([{"name": d["name"], "sizeOnDisk": d.get("sizeOnDisk")}
                              for d in client.list_databases()])
            if what in ("collections", "tables"):
                return _one_column("collection", sorted(client[self.database].list_collection_names()))
            raise ParseError(f"show {op.method}: use 'show dbs' or 'show collections'")

        db = client[op.database or self.database]
        if op.kind == "command":
            return self._command(db, op.args[0], op.chain)
        if op.kind == "db":
            if op.method == "runCommand":
                return self._command(db, op.args[0], op.chain)
            if op.method == "adminCommand":
                return self._command(client.admin, op.args[0], op.chain)
            if op.method == "getCollectionNames":
                return _one_column("collection", sorted(db.list_collection_names()))
            if op.method == "stats":
                return _grid([db.command("dbStats")])
            if op.method == "dropDatabase":
                client.drop_database(db.name)
                return QueryResult(row_count=0)

        coll = db[op.collection]
        m, a = op.method, op.args
        explain = next((c for c in op.chain if c[0] == "explain"), None)
        chain = [c for c in op.chain if c[0] != "explain"]

        if m in ("find", "findOne"):
            cmd: dict = {"find": coll.name, "filter": a[0] if a else {}}
            if len(a) > 1:
                cmd["projection"] = a[1]
            for name, cargs in chain:
                if name in _FIND_OPTIONS:
                    cmd[name] = cargs[0] if cargs else None
                elif name in ("count", "itcount", "size"):
                    return _one_column("count", [coll.count_documents(cmd["filter"])])
                elif name in ("toArray", "pretty"):
                    pass
                else:
                    raise ParseError(f".{name}() isn't supported after find()")
            if m == "findOne":
                cmd["limit"] = 1
            if explain:
                return self._explain(db, cmd, explain[1])
            cmd.setdefault("limit", MAX_ROWS)       # the grid shows at most MAX_ROWS anyway
            return _grid(list(db.cursor_command(cmd)))
        if m == "aggregate":
            pipeline = a[0] if a else []
            if explain:
                return self._explain(db, {"aggregate": coll.name, "pipeline": pipeline, "cursor": {}}, explain[1])
            docs = []
            for d in coll.aggregate(pipeline, **(a[1] if len(a) > 1 else {})):
                docs.append(d)
                if len(docs) >= MAX_ROWS:
                    break
            return _grid(docs)
        if m == "countDocuments":
            return _one_column("count", [coll.count_documents(a[0] if a else {})])
        if m in ("estimatedDocumentCount", "count"):
            return _one_column("count", [coll.estimated_document_count() if m != "count" or not a
                                         else coll.count_documents(a[0])])
        if m == "distinct":
            return _one_column(str(a[0]), coll.distinct(a[0], a[1] if len(a) > 1 else None))
        if m == "insertOne":
            r = coll.insert_one(a[0])
            return QueryResult(columns=["insertedId"], rows=[[_cell(r.inserted_id)]], row_count=1)
        if m == "insertMany":
            r = coll.insert_many(a[0])
            return _one_column("insertedId", r.inserted_ids)
        if m in ("updateOne", "updateMany", "replaceOne"):
            fn = {"updateOne": coll.update_one, "updateMany": coll.update_many,
                  "replaceOne": coll.replace_one}[m]
            r = fn(a[0], a[1], **(a[2] if len(a) > 2 else {}))
            return QueryResult(row_count=r.modified_count + (1 if r.upserted_id is not None else 0))
        if m in ("deleteOne", "deleteMany"):
            r = (coll.delete_one if m == "deleteOne" else coll.delete_many)(a[0] if a else {})
            return QueryResult(row_count=r.deleted_count)
        if m == "createIndex":
            keys = list((a[0] or {}).items())
            name = coll.create_index(keys, **(a[1] if len(a) > 1 else {}))
            return _one_column("index created", [name])
        if m == "dropIndex":
            coll.drop_index(a[0] if isinstance(a[0], str) else list(a[0].items()))
            return QueryResult(row_count=0)
        if m in ("getIndexes", "indexes"):
            return _grid(list(coll.list_indexes()))
        if m == "drop":
            coll.drop()
            return QueryResult(row_count=0)
        if m == "stats":
            return _grid([db.command("collStats", coll.name)])
        raise ParseError(f"db.{coll.name}.{m}() isn't supported here")

    def _command(self, db, cmd: dict, chain: list) -> QueryResult:
        cmd = {k: v for k, v in cmd.items() if k != "//"}   # field-guide query files carry comments there
        if chain and chain[0][0] == "explain":
            return self._explain(db, cmd, chain[0][1])
        name = next(iter(cmd), "")
        if name in ("find", "aggregate"):
            docs = []
            for d in db.cursor_command(cmd):
                docs.append(d)
                if len(docs) >= MAX_ROWS:
                    break
            return _grid(docs)
        reply = db.command(cmd)
        reply = {k: v for k, v in reply.items()
                 if not k.startswith("$") and k not in ("operationTime", "electionId", "opTime")}
        return _grid([reply])

    def _explain(self, db, cmd: dict, args: list) -> QueryResult:
        verbosity = args[0] if args and isinstance(args[0], str) else "executionStats"
        if args and args[0] is True:
            verbosity = "allPlansExecution"
        return _explain_rows(db.command({"explain": cmd, "verbosity": verbosity}))
