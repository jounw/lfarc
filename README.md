# lfarc — Looking For A Repo to Contribute

Not another good-first-issue list. lfarc scores GitHub repos that **many people use but few people maintain**, so you can find projects where your contribution actually matters.

## This week's ranking
<!-- LFARC:START -->
_No snapshot yet. This section is filled in automatically after running `python3 snapshot.py`._
<!-- LFARC:END -->

## Usage

```bash
python3 lfarc.py -l python --pool 200 -n 20
python3 lfarc.py -t cli --min-stars 500 --max-stars 10000 --json
```

The default star range is 2,500 and up with no upper cap (effectively 1,000,000); change it with `--min-stars` / `--max-stars`.

Token: the `GITHUB_TOKEN` environment variable or `gh auth login` (without a token the rate limit is very low).

## Scoring (0-100): burden per effective contributor x outsider acceptance

Effective contributors (EFF) = `1 / sum(commit share^2)` over the last N days (default 90), bots excluded. If one person does 90% of the commits and everyone else only fixes typos, EFF is about 1.2, so the project is treated as essentially a one-person project.

1. **Base score**: `100 * scarcity * (0.4 + 0.4 * popularity + 0.2 * demand)`
   - scarcity = `1 / (1 + (EFF - 1) / 2.5)` (EFF 1 -> 1.0, 2 -> 0.71, 4 -> 0.45, 6 -> 0.33, 10 -> 0.22). It is **multiplicative**, so a repo with many contributors scores low no matter how many stars it has.
   - popularity = `sat(log(stars): 500 -> 0, 50,000 -> 1)`, demand = `sat(log(issues / EFF): 1 -> 0, 100 -> 1)`, where `sat(x) = 1 - e^(-x)` saturates smoothly so scores never tie at a cap.
2. **Re-scoring** (only the top `--enrich` repos by base score, 3 API calls per repo):
   - Outsider PR acceptance (ACCEPT): among the 50 most recently closed PRs, the merge ratio of outsider PRs (author is not OWNER/MEMBER/COLLABORATOR, bots excluded). The factor is `0.25 + 0.75 * acceptance`. With fewer than 3 outsider PRs there is not enough data (`?`), and acceptance is taken as 0.4.
     - In the backtest (`tune.py`, 94 samples) past acceptance was the best predictor of future acceptance (AUC 0.72), and the top 25% of a scarcity-only ranking was accepted only 30% of the time versus 52% for a random repo, so the factor is kept strong.
   - Entry-level issues (HW/GFI): the number of open `help wanted` + `good first issue` issues. 5 or more gives a +10% bonus.

- Repos with zero commits (dead repos) score 0.
- `--min-commits` (default 5): repos with fewer commits in the last N days are excluded as effectively dead.
- `--max-contributors` (default 10): repos whose EFF exceeds this are excluded as red oceans.
- Docs / curated-list / tutorial repos are excluded heuristically (no language, topics, names or descriptions such as awesome/tutorial/roadmap/interview). Use `--include-docs` to keep them.
- Corporate and closed repos are excluded (use `--include-closed` to keep them):
  - Owners on the known big-company / government list (`CORP_ORGS` in `lfarc.py`: microsoft, google, meta, aws, nvidia, openai, anthropics, android, NSA, ...). Add or edit entries in `lfarc.py`.
  - Owners that are GitHub **verified organizations** (domain-verified, via `is_verified` from `/orgs/{login}`).
  - Repos where 50%+ of recent commit authors share one company email domain (personal mail and universities excluded) with 2+ authors, i.e. company-driven projects.
  - Repos where outsiders make up under 10% of recently closed PRs, or outsider PR acceptance is under 20%.
- The `TOP1%` column in the output is the top contributor's commit share (a bus-factor signal).
- Commits are fetched up to `--pages` pages (default 3 -> 300 commits). If that limit is hit, values are shown as lower bounds such as `300+`.

## Snapshots (for the web and other consumers)

```bash
python3 snapshot.py                # pool 1000, re-score 200, save top 200 -> data/latest.json, data/history/YYYY-MM-DD.json
python3 snapshot.py --pool 50 --out /tmp/snap   # quick test
```

- File format: `{"meta": {generated_at, score_version, count, params}, "repos": [...]}` (each repo entry matches the `--json` output).
- Running `snapshot.py` also rewrites the ranking table between the `LFARC:START` / `LFARC:END` markers at the top of this README (`--readme-top` sets the length, `--readme ""` disables it).
- `.github/workflows/snapshot.yml` runs it every Monday and commits `data/` and `README.md`. The default `GITHUB_TOKEN` is limited to 1,000 requests/hour, so registering a public-read-only PAT as the `LFARC_TOKEN` secret (5,000/hour) is recommended.
- Bump `SCORE_VERSION` in `lfarc.py` whenever the scoring formula or filters change, so old snapshots can be told apart.
- When the API rate limit is hit, requests wait until the reset time and retry (for unattended runs).

## Limitations
- Candidates come from GitHub search: the star range is split into `--buckets` (default 8) log-scale buckets, and each bucket takes the top-starred repos first (`pool / buckets` each, with unused shares redistributed). To see deeper, raise `--pool` or narrow the range.
- Issue counts include questions, spam, and, in the GitHub API, open PRs. Treat the score as a guide.
- The filters are heuristics and can both miss and wrongly exclude repos.
