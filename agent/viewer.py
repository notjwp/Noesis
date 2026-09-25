"""A read-only viewer for what this agent has already done.

A STATED DEVIATION from section 12, created 2026-09-10, and section 11 was
amended in writing before it (883b6fe) rather than reinterpreted. The request
was for a web dashboard; this is the capability behind that request, sized for
one person on one machine.

Three conditions came with the amendment, and they are the whole design:

  LOOPBACK. `config.VIEWER_HOST` is the constant "127.0.0.1" and not a tunable.
  This page serves traces, goals and workspace paths, and an env var is how
  loopback becomes 0.0.0.0 by accident.

  A SECRET on every request, compared with `hmac.compare_digest`. One secret, no
  accounts, no sessions - their auth.py is 9,409 lines because it is OAuth,
  tenancy and subscriptions, and none of that has a job here.

  READ ONLY. `Handler` defines `do_GET` and nothing else, so the gate in
  policy.py remains the only path to a side effect. A write path reopens the
  amendment rather than stretching it.

Everything served passes through `context.redact()` - the same function
`shrink()` uses on the way INTO the model. A key that a tool echoed into a trace
must not become a key on a web page, and one chokepoint is the only way to be
sure of that. CE-05: no I/O at import.
"""
from __future__ import annotations

import hmac
import http.server
import json
import pathlib
import secrets as _stdlib_secrets

from agent import config
from agent.context import redact

# The queue's own states, in the order a person reads them. Nothing new is
# stored to draw a board: worker.tasks() already records `status`, and their
# kanban is 15,581 lines because it is a team tool with assign/attach/comment.
COLUMNS = ("queued", "running", "awaiting_approval", "done", "failed")


def _secret() -> str:
    """The shared secret, minted once into AGENT_HOME and reused after."""
    path = config.VIEWER_SECRET
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    minted = _stdlib_secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(minted, encoding="utf-8", newline="\n")
    return minted


def _authorised(token: str) -> bool:
    """Constant time, because `==` leaks length and prefix through timing."""
    return hmac.compare_digest(str(token or ""), _secret())


# --------------------------------------------------------------- the payloads
#
# Each is a seam a test replaces, and each reads a store that already exists.
# Nothing here computes anything the CLI does not already print.

def _threads() -> list[dict]:
    from agent import cli, graph

    return cli._thread_rows(graph.get_app())


def _tasks() -> list[dict]:
    from agent import worker

    return worker.tasks()


def _trace(task_id: str) -> list[dict]:
    from agent import worker

    return worker.events(task_id)


def _schedules() -> list[dict]:
    from agent import worker

    return worker.schedules()


def _attention() -> list[str]:
    from agent import worker

    return worker.attention()


def _runs(limit: int = 20) -> list[dict]:
    """The newest scored rows. The measurement record, not a live feed."""
    # From THIS FILE, not the workspace: the workspace is wherever the agent
    # was pointed and has no eval/ in it. Measured - this returned [] against a
    # repository holding sixty scored runs.
    root = pathlib.Path(__file__).resolve().parent.parent / "eval" / "runs"
    if not root.is_dir():
        return []
    out = []
    for directory in sorted(root.iterdir(), reverse=True)[:limit]:
        summary = directory / "summary.jsonl"
        if not summary.is_file():
            continue
        for line in summary.read_text(encoding="utf-8",
                                      errors="replace").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out[:200]


def _board(rows: list[dict]) -> dict:
    """Tasks grouped by the status they already carry."""
    board: dict = {name: [] for name in COLUMNS}
    for row in rows:
        board.setdefault(str(row.get("status", "other")), []).append(row)
    return board


PAGE = """<!doctype html><meta charset=utf-8><title>NOESIS</title>
<style>
 body{background:#16161e;color:#c0caf5;font:14px ui-monospace,monospace;margin:0;padding:24px}
 h1{font-size:15px;letter-spacing:.3em;color:#7aa2f7;margin:0 0 20px}
 h2{font-size:12px;text-transform:uppercase;letter-spacing:.15em;color:#565f89;margin:24px 0 8px}
 .cols{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
 .col{background:#1a1b26;border:1px solid #24283b;border-radius:6px;padding:10px;min-width:190px;flex:1}
 .col b{color:#7aa2f7;font-weight:400;font-size:11px;text-transform:uppercase}
 .item{border-top:1px solid #24283b;padding:6px 0;font-size:12px}
 table{border-collapse:collapse;width:100%;font-size:12px}
 td,th{text-align:left;padding:4px 10px 4px 0;border-bottom:1px solid #24283b}
 th{color:#565f89;font-weight:400}
 .muted{color:#565f89}
</style>
<h1>N O E S I S</h1><div id=x class=muted>loading</div>
<script>
const t=new URLSearchParams(location.search).get('token')||'';
const get=p=>fetch(p+'?token='+encodeURIComponent(t)).then(r=>r.json());
const esc=s=>String(s??'').replace(/[<&]/g,c=>({'<':'&lt;','&':'&amp;'}[c]));
Promise.all([get('/api/tasks'),get('/api/threads'),get('/api/runs'),
             get('/api/schedules'),get('/api/attention')])
.then(([board,threads,runs,scheds,att])=>{
  const col=(k,v)=>`<div class=col><b>${esc(k)} (${v.length})</b>`+
    v.map(i=>`<div class=item>${esc(i.goal||i.id)}</div>`).join('')+'</div>';
  const rows=(r,cs)=>'<table><tr>'+cs.map(c=>`<th>${c}</th>`).join('')+'</tr>'+
    r.map(x=>'<tr>'+cs.map(c=>`<td>${esc(x[c])}</td>`).join('')+'</tr>').join('')+'</table>';
  document.getElementById('x').innerHTML=
    (att.length?`<h2>needs attention</h2><div>${att.map(esc).join('<br>')}</div>`:'')+
    '<h2>queue</h2><div class=cols>'+Object.entries(board).map(([k,v])=>col(k,v)).join('')+'</div>'+
    '<h2>threads</h2>'+rows(threads,['id','verdict','turns','goal'])+
    '<h2>schedules</h2>'+rows(scheds,['id','expr','goal'])+
    '<h2>recent scored runs</h2>'+rows(runs.slice(0,40),['id','verdict','pass','turns','tokens']);
}).catch(e=>document.getElementById('x').textContent='failed: '+e);
</script>"""


# One task's trace, which carries tool OUTPUT - so it leaves through the same
# redact chokepoint below as everything else, not a route of its own.
_TRACE = ("/api/tasks/", "/trace")


def serve_path(path: str, token: str) -> tuple[int, str, str]:
    """Answer one request as (status, content type, body).

    Separate from the HTTP plumbing so the conditions section 11 attached can be
    tested without binding a port.
    """
    if not _authorised(token):
        return 401, "text/plain; charset=utf-8", "unauthorised"
    if path == "/" or path.startswith("/?"):
        return 200, "text/html; charset=utf-8", PAGE

    routes = {
        "/api/threads": _threads,
        "/api/tasks": lambda: _board(_tasks()),
        "/api/schedules": _schedules,
        "/api/attention": _attention,
        "/api/runs": _runs,
    }
    bare = path.split("?", 1)[0]
    source = routes.get(bare)
    if source is None and bare.startswith(_TRACE[0]) and bare.endswith(_TRACE[1]):
        task_id = bare[len(_TRACE[0]):-len(_TRACE[1])]
        if task_id:
            source = lambda tid=task_id: _trace(tid)      # noqa: E731
    if source is None:
        return 404, "text/plain; charset=utf-8", "no such path"
    try:
        payload = source()
    except Exception as exc:                       # a broken store is not a crash
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    # ONE chokepoint. Redacting three routes of four is the same defect as
    # redacting none, and it is invisible from outside.
    return 200, "application/json", redact(json.dumps(payload, default=str))


class Handler(http.server.BaseHTTPRequestHandler):
    """GET and nothing else. A `do_POST` here would be a second path to a side
    effect, past the gate - which is what section 11's amendment forbids."""

    def do_GET(self) -> None:                      # noqa: N802 - the stdlib name
        from urllib.parse import parse_qs, urlparse

        parts = urlparse(self.path)
        token = (parse_qs(parts.query).get("token") or [""])[0]
        status, kind, body = serve_path(parts.path, token)
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args) -> None:
        """Silent. The access log would print the token in every line."""


def serve_in_background() -> int:
    """Start the viewer on a daemon thread and return the port it took.

    `run()` blocks, which is right at a shell prompt and wrong inside the TUI -
    a session cannot wait on serve_forever(). Port 0 lets the OS pick when the
    configured one is taken, so a second NOESIS does not fail to show a viewer.
    """
    import threading

    try:
        server = http.server.ThreadingHTTPServer(
            (config.VIEWER_HOST, config.VIEWER_PORT), Handler)
    except OSError:
        server = http.server.ThreadingHTTPServer((config.VIEWER_HOST, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return int(server.server_address[1])


def run() -> int:
    """Serve until interrupted. Prints the URL with the token once."""
    token = _secret()
    server = http.server.ThreadingHTTPServer(
        (config.VIEWER_HOST, config.VIEWER_PORT), Handler)
    print(f"http://{config.VIEWER_HOST}:{config.VIEWER_PORT}/?token={token}")
    print("read-only, loopback only. ctrl+c to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
    return 0
