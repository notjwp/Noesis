"""Secret-shaped values in tool output (NFR-203).

`context.redact()` replaces values it finds in `os.environ`. That covers the
container's own credentials and nothing else - measured, a workspace `.env`, an
`AWS_KEY = "AKIA..."` in source, and a `postgres://user:pw@host` DSN all reached
the model verbatim.

THE PATTERN LIST IS THE VALUE: forty issuers with a literal prefix each,
accumulated from real incidents. A hand-written list would be shorter and wrong.

WHAT WAS DELIBERATELY LEFT OUT, and this is the whole design decision: matching
`NAME=value` where NAME merely *contains* key/token/secret/password. That is
right for terminal output and chat, and catastrophic on source code, which is
what this agent reads all day. Measured against our own tree, a redactor that
does it altered 29 lines of 14,243, including:

    spent_tokens: int              ->  spent_tokens: ***
    budget_tokens: int | None      ->  budget_tokens: *** | None
    max_tokens=settings.MAX_TOKENS ->  max_tokens=settin...ENS

Type annotations and identifiers destroyed. Any tokenizer, auth module or config
file would become unreadable to the agent trying to fix it.

Only shapes that cannot collide with an identifier are kept: a literal issuer
prefix, a PEM block, a JWT, or a DSN password. All four are unambiguous.
"""
from __future__ import annotations

import re

# Issuer prefixes. Each carries a literal prefix, so none can match an ordinary
# identifier.
_PREFIXES = [
    r"sk-[A-Za-z0-9_-]{10,}",            # OpenAI / OpenRouter / Anthropic
    # The provider this project actually runs on, and it was missing while
    # eleven others were listed. The env path hid it: AGENT_API_KEY ends in
    # _KEY so OUR key was caught, and any OTHER nvapi- key - a workspace
    # .env, one in source, a second account - reached the model verbatim.
    r"nvapi-[A-Za-z0-9_-]{10,}",         # NVIDIA NIM
    r"sk_live_[A-Za-z0-9]{10,}",         # Stripe live
    r"sk_test_[A-Za-z0-9]{10,}",         # Stripe test
    r"rk_live_[A-Za-z0-9]{10,}",         # Stripe restricted
    r"sk_[A-Za-z0-9_]{10,}",             # ElevenLabs
    r"ghp_[A-Za-z0-9]{10,}",             # GitHub PAT, classic
    r"github_pat_[A-Za-z0-9_]{10,}",     # GitHub PAT, fine-grained
    r"gh[ousr]_[A-Za-z0-9]{10,}",        # GitHub OAuth / user / server / refresh
    r"glpat-[A-Za-z0-9_\-]{10,}",        # GitLab PAT
    r"gl(?:oas|dt|rtr|rt|cbt|ptt|ft|imt|agent|soat|ffct|wt)-[A-Za-z0-9_.\-]{10,}",
    r"GR1348941[A-Za-z0-9_\-]{10,}",     # GitLab legacy runner
    r"xox[baprs]-[A-Za-z0-9-]{10,}",     # Slack
    r"xapp-\d+-[A-Za-z0-9-]{10,}",       # Slack app-level
    r"AKIA[A-Z0-9]{16}",                 # AWS access key id
    r"AIza[A-Za-z0-9_-]{30,}",           # Google
    r"SG\.[A-Za-z0-9_-]{10,}",           # SendGrid
    r"hf_[A-Za-z0-9]{10,}",              # HuggingFace
    r"npm_[A-Za-z0-9]{10,}",             # npm
    r"pypi-[A-Za-z0-9_-]{10,}",          # PyPI
    r"r8_[A-Za-z0-9]{10,}",              # Replicate
    r"gsk_[A-Za-z0-9]{10,}",             # Groq
    r"xai-[A-Za-z0-9]{30,}",             # xAI
    r"ntn_[A-Za-z0-9]{10,}",             # Notion
    r"fw[-_][A-Za-z0-9]{30,}",           # Fireworks
    r"dop_v1_[A-Za-z0-9]{10,}",          # DigitalOcean
    r"tvly-[A-Za-z0-9]{10,}",            # Tavily
    r"pplx-[A-Za-z0-9]{10,}",            # Perplexity
    r"gAAAA[A-Za-z0-9_=-]{20,}",         # Codex encrypted token
]

# A PEM block and a JWT are self-identifying, and a DSN password sits between
# `://user:` and `@host` where nothing else can.
_PRIVATE_KEY = r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
_JWT = r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){1,2}"

# The lookbehind is load-bearing. Without it `sk_[A-Za-z0-9_]{10,}` matched INSIDE
# ordinary identifiers - `risk_classified`, `task_to_running` - and redacted 14
# lines of this project's own source. A prefix only counts at a token boundary.
_BOUNDARY = "(?<![A-Za-z0-9_])"
_SECRET = re.compile("|".join(f"(?:{_BOUNDARY}{p})"
                              for p in _PREFIXES + [_PRIVATE_KEY, _JWT]))

# Only the password is replaced, so the host and database stay readable - an
# agent debugging a connection needs to see where it points.
# EVERY QUANTIFIER IS BOUNDED, and that is not tidiness. The scheme part was
# `[a-zA-Z0-9+.\-]*` and on 60,000 repeated characters it consumed the whole
# string looking for `://`, failed, backtracked one, and repeated from every
# start position: 36 SECONDS to scrub 720 KB. redact() runs on every tool result,
# so that is minutes added to a run. A URL scheme is never 30 characters, a DSN
# user is never 200, and a password is never 400.
_DSN = re.compile(r"(?P<head>[a-zA-Z][a-zA-Z0-9+.\-]{0,30}://[^:/\s@]{1,200}:)"
                  r"(?P<secret>[^@\s]{1,400})(?P<tail>@)")


def scrub(text: str) -> str:
    """Replace secret-shaped values with a marker naming what was found."""
    text = _DSN.sub(lambda m: f"{m['head']}[redacted:password]{m['tail']}", text)
    return _SECRET.sub("[redacted:secret]", text)
