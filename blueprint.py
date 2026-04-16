#!/usr/bin/env python3
"""Project context generator + queryable code index.

Walks a project and writes two files:
    PROJECT_CONTEXT.md   readable summary (tree, stack, key files)
    PROJECT_INDEX.json   every function, class, method, and import with line ranges

Run with no args to build both. Subcommands query the index:
    find <name>          look up a symbol
    show <file>[:a-b]    print a file or line range
    list <kind>          enumerate symbols
    refresh              rebuild the JSON only

Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

# Defaults

DEFAULT_IGNORES = {
    ".git",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".next",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "*.pyc",
    "*.lock",
}

# files we always summarize when found
KEY_FILENAMES = {
    "README.md",
    "readme.md",
    "README.rst",
    "main.py",
    "app.py",
    "server.py",
    "config.py",
    "settings.py",
    "index.js",
    "index.ts",
    "index.tsx",
    "index.jsx",
    "main.js",
    "main.ts",
    "server.js",
    "server.ts",
    "app.js",
    "app.ts",
}

# filename / dirname substrings that flag an interesting file
KEY_SUBSTRINGS = ("schema", "model", "router", "controller")

# config files used to guess the stack
STACK_SIGNALS = {
    "package.json": "Node.js / JavaScript",
    "pnpm-lock.yaml": "Node.js (pnpm)",
    "yarn.lock": "Node.js (yarn)",
    "tsconfig.json": "TypeScript",
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "requirements.txt": "Python",
    "Pipfile": "Python (pipenv)",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)",
    "build.gradle.kts": "Kotlin (Gradle)",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "mix.exs": "Elixir",
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "next.config.js": "Next.js",
    "next.config.mjs": "Next.js",
    "vite.config.ts": "Vite",
    "vite.config.js": "Vite",
    "svelte.config.js": "Svelte",
    "nuxt.config.ts": "Nuxt",
}

# extension -> language (fallback when no config file is present)
EXT_LANGUAGES = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript (JSX)",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (TSX)",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".swift": "Swift",
    ".c": "C",
    ".h": "C/C++ headers",
    ".cpp": "C++",
    ".cs": "C#",
}

SUMMARY_LINE_LIMIT = 50
TREE_FILE_CAP_PER_DIR = 200  # keeps the tree readable in huge dirs


def is_ignored(name: str, patterns: set[str]) -> bool:
    """True if name matches any literal or glob pattern."""
    if name in patterns:
        return True
    for pat in patterns:
        if any(ch in pat for ch in "*?[") and fnmatch.fnmatch(name, pat):
            return True
    return False


def build_tree(root: Path, ignores: set[str], max_depth: int) -> tuple[str, int]:
    """Returns (ascii_tree, file_count)."""
    lines: list[str] = [root.name + "/"]
    file_count = 0

    def walk(directory: Path, prefix: str, depth: int) -> None:
        nonlocal file_count
        if depth > max_depth:
            return
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except (PermissionError, OSError):
            return

        entries = [e for e in entries if not is_ignored(e.name, ignores)]

        # cap huge dirs
        truncated = False
        if len(entries) > TREE_FILE_CAP_PER_DIR:
            entries = entries[:TREE_FILE_CAP_PER_DIR]
            truncated = True

        for i, entry in enumerate(entries):
            last = i == len(entries) - 1 and not truncated
            connector = "└── " if last else "├── "
            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                extension = "    " if last else "│   "
                walk(entry, prefix + extension, depth + 1)
            else:
                file_count += 1
                lines.append(f"{prefix}{connector}{entry.name}")

        if truncated:
            lines.append(f"{prefix}└── … ({TREE_FILE_CAP_PER_DIR}+ entries truncated)")

    walk(root, "", 1)
    return "\n".join(lines), file_count


# Picking and summarizing the interesting files

SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".rb"}


def is_key_file(path: Path, root: Path) -> bool:
    name = path.name
    if name in KEY_FILENAMES:
        return True
    lower = name.lower()
    stem = lower.rsplit(".", 1)[0]
    if any(token in stem for token in KEY_SUBSTRINGS):
        return True
    # also pick up files inside dirs like models/, routers/, controllers/
    if path.suffix.lower() in SOURCE_SUFFIXES:
        try:
            rel_parts = path.relative_to(root).parts[:-1]
        except ValueError:
            rel_parts = ()
        for part in rel_parts:
            part_lower = part.lower()
            if any(token in part_lower for token in KEY_SUBSTRINGS):
                return True
    return False


def collect_key_files(root: Path, ignores: set[str], max_depth: int) -> list[Path]:
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(directory.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if is_ignored(entry.name, ignores):
                continue
            if entry.is_dir():
                walk(entry, depth + 1)
            elif is_key_file(entry, root):
                found.append(entry)

    walk(root, 1)
    found.sort(key=lambda p: (len(p.parts), str(p).lower()))
    return found


def _read_first_lines(path: Path, line_limit: int = SUMMARY_LINE_LIMIT) -> list[str]:
    """First N lines, IO errors return []."""
    out: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(line_limit):
                line = fh.readline()
                if not line:
                    break
                out.append(line.rstrip("\n"))
    except OSError:
        return []
    return out


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _first_sentences(text: str, count: int = 2) -> str:
    text = text.strip()
    if not text:
        return ""
    parts = _SENTENCE_SPLIT.split(text, maxsplit=count)
    chosen = " ".join(parts[:count]).strip()
    return re.sub(r"\s+", " ", chosen)


def _python_docstring(lines: list[str]) -> str | None:
    """Module docstring, or None."""
    i = 0
    # skip shebang / encoding decl / blank lines
    while i < len(lines) and (
        not lines[i].strip()
        or lines[i].lstrip().startswith("#!")
        or "coding" in lines[i] and lines[i].lstrip().startswith("#")
    ):
        i += 1
    if i >= len(lines):
        return None
    stripped = lines[i].lstrip()
    for quote in ('"""', "'''"):
        if stripped.startswith(quote):
            # one-liner
            rest = stripped[len(quote):]
            if quote in rest:
                return rest.split(quote, 1)[0].strip()
            collected = [rest]
            for j in range(i + 1, len(lines)):
                if quote in lines[j]:
                    collected.append(lines[j].split(quote, 1)[0])
                    return "\n".join(collected).strip()
                collected.append(lines[j])
            return "\n".join(collected).strip()
    return None


def _leading_block_comment(lines: list[str]) -> str | None:
    """Leading // or /* */ block from a JS/TS file."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return None

    first = lines[i].lstrip()

    # /* ... */
    if first.startswith("/*"):
        body: list[str] = []
        rest = first[2:]
        if "*/" in rest:
            return rest.split("*/", 1)[0].strip(" *")
        body.append(rest)
        for j in range(i + 1, len(lines)):
            if "*/" in lines[j]:
                body.append(lines[j].split("*/", 1)[0])
                break
            body.append(lines[j])
        cleaned = [ln.strip().lstrip("*").strip() for ln in body]
        return " ".join(c for c in cleaned if c).strip() or None

    # consecutive //
    if first.startswith("//"):
        collected: list[str] = []
        for j in range(i, len(lines)):
            stripped = lines[j].lstrip()
            if stripped.startswith("//"):
                collected.append(stripped[2:].strip())
            elif not stripped:
                continue
            else:
                break
        return " ".join(c for c in collected if c).strip() or None

    return None


def _readme_summary(lines: list[str]) -> str | None:
    """First real paragraph of a README (skip headings/badges)."""
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith("#"):
            if paragraph:
                break
            continue
        if stripped.startswith(("![", "[!", "<")):
            # badges / html / images
            continue
        paragraph.append(stripped)
    if not paragraph:
        return None
    return " ".join(paragraph)


def _heuristic_summary(path: Path, lines: list[str]) -> str:
    """Fallback when no docstring/comment is present."""
    text = "\n".join(lines)
    suffix = path.suffix.lower()

    if suffix == ".py":
        classes = re.findall(r"^\s*class\s+(\w+)", text, re.MULTILINE)
        functions = re.findall(r"^\s*def\s+(\w+)", text, re.MULTILINE)
        bits = []
        if classes:
            bits.append(f"defines class{'es' if len(classes) > 1 else ''} "
                        + ", ".join(classes[:3]))
        if functions:
            bits.append(f"function{'s' if len(functions) > 1 else ''} "
                        + ", ".join(functions[:3]))
        if bits:
            return f"Python module that {'; '.join(bits)}."

    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        exports = re.findall(
            r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)",
            text,
        )
        components = re.findall(r"function\s+([A-Z]\w+)\s*\(", text)
        bits = []
        if exports:
            bits.append("exports " + ", ".join(dict.fromkeys(exports))[:120])
        if components and not exports:
            bits.append("defines " + ", ".join(dict.fromkeys(components))[:120])
        if bits:
            kind = "TypeScript" if suffix.startswith(".ts") else "JavaScript"
            return f"{kind} module that {'; '.join(bits)}."

    nonblank = [ln for ln in lines if ln.strip()]
    if not nonblank:
        return "Empty or whitespace-only file."
    return f"Source file ({len(nonblank)} non-blank lines in first {SUMMARY_LINE_LIMIT})."


def summarize_file(path: Path) -> str:
    lines = _read_first_lines(path)
    if not lines:
        return "(unreadable or empty file)"

    name_lower = path.name.lower()
    suffix = path.suffix.lower()

    if name_lower.startswith("readme"):
        summary = _readme_summary(lines)
        if summary:
            return _first_sentences(summary)

    if suffix == ".py":
        doc = _python_docstring(lines)
        if doc:
            return _first_sentences(doc)

    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        comment = _leading_block_comment(lines)
        if comment:
            return _first_sentences(comment)

    return _first_sentences(_heuristic_summary(path, lines))


def detect_metadata(root: Path, ignores: set[str], max_depth: int) -> dict:
    """Find config files, language counts, and the implied stack."""
    config_files: list[str] = []
    stack: set[str] = set()
    extension_counts: dict[str, int] = {}

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(directory.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if is_ignored(entry.name, ignores):
                continue
            if entry.is_dir():
                walk(entry, depth + 1)
                continue
            rel = entry.relative_to(root).as_posix()
            if entry.name in STACK_SIGNALS and depth <= 3:
                config_files.append(rel)
                stack.add(STACK_SIGNALS[entry.name])
            ext = entry.suffix.lower()
            if ext in EXT_LANGUAGES:
                extension_counts[ext] = extension_counts.get(ext, 0) + 1

    walk(root, 1)

    # also surface the dominant languages by file count
    if extension_counts:
        top_exts = sorted(extension_counts.items(), key=lambda kv: -kv[1])[:3]
        for ext, _ in top_exts:
            stack.add(EXT_LANGUAGES[ext])

    return {
        "config_files": sorted(config_files),
        "stack": sorted(stack),
        "extension_counts": extension_counts,
    }


def render_document(
    root: Path,
    tree: str,
    metadata: dict,
    key_summaries: list[tuple[Path, str]],
    index: dict | None = None,
) -> str:
    timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stack = ", ".join(metadata["stack"]) if metadata["stack"] else "Unknown"

    out: list[str] = []
    out.append(f"# Project Context: {root.name}")
    out.append(f"Generated: {timestamp}")
    out.append(f"Stack: {stack}")
    out.append("")

    if index is not None:
        stats = index.get("stats", {})
        out.append("## Code Index")
        out.append(
            f"`{INDEX_FILENAME}` indexes "
            f"**{stats.get('functions', 0)} functions**, "
            f"**{stats.get('classes', 0)} classes** "
            f"(+ {stats.get('methods', 0)} methods), and "
            f"**{stats.get('imports', 0)} imports** across "
            f"{stats.get('files', 0)} source files."
        )
        out.append("")
        out.append("Query it from the CLI:")
        out.append("```")
        out.append("python blueprint.py find <symbol>     # locate by name")
        out.append("python blueprint.py show <file>:42-90 # print a line range")
        out.append("python blueprint.py list functions    # enumerate symbols")
        out.append("```")
        out.append("")

    if metadata["config_files"]:
        out.append("## Detected Config Files")
        for cf in metadata["config_files"]:
            out.append(f"- `{cf}`")
        out.append("")

    if metadata["extension_counts"]:
        out.append("## Language Footprint")
        ranked = sorted(
            metadata["extension_counts"].items(), key=lambda kv: -kv[1]
        )
        for ext, count in ranked:
            out.append(f"- {EXT_LANGUAGES[ext]} (`{ext}`): {count} file(s)")
        out.append("")

    out.append("## Directory Structure")
    out.append("```")
    out.append(tree)
    out.append("```")
    out.append("")

    if key_summaries:
        out.append("## Key Files")
        for path, summary in key_summaries:
            rel = path.relative_to(root).as_posix()
            out.append(f"### {rel}")
            out.append(summary if summary else "(no summary available)")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


# Symbol extraction

INDEX_VERSION = 1
INDEX_FILENAME = "PROJECT_INDEX.json"

# suffix -> language used by the indexer
LANGUAGE_BY_EXT = {
    ".py":  "python",
    ".js":  "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts":  "typescript",
    ".tsx": "typescript",
    ".go":  "go",
    ".rs":  "rust",
    ".rb":  "ruby",
}


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _signature_from_args(node: ast.AST) -> str:
    """`name(args)` using ast.unparse when available."""
    args_node = getattr(node, "args", None)
    name = getattr(node, "name", "")
    if args_node is None:
        return f"{name}()"
    try:
        return f"{name}({ast.unparse(args_node)})"
    except (AttributeError, ValueError):
        # python < 3.9: positional names only
        names = [a.arg for a in getattr(args_node, "args", [])]
        return f"{name}({', '.join(names)})"


def _end_line(node: ast.AST, fallback: int) -> int:
    return getattr(node, "end_lineno", None) or fallback


def extract_python_index(source: str) -> dict:
    """Imports + top-level defs + classes (with methods) via ast."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"error": f"SyntaxError: {exc.msg} (line {exc.lineno})",
                "imports": [], "symbols": []}

    imports: list[dict] = []
    symbols: list[dict] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "module": alias.name,
                    "alias": alias.asname,
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = ("." * (node.level or 0)) + (node.module or "")
            imports.append({
                "module": module,
                "names": [
                    {"name": a.name, "alias": a.asname} for a in node.names
                ],
                "line": node.lineno,
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({
                "name": node.name,
                "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                "line": node.lineno,
                "end_line": _end_line(node, node.lineno),
                "signature": _signature_from_args(node),
                "docstring": ast.get_docstring(node),
            })
        elif isinstance(node, ast.ClassDef):
            methods: list[dict] = []
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({
                        "name": child.name,
                        "kind": "async_method" if isinstance(child, ast.AsyncFunctionDef) else "method",
                        "line": child.lineno,
                        "end_line": _end_line(child, child.lineno),
                        "signature": _signature_from_args(child),
                        "docstring": ast.get_docstring(child),
                    })
            bases = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))
                except (AttributeError, ValueError):
                    bases.append(getattr(b, "id", "?"))
            symbols.append({
                "name": node.name,
                "kind": "class",
                "line": node.lineno,
                "end_line": _end_line(node, node.lineno),
                "bases": bases,
                "docstring": ast.get_docstring(node),
                "methods": methods,
            })

    return {"imports": imports, "symbols": symbols}


# regex extractors for everything else

_JS_FUNC      = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\(([^)]*)\)")
_JS_CLASS     = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?class\s+(\w+)(?:\s+extends\s+([\w.]+))?")
_JS_ARROW     = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:const|let|var)\s+(\w+)\s*(?::\s*[^=]+)?=\s*(?:async\s*)?\(([^)]*)\)\s*(?::\s*[^=]+)?=>")
_JS_METHOD    = re.compile(r"^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|async\s+|get\s+|set\s+)*(\w+)\s*\(([^)]*)\)\s*[:{]")
_JS_IMPORT_FROM = re.compile(r"""^\s*import\s+(?:(?:type\s+)?(\{[^}]+\}|\*\s+as\s+\w+|\w+)(?:\s*,\s*(\{[^}]+\}|\*\s+as\s+\w+|\w+))?\s+from\s+)?['"]([^'"]+)['"]""")

_GO_FUNC   = re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(([^)]*)\)")
_GO_TYPE   = re.compile(r"^type\s+(\w+)\s+(struct|interface)\b")
_GO_IMPORT = re.compile(r'^\s*"([^"]+)"')

_RUST_FUNC  = re.compile(r"^(?:pub\s+(?:\([^)]*\)\s+)?)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)")
_RUST_TYPE  = re.compile(r"^(?:pub\s+(?:\([^)]*\)\s+)?)?(struct|enum|trait|type|impl)\s+(\w+)")
_RUST_USE   = re.compile(r"^\s*use\s+([^;]+);")

_RB_DEF     = re.compile(r"^\s*def\s+(?:self\.)?(\w[\w?!=]*)\s*(?:\(([^)]*)\))?")
_RB_CLASS   = re.compile(r"^\s*(class|module)\s+(\w+)(?:\s*<\s*([\w:]+))?")
_RB_REQUIRE = re.compile(r"^\s*require(?:_relative)?\s+['\"]([^'\"]+)['\"]")


def _brace_end_line(lines: list[str], start_idx: int) -> int:
    """Brace-balance from start_idx, return the 1-based closing line."""
    depth = 0
    seen_open = False
    in_string: str | None = None
    in_line_comment = False
    in_block_comment = False

    for i in range(start_idx, len(lines)):
        line = lines[i]
        j = 0
        in_line_comment = False
        while j < len(line):
            ch = line[j]
            nxt = line[j + 1] if j + 1 < len(line) else ""
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    j += 2
                    continue
            elif in_string:
                if ch == "\\":
                    j += 2
                    continue
                if ch == in_string:
                    in_string = None
            elif in_line_comment:
                pass
            else:
                if ch == "/" and nxt == "/":
                    in_line_comment = True
                    j += 2
                    continue
                if ch == "/" and nxt == "*":
                    in_block_comment = True
                    j += 2
                    continue
                if ch in ("'", '"', "`"):
                    in_string = ch
                elif ch == "{":
                    depth += 1
                    seen_open = True
                elif ch == "}":
                    depth -= 1
                    if seen_open and depth == 0:
                        return i + 1
            j += 1

    return start_idx + 1


def extract_js_ts_index(source: str) -> dict:
    lines = source.splitlines()
    imports: list[dict] = []
    symbols: list[dict] = []
    class_ranges: list[tuple[int, int, dict]] = []

    for i, line in enumerate(lines):
        line_no = i + 1
        m = _JS_IMPORT_FROM.match(line)
        if m and ("from" in line or line.strip().startswith("import ")):
            module = m.group(3)
            imports.append({"module": module, "line": line_no})
            continue
        m = _JS_CLASS.match(line)
        if m:
            end = _brace_end_line(lines, i)
            sym = {
                "name": m.group(1),
                "kind": "class",
                "line": line_no,
                "end_line": end,
                "bases": [m.group(2)] if m.group(2) else [],
                "methods": [],
            }
            symbols.append(sym)
            class_ranges.append((line_no, end, sym))
            continue
        m = _JS_FUNC.match(line)
        if m:
            end = _brace_end_line(lines, i)
            symbols.append({
                "name": m.group(1),
                "kind": "function",
                "line": line_no,
                "end_line": end,
                "signature": f"{m.group(1)}({m.group(2).strip()})",
            })
            continue
        m = _JS_ARROW.match(line)
        if m:
            end = _brace_end_line(lines, i)
            symbols.append({
                "name": m.group(1),
                "kind": "function",
                "line": line_no,
                "end_line": end,
                "signature": f"{m.group(1)}({m.group(2).strip()})",
            })
            continue
        # Method (only count if inside a class range)
        m = _JS_METHOD.match(line)
        if m and not line.lstrip().startswith(("if ", "for ", "while ", "switch ", "catch", "return")):
            for cstart, cend, csym in class_ranges:
                if cstart < line_no <= cend:
                    end = _brace_end_line(lines, i)
                    csym["methods"].append({
                        "name": m.group(1),
                        "kind": "method",
                        "line": line_no,
                        "end_line": end,
                        "signature": f"{m.group(1)}({m.group(2).strip()})",
                    })
                    break

    return {"imports": imports, "symbols": symbols}


def extract_go_index(source: str) -> dict:
    lines = source.splitlines()
    imports: list[dict] = []
    symbols: list[dict] = []
    in_import_block = False
    for i, line in enumerate(lines):
        line_no = i + 1
        stripped = line.strip()
        if stripped.startswith("import ("):
            in_import_block = True
            continue
        if in_import_block:
            if stripped == ")":
                in_import_block = False
                continue
            m = _GO_IMPORT.match(line)
            if m:
                imports.append({"module": m.group(1), "line": line_no})
            continue
        if stripped.startswith("import "):
            m = re.search(r'"([^"]+)"', line)
            if m:
                imports.append({"module": m.group(1), "line": line_no})
            continue
        m = _GO_FUNC.match(line)
        if m:
            end = _brace_end_line(lines, i)
            symbols.append({
                "name": m.group(1),
                "kind": "function",
                "line": line_no,
                "end_line": end,
                "signature": f"{m.group(1)}({m.group(2).strip()})",
            })
            continue
        m = _GO_TYPE.match(line)
        if m:
            end = _brace_end_line(lines, i) if "{" in line else line_no
            symbols.append({
                "name": m.group(1),
                "kind": m.group(2),
                "line": line_no,
                "end_line": end,
            })
    return {"imports": imports, "symbols": symbols}


def extract_rust_index(source: str) -> dict:
    lines = source.splitlines()
    imports: list[dict] = []
    symbols: list[dict] = []
    for i, line in enumerate(lines):
        line_no = i + 1
        m = _RUST_USE.match(line)
        if m:
            imports.append({"module": m.group(1).strip(), "line": line_no})
            continue
        m = _RUST_FUNC.match(line)
        if m:
            end = _brace_end_line(lines, i)
            symbols.append({
                "name": m.group(1),
                "kind": "function",
                "line": line_no,
                "end_line": end,
                "signature": f"{m.group(1)}({m.group(2).strip()})",
            })
            continue
        m = _RUST_TYPE.match(line)
        if m:
            end = _brace_end_line(lines, i) if "{" in line else line_no
            symbols.append({
                "name": m.group(2),
                "kind": m.group(1),
                "line": line_no,
                "end_line": end,
            })
    return {"imports": imports, "symbols": symbols}


def extract_ruby_index(source: str) -> dict:
    """Balance def/class/module against matching `end` keywords."""
    lines = source.splitlines()
    imports: list[dict] = []
    symbols: list[dict] = []
    # (symbol_index_or_-1, kind, open_line)
    open_stack: list[tuple[int, str, int]] = []

    def is_block_opener(stripped: str) -> bool:
        # anything that needs a matching `end`
        return bool(re.match(
            r"^(def|class|module|if|unless|while|until|case|begin|do)\b", stripped))

    for i, line in enumerate(lines):
        line_no = i + 1
        stripped = line.strip()

        m = _RB_REQUIRE.match(line)
        if m:
            imports.append({"module": m.group(1), "line": line_no})

        m = _RB_CLASS.match(line)
        if m:
            sym = {
                "name": m.group(2),
                "kind": m.group(1),  # class | module
                "line": line_no,
                "end_line": line_no,
                "bases": [m.group(3)] if m.group(3) else [],
                "methods": [],
            }
            symbols.append(sym)
            open_stack.append((len(symbols) - 1, "class", line_no))
            continue

        m = _RB_DEF.match(line)
        if m:
            sym = {
                "name": m.group(1),
                "kind": "method" if open_stack and open_stack[-1][1] == "class" else "function",
                "line": line_no,
                "end_line": line_no,
                "signature": f"{m.group(1)}({(m.group(2) or '').strip()})",
            }
            symbols.append(sym)
            # attach to enclosing class
            if open_stack and open_stack[-1][1] == "class":
                symbols[open_stack[-1][0]]["methods"].append(sym)
            open_stack.append((len(symbols) - 1, "def", line_no))
            continue

        if is_block_opener(stripped) and not stripped.endswith(("end", "end")):
            open_stack.append((-1, "block", line_no))

        if re.match(r"^end\b", stripped):
            if open_stack:
                idx, _, _ = open_stack.pop()
                if idx >= 0:
                    symbols[idx]["end_line"] = line_no

    return {"imports": imports, "symbols": symbols}


EXTRACTORS = {
    "python":     extract_python_index,
    "javascript": extract_js_ts_index,
    "typescript": extract_js_ts_index,
    "go":         extract_go_index,
    "rust":       extract_rust_index,
    "ruby":       extract_ruby_index,
}


# Index builder + lookups

def _iter_source_files(root: Path, ignores: set[str], max_depth: int):
    def walk(directory: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = list(directory.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if is_ignored(entry.name, ignores):
                continue
            if entry.is_dir():
                yield from walk(entry, depth + 1)
            elif entry.suffix.lower() in LANGUAGE_BY_EXT:
                yield entry

    yield from walk(root, 1)


def build_index(root: Path, ignores: set[str], max_depth: int) -> dict:
    """Walk the project and collect every symbol into one dict."""
    files: dict[str, dict] = {}
    reverse: dict[str, list[dict]] = {}
    counts = {"functions": 0, "classes": 0, "methods": 0, "imports": 0}

    for path in _iter_source_files(root, ignores, max_depth):
        language = LANGUAGE_BY_EXT[path.suffix.lower()]
        source = _safe_read_text(path)
        if source is None:
            continue
        extractor = EXTRACTORS[language]
        result = extractor(source)

        rel = path.relative_to(root).as_posix()
        files[rel] = {
            "language": language,
            "lines": source.count("\n") + 1,
            "imports": result.get("imports", []),
            "symbols": result.get("symbols", []),
        }
        if "error" in result:
            files[rel]["error"] = result["error"]

        counts["imports"] += len(result.get("imports", []))
        for sym in result.get("symbols", []):
            entry = {
                "file": rel,
                "line": sym["line"],
                "end_line": sym.get("end_line", sym["line"]),
                "kind": sym["kind"],
                "signature": sym.get("signature"),
            }
            reverse.setdefault(sym["name"], []).append(entry)
            if sym["kind"] == "class":
                counts["classes"] += 1
                for method in sym.get("methods", []):
                    counts["methods"] += 1
                    qualified = f"{sym['name']}.{method['name']}"
                    method_entry = {
                        "file": rel,
                        "line": method["line"],
                        "end_line": method.get("end_line", method["line"]),
                        "kind": method["kind"],
                        "signature": method.get("signature"),
                        "class": sym["name"],
                    }
                    reverse.setdefault(qualified, []).append(method_entry)
                    reverse.setdefault(method["name"], []).append(method_entry)
            else:
                counts["functions"] += 1

    return {
        "version": INDEX_VERSION,
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "stats": {
            "files": len(files),
            **counts,
        },
        "files": files,
        "symbols": reverse,
    }


def save_index(index: dict, path: Path) -> None:
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def load_index(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"No index at {path}. Run `blueprint.py` first to build it.")
    return json.loads(path.read_text(encoding="utf-8"))


def find_symbols(index: dict, query: str, exact: bool = False) -> list[dict]:
    """Exact match first, then case-insensitive substring."""
    symbols = index.get("symbols", {})
    out: list[dict] = []

    if query in symbols:
        for entry in symbols[query]:
            out.append({"name": query, **entry})
        if exact or out:
            return out

    if exact:
        return out

    q = query.lower()
    for name, entries in symbols.items():
        if q in name.lower():
            for entry in entries:
                out.append({"name": name, **entry})
    return out


def show_range(file_path: Path, start: int | None, end: int | None) -> str:
    """File contents (or a slice) with line-number prefixes."""
    text = _safe_read_text(file_path)
    if text is None:
        return f"(could not read {file_path})"
    lines = text.splitlines()
    if start is None:
        start = 1
    if end is None:
        end = len(lines)
    start = max(1, start)
    end = min(len(lines), end)
    width = len(str(end))
    out = []
    for i in range(start - 1, end):
        out.append(f"{str(i + 1).rjust(width)} | {lines[i]}")
    return "\n".join(out)


def parse_location(spec: str) -> tuple[str, int | None, int | None]:
    """Accepts `file`, `file:line`, `file:start-end`, or `file:start:end`."""
    if ":" not in spec:
        return spec, None, None
    file_part, _, range_part = spec.partition(":")
    if not range_part:
        return file_part, None, None
    if "-" in range_part:
        a, _, b = range_part.partition("-")
    elif ":" in range_part:
        a, _, b = range_part.partition(":")
    else:
        a, b = range_part, range_part
    try:
        start = int(a)
        end = int(b) if b else start
    except ValueError:
        return file_part, None, None
    return file_part, start, end


# CLI


SUBCOMMANDS = {"find", "show", "list", "refresh", "index"}


def _build_default_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blueprint.py",
        description=(
            "Generate PROJECT_CONTEXT.md (markdown summary) and "
            "PROJECT_INDEX.json (queryable code index) for a project. "
            "Use subcommands `find`, `show`, `list`, or `refresh` to query/rebuild."
        ),
    )
    parser.add_argument("path", nargs="?", default=".",
                        help="Project directory to scan (default: current directory).")
    parser.add_argument("--ignore", default="",
                        help="Comma-separated additional ignore patterns.")
    parser.add_argument("--output", default="PROJECT_CONTEXT.md",
                        help="Markdown output filename (default: PROJECT_CONTEXT.md).")
    parser.add_argument("--index-output", default=INDEX_FILENAME,
                        help=f"JSON index filename (default: {INDEX_FILENAME}).")
    parser.add_argument("--depth", type=int, default=5,
                        help="Maximum tree depth (default: 5).")
    parser.add_argument("--no-index", action="store_true",
                        help="Skip building the JSON code index.")
    parser.add_argument("--no-markdown", action="store_true",
                        help="Skip writing the markdown summary.")
    return parser


def _resolve_root(maybe_path: str) -> Path:
    root = Path(maybe_path).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"error: {root} is not a directory")
    return root


def _resolve_index_path(root: Path, name: str) -> Path:
    p = Path(name)
    return p if p.is_absolute() else root / p


def _cmd_build(argv: list[str]) -> int:
    args = _build_default_parser().parse_args(argv)
    root = _resolve_root(args.path)

    ignores = set(DEFAULT_IGNORES)
    ignores.update(p.strip() for p in args.ignore.split(",") if p.strip())
    ignores.add(args.output)
    ignores.add(args.index_output)

    tree, file_count = build_tree(root, ignores, args.depth)
    metadata = detect_metadata(root, ignores, args.depth)
    key_files = collect_key_files(root, ignores, args.depth)
    key_summaries = [(p, summarize_file(p)) for p in key_files]

    index = None
    if not args.no_index:
        index = build_index(root, ignores, args.depth)
        index_path = _resolve_index_path(root, args.index_output)
        save_index(index, index_path)

    if not args.no_markdown:
        document = render_document(root, tree, metadata, key_summaries, index=index)
        output_path = _resolve_index_path(root, args.output)
        output_path.write_text(document, encoding="utf-8")

    parts = [f"\u2713 Scanned {file_count} files"]
    if not args.no_markdown:
        parts.append(os.path.relpath(_resolve_index_path(root, args.output), os.getcwd()))
    if index is not None:
        s = index["stats"]
        parts.append(
            f"indexed {s['functions']} fn / {s['classes']} cls / {s['imports']} imports"
            f" \u2192 {os.path.relpath(_resolve_index_path(root, args.index_output), os.getcwd())}"
        )
    print(" \u2192 ".join(parts))
    return 0


def _cmd_refresh(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="blueprint.py refresh")
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--ignore", default="")
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--index-output", default=INDEX_FILENAME)
    args = parser.parse_args(argv)
    root = _resolve_root(args.path)
    ignores = set(DEFAULT_IGNORES)
    ignores.update(p.strip() for p in args.ignore.split(",") if p.strip())
    ignores.add(args.index_output)
    index = build_index(root, ignores, args.depth)
    index_path = _resolve_index_path(root, args.index_output)
    save_index(index, index_path)
    s = index["stats"]
    print(f"\u2713 Indexed {s['functions']} fn / {s['classes']} cls / "
          f"{s['imports']} imports \u2192 {os.path.relpath(index_path, os.getcwd())}")
    return 0


def _format_match(match: dict, root: Path, with_source: bool) -> str:
    file_rel = match["file"]
    line = match["line"]
    end = match["end_line"]
    sig = match.get("signature") or ""
    cls = f" [{match['class']}]" if match.get("class") else ""
    header = f"{file_rel}:{line}-{end}  {match['kind']}{cls}  {match['name']}{('  ' + sig) if sig else ''}"
    if not with_source:
        return header
    abs_path = (root / file_rel) if not Path(file_rel).is_absolute() else Path(file_rel)
    body = show_range(abs_path, line, end)
    return f"{header}\n{'-' * min(len(header), 80)}\n{body}"


def _cmd_find(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="blueprint.py find")
    parser.add_argument("query", help="Symbol name (exact or substring).")
    parser.add_argument("--root", default=".",
                        help="Project root containing PROJECT_INDEX.json.")
    parser.add_argument("--exact", action="store_true",
                        help="Require an exact name match.")
    parser.add_argument("--source", action="store_true",
                        help="Always include source for every match.")
    parser.add_argument("--limit", type=int, default=20,
                        help="Maximum matches to print (default: 20).")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON instead of formatted text.")
    args = parser.parse_args(argv)

    root = _resolve_root(args.root)
    index_path = _resolve_index_path(root, INDEX_FILENAME)
    index = load_index(index_path)
    matches = find_symbols(index, args.query, exact=args.exact)

    if args.json:
        print(json.dumps(matches[:args.limit], indent=2, ensure_ascii=False))
        return 0 if matches else 1

    if not matches:
        print(f"No matches for '{args.query}'.")
        return 1

    truncated = len(matches) > args.limit
    matches = matches[:args.limit]
    # Auto include source when there's a single match
    include_source = args.source or len(matches) == 1

    blocks = [_format_match(m, root, with_source=include_source) for m in matches]
    print(("\n\n" if include_source else "\n").join(blocks))
    if truncated:
        print(f"\n… {len(matches)} of more shown. Use --limit to widen.")
    return 0


def _cmd_show(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="blueprint.py show")
    parser.add_argument("location", help="file, file:line, or file:start-end")
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)

    root = _resolve_root(args.root)
    file_part, start, end = parse_location(args.location)
    abs_path = Path(file_part)
    if not abs_path.is_absolute():
        abs_path = root / file_part
    if not abs_path.is_file():
        print(f"error: {abs_path} is not a file", file=sys.stderr)
        return 1
    print(show_range(abs_path, start, end))
    return 0


def _cmd_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="blueprint.py list")
    parser.add_argument("kind",
                        choices=["functions", "classes", "methods", "imports", "files", "symbols"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--language", default=None,
                        help="Filter by language (python, javascript, typescript, go, rust, ruby).")
    parser.add_argument("--file", default=None,
                        help="Filter by file path (substring match).")
    args = parser.parse_args(argv)

    root = _resolve_root(args.root)
    index = load_index(_resolve_index_path(root, INDEX_FILENAME))

    rows: list[str] = []
    if args.kind == "files":
        for rel, info in sorted(index["files"].items()):
            if args.language and info["language"] != args.language:
                continue
            if args.file and args.file not in rel:
                continue
            sym_count = len(info.get("symbols", []))
            rows.append(f"{rel}  [{info['language']}, {info['lines']} lines, {sym_count} symbols]")
    elif args.kind == "symbols":
        for name, entries in sorted(index["symbols"].items()):
            for e in entries:
                if args.file and args.file not in e["file"]:
                    continue
                rows.append(f"{e['file']}:{e['line']}-{e['end_line']}  {e['kind']}  {name}")
    else:
        wanted_kinds = {
            "functions": {"function", "async_function"},
            "classes":   {"class", "struct", "interface", "enum", "trait", "module"},
            "methods":   {"method", "async_method"},
            "imports":   set(),  # handled separately
        }[args.kind]
        for rel, info in sorted(index["files"].items()):
            if args.language and info["language"] != args.language:
                continue
            if args.file and args.file not in rel:
                continue
            if args.kind == "imports":
                for imp in info.get("imports", []):
                    rows.append(f"{rel}:{imp['line']}  {imp['module']}")
            else:
                for sym in info.get("symbols", []):
                    if sym["kind"] in wanted_kinds:
                        sig = sym.get("signature", sym["name"])
                        rows.append(f"{rel}:{sym['line']}-{sym.get('end_line', sym['line'])}  {sig}")
                    if args.kind == "methods":
                        for m in sym.get("methods", []):
                            sig = m.get("signature", m["name"])
                            rows.append(f"{rel}:{m['line']}-{m.get('end_line', m['line'])}  {sym['name']}.{sig}")

    if not rows:
        print("(no matches)")
        return 1
    print("\n".join(rows))
    print(f"\n{len(rows)} result(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in SUBCOMMANDS:
        cmd, rest = argv[0], argv[1:]
        dispatch = {
            "index":   _cmd_build,
            "refresh": _cmd_refresh,
            "find":    _cmd_find,
            "show":    _cmd_show,
            "list":    _cmd_list,
        }[cmd]
        return dispatch(rest)
    return _cmd_build(argv)


if __name__ == "__main__":
    raise SystemExit(main())
