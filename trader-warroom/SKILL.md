---
name: trader-warroom
description: >-
  War Room. The whole day trade in one command, and one page to see it. Runs
  the pack's own tools in order for a ticker (entry timer, exit planner,
  10,000-replay time machine, budget, risk check, news), or scans for
  opening-range breakouts and replays every one of them 10,000 times, sizes
  and risk-checks each, and ranks them. Draws everything on one self-contained
  dashboard: the session's candles, the opening range, stop and target, the
  time machine's cone of futures, the odds, every past time the setup fired,
  the bracket ticket and the risk verdict. On a replay of a past session it
  also shows what the rest of the day really did. Use when the user says
  "war room", "run the whole thing", "do everything for TSLA", "trade TSLA",
  "full pipeline", "what's breaking out and is any of it worth it", "rank the
  breakouts", "replay this morning", "backtest today's scan", "show me the
  dashboard", or "/trader" with a ticker.
argument-hint: "[TICKER | scan] [--now \"YYYY-MM-DD HH:MM\"] [--at-trigger]"
---

# trader-warroom

The War Room runs the other ten steps of a day trade for you, in order, and
puts the answer on one page. It does no math of its own: every number comes
from trader-entry, trader-exit, trader-timemachine, trader-budget,
trader-risk and trader-news, run exactly as their own skills run them. What it
adds is speed (a whole scan, every breakout replayed, in under a minute), one
picture of all of it, and the one honest check none of them can do alone:
on a replay, what the rest of the day really did. Most people who day trade
lose money. A good-looking dashboard does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
It needs the other trader-* skill folders next to it (install the whole pack).

```bash
python3 "${CLAUDE_SKILL_DIR}/warroom.py" run TSLA                          # live, now
python3 "${CLAUDE_SKILL_DIR}/warroom.py" run TSLA --now "2026-10-22 10:15" # replay a past session
python3 "${CLAUDE_SKILL_DIR}/warroom.py" run TSLA --now "2026-10-22 10:15" --at-trigger
python3 "${CLAUDE_SKILL_DIR}/warroom.py" run TSLA --stop or-low --target 1R
python3 "${CLAUDE_SKILL_DIR}/warroom.py" scan                              # ~110 liquid names, the 8 latest breakouts
python3 "${CLAUDE_SKILL_DIR}/warroom.py" scan --now "2026-10-22 10:15" --at-trigger --top 10
python3 "${CLAUDE_SKILL_DIR}/warroom.py" scan --symbols TSLA,NVDA,AMD      # or --watchlist FILE, or --universe
python3 "${CLAUDE_SKILL_DIR}/warroom.py" pick NVDA                         # promote one ticker into trader/
python3 "${CLAUDE_SKILL_DIR}/warroom.py" open                              # open the last dashboard
```

Other flags: `--replays N` (10,000), `--seed N` (19), `--or-minutes N` (15),
`--top N` (8; 0 replays every breakout), `--min-price`, `--max-confirm`,
`--no-news`, `--no-promote`, `--offline PATH` (saved 60-day charts), `--open`
(open the page when done), `--json`.

## What it does, per ticker

Each ticker runs in its own sandbox, `trader/warroom/<run>/<TICKER>/trader/`,
seeded with the user's `trader/config.json` (or the example config, said
plainly), `portfolio.json` and today's broker log, so the risk check counts
what was already placed today. Sixty days of 5-minute bars are fetched once
and handed to every tool as `--offline chart.json`, so all of them see the
same data at the same `--now`.

| step | tool | what the war room reads |
| --- | --- | --- |
| 1 | `entry.py check --write` | waiting, triggered, expired: `entry.json` |
| 2 | `exit.py plan --write` | stop (or-mid by default), target (2R), flat by: `plan.json` |
| 3 | `timemachine.py run --until <flat by> --write` | 10,000 replays and the history: `timemachine.json` |
| 4 | the price at `--now` | written to `prices.json`, so a move since the trigger raises the risk |
| 5 | `size.py --write` | one bracket order, or the refusal line: `orders.json` |
| 6 | `check.py` | PASS or BLOCK: `state/risk.json` |
| 7 | `news.py --json` | headlines from the 24 hours before `--now` (never after it) |

Step 4 matters. A breakout that fired at 9:50 and has run since is a worse
trade at 10:15: the same stop is further away, so fewer shares fit, and the
target is closer, so reward:risk falls. trader-budget and trader-risk already
measure from the higher of the plan's entry and a fresh price; the war room
just hands them the fresh price. A plan whose stop or target was already hit
before `--now` is marked PLAYED OUT: it is history, not a trade.

## Statuses

| status | meaning |
| --- | --- |
| PASS | triggered, planned, replayed, sized, and trader-risk passed the order |
| BLOCK | trader-risk blocked the order, with the reason |
| REFUSED | trader-exit or trader-budget refused, with the reason (often: the move already happened) |
| PLAYED OUT | the stop or target was hit before `--now` |
| WAITING / EXPIRED / NO DATA | the entry never triggered, the window closed, or no bars |

## Replays and what really happened

`--now` in the past is a replay. Everything is judged with only the bars
closed by then; the bars after it are drawn faded, and the orange line shows
what the price really did with the plan: stop first, target first, or where
it sat at the flat-by time, and where that close fell in the time machine's
cone. `--at-trigger` judges each breakout at the moment it fired instead of
at `--now`: a fair replay of what you could have taken. A scan replay ends
with one line on how the day's plans did, and says that one day is one day.

A replay is research. It is never promoted and never becomes an order.

## Scan

`scan` runs trader-scanner's opening-range scan (`scan.py orb`) on the liquid
list (about 110 names), your `--symbols` or `--watchlist`, or `--universe`
(the whole market from `trader/universe.txt`: minutes, not seconds), then
replays the `--top` latest breakouts (closest to the price now), four at a
time. The board ranks them by the replays' average R. That is a ranking of
simulations, never a list of trades to take. Live, the scan's watchlist is
also copied to `trader/watchlist.json` for trader-entry.

## Live: from the board to the broker

A live `run` whose order passed trader-risk (and is not played out) is
promoted: `entry.json`, `plan.json`, `timemachine.json`, `orders.json` and the
fresh price are copied into `trader/`, and `check.py` runs again there against
the real broker log. For a scan, `warroom.py pick TICKER` does the same for
one row of today's live board. Then `/trader-broker` shows the ticket and asks
the user yes or no. The war room itself never calls a broker tool.

## The dashboard

`trader/warroom/<run>/warroom.html`, one self-contained page (no network,
light and dark): the leaderboard with each breakout's odds bar and replay
cone; then, per ticker, the session's candles with the opening range, the
entry, stop and target, the cone of 10,000 futures with 60 sample paths, the
odds, where the replays ended, every past session the rule fired (and today),
the bracket ticket, the risk lines, the headlines, and the raw output of
every tool it ran. Hover any chart for the numbers. Open it with
`warroom.py open` or `--open`.

## Steps

1. If `trader/config.json` does not exist, say the run will use the example
   limits ($5,000, $50 max loss a trade) and offer `/trader-budget` to set
   the user's own. Never treat example sizes as the user's.
2. Run what they asked: `run TICKER`, or `scan`. Before 9:50 AM ET, after
   3:30 PM ET, or on a closed day there is no live setup: offer a replay of
   the latest session at 10:15 with `--at-trigger`.
3. Tell the user, from the printed text: the status line per ticker, the
   time machine's odds and verdict line exactly as printed, the size and the
   risk verdict, and on a replay what really happened. Say the seed.
4. Open the dashboard (`warroom.py open`) and say where it is.
5. If one passed live and they want it: it is already promoted (`run`) or
   `pick TICKER` (scan). Hand off to `/trader-broker`. Offer `/trader-alerts
   add --from-plan` for the stop, the target and the flat-by time.

## Rules

- Every number comes from the pack's tools. Repeat their verdict lines, do
  not rewrite them. Never round a coin flip up into an edge.
- Say "matched a rule" and "ranked by the replays", never "a buy", "a
  winner" or "the best trade".
- Never print a P&L, a return or a profit figure. R and the max loss at the
  stop are plain plan numbers.
- A replay is never an order. A board from another day is never promoted.
- Simulated odds are not predictions. Say it once, plainly.

## What it never does

It never places an order, never calls a broker tool, never changes a limit,
never edits an order to make it pass, and never promotes a replay or a played
out plan.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
