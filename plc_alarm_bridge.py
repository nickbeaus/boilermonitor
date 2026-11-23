#!/usr/bin/env python3
"""
plc_alarm_bridge.py - Boiler monitor: CLICK PLC -> voip.ms SMS + FreePBX calls.

Polls a CLICK PLUS PLC (Modbus TCP) for latched alarm coils, sends SMS via the
voip.ms API and rings FreePBX extensions via AMI, then writes the ACK coil so
the PLC ladder can unlatch.

Configuration comes from a .env file next to this script (copy .env.example).
Real environment variables override .env values. Set BOILERMONITOR_ENV to use
a different file. After changing .env:  sudo systemctl restart boilermonitor
"""
import logging
import logging.handlers
import os
import socket
import sys
import time
from urllib.parse import quote_plus

import requests
from pymodbus.client import ModbusTcpClient

log = logging.getLogger("plc-bridge")


# =============================================================================
#                         CONFIGURATION (.env loader)
# =============================================================================
ENV_FILE = os.environ.get(
    "BOILERMONITOR_ENV",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
CONFIG_ERRORS = []


def load_env_file(path: str) -> bool:
    """Minimal .env reader: KEY=VALUE per line, # comments on their own line,
    optional surrounding quotes. Same rules as systemd EnvironmentFile."""
    if not os.path.exists(path):
        return False
    with open(path) as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                CONFIG_ERRORS.append(f"{path} line {n}: expected KEY=VALUE")
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)   # real env vars win
    return True


def _get(name):
    v = os.environ.get(name)
    return None if v is None or v.strip() == "" else v.strip()


def env_str(name, default=""):
    v = _get(name)
    return default if v is None else v


def env_int(name, default=None):
    v = _get(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        CONFIG_ERRORS.append(f"{name}={v!r} is not a whole number")
        return default


def env_float(name, default=None):
    v = _get(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        CONFIG_ERRORS.append(f"{name}={v!r} is not a number")
        return default


def env_bool(name, default):
    v = _get(name)
    if v is None:
        return default
    if v.lower() in ("1", "true", "yes", "on"):
        return True
    if v.lower() in ("0", "false", "no", "off"):
        return False
    CONFIG_ERRORS.append(f"{name}={v!r} must be true or false")
    return default


def env_list(name):
    v = _get(name)
    return [x.strip() for x in v.split(",") if x.strip()] if v else []


def load_alarms():
    """ALARM_1_*, ALARM_2_*, ... until an ALARM_n_NAME is missing."""
    alarms, n = [], 1
    while _get(f"ALARM_{n}_NAME"):
        p = f"ALARM_{n}_"
        a = {
            "name": env_str(p + "NAME"),
            "group": env_str(p + "GROUP") or None,
            "alarm_coil": env_int(p + "COIL"),
            "ack_coil": env_int(p + "ACK_COIL"),
            "message": env_str(p + "MESSAGE"),
            "sms": env_list(p + "SMS"),
            "extensions": env_list(p + "EXTENSIONS"),
            "recording": env_str(p + "RECORDING", "custom/plc-alarm"),
        }
        if _get(p + "COOLDOWN") is not None:
            a["cooldown"] = env_int(p + "COOLDOWN", 0)
        alarms.append(a)
        n += 1
    return alarms


ENV_FILE_FOUND = load_env_file(ENV_FILE)

# ---- Notification channels
SMS_ENABLED = env_bool("SMS_ENABLED", True)
CALLS_ENABLED = env_bool("CALLS_ENABLED", True)

# ---- PLC
PLC_HOST = env_str("PLC_HOST")
PLC_PORT = env_int("PLC_PORT", 502)
POLL_SECONDS = env_float("POLL_SECONDS", 1.0)
COMMS_ALERT_AFTER_SECONDS = env_int("COMMS_ALERT_AFTER_SECONDS", 60)

# ---- voip.ms
VOIPMS_API_USERNAME = env_str("VOIPMS_API_USERNAME")
VOIPMS_API_PASSWORD = env_str("VOIPMS_API_PASSWORD")
VOIPMS_DID = env_str("VOIPMS_DID")
SMS_TIMEOUT_SECONDS = env_int("SMS_TIMEOUT_SECONDS", 45)

# ---- FreePBX AMI
AMI_HOST = env_str("AMI_HOST", "127.0.0.1")
AMI_PORT = env_int("AMI_PORT", 5038)
AMI_USERNAME = env_str("AMI_USERNAME")
AMI_SECRET = env_str("AMI_SECRET")
CALLERID_NAME = env_str("CALLERID_NAME", "PLC Alarm")
CALLERID_NUM = env_str("CALLERID_NUM", "900")
CALL_RING_SECONDS = env_int("CALL_RING_SECONDS", 30)

# ---- Repeat suppression
COOLDOWNS_ENABLED = env_bool("COOLDOWNS_ENABLED", True)
DEFAULT_COOLDOWN_SECONDS = env_int("DEFAULT_COOLDOWN_SECONDS", 300)

# ---- Logging
LOG_LEVEL = env_str("LOG_LEVEL", "INFO").upper()
LOG_FILE = env_str("LOG_FILE") or None

# ---- Comms lost / restored
COMMS_SMS = env_list("COMMS_SMS")
COMMS_EXTENSIONS = env_list("COMMS_EXTENSIONS")
COMMS_RECORDING = env_str("COMMS_RECORDING", "custom/plc-comms-lost")
COMMS_LOST_MESSAGE = env_str("COMMS_LOST_MESSAGE", "PLC comms LOST - not monitoring")
COMMS_RESTORED_MESSAGE = env_str("COMMS_RESTORED_MESSAGE", "PLC comms restored")

# ---- Alarms
ALARMS = load_alarms()


def validate_config() -> bool:
    """Log every problem at once so a bad .env is fixed in one pass."""
    errors = list(CONFIG_ERRORS)
    if not ENV_FILE_FOUND:
        log.warning("No .env file at %s - using environment variables only", ENV_FILE)
    if not PLC_HOST:
        errors.append("PLC_HOST is not set")
    if not ALARMS:
        errors.append("No alarms defined (need at least ALARM_1_NAME)")
    names = [a["name"] for a in ALARMS]
    if len(names) != len(set(names)):
        errors.append("Alarm names must be unique")
    for i, a in enumerate(ALARMS, 1):
        for field, var in (("alarm_coil", "COIL"), ("ack_coil", "ACK_COIL"), ("message", "MESSAGE")):
            if not a[field]:
                errors.append(f"ALARM_{i}_{var} is not set ({a['name']})")
        if len(a["message"]) > 160:
            log.warning("ALARM_%d_MESSAGE is %d chars - SMS will be cut to 160",
                        i, len(a["message"]))
    if SMS_ENABLED:
        for var in ("VOIPMS_API_USERNAME", "VOIPMS_API_PASSWORD", "VOIPMS_DID"):
            if not globals()[var]:
                errors.append(f"{var} is not set (or set SMS_ENABLED=false)")
    if CALLS_ENABLED:
        for var in ("AMI_USERNAME", "AMI_SECRET"):
            if not globals()[var]:
                errors.append(f"{var} is not set (or set CALLS_ENABLED=false)")
    for e in errors:
        log.error("CONFIG: %s", e)
    return not errors


# =============================================================================
#                                  BRIDGE
# =============================================================================
CLICK_C_BASE = 16384  # CLICK C1 = Modbus coil 016385 -> 0-based 16384


def c_addr(c_num: int) -> int:
    """Convert CLICK 'C' bit number (C1, C2...) to 0-based Modbus coil address."""
    return CLICK_C_BASE + (c_num - 1)


# ---------------------------------------------------------------- voip.ms SMS
def send_sms(dst: str, message: str) -> bool:
    params = {
        "api_username": VOIPMS_API_USERNAME,
        "api_password": VOIPMS_API_PASSWORD,
        "method": "sendSMS",
        "did": VOIPMS_DID,
        "dst": dst,
        "message": message[:160],
    }
    try:
        r = requests.get("https://voip.ms/api/v1/rest.php", params=params,
                         timeout=SMS_TIMEOUT_SECONDS)
        data = r.json()
        if data.get("status") == "success":
            log.info("    SMS sent to %s", dst)
            return True
        log.error("    SMS to %s FAILED: voip.ms status=%s", dst, data.get("status"))
    except Exception as e:
        # requests errors include the full URL - never let the API password reach the logs
        msg = str(e)
        for secret in (VOIPMS_API_PASSWORD, quote_plus(VOIPMS_API_PASSWORD)):
            if secret:
                msg = msg.replace(secret, "********")
        log.error("    SMS to %s FAILED: %s: %s", dst, type(e).__name__, msg[:300])
    return False


# ------------------------------------------------------------- FreePBX / AMI
def ami_originate(extension: str, recording: str) -> bool:
    """Ring an extension; when answered, play a System Recording (twice)."""
    msg = (
        "Action: Login\r\n"
        f"Username: {AMI_USERNAME}\r\n"
        f"Secret: {AMI_SECRET}\r\n"
        "Events: off\r\n\r\n"
        "Action: Originate\r\n"
        f"Channel: Local/{extension}@from-internal\r\n"
        "Application: Playback\r\n"
        f"Data: {recording}&{recording}\r\n"
        f"CallerID: \"{CALLERID_NAME}\" <{CALLERID_NUM}>\r\n"
        f"Timeout: {CALL_RING_SECONDS * 1000}\r\n"
        "Async: true\r\n\r\n"
        "Action: Logoff\r\n\r\n"
    )
    try:
        with socket.create_connection((AMI_HOST, AMI_PORT), timeout=10) as s:
            s.recv(1024)  # banner
            s.sendall(msg.encode())
            time.sleep(0.5)
            resp = s.recv(4096).decode(errors="ignore")
        if "Authentication accepted" not in resp:
            log.error("    Call to ext %s FAILED: AMI login rejected", extension)
            return False
        log.info("    Call placed to ext %s", extension)
        return True
    except Exception as e:
        log.error("    Call to ext %s FAILED: %s (is the SSH tunnel up?)", extension, e)
        return False


# ------------------------------------------------------------------ notifier
def notify(label: str, text: str, sms_to: list, extensions: list, recording: str):
    log.warning("NOTIFY [%s]: \"%s\"", label, text)

    if sms_to:
        if SMS_ENABLED:
            ok = sum(send_sms(num, text) for num in sms_to)
            log.log(logging.INFO if ok == len(sms_to) else logging.ERROR,
                    "  SMS:   %d/%d sent", ok, len(sms_to))
        else:
            log.warning("  SMS:   DISABLED (SMS_ENABLED = False) - skipped %s", ", ".join(sms_to))

    if extensions:
        if CALLS_ENABLED:
            ok = sum(ami_originate(ext, recording) for ext in extensions)
            log.log(logging.INFO if ok == len(extensions) else logging.ERROR,
                    "  Calls: %d/%d placed (%s)", ok, len(extensions), recording)
        else:
            log.warning("  Calls: DISABLED (CALLS_ENABLED = False) - skipped ext %s",
                        ", ".join(extensions))

    if not (SMS_ENABLED and sms_to) and not (CALLS_ENABLED and extensions):
        log.warning("  NOBODY WAS NOTIFIED for [%s]", label)


# ----------------------------------------------------------------- logging
def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    handlers = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        handlers.append(logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5_000_000, backupCount=5))
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    # pymodbus logs every failed reconnect; we do our own (rate-limited) logging
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


def channels() -> str:
    return f"SMS={'ON' if SMS_ENABLED else 'OFF'} calls={'ON' if CALLS_ENABLED else 'OFF'}"


# ---------------------------------------------------------------- main loop
def main():
    setup_logging()
    if not validate_config():
        sys.exit(1)
    log.info("Bridge starting: PLC %s:%s, %d alarm(s): %s", PLC_HOST, PLC_PORT, len(ALARMS),
             ", ".join(f"{a['name']} (C{a['alarm_coil']})" for a in ALARMS))
    if SMS_ENABLED and CALLS_ENABLED:
        log.info("Notifications: %s", channels())
    else:
        log.warning("Notifications: %s  <-- some channels are switched OFF", channels())
    if not COOLDOWNS_ENABLED:
        log.warning("Cooldowns DISABLED: every event is sent immediately (no repeat suppression)")

    client = ModbusTcpClient(PLC_HOST, port=PLC_PORT, timeout=3)
    last_ok = time.time()
    last_reminder = time.time()
    connected_once = False
    comms_alerted = False
    fail_logged_at = None   # rate-limit "poll failed" log lines during an outage
    last_sent = {}          # alarm name -> epoch of last notification
    last_latched = {}       # alarm name -> epoch the most recent occurrence was first seen
    pending = {}            # alarm name -> {"seen", "acked", "held_logged"}

    def ack(a):
        client.write_coil(c_addr(a["ack_coil"]), True)
        log.info("  ACK -> PLC C%d (clears C%d)", a["ack_coil"], a["alarm_coil"])

    while True:
        if not (SMS_ENABLED and CALLS_ENABLED) and time.time() - last_reminder >= 3600:
            log.warning("REMINDER: notifications partly disabled (%s)", channels())
            last_reminder = time.time()
        try:
            if not client.connected:
                client.connect()

            # 1) read every alarm coil
            states = {}
            for a in ALARMS:
                rr = client.read_coils(c_addr(a["alarm_coil"]), count=1)
                if rr.isError():
                    raise ConnectionError(f"read error on C{a['alarm_coil']}: {rr}")
                states[a["name"]] = bool(rr.bits[0])

            if not connected_once:
                log.info("Connected to PLC; monitoring")
                connected_once = True

            # 2) decide what to do with each one
            now = time.time()
            for a in ALARMS:
                name = a["name"]
                if not states[name]:
                    if pending.pop(name, None):
                        log.debug("PLC cleared C%d (%s)", a["alarm_coil"], name)
                    continue

                if name not in pending:               # new occurrence
                    pending[name] = {"seen": now, "acked": False, "held_logged": False}
                    last_latched[name] = now
                    log.warning("EVENT: %s (PLC C%d latched)", name, a["alarm_coil"])
                p = pending[name]
                if p["acked"]:
                    continue                          # waiting for PLC to clear it

                # Superseded? e.g. a held NORMAL when pressure has since gone LOW again.
                group = a.get("group")
                newer = [o["name"] for o in ALARMS
                         if group and o.get("group") == group and o["name"] != name
                         and last_latched.get(o["name"], 0) > p["seen"]]
                if newer:
                    log.info("  %s superseded by newer %s - not sending", name, newer[0])
                    ack(a)
                    p["acked"] = True
                    continue

                if COOLDOWNS_ENABLED:
                    wait = a.get("cooldown", DEFAULT_COOLDOWN_SECONDS) - (now - last_sent.get(name, 0))
                else:
                    wait = 0
                if wait <= 0:
                    notify(name, a["message"], a.get("sms", []),
                           a.get("extensions", []), a.get("recording", "custom/plc-alarm"))
                    last_sent[name] = now
                    ack(a)                            # PLC resets alarm + ack coils
                    p["acked"] = True
                elif not p["held_logged"]:
                    log.warning("  %s held: cooldown, will send in %ds unless superseded",
                                name, max(1, int(wait + 0.999)))
                    p["held_logged"] = True

            last_ok = time.time()
            fail_logged_at = None
            if comms_alerted:
                log.warning("PLC comms RESTORED")
                notify("Comms restored", COMMS_RESTORED_MESSAGE, COMMS_SMS, [], "")
                comms_alerted = False

        except Exception as e:
            client.close()
            now = time.time()
            if fail_logged_at is None or now - fail_logged_at >= 300:
                log.error("PLC poll failed (repeats suppressed for 5 min): %s", e)
                fail_logged_at = now
            if not comms_alerted and now - last_ok > COMMS_ALERT_AFTER_SECONDS:
                log.error("PLC comms LOST for > %ss", COMMS_ALERT_AFTER_SECONDS)
                notify("Comms lost", COMMS_LOST_MESSAGE, COMMS_SMS,
                       COMMS_EXTENSIONS, COMMS_RECORDING)
                comms_alerted = True

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
