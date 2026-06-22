"""Language-agnostic workspace dependency scanner.

Extracted from Phase-1/node_modified.py (lines 981-1302).

``scan_workspace_dependencies(workspace_dir, entrypoint)`` is the single
entry point. It autodetects the language from the entrypoint command and
dispatches to the matching scanner (Python / JavaScript / C++).

Each scanner is pure: it reads files, parses imports, returns a dict. No
network calls, no caching, no side-effects on disk. Scanning is CPU-bound
and synchronous — callers that need to offload it (``nexus.runtime``) wrap
this in a thread executor themselves.
"""

from __future__ import annotations

import ast
import os
import re
import sys


# ---------------------------------------------------------------------------
# Python stdlib (for filtering third-party imports)
# ---------------------------------------------------------------------------

_PYTHON_STDLIB_MODULES: set[str] = set()
try:
    _PYTHON_STDLIB_MODULES = sys.stdlib_module_names  # type: ignore[attr-defined]
except AttributeError:
    _PYTHON_STDLIB_MODULES = {
        "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio", "asyncore",
        "atexit", "audioop", "base64", "bdb", "binascii", "binhex", "bisect", "builtins",
        "bz2", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd", "code", "codecs",
        "codeop", "collections", "colorsys", "compileall", "concurrent", "configparser",
        "contextlib", "contextvars", "copy", "copyreg", "cProfile", "crypt", "csv",
        "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal", "difflib",
        "dis", "distutils", "doctest", "email", "encodings", "enum", "errno", "faulthandler",
        "fcntl", "filecmp", "fileinput", "fnmatch", "fractions", "ftplib", "functools",
        "gc", "getopt", "getpass", "gettext", "glob", "grp", "gzip", "hashlib", "heapq",
        "hmac", "html", "http", "idlelib", "imaplib", "imghdr", "imp", "importlib",
        "inspect", "io", "ipaddress", "itertools", "json", "keyword", "lib2to3",
        "linecache", "locale", "logging", "lzma", "mailbox", "mailcap", "marshal",
        "math", "mimetypes", "mmap", "modulefinder", "multiprocessing", "netrc",
        "nis", "nntplib", "numbers", "operator", "optparse", "os", "ossaudiodev",
        "pathlib", "pdb", "pickle", "pickletools", "pipes", "pkgutil", "platform",
        "plistlib", "poplib", "posix", "posixpath", "pprint", "profile", "pstats",
        "pty", "pwd", "py_compile", "pyclbr", "pydoc", "queue", "quopri", "random",
        "re", "readline", "reprlib", "resource", "rlcompleter", "runpy", "sched",
        "secrets", "select", "selectors", "shelve", "shlex", "shutil", "signal",
        "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "spwd",
        "sqlite3", "sre_compile", "sre_constants", "sre_parse", "ssl", "stat",
        "statistics", "string", "stringprep", "struct", "subprocess", "sunau",
        "symtable", "sys", "sysconfig", "syslog", "tabnanny", "tarfile", "telnetlib",
        "tempfile", "termios", "test", "textwrap", "threading", "time", "timeit",
        "tkinter", "token", "tokenize", "tomllib", "trace", "traceback", "tracemalloc",
        "tty", "turtle", "turtledemo", "types", "typing", "unicodedata", "unittest",
        "urllib", "uu", "uuid", "venv", "warnings", "wave", "weakref", "webbrowser",
        "winreg", "winsound", "wsgiref", "xdrlib", "xml", "xmlrpc", "zipapp",
        "zipfile", "zipimport", "zlib", "_thread", "__future__",
    }


_PY_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "venv", ".venv", "env", ".env", "_nexus_venv", "__pycache__",
        ".git", ".hg", ".svn", "node_modules", "site-packages",
        ".tox", ".mypy_cache", ".pytest_cache", "build", "dist",
    }
)


# ---------------------------------------------------------------------------
# Python scanning
# ---------------------------------------------------------------------------

def extract_imports_from_source(source: str) -> set[str]:
    """Parse *source* and return the set of top-level modules it imports."""
    modules: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return modules
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:  # skip relative imports
                modules.add(node.module.split(".")[0])
    return modules


def _collect_local_python_modules(workspace_dir: str) -> set[str]:
    """Names of .py files and package dirs inside *workspace_dir*."""
    local_mods: set[str] = set()
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [
            d for d in dirs
            if d not in _PY_SCAN_SKIP_DIRS and not d.endswith(".dist-info")
        ]
        for fname in files:
            if fname.endswith(".py"):
                local_mods.add(fname[:-3])
        for d in dirs:
            if os.path.isfile(os.path.join(root, d, "__init__.py")):
                local_mods.add(d)
    return local_mods


def scan_workspace_imports(workspace_dir: str, entrypoint: str = "") -> dict:
    """Return the set of third-party Python imports in *workspace_dir*.

    The ``entrypoint`` argument is accepted for backwards compatibility but
    ignored — the scanner always walks the whole workspace, so nested
    layouts work.
    """
    del entrypoint  # retained for API compatibility

    local_mods = _collect_local_python_modules(workspace_dir)
    all_third_party: set[str] = set()
    scanned: list[str] = []

    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [
            d for d in dirs
            if d not in _PY_SCAN_SKIP_DIRS and not d.endswith(".dist-info")
        ]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, workspace_dir)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except Exception:
                continue
            scanned.append(rel)
            for mod in extract_imports_from_source(source):
                if mod in _PYTHON_STDLIB_MODULES or mod in local_mods:
                    continue
                all_third_party.add(mod)

    return {"packages": sorted(all_third_party), "scanned_files": sorted(scanned)}


# ---------------------------------------------------------------------------
# JavaScript / Node.js scanning
# ---------------------------------------------------------------------------

_NODE_BUILTINS: frozenset[str] = frozenset(
    {
        "assert", "buffer", "child_process", "cluster", "console", "constants",
        "crypto", "dgram", "dns", "domain", "events", "fs", "http", "http2",
        "https", "inspector", "module", "net", "os", "path", "perf_hooks",
        "process", "punycode", "querystring", "readline", "repl", "stream",
        "string_decoder", "sys", "timers", "tls", "trace_events", "tty",
        "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
    }
)
_JS_REQUIRE_RE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+|import\s+)['"]([^'"]+)['"]"""
)


def extract_js_imports(source: str) -> set[str]:
    """Extract npm package names referenced by *source*."""
    modules: set[str] = set()
    for pattern in (_JS_REQUIRE_RE, _JS_IMPORT_RE):
        for match in pattern.finditer(source):
            mod = match.group(1)
            if mod.startswith(".") or mod.startswith("/") or mod.startswith("node:"):
                continue
            if mod.startswith("@"):
                parts = mod.split("/")
                pkg = "/".join(parts[:2]) if len(parts) >= 2 else mod
            else:
                pkg = mod.split("/")[0]
            if pkg not in _NODE_BUILTINS:
                modules.add(pkg)
    return modules


def scan_workspace_js(workspace_dir: str) -> dict:
    """Scan JS/TS files in *workspace_dir* for npm dependencies."""
    all_packages: set[str] = set()
    scanned: list[str] = []
    for root, _dirs, files in os.walk(workspace_dir):
        if "node_modules" in root.split(os.sep):
            continue
        for fname in files:
            if not fname.endswith((".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, workspace_dir)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except Exception:
                continue
            scanned.append(rel)
            all_packages.update(extract_js_imports(source))
    return {
        "packages": sorted(all_packages),
        "scanned_files": sorted(scanned),
        "language": "javascript",
        "output_file": "package.json",
    }


# ---------------------------------------------------------------------------
# C / C++ scanning
# ---------------------------------------------------------------------------

_CPP_STD_HEADERS: frozenset[str] = frozenset(
    {
        # C standard headers
        "assert.h", "complex.h", "ctype.h", "errno.h", "fenv.h", "float.h",
        "inttypes.h", "iso646.h", "limits.h", "locale.h", "math.h", "setjmp.h",
        "signal.h", "stdalign.h", "stdarg.h", "stdatomic.h", "stdbool.h",
        "stddef.h", "stdint.h", "stdio.h", "stdlib.h", "stdnoreturn.h",
        "string.h", "tgmath.h", "threads.h", "time.h", "uchar.h", "wchar.h", "wctype.h",
        # C++ standard headers
        "algorithm", "any", "array", "atomic", "barrier", "bit", "bitset",
        "cassert", "cctype", "cerrno", "cfenv", "cfloat", "charconv",
        "chrono", "cinttypes", "climits", "clocale", "cmath", "codecvt",
        "compare", "complex", "concepts", "condition_variable", "coroutine",
        "csetjmp", "csignal", "cstdarg", "cstddef", "cstdint", "cstdio",
        "cstdlib", "cstring", "ctime", "cuchar", "cwchar", "cwctype",
        "deque", "exception", "execution", "expected", "filesystem", "format",
        "forward_list", "fstream", "functional", "future",
        "initializer_list", "iomanip", "ios", "iosfwd", "iostream", "istream",
        "iterator", "latch", "limits", "list", "locale", "map", "mdspan",
        "memory", "memory_resource", "mutex", "new", "numbers", "numeric",
        "optional", "ostream", "print", "queue", "random", "ranges", "ratio",
        "regex", "scoped_allocator", "semaphore", "set", "shared_mutex",
        "source_location", "span", "spanstream", "sstream", "stack",
        "stacktrace", "stdexcept", "stop_token", "streambuf", "string",
        "string_view", "strstream", "syncstream", "system_error",
        "thread", "tuple", "type_traits", "typeindex", "typeinfo",
        "unordered_map", "unordered_set", "utility", "valarray", "variant",
        "vector", "version",
    }
)
_CPP_INCLUDE_RE = re.compile(r"""^\s*#\s*include\s*<([^>]+)>""", re.MULTILINE)


def scan_workspace_cpp(workspace_dir: str) -> dict:
    """Scan C/C++ files for non-standard ``#include <...>`` headers."""
    all_libs: set[str] = set()
    scanned: list[str] = []
    for root, _dirs, files in os.walk(workspace_dir):
        for fname in files:
            if not fname.endswith((".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx")):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, workspace_dir)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except Exception:
                continue
            scanned.append(rel)
            for match in _CPP_INCLUDE_RE.finditer(source):
                header = match.group(1).strip()
                base = header.split("/")[0]
                if header not in _CPP_STD_HEADERS and base not in _CPP_STD_HEADERS:
                    all_libs.add(header)
    return {
        "packages": sorted(all_libs),
        "scanned_files": sorted(scanned),
        "language": "cpp",
        "output_file": "detected_libraries.txt",
    }


# ---------------------------------------------------------------------------
# Language detection + dispatcher
# ---------------------------------------------------------------------------

def detect_language_from_entrypoint(entrypoint: str) -> str:
    """Return ``"python"``, ``"javascript"``, or ``"cpp"``. Defaults to python."""
    lines = [l.strip() for l in entrypoint.strip().splitlines() if l.strip()]
    for line in lines:
        first_word = line.split()[0] if line.split() else ""
        if first_word in ("python", "python3", "py"):
            return "python"
        if first_word in ("node", "npm", "npx", "bun", "deno"):
            return "javascript"
        if first_word in (
            "gcc", "g++", "cc", "c++", "clang", "clang++", "make", "cmake"
        ):
            return "cpp"
        for part in line.split():
            if part.endswith(".py"):
                return "python"
            if part.endswith((".js", ".ts", ".mjs", ".cjs")):
                return "javascript"
            if part.endswith((".c", ".cpp", ".cc", ".cxx")):
                return "cpp"
    return "python"


def scan_workspace_dependencies(workspace_dir: str, entrypoint: str) -> dict:
    """Auto-detect language from *entrypoint* and run the matching scanner."""
    lang = detect_language_from_entrypoint(entrypoint)
    if lang == "javascript":
        result = scan_workspace_js(workspace_dir)
    elif lang == "cpp":
        result = scan_workspace_cpp(workspace_dir)
    else:
        result = scan_workspace_imports(workspace_dir, entrypoint)
        result["language"] = "python"
        result["output_file"] = "requirements.txt"
    return result


__all__ = [
    "extract_imports_from_source",
    "extract_js_imports",
    "scan_workspace_imports",
    "scan_workspace_js",
    "scan_workspace_cpp",
    "detect_language_from_entrypoint",
    "scan_workspace_dependencies",
]
