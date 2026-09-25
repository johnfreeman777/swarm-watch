# Swarm Watch

A Telegram bot that watches IdentityMD swarm seats from public data. Unofficial,
read-only. It never asks for keys, wallets or access to anyone's machine.

**Bot:** [@imd_swarm_watch_bot](https://t.me/imd_swarm_watch_bot)

```
/watch 7 1234     watch these NFT seats
/status            your seats right now: rank, accepted, rejected, pending, last work
/network           the swarm right now: online, accepted in 24 h, paid orders
/allocations       launch tokens allocated to your seats' wallets, claimed or not, with claim links
/drops             IMD that arrived in those wallets (the dev's airdrops to active nodes)
/digest on|off     daily summary at 08:00 UTC
/news on|off       the dev's on-chain messages as they land
```

Every reply carries a button bar (Status, Network, Watch, Unwatch, and toggles for the
daily digest and dev news), so after `/start` nothing needs typing except NFT numbers.

Alerts, per watched seat:

- **stalled** — no work taken for 2 h while at least half the fleet took some. When the whole
  network is quiet, silence is normal and nothing fires.
- **rejections** — three or more new rejections within a day.
- **gone** — the seat disappeared from the network's contributor list.
- **allocation** — a new launch allocated tokens to the seat's wallet (amount, share, claim
  link), and a note when it gets claimed. From the
  [Swarm Ledger](https://johnfreeman777.github.io/swarm-ledger/) snapshot, every 30 min.
- **drop** — IMD arrived in the seat's wallet on mainnet, e.g. the dev's airdrops to active
  nodes. Each watched wallet's incoming IMD transfers are read from a public explorer, a few
  wallets a minute in rotation; history seen before you subscribed is never announced.

What it cannot see: pauses from the control plane and the reasons behind failed runs.
Those are shown only to the node itself by `imd doctor`. For that there is
`imd-doctor-alert`, a small script an operator runs on their own server (below).

## How it works

`swarmwatch.py` (standard library only) polls `api.imd.fun/contributors` and `/health`
once a minute, keeps three days of per-seat history in sqlite, and reads the collection
owner's self-transactions from a public block explorer to relay on-chain messages. One
process, one sqlite file, no other dependencies.

If the network API stops answering for 10 minutes, or the block explorer for 2 hours, the
bot tells its admin chat once and again when the source is back; while data is stale it
does not raise seat alerts, since stale counts would look like silence.

Seats are merged per NFT: the API lists one entry per paired device, so an NFT moved to a
new machine appears twice there.

## Run your own

```sh
useradd -r -s /usr/sbin/nologin swarmwatch
mkdir -p /opt/swarmwatch /etc/swarmwatch /var/lib/swarmwatch
cp swarmwatch.py /opt/swarmwatch/
cp swarmwatch.service /etc/systemd/system/
chown swarmwatch:swarmwatch /var/lib/swarmwatch
# token from @BotFather, readable by the service user only
umask 077; read -rs t; printf '%s' "$t" > /etc/swarmwatch/token; chown swarmwatch /etc/swarmwatch/token
systemctl enable --now swarmwatch
```

## Operator alerts (`imd-doctor-alert`)

For the machine that runs your nodes. What the network says about a machine (a pause,
the failed runs and their reasons) is shown only to that machine by `imd doctor`, so no
public bot can watch it for you. This script does it locally: hourly from root cron, it
runs `imd doctor` for each worker user and sends only real problems to your Telegram: a
pause from the control plane, three or more failed runs in a day, a disconnected daemon,
a stale release, a stopped service, a full disk. It repeats the same problem set at most
every six hours and says nothing when everything is fine.

It works with **your own** bot, so you never share a token with anyone: make one at
@BotFather (one minute), send it `/start`, get your chat id from @userinfobot.

```sh
curl -fsSLO https://raw.githubusercontent.com/johnfreeman777/swarm-watch/main/imd-doctor-alert
install -m 755 imd-doctor-alert /usr/local/bin/
umask 077; printf 'TOKEN=%s\nCHAT=%s\n' "<bot token>" "<chat id>" > /etc/imd-doctor-alert.conf
DRY_RUN=1 IMD_USERS="imd1 imd2" /usr/local/bin/imd-doctor-alert      # prints what it would send
echo '17 * * * * root IMD_USERS="imd1 imd2" /usr/local/bin/imd-doctor-alert' > /etc/cron.d/imd-doctor-alert
```

Replace `imd1 imd2` with the users your daemons run as (one user per NFT, as in the
[node guide](https://github.com/johnfreeman777/imd-node-guide)).

Not affiliated with the IdentityMD developer.
