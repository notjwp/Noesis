"""What the recorded traces say actually happened.

The evidence half of the eval loop. `harness.py` produces rows; this reads every
row ever produced and answers the questions a cycle starts from: which split is
failing, what shape the failures take, which mechanisms FIRED, and what a run
costs. Separate from harness.py for the same reason measure_recall.py is - no
model, no network, no quota, so it can be run at any time without spending a
scored pass.

Built 2026-09-25, after a requirement audit did all of this by hand through a
dozen throwaway scripts and turned up two defects that seven passing tests and a
Linux container had both missed.

It ATTRIBUTES ONLY WHAT THE ROWS SUPPORT. A failing run whose cause is not on the
record is counted `unattributed` rather than guessed into a bucket, and buckets
are never aggregated across splits - `search` cases are questions, so not editing
is correct there and a defect in `dev`.
"""
import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "eval" / "runs"
TASKS = REPO / "eval" / "tasks.jsonl"

WRITES = ("write_file", "edit_file")


def splits() -> dict[str, str]:
    """Case id -> split, from the fixture file rather than the row."""
    out = {}
    for line in TASKS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            out[case["id"]] = case.get("split") or "?"
    return out


def rows(since: str = "", only: str = "") -> list[dict]:
    """Every recorded trace, newest run last. `since` is a YYYYMMDD prefix."""
    where = splits()
    found = []
    for path in sorted(RUNS.glob("*/*.json")):
        if path.name == "manifest.json" or path.name == "summary.jsonl":
            continue
        day = "".join(c for c in path.parent.name if c.isdigit())[:8]
        if since and day < since:
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "id" not in row:
            continue
        row["split"] = where.get(row["id"], "?")
        row["run"] = path.parent.name
        if only and row["split"] != only:
            continue
        found.append(row)
    return found


def bucket(row: dict) -> str:
    """Why this failing run ENDED, from the record alone.

    The TERMINAL VERDICT comes first, and that ordering is the whole correctness
    of this function. `failures_after` is the counter's final value, not a cause:
    27 of 63 `real` failures carried it at 3 or more while ending `done`, and
    reading it first attributed them all to a tool-failure exit they never took.
    Tamper is counted separately, for the same reason - it is a wasted-turns
    signal, not a way for a run to end.
    """
    verdict = row.get("verdict")
    if verdict == "done":
        return "ended `done` with the check still failing"
    if verdict == "stuck":
        if (row.get("failures_after") or 0) >= 3:
            return "stuck: three consecutive failing turns"
        if row.get("compact_count", 0) >= 3:
            return "stuck: compacted to the limit and still over"
        if not any(c.get("tool") in WRITES for c in row.get("calls") or []):
            return "stuck: out of turns or seconds, having written nothing"
        return "stuck: out of turns or seconds"
    if verdict:
        return f"ended `{verdict}`"
    return "no verdict recorded"


def _pct(n: int, of: int) -> str:
    return f"{n / of * 100:.0f}%" if of else "-"


def per_split(found: list[dict]) -> None:
    by = defaultdict(list)
    for row in found:
        by[row["split"]].append(row)
    print(f"{'split':12}{'scored':>8}{'passed':>8}{'rate':>7}{'blocked':>9}"
          f"{'median tokens':>15}")
    for split in sorted(by):
        group = by[split]
        ok = [r for r in group if r.get("status") == "ok"]
        won = [r for r in ok if r.get("pass")]
        spend = [r["tokens"] for r in ok if r.get("tokens")]
        print(f"{split:12}{len(ok):>8}{len(won):>8}{_pct(len(won), len(ok)):>7}"
              f"{len(group) - len(ok):>9}"
              f"{(f'{statistics.median(spend):,.0f}' if spend else '-'):>15}")


def per_case(found: list[dict], split: str) -> None:
    by = defaultdict(list)
    for row in found:
        if row["split"] == split and row.get("status") == "ok":
            by[row["id"]].append(row)
    if not by:
        return
    print(f"\n{split} by case, every recorded run:")
    for case in sorted(by):
        group = by[case]
        won = sum(1 for r in group if r.get("pass"))
        verdicts = Counter(r.get("verdict") for r in group)
        shape = " ".join(f"{v}x{n}" for v, n in verdicts.most_common())
        print(f"  {case:22}{won:>4}/{len(group):<4}  {shape}")


def buckets(found: list[dict]) -> None:
    by, tampered = defaultdict(Counter), Counter()
    for row in found:
        if row.get("status") == "ok" and not row.get("pass"):
            by[row["split"]][bucket(row)] += 1
            if row.get("tampered"):
                tampered[row["split"]] += 1
    if not by:
        return
    print("\nHow failing runs ENDED, per split - never aggregated, because the")
    print("splits want different things (a `search` case is a question, so not")
    print("editing is correct there and a defect in `dev`):")
    for split in sorted(by):
        total = sum(by[split].values())
        print(f"\n  {split}  ({total} failing)")
        for name, n in by[split].most_common():
            print(f"    {n:>4}  {_pct(n, total):>4}  {name}")
        if tampered[split]:
            print(f"    {tampered[split]:>4}  {_pct(tampered[split], total):>4}  "
                  f"(of those, edited the tests they are judged by)")


def census(found: list[dict]) -> None:
    tools, verdicts, kinds = Counter(), Counter(), Counter()
    for row in found:
        for call in row.get("calls") or []:
            tools[call.get("tool")] += 1
            verdicts[call.get("verdict")] += 1
        for event in row.get("trace") or []:
            kinds[event.get("kind")] += 1
    # Only tools that are DECLARED: a hallucinated name is a model error, not a
    # tool, and 40 one-off strings would bury the ten that matter.
    known = [(n, c) for n, c in tools.most_common() if c > 10 and n]
    print(f"\nTool calls ({sum(tools.values()):,} total, "
          f"{sum(tools.values()) - sum(c for _, c in known):,} in names called 10 times or fewer):")
    for name, n in known:
        print(f"  {name:16}{n:>8,}")
    print("\nGate verdicts: " + ", ".join(f"{v} {n:,}" for v, n in verdicts.most_common()))
    if kinds:
        print("Trace events: " + ", ".join(f"{k} {n:,}" for k, n in kinds.most_common(8)))


def cost(found: list[dict]) -> None:
    ok = [r for r in found if r.get("status") == "ok" and r.get("tokens")]
    if not ok:
        return
    spend = statistics.median(r["tokens"] for r in ok)
    schema = [r["schema_tokens_est"] for r in ok if r.get("schema_tokens_est")]
    chars = {r["schema_chars"] for r in ok if r.get("schema_chars")}
    print(f"\nCost, n={len(ok)}: median run {spend:,.0f} tokens")
    if schema:
        share = statistics.median(schema)
        print(f"  schema {share:,.0f} of that, {_pct(int(share), int(spend))}, "
              f"re-sent every turn and cached nowhere")
        print(f"  schema_chars seen: {', '.join(f'{c:,}' for c in sorted(chars))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", default="", help="only this split")
    parser.add_argument("--since", default="", help="runs on or after YYYYMMDD")
    parser.add_argument("--cases", default="", metavar="SPLIT",
                        help="per-case breakdown for one split")
    args = parser.parse_args(argv)

    found = rows(since=args.since, only=args.split)
    if not found:
        print("no recorded runs match", file=sys.stderr)
        return 1
    scope = f"{len(found):,} rows"
    if args.since:
        scope += f" from {args.since}"
    if args.split:
        scope += f", split {args.split}"
    print(f"{scope}, {len({r['run'] for r in found})} run directories\n")
    per_split(found)
    if args.cases:
        per_case(found, args.cases)
    buckets(found)
    census(found)
    cost(found)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
