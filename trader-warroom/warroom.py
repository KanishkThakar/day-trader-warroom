#!/usr/bin/env python3
"""trader-warroom: the whole day trade in one command, and one page to see it.

    python3 warroom.py run TSLA                            # live: entry, exit, time machine, budget, risk, news
    python3 warroom.py run TSLA --now "2026-10-22 10:15"   # replay a past session, then see what really happened
    python3 warroom.py run TSLA --stop or-low --target 1R  # any stop and target trader-exit knows
    python3 warroom.py scan                                # opening-range breakouts across ~110 liquid names, ranked
    python3 warroom.py scan --now "2026-10-22 10:15" --top 10
    python3 warroom.py scan --symbols TSLA,NVDA,AMD,PLTR   # or --watchlist FILE, or --universe (the whole market)
    python3 warroom.py pick NVDA                           # promote one ticker from the last board into trader/
    python3 warroom.py open                                # open the last dashboard in the browser

For each ticker, in its own sandbox (trader/warroom/<run>/<TICKER>/), it runs
the pack's own tools in order and reads what they wrote:

  1. 60 days of 5-minute bars, fetched once (Yahoo v8 chart, free, unofficial)
  2. trader-entry       entry.py check          waiting, triggered or expired
  3. trader-exit        exit.py plan            entry, stop and target, set before the entry
  4. trader-timemachine timemachine.py run      10,000 replays to the flat-by time, plus the history
  5. the price now      prices.json             a move since the trigger raises the risk
  6. trader-budget      size.py                 one bracket order, or a refusal
  7. trader-risk        check.py                PASS or BLOCK
  8. trader-news        news.py                 the last 24 hours of headlines (skipped offline)

Every number comes from those tools. The war room adds one thing none of them
can: on a replay (--now in the past) the bars after --now are known, so it
shows what the rest of the day really did beside the time machine's cone.

The board is trader/warroom/<run>/warroom.json, drawn as warroom.html next to
it (one self-contained page, no network). A live `run` whose order passed
trader-risk, and has not already hit its stop or target, is promoted into
trader/ (entry, plan, timemachine, orders, prices) and checked again there, so
/trader-broker can show the ticket. A replay is never promoted. Nothing here
places an order.

--offline PATH reads saved 60-day Yahoo charts instead of the network: one
.json/.json.gz file for `run`, or a folder of <TICKER>.json[.gz] files.
Exit code 0 when the board was drawn, 1 when it could not run (one plain line).
"""

import argparse
import concurrent.futures
import datetime
import gzip
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.dirname(HERE)
TEMPLATE = os.path.join(HERE, "dashboard.html")
TOOLS = {
    "scan": ("trader-scanner", "scan.py"),
    "entry": ("trader-entry", "entry.py"),
    "exit": ("trader-exit", "exit.py"),
    "timemachine": ("trader-timemachine", "timemachine.py"),
    "budget": ("trader-budget", "size.py"),
    "risk": ("trader-risk", "check.py"),
    "news": ("trader-news", "news.py"),
}
EXAMPLE_CONFIG = os.path.join(SKILLS_DIR, "trader-budget", "config.example.json")
EXAMPLE_UA = "you@example.com"

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.1; +https://github.com/KanishkThakar/day-trader-warroom)"
HOSTS = ("query2", "query1")
BACKOFF = 5.0       # seconds before the one retry after an HTTP 429
PAUSE = 0.3         # seconds between chart requests, per worker
WORKERS = 4         # tickers replayed at once in a scan
STEP_TIMEOUT = 300  # seconds one tool may take
SAMPLE_KEEP = 60    # sample paths kept per card for the dashboard
LIVE_SLACK = 900    # a Yahoo tick this recent (seconds) is "the price now"
EPOCH = datetime.datetime(1970, 1, 1)
OPEN_MIN, CLOSE_MIN, EARLY_MIN = 570, 960, 780
NYSE_EARLY_CLOSE = ("2026-11-27", "2026-12-24", "2027-11-26")
BOILERPLATE = ("Education and tooling", "Nothing was placed", "Next:")

# Liquid US names a day trader watches: the default scan when no list is given.
LIQUID = (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX ORCL CRM ADBE INTC MU QCOM TXN AMAT LRCX KLAC "
    "MRVL ARM SMCI PLTR SNOW CRWD PANW NET DDOG SHOP UBER ABNB COIN HOOD SOFI PYPL RIVN LCID NIO F "
    "GM BA CAT DE GE LMT RTX JPM BAC WFC C GS MS SCHW V MA AXP UNH LLY JNJ PFE MRK ABBV AMGN GILD "
    "MRNA CVS WMT COST TGT HD LOW NKE SBUX MCD KO PEP PG DIS T VZ XOM CVX OXY DAL UAL AAL CCL RCL "
    "MARA RIOT MSTR AFRM DKNG RBLX SNAP PINS SPOT TTD ROKU IBM CSCO DELL ANET VRT TSM BABA PDD MELI "
    "NU GME AMC PLUG"
).split()

STATUS_TEXT = {
    "ready": "PASS",
    "blocked": "BLOCK",
    "refused": "REFUSED",
    "played out": "PLAYED OUT",
    "waiting": "WAITING",
    "expired": "EXPIRED",
    "no data": "NO DATA",
    "error": "ERROR",
}


class WarError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def parse_now(text):
    """--now -> naive UTC. "2026-10-22 10:15" (or just 10:15 for today) is New York time;
    a Z or an offset makes it absolute."""
    t = text.strip()
    if t.upper().endswith(" ET"):
        t = t[:-3].strip()
    if len(t) <= 5 and ":" in t:
        t = "%s %s" % (to_eastern(utc_now()).date().isoformat(), t.zfill(5))
    try:
        dt = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        raise WarError('--now wants a New York time like "2026-10-22 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def clock12(minutes):
    h, m = divmod(int(minutes), 60)
    return "%d:%02d %s" % ((h - 1) % 12 + 1, m, "AM" if h < 12 else "PM")


def clock24(minutes):
    return "%02d:%02d" % divmod(int(minutes), 60)


def minutes_of(hhmm):
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def ny_label(utc):
    ny = to_eastern(utc)
    return "%s %s ET" % (ny.strftime("%a %Y-%m-%d"), clock12(ny.hour * 60 + ny.minute))


# ---------------------------------------------------------------- data

def yahoo_symbol(sym):
    return sym.upper().replace(".", "-")


def fetch_chart(sym):
    """60 days of 5-minute bars: query2 first, query1 on a 429 or 5xx, one retry on a 429."""
    path = "/v8/finance/chart/%s?range=60d&interval=5m&includePrePost=false" % yahoo_symbol(sym)
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                code = e.code
                e.close()
                last = "HTTP %d" % code
                if code == 404:
                    raise WarError("Yahoo has no chart for %s (HTTP 404): check the ticker" % sym)
                if code == 429 and attempt == 0:
                    time.sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise WarError("Yahoo answered HTTP %d for %s" % (code, sym))
            except (urllib.error.URLError, OSError) as e:
                last = "could not connect: %s" % (getattr(e, "reason", None) or e,)
                break
    raise WarError("Yahoo did not answer for %s (%s): wait a minute and try again" % (sym, last))


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_chart(path, sym):
    if os.path.isdir(path):
        for name in (sym, yahoo_symbol(sym)):
            for ext in (".json", ".json.gz"):
                p = os.path.join(path, name + ext)
                if os.path.exists(p):
                    return read_text(p)
        raise WarError("no offline chart for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise WarError("no offline chart file %s" % path)
    return read_text(path)


def parse_chart(text, sym):
    """Yahoo chart JSON -> (meta, [(ts, o, h, l, c, v)]). Null closes are dropped."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise WarError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = ((data.get("chart") or {}).get("error") or {}).get("description")
        raise WarError("Yahoo has no data for %s: %s" % (sym, err or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != yahoo_symbol(sym):
        raise WarError("the chart holds %s, not %s" % (msym, sym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append((int(t), float(c if o is None else o), float(c if h is None else h),
                     float(c if lo is None else lo), float(c), int(at("volume", i) or 0)))
    return meta, bars


def session_end(day):
    return EARLY_MIN if day in NYSE_EARLY_CLOSE else CLOSE_MIN


def day_bars(bars, day, now_utc, data_utc):
    """The day's complete regular-session 5-minute bars, each marked known (closed by --now)."""
    end = session_end(day)
    rows = {}
    for t, o, h, lo, c, v in bars:
        u = utc_of(t)
        ny = to_eastern(u)
        if ny.date().isoformat() != day:
            continue
        m = ny.hour * 60 + ny.minute
        if ny.second or (m - OPEN_MIN) % 5 or not OPEN_MIN <= m < end:
            continue
        close_utc = u + datetime.timedelta(minutes=5)
        if data_utc and close_utc > data_utc + datetime.timedelta(seconds=60):
            continue  # still forming when the data was read
        rows[(m - OPEN_MIN) // 5] = {
            "slot": (m - OPEN_MIN) // 5, "start": clock24(m), "o": round(o, 4), "h": round(h, 4),
            "l": round(lo, 4), "c": round(c, 4), "v": v, "close_utc": iso(close_utc),
            "known": close_utc <= now_utc,
        }
    return [rows[k] for k in sorted(rows)]


def prev_close(bars, day):
    """The last regular-session close before the day, from the same 60 days of bars."""
    best = None
    for t, o, h, lo, c, v in bars:
        ny = to_eastern(utc_of(t))
        d = ny.date().isoformat()
        m = ny.hour * 60 + ny.minute
        if d < day and OPEN_MIN <= m < session_end(d):
            if best is None or t > best[0]:
                best = (t, c)
    return round(best[1], 4) if best else None


# ---------------------------------------------------------------- the pipeline

def tool_path(name):
    folder, script = TOOLS[name]
    path = os.path.join(SKILLS_DIR, folder, script)
    if not os.path.exists(path):
        raise WarError("the war room runs the other trader-* skills and cannot find %s/%s next to it: "
                       "install the whole pack" % (folder, script))
    return path


def run_step(doc, name, args, cwd, label=None):
    """Run one pack tool in cwd, record its command and output in doc["steps"]."""
    args = [str(a) for a in args]
    cmd = [sys.executable, tool_path(name)] + args
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=STEP_TIMEOUT)
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, err, code = "", "timed out after %d seconds" % STEP_TIMEOUT, 124
    shown = " ".join(["python3", "%s/%s" % TOOLS[name]] + [shlex.quote(a) for a in args])
    text = (out.rstrip() + ("\n" + err.rstrip() if err.strip() else "")).strip()
    doc["steps"].append({"step": label or name, "cmd": shown, "code": code, "out": text[-8000:],
                         "seconds": round(time.time() - t0, 2)})
    return code, out, err


def say(out, err=""):
    """The one line that matters from a tool's output: its refusal or its headline."""
    lines = [x.strip() for x in (out + "\n" + err).splitlines() if x.strip()]
    for x in lines:
        low = x.lower()
        if any(w in low for w in ("refus", "no order", "cannot", "can't", "too far", "not below", "not above",
                                  "under your", "over your", "blocked")):
            return strip_prog(x)
    keep = [x for x in lines if not x.startswith(BOILERPLATE)]
    return strip_prog(keep[-1] if keep else (lines[-1] if lines else "no output"))


def strip_prog(line):
    for _, script in TOOLS.values():
        if line.startswith(script + ": "):
            return line[len(script) + 2:]
    return line


def read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(main_dir):
    """(config dict, where it came from)."""
    path = os.path.join(main_dir, "config.json")
    cfg = read_json(path)
    if isinstance(cfg, dict):
        return cfg, "trader/config.json"
    cfg = read_json(EXAMPLE_CONFIG) or {}
    return cfg, "the example config (trader-budget/config.example.json): set yours with /trader-budget"


def seed_box(tdir, main_dir, cfg):
    """A sandbox trader/ folder with the user's limits and today's broker log, so the
    risk check counts what was already placed today."""
    os.makedirs(os.path.join(tdir, "state"), exist_ok=True)
    write_json(os.path.join(tdir, "config.json"), cfg)
    for rel in ("portfolio.json", os.path.join("state", "broker.json")):
        src = os.path.join(main_dir, rel)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(tdir, rel))


def safe_reset(path, root):
    """Clear a sandbox from an earlier run with the same name, only inside the war room's folder."""
    path, root = os.path.abspath(path), os.path.abspath(root)
    if os.path.isdir(path) and path.startswith(root + os.sep):
        shutil.rmtree(path)


def price_now(bars, meta, now_utc, data_utc):
    """The price at --now: Yahoo's last trade when the data is that fresh, else the last closed bar."""
    px = meta.get("regularMarketPrice")
    if data_utc and px and 0 <= (now_utc - data_utc).total_seconds() <= LIVE_SLACK:
        return {"price": round(float(px), 2), "time": iso(data_utc), "from": "Yahoo's last trade"}
    known = [b for b in bars if b["known"]]
    if not known:
        return None
    b = known[-1]
    return {"price": round(b["c"], 2), "time": b["close_utc"], "from": "the %s bar's close" % b["start"]}


def write_prices(path, sym, px, now_utc):
    doc = read_json(path) or {}
    prices = dict(doc.get("prices") or {})
    detail = dict(doc.get("detail") or {})
    prices[sym] = px["price"]
    detail[sym] = {"time": px["time"], "fetched": iso(now_utc), "source": "yahoo", "from": px["from"]}
    write_json(path, {"as_of": iso(now_utc), "source": "yahoo",
                      "note": ("Delayed quotes for sizing checks, written by trader-warroom. Your broker's "
                               "price is the one that fills."),
                      "prices": prices, "detail": detail})


def reality(bars, plan, cut_utc=None):
    """What the bars did with the plan after the entry bar: stop first (a bar that touches
    both counts as the stop, as the time machine does), target first, or the price at the
    flat-by time. cut_utc: only bars closed by then. None when the data stops too early."""
    k = int(plan["entry_bar"])
    e, s, t = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    risk = e - s
    last = (minutes_of(plan.get("flat_by") or "15:55") - OPEN_MIN) // 5 - 1
    by = {b["slot"]: b for b in bars if cut_utc is None or parse_iso(b["close_utc"]) <= cut_utc}
    if not by or max(by) <= k:
        return {"outcome": "open", "R": 0.0, "price": e, "slot": k, "at": clock12(OPEN_MIN + 5 * (k + 1))}
    seen = None
    for i in range(k + 1, last + 1):
        b = by.get(i)
        if b is None:
            if any(j > i for j in by):
                continue  # a missing bar (a halt or a gap in the data)
            break
        seen = b
        at = clock12(OPEN_MIN + 5 * (i + 1))
        if b["l"] <= s:
            return {"outcome": "stop", "R": -1.0, "price": s, "slot": i, "at": at}
        if b["h"] >= t:
            return {"outcome": "target", "R": round((t - e) / risk, 2), "price": t, "slot": i, "at": at}
    if seen is None:
        return {"outcome": "open", "R": 0.0, "price": e, "slot": k, "at": clock12(OPEN_MIN + 5 * (k + 1))}
    done = seen["slot"] == last
    return {"outcome": "close" if done else "open", "R": round((seen["c"] - e) / risk, 2),
            "price": round(seen["c"], 2), "slot": seen["slot"], "at": clock12(OPEN_MIN + 5 * (seen["slot"] + 1))}


def cone_position(price, bands):
    """Where a price sits in the last column of the fan (5th, 25th, 50th, 75th, 95th percentiles)."""
    p5, p25, p50, p75, p95 = bands[-1]
    if price < p5:
        return "below the 5th percentile"
    if price > p95:
        return "above the 95th percentile"
    if price < p25:
        return "between the 5th and 25th percentiles"
    if price < p50:
        return "between the 25th and 50th percentiles"
    if price < p75:
        return "between the 50th and 75th percentiles"
    return "between the 75th and 95th percentiles"


def news_ok(cfg):
    ua = os.environ.get("SEC_USER_AGENT") or cfg.get("sec_user_agent") or ""
    return bool(ua) and EXAMPLE_UA not in ua


def run_ticker(sym, run_dir, main_dir, now_utc, o, cfg, hit=None, text=None, at_trigger=None):
    """One ticker through the whole pack, in its own sandbox. Returns its card.
    With --at-trigger (replays only) a breakout that fired before --now is judged again
    at the moment it fired, so the plan is the one you could have taken then."""
    at_trigger = o.at_trigger if at_trigger is None else at_trigger
    sym = sym.upper()
    box = os.path.join(run_dir, yahoo_symbol(sym))
    safe_reset(box, run_dir)
    tdir = os.path.join(box, "trader")
    seed_box(tdir, main_dir, cfg)
    nowz = iso(now_utc)
    day = to_eastern(now_utc).date().isoformat()
    card = {"symbol": sym, "date": day, "steps": [], "status": "error", "headline": None, "hit": hit,
            "sandbox": os.path.relpath(box), "now_utc": nowz, "now_ny": ny_label(now_utc)}
    t0 = time.time()
    try:
        text = text or (offline_chart(o.offline, sym) if o.offline else fetch_chart(sym))
        meta, raw = parse_chart(text, sym)
    except WarError as e:
        card.update(status="no data", headline=str(e))
        return card
    with open(os.path.join(box, "chart.json"), "w", encoding="utf-8") as fh:
        fh.write(text)
    data_utc = utc_of(meta["regularMarketTime"]) if meta.get("regularMarketTime") else None
    bars = day_bars(raw, day, now_utc, data_utc)
    card.update(bars=bars, prev_close=prev_close(raw, day), session_end=clock24(session_end(day)),
                name=meta.get("longName") or meta.get("shortName"),
                data_time=iso(data_utc) if data_utc else None, fetch_seconds=round(time.time() - t0, 2))

    code, out, err = run_step(card, "entry", ["check", sym, "--now", nowz, "--offline", "chart.json",
                                              "--or-minutes", o.or_minutes, "--write"], box)
    entry = read_json(os.path.join(tdir, "entry.json"))
    card["entry"] = entry
    if not entry:
        card.update(status="error", headline=say(out, err))
        return card
    if entry.get("status") != "triggered":
        st = {"no-data": "no data"}.get(entry.get("status"), entry.get("status") or "error")
        st = st if st in STATUS_TEXT else "waiting"
        card.update(status=st, headline="%s: %s" % (STATUS_TEXT[st], entry.get("reason") or say(out, err)))
        return card
    trig = trigger_utc(entry)
    card["entry_age_min"] = int((now_utc - trig).total_seconds() // 60) if trig else None
    if at_trigger and trig and trig < now_utc:
        again = run_ticker(sym, run_dir, main_dir, trig, o, cfg, hit, text=text, at_trigger=False)
        again["judged_at_trigger"] = True
        return again

    code, out, err = run_step(card, "exit", ["plan", "--stop", o.stop, "--target", o.target, "--now", nowz,
                                             "--offline", "chart.json", "--write"], box)
    plan = read_json(os.path.join(tdir, "plan.json"))
    if code or not plan:
        card.update(status="refused", stage="exit", headline=say(out, err))
        return card
    card["plan"] = plan

    until = plan.get("flat_by") or "15:55"
    code, out, err = run_step(card, "timemachine", ["run", "--until", until, "--now", nowz, "--offline",
                                                    "chart.json", "--replays", o.replays, "--seed", o.seed,
                                                    "--write"], box)
    tm = read_json(os.path.join(tdir, "timemachine.json"))
    if tm and not code:
        mc = tm.get("mc") or {}
        mc["sample_paths"] = (mc.get("sample_paths") or [])[:SAMPLE_KEEP]
        card["tm"] = tm
    else:
        card["tm_error"] = say(out, err)

    card["so_far"] = reality(bars, plan, now_utc)
    if any(not b["known"] for b in bars):
        actual = reality(bars, plan)
        last = [b for b in bars if b["slot"] == (minutes_of(until) - OPEN_MIN) // 5 - 1]
        if card.get("tm") and last:
            actual["close_at_until"] = round(last[0]["c"], 2)
            actual["close_cone"] = cone_position(last[0]["c"], card["tm"]["mc"]["bands"])
        card["actual"] = actual

    px = price_now(bars, meta, now_utc, data_utc)
    card["price_now"] = px
    if px:
        write_prices(os.path.join(tdir, "prices.json"), sym, px, now_utc)

    code, out, err = run_step(card, "budget", ["--now", nowz, "--write"], box)
    orders = read_json(os.path.join(tdir, "orders.json"))
    if code or not orders or not orders.get("orders"):
        card.update(status="refused", stage="budget", headline=say(out, err))
    else:
        card["orders"] = orders
        code, out, err = run_step(card, "risk", ["--now", nowz], box)
        risk = read_json(os.path.join(tdir, "state", "risk.json"))
        card["risk"] = risk
        if risk and risk.get("results"):
            r0 = risk["results"][0]
            ok = r0.get("verdict") == "PASS"
            card.update(status="ready" if ok else "blocked",
                        headline="; ".join(r0.get("info") if ok else r0.get("reasons") or ["see the risk check"]))
        else:
            card.update(status="error", headline=say(out, err))

    sf = card["so_far"]
    if sf["outcome"] in ("stop", "target"):
        card["status"] = "played out"
        card["headline"] = ("The %s was hit at %s, before now (%+.2fR). This plan is history, not a trade."
                            % (sf["outcome"], sf["at"], sf["R"]))

    if o.news and not o.offline and abs((utc_now() - now_utc).total_seconds()) < 2 * 86400:
        card["news"] = run_news(card, sym, box, now_utc, cfg)
    card["seconds"] = round(time.time() - t0, 2)
    return card


def trigger_utc(entry):
    """The trigger bar's close, naive UTC."""
    try:
        close_min = minutes_of(entry["bar_start"]) + 5
        day = datetime.date.fromisoformat(entry["date"])
    except (KeyError, TypeError, ValueError):
        return None
    return eastern_to_utc(datetime.datetime.combine(day, datetime.time(*divmod(close_min, 60))))


def run_news(card, sym, box, now_utc, cfg):
    args = ["--tickers", sym, "--hours", 24, "--no-moves", "--dry-run", "--json", "--now", iso(now_utc)]
    if not news_ok(cfg):
        args.append("--no-sec")
    code, out, err = run_step(card, "news", args, box)
    try:
        doc = json.loads(out)
    except ValueError:
        return {"error": say(out, err), "items": []}
    keep = []
    for it in doc.get("items") or []:
        t = parse_iso(it.get("time"))
        if t is None or t > now_utc or (now_utc - t).total_seconds() > 86400:
            continue  # a replay never sees a headline from after --now
        keep.append({k: it.get(k) for k in ("source", "kind", "title", "link", "time", "tags")})
    return {"items": keep[:12], "sec": news_ok(cfg)}


# ---------------------------------------------------------------- boards

def card_score(c):
    tm = c.get("tm")
    return tm["mc"]["avg_R"] if tm else None


def rank(cards):
    """Cards with replays first, by the replays' average R; then the rest by status."""
    order = ["ready", "blocked", "refused", "played out", "waiting", "expired", "no data", "error"]

    def key(c):
        s = card_score(c)
        return (0 if s is not None else 1, -(s or 0), order.index(c["status"]) if c["status"] in order else 99,
                c["symbol"])

    out = sorted(cards, key=key)
    for i, c in enumerate(out):
        c["rank"] = i + 1
    return out


def new_run(main_dir, now_utc, label):
    ny = to_eastern(now_utc)
    name = "%s-%s-%s" % (ny.date().isoformat(), ny.strftime("%H%M"), label)
    root = os.path.join(main_dir, "warroom")
    run_dir = os.path.join(root, name)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def board_head(mode, now_utc, replay, cfg, cfg_from, o):
    return {
        "created_by": "trader-warroom", "created_at": iso(utc_now()), "mode": mode, "replay": replay,
        "now_utc": iso(now_utc), "now_ny": ny_label(now_utc), "date": to_eastern(now_utc).date().isoformat(),
        "paper": cfg.get("paper", True), "config_from": cfg_from, "offline": bool(o.offline),
        "limits": {k: cfg.get(k) for k in ("budget", "max_loss_per_trade_usd", "max_daily_loss_usd",
                                            "max_trades_per_day", "min_reward_risk", "no_entries_before",
                                            "no_entries_after", "flat_by")},
        "rules": {"stop": o.stop, "target": o.target, "or_minutes": o.or_minutes, "replays": o.replays,
                  "seed": o.seed},
        "notes": [],
    }


def render_html(board, path):
    with open(TEMPLATE, encoding="utf-8") as fh:
        page = fh.read()
    data = json.dumps(board, separators=(",", ":"), ensure_ascii=True).replace("</", "<\\/")
    page = page.replace("/*__BOARD__*/null", data)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)


def finish(board, run_dir, main_dir, o):
    path_json = os.path.join(run_dir, "warroom.json")
    path_html = os.path.join(run_dir, "warroom.html")
    board["html"] = os.path.relpath(path_html)
    write_json(path_json, board)
    render_html(board, path_html)
    write_json(os.path.join(main_dir, "warroom", "latest.json"),
               {"run": os.path.abspath(run_dir), "html": os.path.abspath(path_html), "mode": board["mode"],
                "now_ny": board["now_ny"], "replay": board["replay"]})
    if o.open:
        webbrowser.open("file://" + os.path.abspath(path_html))
    return path_html


def clock(o):
    """(now_utc, replay): --now pins the clock; a time more than 5 minutes ago is a replay."""
    if o.now:
        now = parse_now(o.now)
        return now, (utc_now() - now).total_seconds() > 300 or bool(o.offline)
    return utc_now(), bool(o.offline)


# ---------------------------------------------------------------- text out

def money(x):
    return "$%s" % format(x, ",.2f")


def pct(x):
    return "%.1f %%" % (100.0 * x)


def card_lines(c, replay):
    sym = c["symbol"]
    lines = []
    e = c.get("entry") or {}
    orng = e.get("opening_range") or {}
    if e.get("status") == "triggered":
        age = c.get("entry_age_min")
        lines.append("  entry    TRIGGERED %s at %s, range %s to %s%s" % (
            e.get("time"), money(e["price"]), orng.get("low"), orng.get("high"),
            (", %d min before now" % age) if age else ""))
    else:
        lines.append("  entry    %s" % (c.get("headline") or c["status"]))
        return lines
    p = c.get("plan")
    if p:
        lines.append("  plan     stop %s (%s), target %s (%gR), risk %s a share, flat by %s" % (
            p["stop"], p.get("stop_label") or p.get("stop_rule"), p["target"], p["target_r"], p["risk_per_share"],
            p.get("flat_by")))
    tm = c.get("tm")
    if tm:
        m, h = tm["mc"], tm["history"]
        lines.append("  replays  target first %s, stop first %s, neither %s; avg R %+.3f (%s, seed %s)" % (
            pct(m["p_target"]), pct(m["p_stop"]), pct(m["p_neither"]), m["avg_R"], format(m["replays"], ","),
            m["seed"]))
        if h.get("triggered"):
            lines.append("  history  fired %d of %d sessions: %d target, %d stop, %d neither; avg R %+.3f" % (
                h["triggered"], h["sessions"], h["target"], h["stop"], h["close"], h["avg_R"]))
        lines.append("  verdict  %s" % tm["verdict"])
        for n in tm.get("verdict_notes") or []:
            lines.append("           %s" % n)
    elif c.get("tm_error"):
        lines.append("  replays  %s" % c["tm_error"])
    px = c.get("price_now")
    if px:
        lines.append("  now      %s (%s)" % (money(px["price"]), px["from"]))
    od = ((c.get("orders") or {}).get("orders") or [None])[0]
    if od:
        pl = od.get("plan") or {}
        lines.append("  size     buy %s %s, bracket: stop %s, target %s; max loss %s at the stop" % (
            od["qty"], sym, od.get("stop_loss_stop_price"), od.get("take_profit_limit_price"),
            money(pl.get("max_loss") or 0)))
    lines.append("  verdict  %s: %s" % (STATUS_TEXT.get(c["status"], c["status"].upper()), c.get("headline") or ""))
    a = c.get("actual")
    if a and replay:
        if a["outcome"] in ("stop", "target"):
            txt = "the %s was hit at %s (%+.2fR)" % (a["outcome"], a["at"], a["R"])
        elif a["outcome"] == "close":
            txt = "neither was hit; at %s the price was %s (%+.2fR)" % (a["at"], a["price"], a["R"])
        else:
            txt = "still open when the data stops, at %s (%+.2fR so far)" % (a["price"], a["R"])
        if a.get("close_cone"):
            txt += "; the %s close of %s sat %s of the replays" % (clock12(minutes_of(p.get("flat_by") or "15:55")),
                                                                  a["close_at_until"], a["close_cone"])
        lines.append("  really   %s" % txt)
    return lines


def print_run(board):
    c = board["cards"][0]
    print("WAR ROOM  %s  %s%s  %s" % (c["symbol"], board["now_ny"], "  (replay)" if board["replay"] else "",
                                       "paper" if board["paper"] else "LIVE ACCOUNT"))
    for line in card_lines(c, board["replay"]):
        print(line)


def print_scan(board):
    s = board.get("scan") or {}
    print("WAR ROOM scan  %s%s  %s" % (board["now_ny"], "  (replay)" if board["replay"] else "",
                                       "paper" if board["paper"] else "LIVE ACCOUNT"))
    print(s.get("line") or "")
    cards = board["cards"]
    if not cards:
        return
    print("Ranked by the replays' average R: a ranking of simulations, not a list of trades to take.")
    hdr = "%3s  %-6s %-9s %9s %9s %9s %7s %7s %8s %7s  %-18s %-11s" % (
        "#", "ticker", "trigger", "entry", "stop", "target", "tgt %", "stop %", "neither", "avg R", "history",
        "status")
    if board["replay"]:
        hdr += "  really"
    print(hdr)
    for c in cards:
        e, p, tm = c.get("entry") or {}, c.get("plan") or {}, c.get("tm")
        m = tm["mc"] if tm else {}
        h = tm["history"] if tm else {}
        hist = ("%d fired, %+.2fR" % (h["triggered"], h["avg_R"])) if h.get("triggered") else "-"
        row = "%3d  %-6s %-9s %9s %9s %9s %7s %7s %8s %7s  %-18s %-11s" % (
            c["rank"], c["symbol"], e.get("time") or "-", p.get("entry", "-"), p.get("stop", "-"),
            p.get("target", "-"), ("%.1f" % (100 * m["p_target"])) if m else "-",
            ("%.1f" % (100 * m["p_stop"])) if m else "-", ("%.1f" % (100 * m["p_neither"])) if m else "-",
            ("%+.3f" % m["avg_R"]) if m else "-", hist, STATUS_TEXT.get(c["status"], c["status"]))
        a = c.get("actual")
        if board["replay"] and a:
            row += "  %s %+.2fR" % (a["outcome"], a["R"])
        print(row)
    sm = board.get("reality_summary")
    if sm:
        print(sm)


# ---------------------------------------------------------------- commands

def opts_common(o):
    o.offline = os.path.abspath(o.offline) if o.offline else None
    if o.at_trigger and not o.now and not o.offline:
        raise WarError("--at-trigger looks back in time, so it is for replays: add --now \"YYYY-MM-DD HH:MM\"")
    if o.replays < 100 or o.replays > 200000:
        raise WarError("--replays must be between 100 and 200,000")
    return o


def cmd_run(o):
    opts_common(o)
    main_dir = os.path.abspath("trader")
    cfg, cfg_from = load_config(main_dir)
    now_utc, replay = clock(o)
    sym = o.symbol.upper()
    run_dir = new_run(main_dir, now_utc, yahoo_symbol(sym))
    t0 = time.time()
    board = board_head("run", now_utc, replay, cfg, cfg_from, o)
    card = run_ticker(sym, run_dir, main_dir, now_utc, o, cfg)
    board["cards"] = rank([card])
    board["seconds"] = round(time.time() - t0, 2)
    board["replays_total"] = o.replays if card.get("tm") else 0
    if not replay and card["status"] == "ready" and o.promote:
        board["promoted"] = promote(card, run_dir, main_dir, board)
    if card["status"] in ("expired", "no data") and not replay:
        board["notes"].append('No live trade to plan now. Replay this morning instead: warroom.py run %s --now "%s 10:15"'
                              % (sym, board["date"]))
    path = finish(board, run_dir, main_dir, o)
    if o.json:
        print(json.dumps({"html": path, "card": slim(card)}, indent=2))
    else:
        print_run(board)
        if board.get("promoted"):
            print(board["promoted"]["line"])
        for n in board["notes"]:
            print(n)
        print("Dashboard: %s" % os.path.relpath(path))
        print("Nothing was placed. Education and tooling, not financial advice.")
    return 0 if card["status"] not in ("error", "no data") else 1


def scan_symbols(o):
    if o.symbols:
        return ["--symbols", ",".join(s.strip().upper() for s in o.symbols.split(",") if s.strip())], \
            "%d names you gave" % len([s for s in o.symbols.split(",") if s.strip()])
    if o.watchlist:
        return ["--watchlist", os.path.abspath(o.watchlist)], o.watchlist
    if o.universe:
        uni = os.path.abspath(os.path.join("trader", "universe.txt"))
        if not os.path.exists(uni):
            raise WarError("no trader/universe.txt yet: run trader-scanner first (scan.py universe), "
                           "or scan the liquid list without --universe")
        return ["--universe", uni], "the whole universe (trader/universe.txt)"
    return ["--symbols", ",".join(LIQUID)], "the liquid list (%d names)" % len(LIQUID)


def cmd_scan(o):
    opts_common(o)
    main_dir = os.path.abspath("trader")
    cfg, cfg_from = load_config(main_dir)
    now_utc, replay = clock(o)
    run_dir = new_run(main_dir, now_utc, "scan")
    seed_box(os.path.join(run_dir, "trader"), main_dir, cfg)
    t0 = time.time()
    board = board_head("scan", now_utc, replay, cfg, cfg_from, o)
    board["steps"] = []
    pick, label = scan_symbols(o)
    if o.offline:
        tickers = [s.strip().upper() for s in (o.symbols or "").split(",") if s.strip()]
        if not tickers:
            raise WarError("an offline scan needs --symbols (one <TICKER>.json per name in the --offline folder)")
        hits = [{"symbol": s} for s in tickers]
        board["scan"] = {"line": "Offline: replaying the %d names given, no scanner pass." % len(tickers),
                         "label": label, "hits": len(hits)}
    else:
        args = ["orb", "--now", iso(now_utc), "--out", "watchlist.json", "--min-price", o.min_price,
                "--or-minutes", o.or_minutes, "--max-confirm", o.max_confirm] + pick
        code, out, err = run_step(board, "scan", args, run_dir)
        wl = read_json(os.path.join(run_dir, "watchlist.json"))
        if not wl or not wl.get("hits"):
            board["scan"] = {"line": say(out, err), "label": label, "hits": 0}
            board["cards"] = []
            board["seconds"] = round(time.time() - t0, 2)
            if not replay:
                board["notes"].append('Replay this morning instead: warroom.py scan --now "%s 10:15"' % board["date"])
            path = finish(board, run_dir, main_dir, o)
            print_scan(board)
            for n in board["notes"]:
                print(n)
            print("Dashboard: %s" % os.path.relpath(path))
            return 1
        hits = wl["hits"]
        cnt = wl.get("counts") or {}
        usable = cnt.get("scanned") or wl.get("scanned") or 0
        board["scan"] = {
            "label": label, "as_of": wl.get("as_of"), "hits": len(hits), "counts": cnt, "usable": usable,
            "line": "Scanned %s: %d of %d usable charts closed above their opening range by %s." % (
                label, len(hits), usable,
                clock12(to_eastern(now_utc).hour * 60 + to_eastern(now_utc).minute)),
        }
        if not replay:
            shutil.copyfile(os.path.join(run_dir, "watchlist.json"), os.path.join(main_dir, "watchlist.json"))
        hits = sorted(hits, key=lambda h: (-(h.get("bar_index") or 0), h.get("symbol")))
    chosen = hits if o.top == 0 else hits[:o.top]
    board["scan"]["replayed"] = len(chosen)

    def one(i_h):
        i, h = i_h
        if not o.offline:
            time.sleep((i % WORKERS) * PAUSE)
        try:
            return run_ticker(h["symbol"], run_dir, main_dir, now_utc, o, cfg, hit=h)
        except WarError as e:
            return {"symbol": h["symbol"], "status": "error", "headline": str(e), "steps": [], "hit": h}

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        cards = list(ex.map(one, enumerate(chosen)))
    board["cards"] = rank(cards)
    board["seconds"] = round(time.time() - t0, 2)
    board["replays_total"] = sum(o.replays for c in cards if c.get("tm"))
    if replay:
        board["reality_summary"] = reality_summary(board["cards"])
    path = finish(board, run_dir, main_dir, o)
    if o.json:
        print(json.dumps({"html": path, "cards": [slim(c) for c in board["cards"]]}, indent=2))
    else:
        print_scan(board)
        print("Dashboard: %s" % os.path.relpath(path))
        if not replay:
            print("Promote one into trader/ for /trader-broker: warroom.py pick TICKER")
        print("Nothing was placed. Education and tooling, not financial advice.")
    return 0


def reality_summary(cards):
    done = [c["actual"] for c in cards if c.get("actual") and c.get("tm")]
    if not done:
        return None
    n = len(done)
    t = sum(1 for a in done if a["outcome"] == "target")
    s = sum(1 for a in done if a["outcome"] == "stop")
    avg = sum(a["R"] for a in done) / n
    return ("What the rest of the day really did with these %d plans: %d target, %d stop, %d neither; average "
            "%+.2fR. One day is one day: it says little about the ranking." % (n, t, s, n - t - s, avg))


def slim(c):
    keep = ("symbol", "rank", "status", "headline", "entry", "plan", "price_now", "so_far", "actual")
    out = {k: c.get(k) for k in keep if c.get(k) is not None}
    tm = c.get("tm")
    if tm:
        m = tm["mc"]
        out["timemachine"] = {k: m.get(k) for k in ("replays", "seed", "p_target", "p_stop", "p_neither",
                                                    "p_green", "median_close", "p5_close", "p95_close", "avg_R")}
        out["timemachine"]["verdict"] = tm.get("verdict")
        out["history"] = {k: tm["history"].get(k) for k in ("sessions", "triggered", "target", "stop", "close",
                                                           "avg_R")}
    od = ((c.get("orders") or {}).get("orders") or [None])[0]
    if od:
        out["order"] = {k: v for k, v in od.items() if k != "why"}
    return out


def promote(card, run_dir, main_dir, board):
    """Copy one ready card's files into trader/ and run the risk check there for real."""
    box = os.path.join(run_dir, yahoo_symbol(card["symbol"]), "trader")
    os.makedirs(main_dir, exist_ok=True)
    for f in ("entry.json", "plan.json", "timemachine.json", "orders.json"):
        shutil.copyfile(os.path.join(box, f), os.path.join(main_dir, f))
    px = card.get("price_now")
    if px:
        write_prices(os.path.join(main_dir, "prices.json"), card["symbol"], px, utc_now())
    if not os.path.exists(os.path.join(main_dir, "config.json")):
        write_json(os.path.join(main_dir, "config.json"), read_json(os.path.join(box, "config.json")) or {})
    doc = {"steps": []}
    code, out, err = run_step(doc, "risk", [], os.path.dirname(main_dir))
    card["steps"].append(dict(doc["steps"][0], step="risk (trader/)"))
    ok = code == 0
    line = ("Promoted %s into trader/ and checked it again there: %s. Next: /trader-broker shows the ticket "
            "and asks you yes or no." % (card["symbol"], "PASS" if ok else "BLOCK, see below")) if ok else \
        ("Promoted %s into trader/, but the risk check there says BLOCK: %s" % (card["symbol"], say(out, err)))
    return {"symbol": card["symbol"], "pass": ok, "line": line}


def cmd_pick(o):
    main_dir = os.path.abspath("trader")
    latest = read_json(os.path.join(main_dir, "warroom", "latest.json"))
    if not latest:
        raise WarError("no war room board yet: run warroom.py scan or warroom.py run TICKER first")
    board = read_json(os.path.join(latest["run"], "warroom.json"))
    if not board:
        raise WarError("the last board is gone: run the war room again")
    sym = o.symbol.upper()
    card = next((c for c in board["cards"] if c["symbol"] == sym), None)
    if card is None:
        raise WarError("%s is not on the last board (%s)" % (sym, ", ".join(c["symbol"] for c in board["cards"])))
    if board.get("replay"):
        raise WarError("the last board is a replay (%s): a replay is research, never an order. Run it live."
                       % board["now_ny"])
    if board["date"] != to_eastern(utc_now()).date().isoformat():
        raise WarError("the last board is from %s, not today: run the war room again" % board["date"])
    if card["status"] != "ready":
        raise WarError("%s is %s on the last board, not PASS: %s" % (
            sym, STATUS_TEXT.get(card["status"], card["status"]), card.get("headline") or ""))
    res = promote(card, latest["run"], main_dir, board)
    print(res["line"])
    print(card["steps"][-1]["out"])
    return 0 if res["pass"] else 1


def cmd_open(o):
    latest = read_json(os.path.join("trader", "warroom", "latest.json"))
    if not latest or not os.path.exists(latest.get("html") or ""):
        raise WarError("no war room board yet: run warroom.py scan or warroom.py run TICKER first")
    webbrowser.open("file://" + latest["html"])
    print("Opened %s (%s)" % (os.path.relpath(latest["html"]), latest.get("now_ny")))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The war room: the whole day-trade pipeline in one command.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-22 10:15" (a replay)')
        p.add_argument("--stop", default="or-mid", help="trader-exit stop: or-mid, or-low, pct:X, atr:K")
        p.add_argument("--target", default="2R", help="trader-exit target: 2R, 1R, 1.5R")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--replays", type=int, default=10000, help="time machine replays (default 10,000)")
        p.add_argument("--seed", type=int, default=19, help="time machine seed (default 19)")
        p.add_argument("--no-news", dest="news", action="store_false", help="skip the headlines")
        p.add_argument("--offline", metavar="PATH", help="saved 60-day charts: a file (run) or a folder")
        p.add_argument("--open", action="store_true", help="open the dashboard in the browser")
        p.add_argument("--at-trigger", action="store_true",
                       help="replays: judge each breakout at the moment it fired, not at --now")
        p.add_argument("--json", action="store_true", help="print a JSON summary instead of the text")

    p = sub.add_parser("run", help="one ticker through the whole pack")
    p.add_argument("symbol")
    p.add_argument("--no-promote", dest="promote", action="store_false",
                   help="never copy a passed order into trader/")
    common(p)
    p = sub.add_parser("scan", help="opening-range breakouts, each replayed and ranked")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--symbols", help="comma-separated tickers instead of the liquid list")
    g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json or a text file of tickers")
    g.add_argument("--universe", action="store_true", help="the whole market (trader/universe.txt, minutes)")
    p.add_argument("--top", type=int, default=8, help="replay the N latest breakouts (0: all, default 8)")
    p.add_argument("--min-price", type=float, default=5.0)
    p.add_argument("--max-confirm", type=int, default=200, help="scanner stage 2 cap (0: all)")
    common(p)
    p = sub.add_parser("pick", help="promote one ticker from the last live board into trader/")
    p.add_argument("symbol")
    sub.add_parser("open", help="open the last dashboard")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "scan": cmd_scan, "pick": cmd_pick, "open": cmd_open}[a.cmd](a)
    except WarError as e:
        print("warroom.py: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
