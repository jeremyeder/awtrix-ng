#!/usr/bin/env python3
"""Install/revert the wide-ticker virtual display across AWTRIX NG devices.

Talks to each device's existing HTTP API only (no firmware changes). Before
touching anything, it snapshots each device's app order/enabled list,
autoTransition setting and currently-active app, so `revert` can put every
device back exactly as it was.

Usage:
    python3 orchestrate.py install --master <ip> --peers <ip1,ip2,...> \
        [--text "HELLO WORLD"] [--color "#FFFFFF"] [--speed 100] \
        [--tick-ms 120] [--app-name wideticker] [--gap 16]
    python3 orchestrate.py revert --master <ip> --peers <ip1,ip2,...> \
        [--app-name wideticker]
    python3 orchestrate.py status --master <ip> --peers <ip1,ip2,...>
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_NAME = "wideticker"
SCRIPT_FILE = Path(__file__).with_name("wide-ticker.berry")
BACKUP_DIR = Path(__file__).with_name("backups")
TIMEOUT = 5


def request(ip, method, path, body=None, raw=False, headers=None):
    url = f"http://{ip}{path}"
    data = None
    if body is not None:
        data = body if raw else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if not raw and body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw_body = resp.read()
            try:
                return resp.status, json.loads(raw_body)
            except json.JSONDecodeError:
                return resp.status, raw_body.decode(errors="replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except urllib.error.URLError as e:
        return 0, str(e)


def get(ip, path):
    return request(ip, "GET", path)


def put_json(ip, path, body):
    return request(ip, "PUT", path, body=body)


def patch_json(ip, path, body):
    return request(ip, "PATCH", path, body=body)


def delete(ip, path):
    return request(ip, "DELETE", path)


def put_raw(ip, path, text):
    return request(ip, "PUT", path, body=text.encode(), raw=True)


def derive_apploop(ip):
    """Fall back for a device that has never had a custom order saved: the
    static /apploop.json file is written lazily on first PUT .../apps/order,
    so a fresh device 404s there. Reconstruct the same shape from /api/v1/apps."""
    status, apps = get(ip, "/api/v1/apps")
    if status != 200 or not isinstance(apps, list):
        raise RuntimeError(f"{ip}: could not read /api/v1/apps (status {status})")
    order = [a["name"] for a in sorted(apps, key=lambda a: (a.get("slot") is None, a.get("slot")))
             if a.get("enabled")]
    disabled = [a["name"] for a in apps if not a.get("enabled")]
    return {"order": order, "disabled": disabled}


def snapshot(ip):
    status, apploop = get(ip, "/apploop.json")
    if status == 404:
        apploop = derive_apploop(ip)
    elif status != 200:
        raise RuntimeError(f"{ip}: could not read /apploop.json (status {status})")
    status, settings = get(ip, "/api/v1/settings")
    if status != 200:
        raise RuntimeError(f"{ip}: could not read /api/v1/settings (status {status})")
    status, device = get(ip, "/api/v1/device")
    if status != 200:
        raise RuntimeError(f"{ip}: could not read /api/v1/device (status {status})")
    snap = {
        "ip": ip,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "apploop": apploop,
        "autoTransition": settings.get("autoTransition"),
        "enlargeApps": settings.get("enlargeApps"),
        "currentApp": device.get("currentApp"),
    }
    BACKUP_DIR.mkdir(exist_ok=True)
    out = BACKUP_DIR / f"{ip.replace(':', '_')}-{int(time.time())}.json"
    out.write_text(json.dumps(snap, indent=2))
    print(f"  snapshot saved: {out}")
    return snap


def latest_snapshot(ip):
    candidates = sorted(BACKUP_DIR.glob(f"{ip.replace(':', '_')}-*.json"))
    if not candidates:
        raise RuntimeError(f"{ip}: no snapshot found under {BACKUP_DIR}")
    return json.loads(candidates[-1].read_text())


def cmd_install(args):
    peers = [p for p in args.peers.split(",") if p]
    devices = [args.master] + peers

    # The address this tool uses to administer a peer (snapshot/delete/config)
    # is not always the address the master's own script should use to reach
    # that peer - e.g. in this repo's Docker-based test setup, the orchestrator
    # (running on the host) reaches a peer container via its published
    # 127.0.0.1:<port>, but the master container must reach that same peer via
    # its Docker-internal IP:port instead. On a real LAN these are identical,
    # so --script-peers defaults to --peers.
    script_peers = [p for p in (args.script_peers or args.peers).split(",") if p]
    if len(script_peers) != len(peers):
        sys.exit("--script-peers must list the same number of addresses as --peers")

    print("Snapshotting current state (for revert)...")
    for ip in devices:
        snapshot(ip)

    print(f"Installing {SCRIPT_NAME} on master {args.master}...")
    source = SCRIPT_FILE.read_text()
    status, resp = put_raw(args.master, f"/api/v1/apps/script/{SCRIPT_NAME}", source)
    if status != 200:
        sys.exit(f"  FAILED to install script: status {status} {resp}")
    if resp.get("error"):
        sys.exit(f"  script installed but has an error: {resp['error']}")
    print("  script installed")

    config = {
        "peers": ",".join(script_peers),
        "tickerText": args.text,
        "textColor": args.color,
        "speedPct": args.speed,
        "tickMs": args.tick_ms,
        "appName": args.app_name,
        "gapPx": args.gap,
        "pinActive": not args.rotate,
    }
    status, resp = patch_json(args.master, f"/api/v1/apps/{SCRIPT_NAME}/config", config)
    if status != 200:
        sys.exit(f"  FAILED to configure script: status {status} {resp}")
    print("  script configured:", config)

    # enlargeApps:false matters on any panel taller than one small-font line:
    # left on (the device default), a pushed app's draw-text renders larger
    # than the master's own text() of the identical content, breaking the
    # seam between panels. Both must render the same font size. This applies
    # in both modes.
    print("Disabling auto-enlarge on every device...")
    for ip in devices:
        status, resp = patch_json(ip, "/api/v1/settings", {"enlargeApps": False})
        if status != 200:
            print(f"  WARNING: {ip} settings patch failed (status {status})")

    if args.rotate:
        print(f"Rotation mode: {SCRIPT_NAME} will take its normal turn in rotation on the "
              f"master, and sync peers to match only while it's showing.")
        return

    print("Pausing automatic app rotation on every device (pinned mode)...")
    for ip in devices:
        status, resp = patch_json(ip, "/api/v1/settings", {"autoTransition": False})
        if status != 200:
            print(f"  WARNING: {ip} autoTransition patch failed (status {status})")

    print(f"Pinning {SCRIPT_NAME} active on master...")
    status, resp = put_json(args.master, "/api/v1/apps/active", {"name": SCRIPT_NAME, "fast": True})
    if status != 200:
        print(f"  WARNING: could not activate {SCRIPT_NAME} on master (status {status})")

    print("Waiting for peers to receive their first frame...")
    time.sleep(2)
    for ip in peers:
        status, apps = get(ip, "/api/v1/apps")
        present = isinstance(apps, list) and any(a.get("name") == args.app_name for a in apps)
        if not present:
            print(f"  WARNING: {ip} has not received a {args.app_name} frame yet "
                  f"(size mismatch, or unreachable - check the master's script log)")
            continue
        status, resp = put_json(ip, "/api/v1/apps/active", {"name": args.app_name, "fast": True})
        if status != 200:
            print(f"  WARNING: could not activate {args.app_name} on {ip} (status {status})")
        else:
            print(f"  {ip} pinned active")

    print("Done. The script's own self-pin loop keeps peers pinned afterward.")


def cmd_revert(args):
    peers = [p for p in args.peers.split(",") if p]
    devices = [args.master] + peers

    print(f"Removing {SCRIPT_NAME} script from master {args.master}...")
    status, resp = delete(args.master, f"/api/v1/apps/{SCRIPT_NAME}")
    print(f"  status {status}")

    for ip in peers:
        print(f"Removing pushed app {args.app_name} from {ip}...")
        status, resp = delete(ip, f"/api/v1/apps/{args.app_name}")
        print(f"  status {status}")

    for ip in devices:
        print(f"Restoring {ip} from snapshot...")
        snap = latest_snapshot(ip)

        status, resp = patch_json(ip, "/api/v1/settings",
                                   {"autoTransition": snap["autoTransition"],
                                    "enlargeApps": snap["enlargeApps"]})
        print(f"  autoTransition -> {snap['autoTransition']}, "
              f"enlargeApps -> {snap['enlargeApps']}: status {status}")

        status, resp = put_json(ip, "/api/v1/apps/order", snap["apploop"])
        print(f"  app order restored: status {status}")

        if snap["currentApp"]:
            status, resp = put_json(ip, "/api/v1/apps/active",
                                     {"name": snap["currentApp"], "fast": True})
            print(f"  currentApp -> {snap['currentApp']}: status {status}")

    print("Revert complete.")


def cmd_status(args):
    peers = [p for p in args.peers.split(",") if p]
    for ip in [args.master] + peers:
        status, device = get(ip, "/api/v1/device")
        if status == 200:
            print(f"{ip}: reachable, currentApp={device.get('currentApp')}")
        else:
            print(f"{ip}: unreachable (status {status})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--master", required=True, help="IP of the device running the script")
    common.add_argument("--peers", default="", help="Comma-separated peer IPs, left to right, no spaces")

    p_install = sub.add_parser("install", parents=[common])
    p_install.add_argument("--text", default="HELLO WORLD")
    p_install.add_argument("--color", default="#FFFFFF")
    p_install.add_argument("--speed", type=int, default=100)
    p_install.add_argument("--tick-ms", type=int, default=120)
    p_install.add_argument("--app-name", default=SCRIPT_NAME)
    p_install.add_argument("--gap", type=int, default=16)
    p_install.add_argument("--rotate", action="store_true",
                            help="Let normal app rotation continue on every device; the ticker "
                                 "just takes its usual turn and syncs peers only while showing, "
                                 "instead of permanently taking over every panel.")
    p_install.add_argument("--script-peers", default=None,
                            help="Addresses the master's own script uses to reach each peer, "
                                 "if different from --peers (e.g. Docker-internal IPs). "
                                 "Same order and count as --peers; defaults to --peers.")
    p_install.set_defaults(func=cmd_install)

    p_revert = sub.add_parser("revert", parents=[common])
    p_revert.add_argument("--app-name", default=SCRIPT_NAME)
    p_revert.set_defaults(func=cmd_revert)

    p_status = sub.add_parser("status", parents=[common])
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
