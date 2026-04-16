# NOTE

On a repo with ~100+ source files where AI Coding is doing multiple symbol lookups per session, this probably cuts navigation related tokens by 50-80%. On a 10 file script, it's breaking even or slightly negative. I do NOT recommend using this on smaller projects as it can increase token consumtion.

# blueprint

A small Python script that walks a project and writes two files:

- `PROJECT_CONTEXT.md` > a readable summary
- `PROJECT_INDEX.json` > all functions, class's, methods, and imports with file paths and line ranges

The JSON index is the point. Once it's built, you (or an AI like Claude Code) can ask "where is `handleRequest`?" and get back the file and line range without re-grepping the whole repo.

Stdlib only.

## Install

There's nothing to install. Drop `blueprint.py` somewhere on your path or just run it in place.

## Usage

Build both files for the current directory:

```
python blueprint.py
```

Or point it at a project:

```
python blueprint.py path/to/repo
```

### Querying

```
python blueprint.py find create_app          # locate by name
python blueprint.py find user                # substring search
python blueprint.py find User.display_name   # qualified method
python blueprint.py find foo --json          # raw JSON, for tools
python blueprint.py find foo --exact         # no substring fallback
```

A single match auto-prints the source. Multiple matches print just locations; pass `--source` to dump bodies for all of them.

### Showing a file or range

```
python blueprint.py show src/main.py
python blueprint.py show src/main.py:42
python blueprint.py show src/main.py:42-90
```

### Listing things

```
python blueprint.py list functions
python blueprint.py list classes
python blueprint.py list methods
python blueprint.py list imports
python blueprint.py list files
python blueprint.py list symbols
```

Filter with `--language python` or `--file src/api`.

### Rebuilding only the JSON

```
python blueprint.py refresh
```

## Options for the default build

```
--ignore foo,bar       additional ignore patterns (defaults already cover .git, node_modules, etc.)
--depth 4              max tree depth (default 5)
--output FILE          markdown filename
--index-output FILE    JSON filename
--no-index             skip the JSON
--no-markdown          skip the markdown
```

## Languages

Python uses the real AST. Signatures, docstrings, line ranges, and method/class nesting are exact

JavaScript, TypeScript, Go, and Rust use regex with brace counting for end of block detection. Ruby uses `def`/`end` balancing. These are good enough for a lookup index but won't catch all of the edge cases

## Index shape

```json
{
  "version": 1,
  "generated": "2026-04-16T01:00:00",
  "root": "/abs/path",
  "stats": { "files": 42, "functions": 120, "classes": 18, "methods": 60, "imports": 230 },
  "files": {
    "src/main.py": {
      "language": "python",
      "lines": 87,
      "imports": [{ "module": "fastapi", "names": [{"name": "FastAPI", "alias": null}], "line": 3 }],
      "symbols": [
        { "name": "create_app", "kind": "function", "line": 10, "end_line": 25,
          "signature": "create_app(config)", "docstring": "..." }
      ]
    }
  },
  "symbols": {
    "create_app": [{ "file": "src/main.py", "line": 10, "end_line": 25, "kind": "function" }]
  }
}
```

`files` is the per file detail. `symbols` is a reverse map keyed by name (and `Class.method`) for O(1) lookup.

## For AI agents

Run `python blueprint.py` once on the project. After that:

- `python blueprint.py find <name> --json` gives a structured location for any symbol
- Reading `PROJECT_INDEX.json` directly works too, and is fast to grep
- `python blueprint.py show <file>:<a>-<b>` returns numbered lines, ready to read

Re run `python blueprint.py refresh` after editing to keep the index up to date.
