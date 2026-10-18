# Wide Ticker — virtual display across N TC001 panels

Combines 2+ identically-sized AWTRIX panels (TC001 = 32×8) into one wide
scrolling-text display. No firmware changes: a Berry script on one "master"
device computes a single scroll position and pushes each peer's slice to it
over the existing HTTP API, relying on the fact that `text()`/`draw` text is
clipped at each panel's own edge rather than wrapped.

## Files

- `wide-ticker.berry` — install this on the master device only (`PUT
  /api/v1/apps/script/wideticker`, or paste it into the web UI's script
  editor). Peers need no script at all.
- `orchestrate.py` — installs/configures/pins the above across all devices,
  and can fully revert every device to its prior state. Python 3 stdlib only.

## Usage

```bash
# Take over master + peers, pausing their normal rotation and pinning the ticker
python3 orchestrate.py install --master 192.168.1.21 --peers 192.168.1.22,192.168.1.23 \
    --text "HELLO WORLD" --color "#FFFFFF" --speed 100

# Or: let normal rotation continue - the ticker just takes its usual turn,
# and syncs peers to match only while it's actually showing
python3 orchestrate.py install --master 192.168.1.21 --peers 192.168.1.22,192.168.1.23 \
    --text "HELLO WORLD" --color "#FFFFFF" --speed 100 --rotate

# Check reachability / currently-shown app on each device
python3 orchestrate.py status --master 192.168.1.21 --peers 192.168.1.22,192.168.1.23

# Put every device back exactly as it was before install
python3 orchestrate.py revert --master 192.168.1.21 --peers 192.168.1.22,192.168.1.23
```

### Pinned vs `--rotate` mode

- **Pinned (default):** `autoTransition` is paused on every device and the
  ticker is the sole, continuously-shown app everywhere. Simplest and most
  reliable sync — there's only ever one state to keep in agreement.
- **`--rotate`:** every device's normal app rotation (clock, weather, etc.)
  keeps running untouched. The ticker is just another app in the master's own
  rotation; when the master's turn reaches it (`on_show()`), it immediately
  forces every live peer to the same pushed app (`PUT .../apps/active`), and
  when the master's turn ends (`on_hide()`), it tells peers to move on
  (`POST .../apps/next`) so they resume their own rotation too. Each turn
  lasts exactly one full scroll pass (`duration()`), so the message is never
  cut off mid-word.

  A peer's own `autoTransition` stays on in this mode, so without extra care
  the pushed ticker app would also sit in *that peer's own* independent
  rotation and could be reached on its own schedule, showing whatever stale
  frame was last pushed. The script avoids this: as soon as a peer passes the
  size check, the master switches that app **off** on the peer by name (`PUT
  .../apps/{name}/enabled false`) — this works even before the app exists, so
  it arrives already excluded. `on_show()`/`on_hide()` flip it on only for the
  moment it's actually needed, then back off. This is best-effort: a peer
  that's briefly unreachable or slow to answer may miss a turn's sync (it'll
  simply show its own rotation that turn and catch the next one), and you may
  occasionally see a one-poll-interval skew right at a turn boundary where a
  peer flips a beat before the master's own display does — acceptable for a
  ticker, not meant for anything that must never visibly desync.

`install` snapshots each device's app order/enabled list, `autoTransition`
setting, and currently-active app into `backups/<ip>-<timestamp>.json`
(gitignored) before changing anything, so `revert` is always available even
across separate runs — it uses each device's most recent snapshot.

## Requirements / caveats

- All panels (master + peers) must be the same size. A peer whose
  `/api/v1/capabilities` reports a different `display.width`/`height` is
  logged and dropped, not partially combined.
- No authentication on the mirroring-style push traffic — same caveat as
  built-in Mirroring: only run this on a trusted LAN.
- Peer order in `--peers` defines left-to-right physical placement; there is
  no auto-discovery.
- `install` pins the ticker as the active, continuously-shown app on every
  device (pausing `autoTransition`) — it takes over the clocks while running,
  by design. `revert` restores normal rotation.
