"""The built-in tools, and the risk each one carries.

Ten of them, and their schemas are DERIVED from the signature and docstring by
`@tool` in agent/registry.py (FR-207). They were hand-written until tool eight,
when §13's arithmetic - ~25 lines plus ~5 per tool for the machinery against ~8
per tool written out - stopped favouring the dicts.

Tools RAISE on failure and never return an error string — the execute node owns
the exception-to-observation conversion (FR-208).

Tools do NOT re-check paths. The gate checks declared path arguments, and the
container's single writable mount bounds arbitrary shell. Two mechanisms guarding
one risk is what §13 cut the INSTALL set for.

Adding a tool touches this file only (NFR-601).
"""
import atexit
import difflib
import ipaddress
import os
import re
import signal
import subprocess
import threading
import time
import sys
from pathlib import Path

from agent import config
from agent.binary_extensions import (has_binary_extension,
                                     has_opaque_document_extension)
from agent.registry import tool


def _int(value, default: int) -> int:
    """Coerce a numeric argument that arrived as a string.

    A declared JSON schema is a hint to the model, NOT enforcement: `"limit": 500`
    and `"limit": "500"` are both routinely emitted, and the second one crashed
    every read_file call in the first live session - so the agent rewrote a file
    it had never managed to read. Coerce at the boundary rather than trusting the
    schema, and treat a nonsense value as absent rather than as a crash.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


SEARCH_HINT = "Search wider with run_shell(command='find . -type f | head -50')."


def _label(path) -> str:
    """A path named relative to the workspace, or in full when it is outside.

    FR-302 as amended lets a path resolve outside the workspace, and
    `relative_to` RAISES on one rather than returning something useless -
    measured, on the missing-file hint.
    """
    try:
        return path.relative_to(config.WORKSPACE).as_posix() or "."
    except ValueError:
        return path.as_posix()


def _nearby(target) -> str:
    """What IS in the directory the agent guessed at, so the retry can be informed.

    Walks up to the nearest directory that exists, because a wrong guess deep in a
    tree ("src/models/user.py" when there is no "models") would otherwise report
    nothing at all. Bounded: a workspace of a thousand files must not produce a
    thousand-name error, which would only breach the result cap in a new way.
    """
    directory = target.parent
    while not directory.is_dir() and config.WORKSPACE in directory.parents:
        directory = directory.parent
    if not directory.is_dir():
        # Outside the workspace the loop above never runs, so a missing path
        # leaves a directory that is not one. Walk up until something exists.
        while directory.parent != directory and not directory.is_dir():
            directory = directory.parent
    try:
        names = sorted(p.name + ("/" if p.is_dir() else "")
                       for p in directory.iterdir())
        where = _label(directory)
    except OSError:
        return SEARCH_HINT
    if not names:
        return f"{where} is empty. {SEARCH_HINT}"
    shown = ", ".join(names[:25])
    more = f" (+{len(names) - 25} more)" if len(names) > 25 else ""
    return f"{where} contains: {shown}{more}. {SEARCH_HINT}"


@tool(risk="read")
def read_file(path: str, offset: int = 0, limit: int = 500) -> str:
    """Read a text file from the workspace. Returns numbered lines. Every call
    costs a turn, so read a whole region at once: the window is never smaller
    than 100 lines, and a small file is best read in one call with no limit.

    path: Path relative to the workspace root.
    offset: First line to return, 0-based. Default 0.
    limit: How many lines to return. Default 500, minimum 100.
    """
    offset = _int(offset, 0)
    limit = max(_int(limit, 500), config.READ_MIN_LINES)
    target = config.resolve(path)
    if target.is_dir():
        # FR-201's "list directories": read_file on a directory returns the listing
        # rather than an error, so the agent needs no second tool to look around.
        return f"{path} is a directory.\n{_nearby(target / '_')}"
    # A binary file read as text is 2,496 characters of mojibake in the context
    # window and no information. Measured on an 8-byte PNG header.
    if has_binary_extension(path):
        return (f"{path} is a binary file and was not read. Use run_shell if you "
                f"need its size or type.")
    if not target.exists():
        # A wrong path is a guess, so the error names what IS in the nearest real
        # directory - a bare "not found" gets the same wrong guess again.
        raise FileNotFoundError(f"{path} does not exist. {_nearby(target)}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    window = lines[offset:offset + limit]

    # The window is sized so the result fits under the cap. Returning a slice the
    # caller must then re-slice was measured worse than returning fewer lines whole.
    cap = config.TOOL_CAPS.get("read_file", config.MAX_RESULT_CHARS)
    header_room = len(path) + 80          # the header line, plus room for the hint
    kept, used = [], header_room
    for i, line in enumerate(window):
        rendered = len(line) + 9          # 6-digit number, tab, newline
        if used + rendered > cap and kept:
            break
        kept.append(line)
        used += rendered

    body = "\n".join(f"{offset + i + 1:6d}\t{line}" for i, line in enumerate(kept))
    shown = f"{offset + 1}-{offset + len(kept)}"
    head = f"{path} (lines {shown} of {len(lines)})"
    if len(kept) < len(window):
        # Say how to continue. A silent truncation costs a turn the same way an
        # unactionable error does - the agent cannot ask for what it cannot see.
        head += (f" - narrowed to fit; continue with "
                 f"read_file(path=\"{path}\", offset={offset + len(kept)})")
    return f"{head}\n{body}"


@tool(risk="write")
def write_file(path: str, content: str) -> str:
    """Write a file in the workspace, replacing its entire contents. Read the
    file first; this does not patch, it overwrites.

    path: Path relative to the workspace root.
    content: The complete new contents of the file.
    """
    target = config.resolve(path)
    # OVERWRITING one, not creating one. Plain text written over an existing binary
    # destroys it irrecoverably and the write SUCCEEDS, so the agent never learns.
    # Measured on both a .docx and a .png. Creating a new file with that name stays
    # the model's business, so only an overwrite is refused.
    if target.exists() and (has_binary_extension(path)
                            or has_opaque_document_extension(path)):
        return (f"refused: {path} already exists and is a binary file. Writing "
                f"plain text over it would destroy it. Write to a new path, or "
                f"delete it first if that is what you mean.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {path} ({len(content)} chars, {content.count(chr(10)) + 1} lines)"


# edit_file exists because whole-file writes cost the run: on real repositories
# the agent could not afford to rewrite a file it had only partly read. Its
# description is load-bearing - this wording took real repos 0/9 to 4/7.
# The agent has no reason to trust an edit it cannot see. A character count is
# not evidence, so it re-reads to check - and re-reading is what the thrash
# detector then punishes. A diff is the receipt; the counts stay for scale.
DIFF_LINES = 40
# DIFF_LINES bounds LINES; this bounds CHARACTERS. A test caught the gap -
# 40 lines of 300 chars is 12,000, double the result cap. Same trap shrink()
# already carries.
DIFF_LINE_CHARS = 120


def _edit_receipt(path: str, before: str, after: str) -> str:
    """A unified diff of the change, bounded so it cannot flood context.

    Past DIFF_LINES the diff stops being a receipt and becomes a second copy
    of the file, so it degrades to the counts plus the first hunk.
    """
    delta = after.count(chr(10)) - before.count(chr(10))
    counts = f"edited {path} ({delta:+d} lines)"
    diff = [l[:DIFF_LINE_CHARS] for l in difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"{path} before", tofile=f"{path} after", lineterm="", n=2)]
    if not diff:
        return counts + " - no textual change"
    if len(diff) > DIFF_LINES:
        shown = diff[:DIFF_LINES]
        shown.append(f"... [{len(diff) - DIFF_LINES} more diff lines; read the file if you need them]")
        diff = shown
    return counts + ":" + chr(10) + chr(10).join(diff)


@tool(risk="write")
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact snippet of a file with new text. Prefer this over
    write_file for any change to an existing file: it costs a few hundred
    characters instead of the whole file. The snippet must appear exactly once -
    include surrounding lines to make it unique.

    path: Path relative to the workspace root.
    old_string: The exact text to replace, copied from the file including indentation.
    new_string: The text to put in its place.
    """
    target = config.resolve(path)
    if target.is_dir():
        raise IsADirectoryError(
            f"{path} is a directory, not a file. "
            f"List it with run_shell(command='ls -la {path}').")
    # edit_file bypassed this entirely: measured, it rewrote a .docx as text and
    # destroyed it. One check, not two - every container document is already in
    # BINARY_EXTENSIONS, so an opaque-document branch here is unreachable. write_file
    # still needs its own, because it has no binary guard.
    if has_binary_extension(path):
        return f"refused: {path} is a binary file and cannot be edited as text."
    # surrogateescape, NOT errors="replace". Measured: a latin-1 byte in a source
    # file became U+FFFD on any edit - the write succeeded, the diff looked clean,
    # and a byte the agent never touched was destroyed. surrogateescape round-trips
    # arbitrary bytes losslessly; shrink() strips the surrogates before display.
    text = target.read_text(encoding="utf-8", errors="surrogateescape")

    found = text.count(old_string)
    if found == 0:
        # An error the model cannot act on costs a turn every time it is retried,
        # so name the tool that recovers from it.
        raise ValueError(
            f"that text was not found in {path}. Read the current contents with "
            f'read_file(path="{path}") and copy the text exactly, including '
            f"indentation and blank lines.")
    if found > 1:
        # Editing the first occurrence silently would corrupt the file in a way no
        # test necessarily catches. Refusing is the safety property of this tool.
        raise ValueError(
            f"that text appears {found} times in {path}; it must match exactly "
            f"once. Include more of the surrounding lines to make it unique.")

    updated = text.replace(old_string, new_string)
    target.write_text(updated, encoding="utf-8", errors="surrogateescape")

    # A write that did not land must not report success. The reference makes
    # this a hard error rather than a silent flag and that is the right call.
    # Read back the SAME WAY it was written. Comparing a surrogateescape write
    # against an errors="replace" read reports every non-UTF-8 file as a failed
    # write, which is how this check first fired on a correct edit.
    if target.read_text(encoding="utf-8", errors="surrogateescape") != updated:
        raise RuntimeError(
            f"the edit to {path} did not persist - the file on disk differs "
            f"from what was written. Re-read it and retry.")

    return _edit_receipt(path, text, updated)


# `write` and NOT `destructive`: the DANGER regex in policy.py escalates the
# dangerous commands, so declaring the whole tool destructive would pause on
# every `ls`.
@tool(risk="write")
def run_shell(command: str, timeout: int = 120) -> str:
    """Run any shell command in the workspace. Returns the exit code, stdout
    and stderr separately. Use it to run the tests, and to look around: `ls`,
    `ls -la` and `find` are shell commands, not tools - call them through here.

    command: The command to run, e.g. 'pytest -q' or 'ls -la src'.
    timeout: Seconds before the command is killed. Default 120.
    """
    # Models emit schema-invalid arguments - this one arrived as the string "120s".
    # Coerce at the boundary; a declared type is a hint, not enforcement.
    seconds = _int(timeout, 120)
    # start_new_session puts the command in its own process GROUP. Without it a
    # timeout kills /bin/sh and its children keep running - measured: two orphaned
    # `sleep 60` survived, holding the workspace for the rest of the run. This is
    # the same orphan failure the harness already records for `timeout`.
    # `text=True` alone decodes with the PLATFORM default - cp1252 on Windows,
    # where one UTF-8 byte raises inside subprocess's reader thread and this
    # returned `exit code: 0` with stdout `None`. The command succeeded and its
    # output was destroyed. `replace` and not `strict`: a shell command may emit
    # any bytes at all, and a mangled character beats a dead tool.
    process = subprocess.Popen(
        command, shell=True, cwd=config.WORKSPACE, text=True,
        encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=seconds)
        code = process.returncode
    except subprocess.TimeoutExpired:
        _kill_group(process)
        # WHAT IT PRINTED BEFORE HANGING IS THE POINT. The old code let
        # TimeoutExpired propagate and the partial output died with it, so a
        # pytest run that hung after reporting 40 failures reported none of them.
        out, err = process.communicate()
        return (
            f"TIMED OUT after {seconds}s and was killed. Output up to that point "
            f"is below - it may be incomplete.\n"
            f"--- stdout ---\n{out}\n"
            f"--- stderr ---\n{err}"
        )
    return (
        f"exit code: {code}\n"
        f"--- stdout ---\n{out}\n"
        f"--- stderr ---\n{err}"
    )


def _kill_group(process) -> None:
    """Kill the command and everything it started. Best effort, never raises."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        process.kill()                    # no process groups here; kill what we can


# FR-203 wants stdout, the traceback if it raised, AND the final expression's
# value - a REPL's contract, not a script's. Implemented in-process so the value
# survives; a subprocess would lose it.
_PYTHON_DRIVER = """
import ast, sys, traceback

# The CHILD's own encoding, which the parent cannot set for it. Without this it
# reads stdin and writes stdout as the platform default - cp1252 on Windows -
# so UTF-8 the parent sent arrives as surrogates and ast.parse refuses it, and
# a print() of any non-ASCII character kills the script.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
source = sys.stdin.buffer.read().decode("utf-8", "replace")
try:
    tree = ast.parse(source)
except SyntaxError:
    traceback.print_exc()
    raise SystemExit(1)

tail = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
scope = {"__name__": "__main__"}
try:
    exec(compile(tree, "<agent>", "exec"), scope)
    if tail is not None:
        value = eval(compile(ast.Expression(tail.value), "<agent>", "eval"), scope)
        if value is not None:
            print("--- value ---")
            print(repr(value))
except BaseException:
    # Returned as text, never raised: FR-208 makes the execute node the one place
    # that turns an exception into an observation, and a traceback IS the answer
    # here rather than a failure of the tool.
    traceback.print_exc()
    raise SystemExit(1)
"""


@tool(risk="write")
def run_python(code: str, timeout: int = 120) -> str:
    """Run Python in the workspace. Returns stdout, the traceback if it raised,
    and the VALUE of the final expression - so end with a bare expression to see
    what it evaluates to, as in a REPL. Prefer this over run_shell for anything
    that computes: `python -c` throws the value away.

    code: Python source. The last line may be a bare expression to return its value.
    timeout: Seconds before it is killed. Default 120.
    """
    done = subprocess.run(
        [sys.executable, "-c", _PYTHON_DRIVER],
        input=code, cwd=config.WORKSPACE,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_int(timeout, 120),
    )
    return (
        f"exit code: {done.returncode}\n"
        f"--- stdout ---\n{done.stdout}\n"
        f"--- stderr ---\n{done.stderr}"
    )


# FR-206: paths and line numbers, never file contents. That clause is the
# requirement - grep returns every matching line unbounded, which is the context
# flood shrink() exists to contain.
MATCH_CAP = 50
LINE_CHARS = 120

# Directories whose contents are never what anyone is searching for, and which
# dominate the result cap when included. Measured on this project: .git alone is
# thousands of files.
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv",
             ".agent", ".mypy_cache", ".tox", "dist", "build", ".eggs"}


# Python rather than grep/ripgrep so the walk is rooted at WORKSPACE and the
# gate need not parse a command line. THE ROOT ALONE IS NOT A BOUNDARY: a test
# caught Path.glob("../*") escaping it, so FR-302 is enforced on resolved paths.
@tool(risk="read")
def search_files(pattern: str, glob: str = "**/*", paths_only: bool = False) -> str:
    """Find where something appears in the workspace. Returns path:line: matches,
    never whole files. **Use this instead of run_shell with grep** -
    it is bounded, so it cannot flood your context the way a raw grep across a
    large repository will. Use read_file once this has told you which file and
    which line to look at.

    pattern: Regular expression to search for.
    glob: Which files to search, e.g. '**/*.py'. Default all files.
    paths_only: Return only the file paths, one per file, without the matching lines. Default false.
    """
    if glob.startswith("/") or ".." in Path(glob).parts:
        raise ValueError(
            f"glob {glob!r} points outside the workspace. Patterns are relative "
            f"to the workspace root - use '**/*' to search everything, or "
            f"'**/*.py' for one file type.")
    try:
        matcher = re.compile(pattern)
    except re.error as exc:
        raise ValueError(
            f"{pattern!r} is not a valid regular expression: {exc}. "
            f"Escape regex characters to search for them literally.") from exc

    root = config.WORKSPACE.resolve()
    hits, total, scanned = [], 0, 0
    for target in sorted(config.WORKSPACE.glob(glob)):
        if not target.is_file() or SKIP_DIRS & set(target.parts):
            continue
        # The same guard read_file has. Measured: a .o and a .zip containing the
        # search term matched, emitted two lines of mojibake, and - sorting before
        # the real hit - pushed it down the list. On a repository with many
        # binaries they crowd out genuine matches against MATCH_CAP entirely.
        if has_binary_extension(target.name):
            continue
        # .resolve() follows symlinks, so a link inside the workspace pointing
        # out resolves outside and is dropped here. Fails closed.
        resolved = target.resolve()
        if root not in resolved.parents:
            continue
        scanned += 1
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue                      # unreadable is not a failure of the search
        where = resolved.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            if not matcher.search(line):
                continue
            total += 1
            if len(hits) >= MATCH_CAP:
                continue
            hits.append(where if paths_only
                        else f"{where}:{number}: {line.strip()[:LINE_CHARS]}")

    if paths_only:
        # dict.fromkeys keeps first-seen order while collapsing repeats, so a file
        # with forty matches appears once.
        hits = list(dict.fromkeys(hits))

    if not hits:
        return (f"no match for {pattern!r} in {scanned} file(s) under {glob!r}. "
                f"Widen the glob, or check the pattern is a regular expression.")

    body = "\n".join(hits)
    if total > MATCH_CAP:
        body += (f"\n[{MATCH_CAP} of {total} matches shown. Narrow the pattern, "
                 f"or pass glob= to search fewer files.]")
    return body


# --------------------------------------------------------------------- FR-501
#
# Ranked title + URL + snippet, never page bodies. The engine fan-out is
# load-bearing: pinning one backend rate-limits hard, so there is no retry.
# Measured numbers and WEB_HOSTS' rationale are in eval/CHANGELOG.md Stage 4.
WEB_HOSTS = ("html.duckduckgo.com", "search.brave.com", "www.mojeek.com",
             "www.startpage.com", "search.yahoo.com", "www.google.com",
             "en.wikipedia.org", "grokipedia.com")
RESULT_CAP = 10
SNIPPET_CHARS = 200
WEB_TIMEOUT = 20        # seconds; the default of 5 is thin through a CONNECT proxy

# ddgs uses ONE exception type for both an empty result and a transport
# failure; this string is all that separates them.
_NO_RESULTS = "no results found"


# FR-505. Both halves are about not abusing a host we do not own.
#
# The rate limit is a lesson already paid for: html.duckduckgo.com returns
# results once, then blocks, and every retry re-arms a ~30s cooldown. That is
# why the fan-out exists; this makes the same courtesy explicit and general.
PER_HOST_INTERVAL = 2.0
_LAST_HIT: dict = {}
_ROBOTS: dict = {}


def _pace(host: str) -> None:
    """Sleep just long enough that this host is not hit faster than agreed."""
    wait = PER_HOST_INTERVAL - (time.monotonic() - _LAST_HIT.get(host, 0.0))
    if wait > 0:
        time.sleep(min(wait, PER_HOST_INTERVAL))
    _LAST_HIT[host] = time.monotonic()


def robots_allows(url: str, agent: str = "*") -> bool:
    """Whether robots.txt permits fetching `url`.

    FAILS OPEN, deliberately. A robots.txt that cannot be fetched - a blocked
    host, a timeout, a 500 - must not disable searching, or one flaky server
    takes the capability down. Parsed once per host and cached: the check is a
    network call itself and must not double the cost of every request.
    """
    from urllib.parse import urlsplit
    from urllib.robotparser import RobotFileParser

    parts = urlsplit(url)
    if not parts.netloc:
        return True
    if parts.netloc not in _ROBOTS:
        parser = RobotFileParser()
        parser.set_url(f"{parts.scheme or 'https'}://{parts.netloc}/robots.txt")
        try:
            _pace(parts.netloc)
            parser.read()
        except Exception:
            parser = None                 # unreachable: fail open, cache that
        _ROBOTS[parts.netloc] = parser
    parser = _ROBOTS[parts.netloc]
    return True if parser is None else parser.can_fetch(agent, url)



# --------------------------------------------------------------- 

# Blocked whatever else is true. These are cloud metadata endpoints - the
# credential-theft target - and no agent has a legitimate reason to reach one.
BLOCKED_HOSTS = frozenset({"metadata.google.internal", "metadata.goog"})

# CGNAT is NOT covered by `is_private`: ipaddress returns False for both
# is_private and is_global on 100.64.0.0/10. Tailscale and carrier NAT live
# there, and so does Alibaba's metadata address.
_EXTRA_BLOCKED = (
    ipaddress.ip_network("100.64.0.0/10"),   # RFC 6598 CGNAT
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, all of it
)


def _blocked_ip(ip) -> bool:
    """Whether an address is one the agent must not reach."""
    # ::ffff:x.x.x.x is a distinct object from x.x.x.x to `ipaddress`, so a
    # resolver returning the mapped form walks straight past a set membership
    # test. Unwrap before deciding.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
                or any(ip in network for network in _EXTRA_BLOCKED))


def url_is_safe(url: str) -> tuple[bool, str]:
    """Whether `url` may be opened, and why not when it may not.

    Every address the name resolves to is checked, not the first: a host with
    one public and one private A record is the whole attack.
    """
    import socket
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        return False, f"that is not a URL I can parse: {exc}"
    if parts.scheme not in ("http", "https"):
        return False, f"only http and https are allowed, not {parts.scheme!r}"
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        return False, "the URL names no host"
    if host in BLOCKED_HOSTS:
        return False, f"{host} is a cloud metadata endpoint"

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return ((False, f"{host} is not a public address")
                if _blocked_ip(literal) else (True, ""))

    # Behind a proxy the NAME is what travels and the proxy resolves it, so a
    # local lookup answers a question nobody asked - and in the scored run it
    # fails outright, because that container has no DNS of its own.
    if any(os.environ.get(name) for name in
           ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")):
        return True, ""

    try:
        resolved = socket.getaddrinfo(host, parts.port or 0,
                                      proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, f"{host} does not resolve: {exc}"
    for entry in resolved:
        try:
            address = ipaddress.ip_address(entry[4][0])
        except ValueError:
            continue
        if _blocked_ip(address):
            return False, f"{host} resolves to {address}, which is not public"
    return True, ""

MAX_TERMINALS = 4
MAX_BUFFERED_LINES = 500
_TERMINALS: dict = {}


def _pump(entry) -> None:
    """Read the pipe on a thread of its own, because readline() BLOCKS.

    It returns "" only at EOF, so draining a process that is still RUNNING never
    ends: a scored run sat 112 minutes at 0.01% CPU and the `tools` split
    produced no rows in nine attempts. MAX_SECONDS is checked BETWEEN turns, so
    a turn that never returns is never checked.
    """
    stream = entry["process"].stdout
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            with entry["lock"]:
                entry["buffer"].append(line)
                if len(entry["buffer"]) > MAX_BUFFERED_LINES:
                    del entry["buffer"][:-MAX_BUFFERED_LINES]
    except (ValueError, OSError):
        return                      # the pipe closed under us, which is EOF


def _drain(session) -> str:
    """Everything printed since the last read, and nothing before it.

    Re-sending the whole buffer each time is how a long-running log floods the
    context - the flood shrink() exists to stop, arriving through another door.
    """
    with session["lock"]:
        fresh, session["buffer"] = session["buffer"], []
    return "".join(fresh)


def stop_terminals() -> None:
    """Kill every session. Best effort, never raises."""
    for session in list(_TERMINALS.values()):
        _kill_group(session["process"])
    _TERMINALS.clear()


atexit.register(stop_terminals)


@tool(risk="write")
def start_terminal(command: str) -> str:
    """Start a long-running command and leave it running - a dev server, a
    watcher, a build. Returns a session name; read its output with
    read_terminal. Use run_shell for anything that finishes on its own.

    command: The command to start.
    """
    if len(_TERMINALS) >= MAX_TERMINALS:
        return (f"refused: too many sessions open ({len(_TERMINALS)}). "
                f"Sessions: {', '.join(_TERMINALS)}.")
    name = f"t{len(_TERMINALS) + 1}"
    while name in _TERMINALS:
        name = f"t{int(name[1:]) + 1}"
    try:
        process = subprocess.Popen(
            command, shell=True, cwd=config.WORKSPACE, text=True,
            encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # Its own process GROUP, so stopping it stops what it started.
            start_new_session=True, bufsize=1)
    except OSError as exc:
        return f"could not start it: {exc}"
    entry = {"process": process, "buffer": [], "command": command,
             "lock": threading.Lock()}
    entry["reader"] = threading.Thread(target=_pump, args=(entry,), daemon=True)
    entry["reader"].start()
    _TERMINALS[name] = entry
    return f"started {name}"


@tool(risk="read")
def read_terminal(session: str) -> str:
    """Read what a terminal session has printed since you last read it.

    session: The name start_terminal returned, e.g. 't1'.
    """
    name = str(session or "").strip()
    if name not in _TERMINALS:
        return (f"no session {name!r}. Open sessions: "
                f"{', '.join(_TERMINALS) or 'none'}.")
    entry = _TERMINALS[name]
    code = entry["process"].poll()
    if code is not None:
        # The reader may still hold the last lines - `watch-build` reports its
        # artifact id there. Bounded, so it cannot become the hang it replaced.
        entry["reader"].join(timeout=1.0)
    output = _drain(entry)
    if code is None:
        return output or f"{name} is running, nothing new."
    _TERMINALS.pop(name, None)
    return f"{output}\n{name} finished, exit code {code}."


# The interface supplies this: cli.py at a terminal, the TUI in a modal. The
# TOOL owns the schema and the bounds, the SURFACE owns the asking - the
# reference splits it the same way, because a tool that owned a prompt would
# work in one surface and hang in every other.
ASK = None

# Four, as the reference caps it. A longer list is a menu nobody reads, and
# it is unbounded text in the transcript on every turn that follows.
MAX_CHOICES = 4

# What comes back when no one is there. A worker, a cron task and the eval
# harness all run unattended, and a tool that BLOCKS there hangs the run - the
# same shape as `confirm` degrading to `deny` without a human.
NOBODY_THERE = ("No one is available to answer. Use your best judgement, say "
                "which reading you chose, and continue.")


@tool(risk="read")
def ask_user(question: str, choices: str = "") -> str:
    """Ask the person a question and wait for their answer. Use it when the goal
    is genuinely ambiguous and guessing would waste the run - which file they
    meant, which of two behaviours they want. Do NOT use it for anything you can
    find out by reading or searching; look first, ask second.

    question: What to ask, in one sentence.
    choices: Optional answers separated by ' | ', best first. Free text if empty.
    """
    if ASK is None:
        return NOBODY_THERE
    # A declared schema is not enforcement: `choices` arrives as a string here
    # and as a list from a model that read the description loosely.
    if isinstance(choices, (list, tuple)):
        options = [str(c).strip() for c in choices]
    else:
        options = [part.strip() for part in str(choices or "").split("|")]
    options = [option for option in options if option][:MAX_CHOICES]
    try:
        answer = ASK(str(question), options)
    except Exception:                       # noqa: BLE001 - FR-208, never propagates
        return NOBODY_THERE
    return str(answer).strip() or NOBODY_THERE


@tool(risk="read")
def web_search(query: str, limit: int = 5) -> str:
    """Search the web for current information and return ranked results: title,
    URL and a one-line snippet. Use this when the answer depends on something
    outside the workspace - documentation, an error message, a library's current
    API. It returns links, NOT page contents: call fetch on a URL from these
    results when you need to read the page itself.

    query: What to search for, phrased as you would type it into a search engine.
    limit: How many results to return. Default 5, maximum 10.
    """
    # Imported here so this file stays importable without ddgs, and so a run that
    # never searches does not pay a Rust extension load.
    try:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException
    except ImportError as exc:
        # A missing package is not a failed search, and it must not read like
        # one: this surfaced in the TUI as a bare ModuleNotFoundError at 0.0s,
        # where a blocked engine and an absent library look identical.
        raise RuntimeError(
            "web_search needs the `ddgs` package and it is not installed here. "
            "The container has it; this interpreter does not. Install it with "
            "`pip install ddgs==9.16.0`.") from exc

    query = " ".join(str(query).split())
    if not query:
        raise ValueError("web_search needs a query. Pass what you would type into "
                         "a search engine, e.g. query='fastapi APIRouter post'.")
    count = max(1, min(_int(limit, 5), RESULT_CAP))

    # FR-505: pace the engines we are about to fan out across.
    for host in WEB_HOSTS:
        _pace(host)

    # Passed EXPLICITLY: the openai SDK gets proxies from httpx's trust_env, but
    # ddgs goes through primp, which makes no such promise. Under a scored run an
    # ignored proxy is every search failing, not a slower one.
    try:
        rows = DDGS(proxy=os.environ.get("HTTPS_PROXY"), timeout=WEB_TIMEOUT).text(
            query, max_results=count)
    except DDGSException as exc:
        if _NO_RESULTS not in str(exc).lower():
            # The cause is NOT asserted: from inside the container a missing allowlist
            # entry and a refused connection are indistinguishable.
            raise RuntimeError(
                f"web search failed for {query!r}: {exc}. The search engines were "
                f"unreachable - egress is restricted to an allowlist, and it may "
                f"not carry them.") from exc
        rows = []

    if not rows:
        return (f"no results for {query!r}. Try fewer or more common words - this "
                f"searches the live web, so an exact phrase with no matches "
                f"returns nothing.")

    out = []
    for rank, row in enumerate(rows[:count], 1):
        snippet = " ".join((row.get("body") or "").split())[:SNIPPET_CHARS]
        out.append(f"{rank}. {row.get('title') or '(untitled)'}\n"
                   f"   {row.get('href') or ''}"
                   + (f"\n   {snippet}" if snippet else ""))
    return "\n".join(out)


# The schema is DERIVED from the signature and docstring above, so this is the
# whole registration. Order is deterministic: tools render first in the prompt.
TOOLS = {fn.__name__: fn.spec for fn in (
    read_file, search_files, write_file, edit_file, run_python, run_shell,
    ask_user, start_terminal, read_terminal,
    web_search)}


def builtins() -> dict:
    """The built-ins exposed for THIS run - the same shape as memory.tools().

    web_search is dropped when AGENT_WEB is off, and AGENT_TOOLS_OFF drops any
    named tool. That switch is what makes a control run a controlled comparison
    rather than two numbers measured on different binaries. Gated here rather
    than by rebuilding TOOLS at import, so `policy.sync()` can still classify the
    tool and a test can flip the flag without reloading the module.
    """
    dropped = set(config.TOOLS_OFF)
    if not config.WEB_ENABLED:
        dropped.add("web_search")
    if not dropped:
        return TOOLS
    return {name: entry for name, entry in TOOLS.items() if name not in dropped}

SCHEMAS = [entry["schema"] for entry in TOOLS.values()]


def toolset() -> dict:
    """Kept as the name every caller already uses; the merge itself moved.

    Three modules contribute tools now (built-ins, MCP, memory), so who owns the
    merged view became a real question and `agent/registry.py` is the answer -
    §12's "add at tool six" trigger, fired by `remember`.
    """
    from agent.registry import toolset as merged

    return merged()
