# Boiler monitor

Watches a boiler pressure switch with an AutomationDirect CLICK PLUS PLC and
alerts people by **SMS (voip.ms)** and **phone calls (FreePBX)** when pressure
goes low, returns to normal, or the PLC stops responding.

```
pressure switch ─► CLICK PLUS PLC ◄─ Modbus TCP ─ plc_alarm_bridge.py ─┬─► voip.ms API ─► SMS
                   (latches events)   (poll + ack)                      └─► SSH tunnel ─► FreePBX AMI ─► calls
```

The PLC latches each event in a coil and holds it until the bridge confirms it
sent notifications, so an alarm that happens while the network or the bridge
is down is delivered when it comes back. The bridge also alerts if it can't
reach the PLC.

## Files

| File | Purpose |
|---|---|
| `plc_alarm_bridge.py` | The bridge (runs as a service) |
| `.env.example` | All settings, documented. Copy to `.env` |
| `fake_plc.py` | Simulated PLC for testing without hardware (stdlib only) |
| `ami_test.py` | Step-by-step FreePBX AMI login and test call (stdlib only) |
| `systemd/` | Service units for the bridge and the AMI SSH tunnel |

## Install (Ubuntu)

```bash
sudo apt install -y python3-venv mbpoll
git clone https://github.com/nickbeaus/boilermonitor ~/boilermonitor && cd ~/boilermonitor
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
nano .env
```

`.env` holds credentials and phone numbers and is ignored by git. Never commit it.

## Configuration

Everything is in `.env`; see the comments in `.env.example`. Real environment
variables override `.env`, and `BOILERMONITOR_ENV=/path/to/file` selects a
different file. Format rules (shared with systemd `EnvironmentFile`): one
`KEY=VALUE` per line, comments on their own line only, quote values with
spaces, comma-separate lists.

Alarms are numbered `ALARM_1_*`, `ALARM_2_*`, ... with no gaps. Each alarm
maps a PLC coil to a message and recipients. Every extension in
`ALARM_n_EXTENSIONS` gets its own call, so one person answering doesn't stop
the others ringing.

The bridge checks the whole configuration at startup and lists every problem
at once.

## PLC side

The bridge talks Modbus TCP (port 502) and only uses C bits:

| Bit | Set by | Meaning |
|---|---|---|
| C1 | PLC | Pressure went low |
| C2 | PLC | Pressure returned to normal |
| C101 | Bridge | C1 handled; PLC resets C1 and C101 |
| C102 | Bridge | C2 handled; PLC resets C2 and C102 |

Ladder outline (pressure switch on X001, closed = low; test button on X002):

```
1  [X001]--+--(TMR T1 5s)          low or test, debounced
   [X002]--+
2  [/X001]-[/X002]--(TMR T2 10s)    normal, debounced
3  [T1]--(SET C10)                 C10 = confirmed low
4  [T2]--(RST C10)
5  [↑C10]--(SET C1)                event: low
6  [↓C10]--(SET C2)                event: normal
7  [C101]--(RST C1)(RST C101)      ack from bridge
8  [C102]--(RST C2)(RST C102)
9  [X001]--+--(OUT Y001)           local beacon, no network needed
   [X002]--+
10 [C1]--+--(TMR T3 600s)          watchdog: unacknowledged event
   [C2]--+
11 [T3]--(OUT Y002)                "notifications failing" lamp
```

Make C1, C2 and C10 retentive. A switch that closes on low pressure is not
fail-safe (a cut wire reads as normal); use a contact that opens on low if the
switch has one, and invert rungs 1, 2 and 9.

## FreePBX side

1. **Settings → Asterisk Manager Users**: add a user with write `originate`,
   `call` and read `call`; Permit `127.0.0.1/255.255.255.255`. Apply Config.
2. **Admin → System Recordings**: create the recordings named in `.env`
   (e.g. `pressure-low` → `custom/pressure-low`).
3. AMI stays bound to the PBX's localhost; the bridge reaches it through an
   SSH tunnel. On the bridge host:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/pbx_tunnel -N ""
   ```
   On the PBX, append the public key to the tunnel user's `authorized_keys`,
   restricted so it can only forward to AMI:
   ```
   from="<bridge-ip>",restrict,port-forwarding,permitopen="127.0.0.1:5038" ssh-ed25519 AAAA... 
   ```
   Set `PBX_SSH_TARGET` and `PBX_SSH_KEY` in `.env`.

## voip.ms side

Under **Main Menu → SOAP and REST/JSON API**: set an API password, enable the
API, and whitelist the bridge's public IP. Enable SMS on the DID
(**Manage DIDs → Message Service**, dialing mode NANPA). Default API limit is
100 SMS/day, and automated alerting is A2P traffic, which carriers may filter
until it's registered with voip.ms.

## Testing

```bash
# Simulated PLC: set PLC_HOST=127.0.0.1, PLC_PORT=5020 in .env
python3 fake_plc.py            # l=low n=normal o=offline u=online s=status

# FreePBX login + test call (tunnel must be up)
set -a; . ./.env; set +a
python3 ami_test.py --host 127.0.0.1 --user "$AMI_USERNAME" --ext 1001

# One test text
venv/bin/python -c "import logging, plc_alarm_bridge as b; logging.basicConfig(level='INFO'); b.send_sms('5555550123', 'test')"

# Real PLC coil C1
mbpoll -m tcp -a 1 -t 0 -r 16385 -c 1 -1 <plc-ip>

# Bridge in the foreground
venv/bin/python plc_alarm_bridge.py
```

## Run as a service

Edit `User=` and the paths in both files under `systemd/`, then:

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now boilermonitor-tunnel boilermonitor
journalctl -u boilermonitor -f
```

Restart `boilermonitor` after changing `.env`. If the tunnel drops, texts
still go out; calls resume when it reconnects.
