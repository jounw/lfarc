#!/usr/bin/env python3
"""lfarc snapshot: run the pipeline at scale and save data/latest.json and data/history/YYYY-MM-DD.json.

Web pages and other consumers read only these files instead of calling the API. Run periodically by cron (GitHub Actions).
  python3 snapshot.py                     # default: pool 1000, re-score 200, save top 200
  python3 snapshot.py --pool 50 --out /tmp/snap   # quick test
"""
import json
import os
from datetime import datetime, timezone

import lfarc

START, END = "<!-- LFARC:START -->", "<!-- LFARC:END -->"


def render_table(doc, top=30):
    """Build a markdown table of the top N repos from a snapshot."""
    m = doc["meta"]
    days = m["params"]["days"]
    lines = [f"_As of {m['generated_at'][:10]} · score v{m['score_version']} · based on commits in the last {days} days · "
             f"top {min(top, len(doc['repos']))} after filtering a candidate pool of {m['params']['pool']}_", "",
             "| # | Repo | Score | ★ | Effective contributors | Top contributor share | Outsider PR acceptance | Language |",
             "|--:|---|--:|--:|--:|--:|--:|---|"]
    for i, r in enumerate(doc["repos"][:top], 1):
        acc = f"{round(r['accept'] * 100)}%" if r.get("accept") is not None else "?"
        lines.append(f"| {i} | [{r['repo']}]({r['url']}) | {r['score']} | {r['stars']:,} | {r['effective_contributors']} "
                     f"| {round(r['top_share'] * 100)}% | {acc} | {r['language'] or '-'} |")
    lines += ["", "Effective contributors is based on the commit distribution (one person doing everything is ~1). "
              "See the Scoring section below for the score and filters. These are heuristics and can be wrong; treat as a guide."]
    return "\n".join(lines)


def update_readme(path, table):
    """Replace the text between the README START/END markers with the table. Returns False and leaves the file untouched if the markers are missing."""
    text = open(path, encoding="utf-8").read()
    if START not in text or END not in text:
        return False
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    open(path, "w", encoding="utf-8").write(f"{head}{START}\n{table}\n{END}{tail}")
    return True


def main():
    p = lfarc.make_parser()
    p.set_defaults(pool=1000, enrich=200, top=200)
    p.add_argument("--out", default="data", help="output folder (default: data)")
    p.add_argument("--readme", default="README.md", help="README to put the ranking table in (skipped if markers are missing; empty string disables)")
    p.add_argument("--readme-top", type=int, default=30, help="number of repos in the README table")
    args = p.parse_args()

    rows = lfarc.run(args)
    now = datetime.now(timezone.utc)
    params = {k: v for k, v in vars(args).items() if k not in ("out", "json", "readme", "readme_top")}
    doc = {"meta": {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "score_version": lfarc.SCORE_VERSION,
                    "count": len(rows), "params": params},
           "repos": rows}

    os.makedirs(os.path.join(args.out, "history"), exist_ok=True)
    for path in (os.path.join(args.out, "latest.json"), os.path.join(args.out, "history", f"{now:%Y-%m-%d}.json")):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        print(f"Saved: {path} ({len(rows)} repos)")
    if args.readme:
        ok = update_readme(args.readme, render_table(doc, args.readme_top))
        print(f"README updated: {args.readme}" if ok else f"No markers in README, skipped: {args.readme}")


if __name__ == "__main__":
    main()
