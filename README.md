# MeshCore repeater telemetry

Continuously polls a fleet of MeshCore repeaters for battery and temperature
telemetry through a USB-connected companion radio, stores every reading in
SQLite, and graphs the history in Grafana.

Built to run unattended on a Raspberry Pi.

```
┌──────────┐  USB serial   ┌───────────┐   LoRa mesh   ┌───────────┐
│ collector│──────────────▶│ companion │──────────────▶│ repeaters │
└────┬─────┘ /dev/ttyACM0  └───────────┘  preset paths └───────────┘
     │ writes
     ▼
 data/telemetry.db (SQLite, WAL) ◀── reads ── Grafana :3000
```

## What it does

Every cycle (15 minutes by default) the collector walks the repeater list and,
for each one:

1. forces the contact onto the hard coded route from the config,
2. logs in with the repeater's admin password,
3. sends a telemetry request and waits for the Cayenne LPP response,
4. stores battery voltage, battery percentage, temperature, humidity and
   pressure, plus the raw LPP frame.

Failures are retried up to `polling.attempts` times per cycle. Every cycle
outcome (success or failure with the last error) is recorded in
`poll_attempts`, so the dashboard can show how reliable each path is.

## Requirements

- Raspberry Pi (or any Linux host) with Docker and the Compose plugin
- A MeshCore companion radio on `/dev/ttyACM0`
- Each repeater already in the companion's contact list (advertised at least
  once, e.g. via `meshcore-cli`)

## Setup

```bash
cp config/config.example.yaml config/config.yaml
$EDITOR config/config.yaml          # passwords, public keys, paths
docker compose up -d --build
```

Grafana is then on `http://<pi>:3000` (default login `admin` / `admin`) with
the **MeshCore Repeater Telemetry** dashboard already provisioned.

`config/config.yaml` holds passwords and is git-ignored.

### Serial device

The collector runs unprivileged and joins the group that owns the serial
device. On this Pi that is `plugdev` (gid 46), which is the default. Check
yours and override it if needed:

```bash
stat -c '%G %g' /dev/ttyACM0
echo "SERIAL_GID=46" >> .env
```

Other `.env` settings: `SERIAL_PORT`, `LOG_LEVEL`, `TZ`, `GRAFANA_USER`,
`GRAFANA_PASSWORD`, and `AWS_PROFILE` for optional cloud publishing.

## Public cloud mirror

The CDK app in [`aws/`](./aws) deploys a private S3 bucket, a CloudFront
distribution with origin access control, and a small public status page.
Deploy it and note the outputs:

```bash
cd aws
npm ci
npx cdk deploy
```

Enable publication in `config/config.yaml` with the `BucketName` output:

```yaml
publishing:
  enabled: true
  bucket: <BucketName>
  prefix: data
  region: us-west-2
```

The collector uses boto3's standard credential chain. Docker Compose mounts
the host's `~/.aws` directory read-only at `/home/collector/.aws`; set
`AWS_PROFILE` in `.env` when using a profile other than `default`, and set
`AWS_CONFIG_GID` to the group that owns the directory:

```bash
stat -c '%g' ~/.aws
chmod -R g+rX ~/.aws
```

The collector joins that group because it otherwise runs as uid/gid 472 and
cannot read the usual mode-0600 AWS files. The mount remains read-only inside
the container. The principal only needs `s3:PutObject` on
`arn:aws:s3:::<BucketName>/data/*`; the stack does not create credentials or
an IAM user.

After each successful collection cycle the collector uploads:

- `data/current.json` — latest sanitized readings and status
- `data/summary.json` — repeater list and available-day manifest
- `data/days/YYYY-MM-DD.json.gz` — sanitized readings, status samples, and
  poll outcomes for a UTC day

The first publication after process startup backfills every retained day.
Later cycles update only the current UTC day. Public files never include
repeater keys, routes, raw payloads, passwords, or poll error text. Publishing
failures are logged and do not interrupt local collection.

## Configuration

See [config/config.example.yaml](./config/config.example.yaml) for the full,
commented file. The config is re-read at the start of every cycle, so edits to
passwords, intervals, attempts or the repeater list take effect without a
restart.

| Key | Meaning |
| --- | --- |
| `polling.interval_seconds` | How often a full sweep starts (900 = 15 min) |
| `polling.attempts` | Tries per repeater per cycle before giving up |
| `polling.retry_delay_seconds` | Pause between retries of the same repeater |
| `polling.stagger_seconds` | Pause between different repeaters |
| `polling.min_request_timeout_seconds` | Floor for the firmware-suggested timeout |
| `device.path_hash_mode` | Bytes per hop in a route (see below) |
| `device.tx_power` | Companion TX power in dBm (see below) |
| `polling.always_login` | Re-login before each request instead of reusing authentication |
| `polling.reauthenticate_interval_seconds` | Re-login interval when `always_login` is false (43200 = 12 hours) |
| `storage.retention_days` | Readings older than this are purged (0 = keep forever) |

### Repeaters

```yaml
device:
  path_hash_mode: 1              # 0 = 1 byte per hop, 1 = 2 bytes per hop

repeaters:
  - name: Hilltop North
    public_key: 1a2b3c4d5e6f7a8b   # public key, or any unique prefix
    password: secret
    path: "3f9c11a2"               # hard coded route, hop hashes
```

- `public_key` is the preferred identifier; `name` (the advertised name) is
  used as a fallback and as the label on the dashboard.
- `path` pins the route and the collector re-applies it whenever the contact
  drifts off it:

  | `path` | meaning |
  | --- | --- |
  | `"3f9c11a2"` | pin this explicit route |
  | `""` | pin to direct / zero-hop |
  | `flood` | pin to flood routing |
  | omitted | don't manage routing at all |

  Pinning is not the same as omitting: `path: ""` keeps a direct contact
  direct even if something later resets it to flood, whereas omitting `path`
  lets it drift.
- A path is only written to the companion when it differs from the contact's
  current route, so pinning an already-correct route causes no writes and no
  mesh traffic.

### Path hash mode

A route is a chain of public-key prefixes, one per hop. `path_hash_mode`
decides how wide each prefix is — `0` means 1 byte per hop (`2c`), `1` means
2 bytes per hop (`2cfe`). **Get this wrong and the hops are silently
misparsed**: `2cfeab07` is two hops in mode 1 but four in mode 0, pointing at
nodes that don't exist.

Set `device.path_hash_mode` to match how you wrote your paths (or omit it to
use whatever the companion reports), and override per repeater with
`path_hash_mode:` if a route uses a different convention. Paths that aren't a
whole number of hops are rejected at startup.

### TX power and USB stability on a Raspberry Pi

Some companion radios (e.g. Heltec V4) default to a high TX power (22 dBm).
On a Raspberry Pi, the current spike from transmitting can exceed a USB
port's over-current limit, which drops the serial link for a moment — right
when a login or telemetry request is in flight. This shows up as every
request timing out at exactly `min_request_timeout_seconds`, and
`journalctl -k | grep -iE 'over-current|USB disconnect'` will show the port
tripping and the companion re-enumerating at the same timestamps.

Setting `device.tx_power` (in dBm, 0-30) works around this without touching
any repeater: the collector applies it to the companion on every connect and
reconnect, so it survives companion reboots. 14 dBm is a reasonable starting
point; raise it later if range becomes a problem instead of power stability.

### Checking the config

`--check` resolves every configured repeater against the companion's contact
list and prints the routing it would apply. It is read-only: nothing is
written to the radio and no mesh traffic is generated.

```bash
docker compose run --rm collector python -m telemetry --check
```

```
repeater                   contact  current path     config path      hops  action
SIERRA Snowshoe Lake       yes      <direct>         <direct>         0     ok
SIERRA Camp Connell        yes      2c               2cfe             1     set path
SIERRA Lake Alpine         yes      flood            2cfeab073d44     3     set path
```

Find keys and current paths with the bundled CLI:

```bash
docker compose exec collector meshcore-cli -s /dev/ttyACM0 contacts
```

## Data model

`data/telemetry.db` (SQLite, WAL mode):

- `repeaters` — one row per configured repeater, with `last_attempt` /
  `last_success`
- `readings` — one row per successful poll: `ts` (unix seconds),
  `battery_voltage`, `battery_percent`, `battery_percent_source`,
  `temperature_c`, `humidity`, `pressure`, `attempt`, `raw_lpp`
- `poll_attempts` — one row per repeater per cycle: `success`, `attempts`,
  `error`
- `stats` — one row per successful status request: `ts`, `uptime_s`,
  `airtime_ms`, `rx_airtime_ms`, `noise_floor_dbm`, `last_rssi_dbm`,
  `last_snr_db`, `tx_queue_len`, `nb_sent`, `nb_recv`, `sent_flood`,
  `sent_direct`, `recv_flood`, `recv_direct`, `direct_dups`, `flood_dups`,
  `full_evts`, `recv_errors`, `battery_mv`, `raw_json`
- `latest_readings` / `latest_stats` — views with the most recent
  reading/stats row per repeater

MeshCore telemetry only reports raw cell voltage, not a charge percentage.
`battery_percent` is filled from whatever the repeater reports (marking
`battery_percent_source = 'device'`), or otherwise estimated from voltage
using a Samsung INR18650-35E discharge curve (`battery_percent_source =
'estimated'`, see `telemetry/battery.py`). Treat estimated values as
indicative — real state of charge also depends on discharge rate,
temperature, and cell wear.

Each poll also requests the repeater's status (uptime, airtime, packet
counts, noise floor) alongside telemetry, via the same login session. This
is best-effort: if the status request fails, the poll still counts as a
success as long as telemetry came back, and no `stats` row is written for
that cycle. `uptime_s`/`airtime_ms`/`nb_sent`/`nb_recv` are cumulative
counters that reset on reboot — a drop is a sign the repeater restarted.
Units are as reported by the firmware: uptime in seconds, airtime in
milliseconds, noise floor/RSSI in dBm, SNR in dB.

Query it directly any time; WAL mode means readers never block the collector:

```bash
sqlite3 data/telemetry.db "SELECT name, battery_voltage, datetime(ts,'unixepoch') FROM latest_readings;"
```

## Operating

```bash
docker compose logs -f collector     # watch a polling cycle
docker compose restart collector     # after changing the serial device
docker compose down                  # stop everything
```

The collector reconnects to the companion radio automatically with backoff if
the USB link drops, and both containers restart unless explicitly stopped.

## Running without Docker

```bash
uv sync
MESH_CONFIG=config/config.yaml uv run python -m telemetry
```

Adjust `storage.path` to a local directory first, e.g. `./data/telemetry.db`.
