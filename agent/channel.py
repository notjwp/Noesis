"""Email intake and delivery: the agent becomes something you can write to.

The whole file rests on one decision - A MESSAGE MAPS TO A TASK, and a task id
IS a thread id. Nothing here invents a second identity space, a second queue or a
second liveness check; worker.py already has all three. A runtime where delivery,
sessions and identity grow up separately ends up carrying all three twice.

Three designs, none of them lifted:

- DEFAULT DENY on who may write (gateway/authz_mixin.py). The moment a message
  can arrive from outside the terminal, "who is asking" is a security question,
  and policy.classify() assumes one trusted local user. An address is trivially
  forged, so this is a filter on a mailbox you control, NOT authentication.
- A FIRST RUN MARKS THE EXISTING INBOX SEEN rather than answering it. Theirs
  logs "%d existing messages skipped" for the same reason: without it, starting
  the listener replies to every message you have ever received.
- AN OUTBOUND REPLY IS A ROW WITH A STATE (gateway/delivery_ledger.py). A send
  that fails must not lose the answer and a poison row must not spin.
"""
import email.message
import email.utils
import imaplib
import os
import smtplib
import time

from agent import config, worker

# Gmail truncates nothing, but a runaway answer is still worth bounding: this is
# a reply to a person, not a log.
MAX_BODY = 8000

# Past this many failed sends a reply is abandoned rather than retried forever.
# Capped because a poison row that cannot be delivered must not occupy the
# sweep on every tick.
MAX_ATTEMPTS = 5

# IMAP flags the agent's own replies so a mail client that syncs them back into
# the mailbox cannot be read as a new instruction.
SUBJECT_TAG = "[agent]"


class ChannelUnavailable(RuntimeError):
    """The mail server could not be reached. The caller retries; no row is lost."""


class ChannelRefused(ChannelUnavailable):
    """The credentials were rejected. Retrying cannot help, so the loop stops.

    A SUBCLASS, so every existing handler still catches it and no row is lost -
    only run_channel singles it out. Not retryable, because bad or revoked
    credentials can never self-heal.
    """


# Only markers a server sends for a REJECTED LOGIN. Deliberately narrow: an
# ambiguous error must stay retryable, because stopping on a transient one is a
# listener that is off when it is most needed. SMTP is classified by exception
# type; IMAP4.error is left alone because imaplib gives us only the server
# text, so these are the unambiguous strings and nothing else.
_REFUSED = ("authenticationfailed", "invalid credentials",
            "authenticate failed", "login failed")


def configured() -> bool:
    return bool(config.EMAIL_USER and config.EMAIL_PASSWORD and config.EMAIL_ALLOW)


def allowed(sender: str) -> bool:
    """Whether this address may task the agent. DEFAULT DENY.

    An empty allowlist authorises nobody, which is the only safe reading: a
    mailbox with credentials and no allowlist would take instructions from
    anyone who can find the address, including every spammer.

    A From header is forged trivially. This is a filter, not authentication, and
    the security rests on the mailbox itself being yours.
    """
    _, address = email.utils.parseaddr(sender or "")
    return bool(address) and address.lower() in config.EMAIL_ALLOW


def _imap() -> imaplib.IMAP4_SSL:
    try:
        box = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT,
                                timeout=config.CHANNEL_TIMEOUT)
        box.login(config.EMAIL_USER, config.EMAIL_PASSWORD)
        box.select("INBOX")
        return box
    except (imaplib.IMAP4.error, OSError) as exc:
        text = str(exc).lower()
        if any(marker in text for marker in _REFUSED):
            raise ChannelRefused(f"imap: {exc}") from exc
        raise ChannelUnavailable(f"imap: {exc}") from exc


def _uid_mark(conn) -> int:
    row = conn.execute(
        "SELECT value FROM channel_state WHERE key='uid'").fetchone()
    return int(row["value"]) if row else 0


def _remember_uid(conn, value: int) -> None:
    conn.execute("INSERT INTO channel_state (key, value) VALUES ('uid', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))


def _body_of(message: email.message.Message) -> str:
    """The plain-text body, first part only, quoted history dropped.

    A reply carries the whole prior thread beneath it; feeding that back would
    re-send the agent its own last answer as a fresh instruction every round.
    """
    part = message
    if message.is_multipart():
        for candidate in message.walk():
            if candidate.get_content_type() == "text/plain":
                part = candidate
                break
    try:
        raw = part.get_payload(decode=True) or b""
        text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    except (LookupError, ValueError):
        return ""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or stripped.startswith("On ") and "wrote:" in stripped:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def intake() -> list[str]:
    """Turn new authorised messages into queued tasks. Returns their ids.

    THE FIRST RUN ANSWERS NOTHING. With no high-water mark recorded, every
    message already in the mailbox is marked seen and skipped - otherwise
    starting the listener replies to your entire inbox history.

    The mark advances for every message SEEN, authorised or not; a message from
    a stranger that did not advance it would be re-read forever and the listener
    would never reach the next one.
    """
    box = _imap()
    try:
        status, data = box.uid("search", None, "ALL")
        if status != "OK":
            raise ChannelUnavailable(f"imap search: {status}")
        uids = [int(u) for u in (data[0] or b"").split()]
        if not uids:
            return []

        with worker._connect() as conn:
            mark = _uid_mark(conn)
            if mark == 0:                 # first run: adopt the inbox, answer none
                _remember_uid(conn, max(uids))
                return []

        queued, highest = [], mark
        for uid in sorted(u for u in uids if u > mark):
            highest = max(highest, uid)
            status, fetched = box.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            message = email.message_from_bytes(fetched[0][1])
            subject = (message.get("Subject") or "").strip()
            if SUBJECT_TAG in subject:
                continue                  # our own reply, synced back into the box
            if not allowed(message.get("From", "")):
                continue
            goal = _body_of(message) or subject
            if not goal:
                continue
            task_id = worker.submit(goal)
            _, address = email.utils.parseaddr(message.get("From", ""))
            with worker._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET reply_to=?, reply_ref=?, subject=? WHERE id=?",
                    (address, message.get("Message-ID"), subject, task_id))
            queued.append(task_id)

        if highest != mark:
            with worker._connect() as conn:
                _remember_uid(conn, highest)
        return queued
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def owed() -> list[dict]:
    """Finished tasks that came from a mailbox and have not been answered."""
    with worker._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE reply_to IS NOT NULL AND delivered_at IS NULL "
            "AND status IN ('done','failed') AND attempts < ? "
            "ORDER BY finished_at", (MAX_ATTEMPTS,))]


def _reply(task: dict) -> email.message.EmailMessage:
    body = (task.get("detail") or task.get("verdict") or "").strip()
    if task.get("status") == "failed" and not body:
        body = "That did not work, and the run left no explanation."
    note = email.message.EmailMessage()
    note["From"] = config.EMAIL_USER
    note["To"] = task["reply_to"]
    original = (task.get("subject") or "your request").strip()
    note["Subject"] = f"{SUBJECT_TAG} {original}"
    if task.get("reply_ref"):
        # Threads the answer under the question instead of starting a new
        # conversation. Both headers: clients disagree about which they honour.
        note["In-Reply-To"] = task["reply_ref"]
        note["References"] = task["reply_ref"]
    note.set_content(f"{body or 'Done.'}\n\n--\ntask {task['id']}\n"[:MAX_BODY])
    return note


def deliver() -> int:
    """Send every owed reply. Returns how many landed.

    ATTEMPTS ARE COUNTED BEFORE THE SEND, not after. A crash between sending and
    recording would otherwise leave the row owed and re-send on every tick - the
    failure mode is a person receiving the same answer forty times.
    """
    pending = owed()
    if not pending:
        return 0
    try:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                  timeout=config.CHANNEL_TIMEOUT)
        server.login(config.EMAIL_USER, config.EMAIL_PASSWORD)
    except smtplib.SMTPAuthenticationError as exc:
        raise ChannelRefused(f"smtp: {exc}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ChannelUnavailable(f"smtp: {exc}") from exc

    sent = 0
    try:
        for task in pending:
            with worker._connect() as conn:
                conn.execute("UPDATE tasks SET attempts=attempts+1 WHERE id=?",
                             (task["id"],))
            try:
                server.send_message(_reply(task))
            except (smtplib.SMTPException, OSError):
                continue                  # the row stays owed; the sweep retries
            with worker._connect() as conn:
                conn.execute("UPDATE tasks SET delivered_at=? WHERE id=?",
                             (time.time(), task["id"]))
            sent += 1
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            pass
    return sent


def check() -> list[str]:
    """Probe both halves of the channel and report. Sends and queues nothing.

    Two connections, because a mailbox that reads and cannot send is the worst
    outcome: the agent would accept work and answer none of it. This project has
    watched a proxy report Running for two hours while failing every request -
    a preflight has to perform the operation the dependent code performs.
    """
    lines = []
    if not config.EMAIL_USER or not config.EMAIL_PASSWORD:
        return ["FAIL  set AGENT_EMAIL_USER and AGENT_EMAIL_PASSWORD"]
    if not config.EMAIL_ALLOW:
        lines.append("FAIL  AGENT_EMAIL_ALLOW is empty, which authorises nobody")
    else:
        lines.append(f"ok    {len(config.EMAIL_ALLOW)} address(es) authorised")

    try:
        box = _imap()
        try:
            status, data = box.uid("search", None, "ALL")
            held = len((data[0] or b"").split()) if status == "OK" else 0
            with worker._connect() as conn:
                mark = _uid_mark(conn)
            lines.append(f"ok    imap {config.IMAP_HOST} - {held} message(s) in INBOX")
            lines.append(f"ok    high-water mark {mark}"
                         + (" (a first run will adopt the inbox and answer none of it)"
                            if mark == 0 else ""))
        finally:
            try:
                box.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    except ChannelUnavailable as exc:
        lines.append(f"FAIL  {exc}")

    try:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                  timeout=config.CHANNEL_TIMEOUT)
        try:
            server.login(config.EMAIL_USER, config.EMAIL_PASSWORD)
            lines.append(f"ok    smtp {config.SMTP_HOST} - login accepted")
        finally:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError):
                pass
    except (smtplib.SMTPException, OSError) as exc:
        lines.append(f"FAIL  smtp: {exc}")
    return lines


def diagnose() -> list[str]:
    """Every precondition this agent needs, each line ok or FAIL.

    ONE command that probes every precondition rather than the one you happened
    to suspect. It covers the two providers we have and nothing more.
    """
    from agent import migrations, registry, worker

    lines = []

    try:
        config.openai_api_key() if config.PROVIDER != "anthropic" else None
        lines.append(f"ok    provider {config.PROVIDER}, key present")
    except RuntimeError as exc:
        lines.append(f"FAIL  {exc}")
    lines.append(f"ok    model {config.OPENAI_MODEL}"
                 if config.PROVIDER != "anthropic" else
                 f"ok    model {config.MODEL}")

    root = config.WORKSPACE
    lines.append(f"ok    workspace {root}" if root.is_dir()
                 else f"FAIL  workspace {root} does not exist")
    # Whether the home CAN be written, from its nearest existing ancestor -
    # creating it here would be a change, and the doctor changes nothing.
    home = config.AGENT_HOME
    nearest = next(p for p in (home, *home.parents) if p.exists())
    lines.append(f"ok    home {home}" if os.access(nearest, os.W_OK)
                 else f"FAIL  home {home} cannot be created: {nearest} is not writable")
    # Said here because nothing else says it: the sandbox belongs to the eval
    # harness, and on this machine the policy gate is the whole boundary.
    lines.append("--    sandbox none: tools run on this machine; the policy gate "
                 "is the boundary. Writes outside the workspace ask first")

    try:
        with worker._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            queued = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
        lines.append(f"ok    tasks db at schema v{version}, {queued} queued"
                     if version == len(migrations.TASKS)
                     else f"FAIL  tasks db at v{version}, code expects "
                          f"v{len(migrations.TASKS)}")
    except Exception as exc:
        lines.append(f"FAIL  tasks db: {exc}")

    running = [t for t in worker.tasks() if t["status"] == "running"]
    alive = [t for t in running if worker._alive(t["pid"], t["pid_started"])]
    lines.append(f"ok    {len(alive)} worker(s) alive, {len(running)} task(s) running"
                 if len(alive) == len(running) else
                 f"FAIL  {len(running) - len(alive)} task(s) running with no live "
                 f"worker - run --worker to recover them")

    try:
        size = registry.check_budget()
        lines.append(f"ok    tool schemas {size:,} of {config.MAX_SCHEMA_CHARS:,} chars")
    except Exception as exc:
        lines.append(f"FAIL  {exc}")

    from agent import setup

    if setup.fragment_path() is not None:
        lines.append(f"ok    windows terminal profile {setup.PROFILE_NAME}"
                     if setup.installed() else
                     "--    no windows terminal profile "
                     "(noesis --terminal-profile)")

    if configured():
        lines.extend(check())
    else:
        lines.append("--    email channel not configured (set AGENT_EMAIL_*)")
    return lines


def run_channel(once: bool = False, poll: float | None = None) -> int:
    """Read the mailbox and pay outstanding replies until stopped.

    Deliberately NOT a second worker: this loop only queues and answers. Running
    the tasks stays with `--worker`, so a channel that dies cannot strand a
    running graph, and a worker that dies cannot lose a person's reply.
    """
    if not configured():
        raise ChannelUnavailable(
            "set AGENT_EMAIL_USER, AGENT_EMAIL_PASSWORD and AGENT_EMAIL_ALLOW")
    gap = config.CHANNEL_POLL if poll is None else poll
    served = 0
    while True:
        try:
            served += len(intake())
            deliver()
        except ChannelRefused:
            raise                         # a rejected password cannot self-heal
        except ChannelUnavailable:
            pass                          # an outage is not a reason to exit
        if once:
            return served
        time.sleep(gap)
