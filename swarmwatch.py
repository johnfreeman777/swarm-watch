#!/usr/bin/env python3
"""Swarm Watch: a Telegram bot that watches IMD swarm seats from public data.

Anyone can /watch an identity.md NFT number and get told when that seat stops taking
work while the rest of the network is busy, when it starts collecting rejections, a
daily digest, and the collection owner's on-chain messages as they land.

Reads only api.imd.fun and a public Ethereum explorer. Never asks for keys, wallets or
access to anyone's machine. Standard library only; Python 3.10+.

Env: SWARMWATCH_TOKEN_FILE (Telegram bot token), SWARMWATCH_DB (sqlite path),
     SWARMWATCH_ADMIN (chat id that receives operational errors; optional).
"""
import html
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.imd.fun"
EXPLORER = "https://explorer.imd.fun"
OWNER = "0x200e710acaa6a93bbc77146026328c40f1d60fb1"  # publishes the project's on-chain messages
BLOCKSCOUT = "https://eth.blockscout.com/api/v2"
IMD = "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"          # the token airdrops arrive in (mainnet)
LEDGER = "https://johnfreeman777.github.io/swarm-ledger/data/snapshot.json"  # launches, allocations, claim status
DROPS_POLL_S = 60
LEDGER_POLL_S = 30 * 60
POLL_S = 60            # network poll
NEWS_POLL_S = 120      # on-chain messages poll
WINDOW_S = 2 * 3600    # "stalled" window
FLEET_BUSY = 0.5       # share of seats that took work in the window for silence to count
REJECT_STEP = 3        # new rejections within a day that trigger an alert
DIGEST_UTC_HOUR = 8
COOLDOWN_S = 6 * 3600
HISTORY_S = 3 * 24 * 3600

TOKEN = open(os.environ.get("SWARMWATCH_TOKEN_FILE", "/etc/swarmwatch/token")).read().strip()
DB_PATH = os.environ.get("SWARMWATCH_DB", "/var/lib/swarmwatch/db.sqlite")
ADMIN = os.environ.get("SWARMWATCH_ADMIN") or None
TG = f"https://api.telegram.org/bot{TOKEN}"

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), *a, flush=True)

# ---------- http ----------
def get_json(url, timeout=30, data=None):
    req = urllib.request.Request(url, data=data, headers={"user-agent": "swarm-watch/1.0", **({"content-type": "application/json"} if data else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def tg(method, **params):
    try:
        return get_json(f"{TG}/{method}", data=json.dumps(params).encode(), timeout=70)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        log("telegram", method, e.code, body)
        return {"ok": False, "error_code": e.code, "description": body}
    except Exception as e:  # network blip: caller decides
        log("telegram", method, "error", e)
        return {"ok": False, "description": str(e)}

def send(chat_id, text, silent=False, ask=None, keys=None):
    """ask: placeholder text; opens the reply field so the user can just type an answer.
    keys: attach the inline button bar."""
    extra = {"reply_markup": {"force_reply": True, "input_field_placeholder": ask}} if ask else {"reply_markup": keyboard(chat_id)} if keys else {}
    r = tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True, disable_notification=silent, **extra)
    if not r.get("ok") and r.get("error_code") in (403, 400):  # blocked the bot or chat gone: drop them
        with db() as c:
            c.execute("DELETE FROM subs WHERE chat_id=?", (chat_id,))
            c.execute("DELETE FROM prefs WHERE chat_id=?", (chat_id,))
    return r

# ---------- storage ----------
_lock = threading.Lock()
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS subs (chat_id INTEGER, token_id TEXT, added INTEGER, PRIMARY KEY (chat_id, token_id));
        CREATE TABLE IF NOT EXISTS prefs (chat_id INTEGER PRIMARY KEY, digest INTEGER DEFAULT 1, news INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS hist (token_id TEXT, ts INTEGER, attempts INTEGER, accepted INTEGER, rejected INTEGER, pending INTEGER, wallet TEXT, PRIMARY KEY (token_id, ts));
        CREATE TABLE IF NOT EXISTS alerts (chat_id INTEGER, token_id TEXT, kind TEXT, last_sent INTEGER, state TEXT, PRIMARY KEY (chat_id, token_id, kind));
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS news_seen (h TEXT PRIMARY KEY, ts INTEGER);
        CREATE TABLE IF NOT EXISTS pending (chat_id INTEGER PRIMARY KEY, action TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS drops_seen (tx TEXT, wallet TEXT, ts INTEGER, PRIMARY KEY (tx, wallet));
        CREATE TABLE IF NOT EXISTS alloc_seen (chat_id INTEGER, launch INTEGER, wallet TEXT, claimed INTEGER, PRIMARY KEY (chat_id, launch, wallet));
        """)

def kv_get(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

def kv_set(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, str(v)))

# ---------- network data ----------
seats = {}      # token_id -> merged row (live, in memory)
network = {}    # /health summary
net_lock = threading.Lock()

def fetch_seats():
    data = get_json(f"{API}/contributors")
    merged = {}
    for c in data.get("contributors", []):
        t = str(c["tokenId"])
        e = merged.setdefault(t, {"tokenId": t, "wallet": "", "attempts": 0, "accepted": 0, "rejected": 0, "pending": 0, "ms": 0})
        e["wallet"] = c["wallet"].lower()  # last entry = current pairing
        for k in ("attempts", "accepted", "rejected", "pending"):
            e[k] += int(c.get(k) or 0)
        e["ms"] += int(c.get("wallClockMs") or 0)
    rank = sorted(merged.values(), key=lambda s: -s["accepted"])
    for i, s in enumerate(rank):
        s["rank"] = i + 1
    return merged

def fetch_health():
    h = get_json(f"{API}/health")
    p = h.get("payments") or {}
    return {"online": h.get("connectedDaemons"), "enrolled": h.get("activeEnrollments"), "accepted24h": h.get("acceptedLastDay"),
            "working": h.get("workingNow"), "paid": (p.get("orders") or {}).get("paid"), "lastPaidAt": p.get("lastPaidAt"), "build": h.get("version")}

def record(now, merged):
    with db() as c:
        c.executemany("INSERT OR IGNORE INTO hist VALUES (?,?,?,?,?,?,?)",
                      [(t, now, s["attempts"], s["accepted"], s["rejected"], s["pending"], s["wallet"]) for t, s in merged.items()])
        c.execute("DELETE FROM hist WHERE ts < ?", (now - HISTORY_S,))

def row_at(token_id, ts):
    """Latest history row at or before ts."""
    with db() as c:
        return c.execute("SELECT * FROM hist WHERE token_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (token_id, ts)).fetchone()

def last_change(token_id, field="attempts"):
    """Timestamp when `field` last changed, or the oldest sample if it never did in history."""
    with db() as c:
        rows = c.execute(f"SELECT ts, {field} AS v FROM hist WHERE token_id=? ORDER BY ts DESC", (token_id,)).fetchall()
    if not rows:
        return None
    cur = rows[0]["v"]
    for r in rows[1:]:
        if r["v"] != cur:
            return r["ts"]
    return rows[-1]["ts"]

def fleet_active_share(now):
    """Share of seats whose attempts changed in the window: how busy the network has been."""
    with db() as c:
        then = c.execute("SELECT token_id, attempts FROM hist WHERE ts=(SELECT MAX(ts) FROM hist WHERE ts<=?)", (now - WINDOW_S,)).fetchall()
    if not then:
        return None
    base = {r["token_id"]: r["attempts"] for r in then}
    with net_lock:
        cur = {t: s["attempts"] for t, s in seats.items()}
    common = [t for t in base if t in cur]
    if len(common) < 20:
        return None
    return sum(1 for t in common if cur[t] != base[t]) / len(common)

# ---------- formatting ----------
def h(s):
    return html.escape(str(s))

def wallet_of(token_id):
    with net_lock:
        s = seats.get(token_id)
    return s["wallet"] if s else None

def watched_wallets():
    """wallet -> [(chat_id, token_id), ...] for every subscription whose seat is known."""
    out = {}
    with db() as c:
        subs = c.execute("SELECT chat_id, token_id FROM subs").fetchall()
    for r in subs:
        w = wallet_of(r["token_id"])
        if w:
            out.setdefault(w, []).append((r["chat_id"], r["token_id"]))
    return out

def fmt_amount(raw, decimals=18, digits=2):
    try:
        v = int(raw) / 10 ** int(decimals)
    except Exception:
        return "?"
    return f"{v:,.{digits}f}" if v < 1000 else f"{v:,.0f}"

ledger = {"launches": [], "at": 0}
def load_ledger():
    d = get_json(LEDGER)
    ledger["launches"] = d.get("launches", [])
    ledger["at"] = int(time.time())
    return True

def launch_void(l):
    return not l.get("distributor") and l.get("status") != "live"

def allocations_for(wallet):
    """(launch, allocation) pairs for a wallet, live launches with a distributor only."""
    out = []
    for l in ledger["launches"]:
        if launch_void(l) or not l.get("distributor"):
            continue
        for a in l.get("allocations", []):
            if a.get("wallet") == wallet:
                out.append((l, a))
    return out

def alloc_line(l, a):
    t = l.get("token") or {}
    amount = fmt_amount(a.get("amount", "0"), t.get("decimals", 18), 0)
    where = f'<a href="{h(l["site"])}">claim page</a>' if l.get("site") else f'distributor <code>{h(l["distributor"])}</code>'
    net = "" if l.get("chainId") == 1 else " · testnet"
    state = "claimed ✅" if a.get("claimed") is True else "unclaimed" if a.get("claimed") is False else "claim status unknown"
    return f"No. {int(l['number']):04d} {h(t.get('name') or '—')} <b>{amount} {h(t.get('symbol') or '')}</b> ({int(a.get('bps', 0)) / 100:.2f}%) · {state} · {where}{net}"

def ago(ts):
    if not ts:
        return "never"
    m = int((time.time() - ts) // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 60 else f"{m // 60} h ago" if m < 48 * 60 else f"{m // 1440} d ago"

def seat_line(t, s, day_base=None):
    acc = f"{s['accepted']:,}"
    if day_base is not None:
        acc += f" (+{s['accepted'] - day_base['accepted']:,} today)"
    rate = f"{100 * s['accepted'] / s['attempts']:.0f}%" if s["attempts"] else "–"
    return (f"<b>#{h(t)}</b> · rank {s['rank']} · accepted {acc} · rejected {s['rejected']:,} · pending {s['pending']:,} · {rate}"
            f" · last work {ago(last_change(t))}")

def network_line():
    with net_lock:
        n = dict(network)
    if not n:
        return "network: no data yet"
    paid = f" · paid orders {n['paid']:,}" if n.get("paid") is not None else ""
    return f"network: {n.get('online')}/{n.get('enrolled')} online · {n.get('accepted24h', 0):,} accepted in 24 h · working now {n.get('working')}{paid}"

def keyboard(chat_id):
    with db() as c:
        r = c.execute("SELECT digest, news FROM prefs WHERE chat_id=?", (chat_id,)).fetchone()
    digest_on, news_on = (r["digest"], r["news"]) if r else (1, 1)
    return {"inline_keyboard": [
        [{"text": "📊 Status", "callback_data": "status"}, {"text": "🌐 Network", "callback_data": "network"}],
        [{"text": "🎁 Allocations", "callback_data": "allocations"}, {"text": "💸 IMD drops", "callback_data": "drops"}],
        [{"text": "➕ Watch", "callback_data": "watch"}, {"text": "➖ Unwatch", "callback_data": "unwatch"}],
        [{"text": f"{'🔔' if digest_on else '🔕'} Daily digest: {'on' if digest_on else 'off'}", "callback_data": "digest"},
         {"text": f"{'📡' if news_on else '📴'} Dev news: {'on' if news_on else 'off'}", "callback_data": "news"}],
    ]}

def toggle(chat_id, field):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO prefs (chat_id) VALUES (?)", (chat_id,))
        r = c.execute(f"SELECT {field} FROM prefs WHERE chat_id=?", (chat_id,)).fetchone()
        new = 0 if r[field] else 1
        c.execute(f"UPDATE prefs SET {field}=? WHERE chat_id=?", (new, chat_id))
    return new

def callback(chat_id, data, cq_id, msg_id=None):
    """Inline button presses map onto the same actions as the commands."""
    if data in ("status", "network", "watch", "unwatch", "allocations", "drops"):
        tg("answerCallbackQuery", callback_query_id=cq_id)
        cmd(chat_id, "/" + data)
    elif data in ("digest", "news"):
        new = toggle(chat_id, data)
        tg("answerCallbackQuery", callback_query_id=cq_id, text=f"{'Daily digest' if data == 'digest' else 'Dev news'} {'on' if new else 'off'}")
        if msg_id:  # refresh the button bar under the message that was pressed
            tg("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id, reply_markup=keyboard(chat_id))
    else:
        tg("answerCallbackQuery", callback_query_id=cq_id)

HELP = (
    "<b>Swarm Watch</b> · unofficial, read-only, public data only.\n\n"
    "/watch 7 1234 — watch these NFT seats\n"
    "/unwatch 7 — stop watching (or /unwatch all)\n"
    "/list — what you watch\n"
    "/status — your seats right now\n"
    "/network — the swarm right now\n"
    "/allocations — launch tokens allocated to your seats, claimed or not\n"
    "/drops — IMD that arrived in your seats' wallets (airdrops from the dev)\n"
    "/digest on|off — daily summary at 08:00 UTC\n"
    "/news on|off — the dev's on-chain messages as they land\n\n"
    "Alerts: a seat that took no work for 2 h while most of the fleet did; 3+ new rejections in a day; a seat that disappears from the network; "
    "a new launch allocation to your seat's wallet (and when it is claimed); IMD arriving in that wallet. "
    "Pauses and failure reasons are only visible to the node itself (imd doctor); this bot infers from public counts.\n"
    "Source: github.com/johnfreeman777/swarm-watch"
)

# ---------- commands ----------
def set_pending(chat_id, action):
    with db() as c:
        if action:
            c.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?)", (chat_id, action, int(time.time())))
        else:
            c.execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))

def get_pending(chat_id):
    with db() as c:
        r = c.execute("SELECT action, ts FROM pending WHERE chat_id=?", (chat_id,)).fetchone()
    return r["action"] if r and time.time() - r["ts"] < 3600 else None

def do_watch(chat_id, ids):
    with db() as c:
        for t in ids:
            c.execute("INSERT OR IGNORE INTO subs VALUES (?,?,?)", (chat_id, t, int(time.time())))
    with net_lock:
        known = [t for t in ids if t in seats]
    unknown = [t for t in ids if t not in known]
    msg = "Watching " + ", ".join(f"#{t}" for t in ids) + "."
    if unknown:
        msg += "\nNot seen on the network yet: " + ", ".join(f"#{t}" for t in unknown) + " (it must be paired and have taken at least one task)."
    send(chat_id, msg)
    for t in known:  # already-known allocations are not news to a new subscriber
        prime_allocs(chat_id, t)
    if known:
        status(chat_id, known)

def prime_allocs(chat_id, token_id):
    w = wallet_of(token_id)
    if not w:
        return
    with db() as c:
        for l, a in allocations_for(w):
            c.execute("INSERT OR IGNORE INTO alloc_seen VALUES (?,?,?,?)", (chat_id, int(l["number"]), w, 1 if a.get("claimed") else 0))

def cmd_allocations(chat_id):
    ids = my_subs(chat_id)
    if not ids:
        return send(chat_id, "You watch nothing yet. Press Watch or type the NFT numbers.", keys=True)
    lines = []
    for t in ids:
        w = wallet_of(t)
        al = allocations_for(w) if w else []
        lines.append(f"<b>#{h(t)}</b> · " + (f"{len(al)} allocation{'s' if len(al) != 1 else ''}" if al else "no allocations yet"))
        for l, a in sorted(al, key=lambda x: -int(x[0]["number"]))[:8]:
            lines.append("  " + alloc_line(l, a))
        if len(al) > 8:
            lines.append(f"  … and {len(al) - 8} more on the ledger")
    lines.append('Full history per wallet: <a href="https://johnfreeman777.github.io/swarm-ledger/">Swarm Ledger</a>')
    send(chat_id, "\n".join(lines), keys=True)

def cmd_drops(chat_id):
    """Last IMD transfers received by the watched wallets (on demand, from the explorer)."""
    ids = my_subs(chat_id)
    if not ids:
        return send(chat_id, "You watch nothing yet. Press Watch or type the NFT numbers.", keys=True)
    lines, seen = [], set()
    for t in ids:
        w = wallet_of(t)
        if not w or w in seen:
            continue
        seen.add(w)
        try:
            d = get_json(f"{BLOCKSCOUT}/addresses/{w}/token-transfers?type=ERC-20&filter=to&token={IMD}")
        except Exception as e:
            lines.append(f"<b>#{h(t)}</b> · explorer unavailable ({h(str(e)[:60])})")
            continue
        items = [x for x in d.get("items", []) if (x.get("to") or {}).get("hash", "").lower() == w][:6]
        who = ", ".join(f"#{h(x)}" for x in ids if wallet_of(x) == w)
        lines.append(f"<b>{who}</b> · wallet <code>{h(w[:6] + '…' + w[-4:])}</code>")
        if not items:
            lines.append("  no IMD received yet")
        for x in items:
            frm = (x.get("from") or {})
            src = frm.get("name") or (frm.get("hash") or "")[:6] + "…"
            lines.append(f"  +{fmt_amount(x['total']['value'], x['total'].get('decimals', 18))} IMD · {h(src)} · {h((x.get('timestamp') or '')[:10])} · <a href=\"https://etherscan.io/tx/{h(x['transaction_hash'])}\">tx</a>")
    send(chat_id, "\n".join(lines), keys=True)

def do_unwatch(chat_id, ids, everything=False):
    with db() as c:
        if everything:
            c.execute("DELETE FROM subs WHERE chat_id=?", (chat_id,))
            c.execute("DELETE FROM alerts WHERE chat_id=?", (chat_id,))
        else:
            c.executemany("DELETE FROM subs WHERE chat_id=? AND token_id=?", [(chat_id, t) for t in ids])
            c.executemany("DELETE FROM alerts WHERE chat_id=? AND token_id=?", [(chat_id, t) for t in ids])
    send(chat_id, "Done. You watch: " + (", ".join(f"#{t}" for t in my_subs(chat_id)) or "nothing."), keys=True)

def parse_ids(args):
    out = []
    for a in args:
        a = a.lstrip("#")
        if a.isdigit() and 0 <= int(a) < 2000:
            out.append(str(int(a)))
    return out

def cmd(chat_id, text):
    parts = text.strip().split()
    if not parts:
        return
    c0 = parts[0].lower().split("@")[0]
    args = parts[1:]
    with db() as c:
        c.execute("INSERT OR IGNORE INTO prefs (chat_id) VALUES (?)", (chat_id,))
    if c0 in ("/start", "/help"):
        send(chat_id, HELP, keys=True)
    elif c0 == "/watch":
        ids = parse_ids(args)
        if not ids:
            set_pending(chat_id, "watch")
            return send(chat_id, "Which NFT numbers? Type them separated by spaces, e.g. <code>7 1234</code>", ask="7 1234")
        set_pending(chat_id, None)
        do_watch(chat_id, ids)
    elif c0 == "/unwatch":
        ids = parse_ids(args)
        if args and args[0].lower() == "all":
            set_pending(chat_id, None)
            return do_unwatch(chat_id, [], everything=True)
        if not ids:
            mine = my_subs(chat_id)
            if not mine:
                return send(chat_id, "You watch nothing yet.")
            set_pending(chat_id, "unwatch")
            return send(chat_id, "You watch " + ", ".join(f"#{t}" for t in mine) + ". Which ones to drop? Type the numbers, or <code>all</code>.", ask="7")
        set_pending(chat_id, None)
        do_unwatch(chat_id, ids)
    elif c0 == "/list":
        send(chat_id, "You watch: " + (", ".join(f"#{t}" for t in my_subs(chat_id)) or "nothing yet. /watch 7"))
    elif c0 == "/status":
        status(chat_id, my_subs(chat_id))
    elif c0 == "/network":
        send(chat_id, network_line(), keys=True)
    elif c0 == "/allocations":
        cmd_allocations(chat_id)
    elif c0 == "/drops":
        cmd_drops(chat_id)
    elif c0 in ("/digest", "/news"):
        on = (args[0].lower() if args else "") in ("on", "1", "yes")
        off = (args[0].lower() if args else "") in ("off", "0", "no")
        if not (on or off):
            with db() as c:
                r = c.execute("SELECT digest, news FROM prefs WHERE chat_id=?", (chat_id,)).fetchone()
            return send(chat_id, f"{c0[1:]} is {'on' if r[c0[1:]] else 'off'}. Use <code>{c0} on</code> or <code>{c0} off</code>.")
        with db() as c:
            c.execute(f"UPDATE prefs SET {c0[1:]}=? WHERE chat_id=?", (1 if on else 0, chat_id))
        send(chat_id, f"{c0[1:]} {'on' if on else 'off'}.")
    else:
        send(chat_id, HELP)

def plain(chat_id, text):
    """Text without a slash: the answer to a /watch or /unwatch prompt, or just NFT numbers."""
    action = get_pending(chat_id)
    ids = parse_ids(text.replace(",", " ").split())
    if action == "unwatch":
        set_pending(chat_id, None)
        if text.strip().lower() == "all":
            return do_unwatch(chat_id, [], everything=True)
        return do_unwatch(chat_id, ids) if ids else send(chat_id, "No numbers there. Use /unwatch again when ready.")
    if ids:
        set_pending(chat_id, None)
        return do_watch(chat_id, ids)
    if action == "watch":
        return send(chat_id, "I need NFT numbers, e.g. <code>7 1234</code>", ask="7 1234")
    send(chat_id, HELP, keys=True)

def my_subs(chat_id):
    with db() as c:
        return [r["token_id"] for r in c.execute("SELECT token_id FROM subs WHERE chat_id=? ORDER BY CAST(token_id AS INTEGER)", (chat_id,))]

def status(chat_id, ids):
    if not ids:
        return send(chat_id, "You watch nothing yet. Press Watch or type the NFT numbers.", keys=True)
    lines = []
    with net_lock:
        snap = {t: dict(seats[t]) for t in ids if t in seats}
    for t in ids:
        s = snap.get(t)
        if not s:
            lines.append(f"<b>#{h(t)}</b> · not on the network")
            continue
        line = seat_line(t, s)
        al = allocations_for(s["wallet"])
        if al:
            un = sum(1 for _, a in al if a.get("claimed") is False)
            line += f" · allocations {len(al)}" + (f" (<b>{un} unclaimed</b>)" if un else "")
        lines.append(line)
    lines.append(network_line())
    send(chat_id, "\n".join(lines), keys=True)

# ---------- alerts ----------
def alert_state(chat_id, t, kind):
    with db() as c:
        return c.execute("SELECT * FROM alerts WHERE chat_id=? AND token_id=? AND kind=?", (chat_id, t, kind)).fetchone()

def set_alert(chat_id, t, kind, state, sent=True):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?)", (chat_id, t, kind, int(time.time()) if sent else (alert_state(chat_id, t, kind) or {"last_sent": 0})["last_sent"], state))

def check_alerts(now):
    with db() as c:
        subs = c.execute("SELECT chat_id, token_id FROM subs").fetchall()
    if not subs:
        return
    busy = fleet_active_share(now)
    with net_lock:
        snap = {t: dict(s) for t, s in seats.items()}
    for r in subs:
        chat_id, t = r["chat_id"], r["token_id"]
        s = snap.get(t)
        # gone from the network
        st = alert_state(chat_id, t, "gone")
        if s is None:
            if (st is None or st["state"] != "gone") and row_at(t, now) is not None:
                send(chat_id, f"⚠️ <b>#{h(t)}</b> is no longer listed by the network (unlinked, or the API dropped it).")
                set_alert(chat_id, t, "gone", "gone")
            continue
        elif st is not None and st["state"] == "gone":
            send(chat_id, f"✅ <b>#{h(t)}</b> is back on the network.")
            set_alert(chat_id, t, "gone", "ok")
        # stalled while the fleet is busy
        lc = last_change(t)
        st = alert_state(chat_id, t, "stalled")
        stalled = busy is not None and busy >= FLEET_BUSY and lc is not None and now - lc >= WINDOW_S
        if stalled and (st is None or st["state"] != "stalled") and (st is None or now - st["last_sent"] >= COOLDOWN_S):
            send(chat_id, f"🔴 <b>#{h(t)}</b> took no work for {(now - lc) // 3600} h while {busy * 100:.0f}% of the fleet did. "
                          f"Check the daemon: <code>imd doctor</code> shows pauses and failed runs.\n{seat_line(t, s)}")
            set_alert(chat_id, t, "stalled", "stalled")
        elif not stalled and st is not None and st["state"] == "stalled" and lc is not None and now - lc < WINDOW_S:
            send(chat_id, f"🟢 <b>#{h(t)}</b> is taking work again.\n{seat_line(t, s)}", silent=True)
            set_alert(chat_id, t, "stalled", "ok", sent=False)
        # rejections piling up
        day = row_at(t, now - 24 * 3600)
        if day is not None:
            new_rej = s["rejected"] - day["rejected"]
            st = alert_state(chat_id, t, "reject")
            if new_rej >= REJECT_STEP and (st is None or now - st["last_sent"] >= 24 * 3600):
                send(chat_id, f"🟠 <b>#{h(t)}</b> collected {new_rej} rejections in the last 24 h. Rejected attempts don't hurt standing, bad reviews do; "
                              f"but a streak usually means a broken runtime or sandbox.\n{seat_line(t, s)}")
                set_alert(chat_id, t, "reject", "sent")

def digest(now):
    hour_key = time.strftime("%Y-%m-%d", time.gmtime(now))
    if time.gmtime(now).tm_hour != DIGEST_UTC_HOUR or kv_get("digest_day") == hour_key:
        return
    kv_set("digest_day", hour_key)
    with db() as c:
        chats = [r["chat_id"] for r in c.execute("SELECT chat_id FROM prefs WHERE digest=1")]
    with net_lock:
        snap = {t: dict(s) for t, s in seats.items()}
    for chat_id in chats:
        ids = my_subs(chat_id)
        if not ids:
            continue
        lines = ["<b>Daily digest</b>"]
        for t in ids:
            s = snap.get(t)
            lines.append(seat_line(t, s, row_at(t, now - 24 * 3600)) if s else f"<b>#{h(t)}</b> · not on the network")
        lines.append(network_line())
        send(chat_id, "\n".join(lines), silent=True)

# ---------- allocations (from the Swarm Ledger snapshot) ----------
def check_allocations():
    try:
        load_ledger()
    except Exception as e:
        last_err["ledger"] = str(e)[:200]
        log("ledger failed", e)
        return False
    if not kv_get("alloc_init"):  # first run: remember what exists, announce nothing
        with db() as c:
            for r in c.execute("SELECT chat_id, token_id FROM subs").fetchall():
                prime_allocs(r["chat_id"], r["token_id"])
        kv_set("alloc_init", "1")
        return True
    for w, subs in watched_wallets().items():
        for l, a in allocations_for(w):
            n = int(l["number"])
            claimed = 1 if a.get("claimed") is True else 0
            for chat_id, t in subs:
                with db() as c:
                    r = c.execute("SELECT claimed FROM alloc_seen WHERE chat_id=? AND launch=? AND wallet=?", (chat_id, n, w)).fetchone()
                if r is None:
                    send(chat_id, f"🎁 <b>#{h(t)}</b> got a launch allocation:\n{alloc_line(l, a)}")
                elif claimed and not r["claimed"]:
                    send(chat_id, f"✅ <b>#{h(t)}</b> claimed its allocation from launch No. {n:04d} {h((l.get('token') or {}).get('symbol') or '')}.", silent=True)
                else:
                    continue
                with db() as c:
                    c.execute("INSERT OR REPLACE INTO alloc_seen VALUES (?,?,?,?)", (chat_id, n, w, claimed))
    return True

# ---------- IMD arriving in watched wallets ----------
DROPS_PER_CYCLE = 10
_drops_cursor = 0

def check_drops():
    """Round-robin over watched wallets, a few per minute: read each wallet's latest incoming
    IMD transfers from the explorer and report the ones not seen before. A wallet seen for the
    first time is primed silently, so old history is never announced as news.
    (The token's global transfer feed is too busy to walk; per-wallet reads stay O(1) each.)"""
    global _drops_cursor
    watched = watched_wallets()
    wallets = sorted(watched)
    if not wallets:
        return True
    batch = [wallets[(_drops_cursor + i) % len(wallets)] for i in range(min(DROPS_PER_CYCLE, len(wallets)))]
    _drops_cursor = (_drops_cursor + len(batch)) % len(wallets)
    ok = True
    for w in batch:
        try:
            d = get_json(f"{BLOCKSCOUT}/addresses/{w}/token-transfers?type=ERC-20&filter=to&token={IMD}")
        except Exception as e:
            last_err["drops"] = str(e)[:200]
            log("drops fetch failed", w[:10], e)
            ok = False
            continue
        items = [x for x in d.get("items", []) if ((x.get("to") or {}).get("hash") or "").lower() == w]
        primed = kv_get(f"drops_primed:{w}")
        with db() as c:
            for x in items:
                txh = x["transaction_hash"]
                if c.execute("SELECT 1 FROM drops_seen WHERE tx=? AND wallet=?", (txh, w)).fetchone():
                    continue
                c.execute("INSERT INTO drops_seen VALUES (?,?,?)", (txh, w, int(time.time())))
                if not primed:
                    continue
                frm = x.get("from") or {}
                src = frm.get("name") or (frm.get("hash") or "")[:6] + "…"
                amount = fmt_amount(x["total"]["value"], x["total"].get("decimals", 18))
                for chat_id, t in watched[w]:
                    send(chat_id, f"💸 <b>+{amount} IMD</b> arrived in the wallet of <b>#{h(t)}</b> · from {h(src)}"
                                  f" · <a href=\"https://etherscan.io/tx/{h(txh)}\">tx</a>")
        if not primed:
            kv_set(f"drops_primed:{w}", "1")
    return ok

# ---------- on-chain messages ----------
def poll_news():
    """Self-transactions from the collection owner whose calldata is UTF-8 text."""
    try:
        data = get_json(f"{BLOCKSCOUT}/addresses/{OWNER}/transactions")
    except Exception as e:
        last_err["news"] = str(e)[:200]
        log("news fetch failed", e)
        return False
    items = data.get("items", [])
    seen_any = kv_get("news_init")
    new = []
    for tx in items:
        frm = ((tx.get("from") or {}).get("hash") or "").lower()
        to = ((tx.get("to") or {}).get("hash") or "").lower()
        raw = tx.get("raw_input") or ""
        if frm != OWNER or to != OWNER or len(raw) <= 2:
            continue
        try:
            text = bytes.fromhex(raw[2:]).decode("utf-8").strip()
        except Exception:
            continue
        if not text.isprintable() and "\n" not in text:
            continue
        with db() as c:
            if c.execute("SELECT 1 FROM news_seen WHERE h=?", (tx["hash"],)).fetchone():
                continue
            c.execute("INSERT INTO news_seen VALUES (?,?)", (tx["hash"], int(time.time())))
        new.append((tx["hash"], tx.get("timestamp"), text))
    if not seen_any:  # first run: remember everything, announce nothing
        kv_set("news_init", "1")
        log(f"news: primed with {len(new)} past messages")
        return True
    if not new:
        return True
    with db() as c:
        chats = [r["chat_id"] for r in c.execute("SELECT chat_id FROM prefs WHERE news=1")]
    for txh, ts, text in reversed(new):
        body = h(text if len(text) <= 3500 else text[:3500] + "…")
        msg = f"📡 <b>On-chain message from the dev</b> · {h(ts or '')}\n\n{body}\n\n<a href=\"https://etherscan.io/tx/{txh}\">tx</a>"
        for chat_id in chats:
            send(chat_id, msg)
        log("news: sent", txh, "to", len(chats))
    return True

# ---------- loops ----------
STALE_S = 10 * 60
last_ok = {"seats": 0, "news": 0, "drops": 0, "ledger": 0}
last_err = {"seats": "", "news": "", "drops": "", "ledger": ""}
stale_flag = {"seats": False, "news": False, "drops": False, "ledger": False}

def watchdog(now, what, limit):
    """Tell the admin once when a data source stops updating, and once when it is back."""
    if not ADMIN or not last_ok[what]:
        return
    stale = now - last_ok[what] > limit
    if stale and not stale_flag[what]:
        stale_flag[what] = True
        send(ADMIN, f"🛑 swarm-watch: no fresh {what} data for {(now - last_ok[what]) // 60} min. Last error: {h(last_err[what] or 'none')}")
    elif not stale and stale_flag[what]:
        stale_flag[what] = False
        send(ADMIN, f"✅ swarm-watch: {what} data is updating again.")

def poll_loop():
    global seats, network
    last_news = last_drops = last_ledger = 0
    while True:
        now = int(time.time())
        try:
            merged = fetch_seats()
            with net_lock:
                seats = merged
            record(now, merged)
            last_ok["seats"] = now
        except Exception as e:
            last_err["seats"] = str(e)[:200]
            log("contributors failed", e)
        try:
            n = fetch_health()
            with net_lock:
                network = n
        except Exception as e:
            log("health failed", e)
        fresh = now - last_ok["seats"] <= STALE_S
        try:
            if fresh:  # stale counts would look like silence; don't alert on them
                check_alerts(now)
            digest(now)
        except Exception as e:
            log("alerts failed", e)
            if ADMIN:
                send(ADMIN, f"swarm-watch alerts error: {h(e)}")
        if now - last_news >= NEWS_POLL_S:
            last_news = now
            if poll_news():
                last_ok["news"] = now
        if fresh and now - last_drops >= DROPS_POLL_S:
            last_drops = now
            if check_drops():
                last_ok["drops"] = now
        if fresh and now - last_ledger >= LEDGER_POLL_S:
            last_ledger = now
            if check_allocations():
                last_ok["ledger"] = now
        watchdog(now, "seats", STALE_S)
        watchdog(now, "news", 2 * 3600)
        watchdog(now, "drops", 2 * 3600)
        watchdog(now, "ledger", 3 * 3600)
        time.sleep(max(1, POLL_S - (time.time() - now)))

def updates_loop():
    offset = int(kv_get("tg_offset", 0))
    while True:
        r = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            kv_set("tg_offset", offset)
            cq = u.get("callback_query")
            if cq:
                try:
                    callback((cq.get("message") or {}).get("chat", {}).get("id"), cq.get("data", ""), cq["id"], (cq.get("message") or {}).get("message_id"))
                except Exception as e:
                    log("callback failed", e)
                continue
            m = u.get("message") or {}
            text = m.get("text")
            chat = (m.get("chat") or {}).get("id")
            if not text or chat is None:
                continue
            try:
                if text.startswith("/"):
                    cmd(chat, text)
                else:
                    plain(chat, text)
            except Exception as e:
                log("cmd failed", text, e)
                send(chat, "Something broke on my side; try again in a minute.")

if __name__ == "__main__":
    init_db()
    me = tg("getMe")
    if not me.get("ok"):
        sys.exit("telegram token rejected: " + str(me))
    log("swarm-watch up as @" + me["result"]["username"])
    tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in [
        ("watch", "watch NFT seats, e.g. /watch 7 1234"), ("unwatch", "stop watching"), ("list", "what you watch"),
        ("status", "your seats right now"), ("network", "the swarm right now"), ("allocations", "launch tokens allocated to your seats"), ("drops", "IMD received by your seats' wallets"), ("digest", "daily summary on|off"),
        ("news", "dev's on-chain messages on|off"), ("help", "how it works")]])
    threading.Thread(target=poll_loop, daemon=True).start()
    updates_loop()
