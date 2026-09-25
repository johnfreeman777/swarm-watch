# Swarm Watch

A Telegram bot that watches IdentityMD swarm seats from public data. Unofficial,
read-only. It never asks for keys, wallets or access to anyone's machine.

**Bot:** [@imd_swarm_watch_bot](https://t.me/imd_swarm_watch_bot)

```
/watch 7 1234     watch these NFT seats
/status            your seats right now: rank, accepted, rejected, pending, last work
/network           the swarm right now: online, accepted in 24 h, paid orders
/digest on|off     daily summary at 08:00 UTC
/news on|off       the dev's on-chain messages as they land
```

Alerts, per watched seat:

- **stalled** — no work taken for 2 h while at least half the fleet took some. When the whole
  network is quiet, silence is normal and nothing fires.
- **rejections** — three or more new rejections within a day.
- **gone** — the seat disappeared from the network's contributor list.

What it cannot see: pauses from the control plane and the reasons behind failed runs.
Those are shown only to the node itself by `imd doctor`. For that there is
`imd-doctor-alert`, a small script an operator runs on their own server (below).

## How it works

`swarmwatch.py` (standard library only) polls `api.imd.fun/contributors` and `/health`
once a minute, keeps three days of per-seat history in sqlite, and reads the collection
owner's self-transactions from a public block explorer to relay on-chain messages. One
process, one sqlite file, no other dependencies.

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

For the machine that runs your nodes. Hourly from root cron, it runs `imd doctor` for
each worker user and sends only real problems to one chat: a pause from the control
plane, three or more failed runs in a day, a disconnected daemon, a stale release, a
stopped service, a full disk. It repeats the same problem set at most every six hours
and says nothing when everything is fine.

```sh
cp imd-doctor-alert /usr/local/bin/ && chmod +x /usr/local/bin/imd-doctor-alert
echo "<your chat id>" > /etc/swarmwatch/admin      # from @userinfobot
echo '17 * * * * root IMD_USERS="imd1 imd2" /usr/local/bin/imd-doctor-alert' > /etc/cron.d/imd-doctor-alert
```

It reuses the same bot token, so one bot serves both the public and your own alerts.

Not affiliated with the IdentityMD developer.
