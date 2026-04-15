#!/bin/sh
set -eux

if ! command -v incus >/dev/null 2>&1; then
  echo "root:1000000:1000000000" >> /etc/subuid
  echo "root:1000000:1000000000" >> /etc/subgid
  dnf install -y --setopt install_weak_deps=False incus incus-client
  systemctl enable --now incus
  mkdir -p /var/cache/locki
  incus admin init --preseed << '__LOCKI_EOF__'
storage_pools:
  - name: default
    driver: dir
networks:
  - name: incusbr0
    type: bridge
    config:
      ipv4.address: 10.99.0.1/24
      ipv4.nat: "true"
      ipv6.address: none
profiles:
  - name: default
    config:
      security.nesting: "true"
      security.privileged: "true"
      raw.lxc: |
        lxc.mount.auto = proc:rw sys:rw
        lxc.cap.drop =
    devices:
      root:
        path: /
        pool: default
        type: disk
      eth0:
        name: eth0
        network: incusbr0
        type: nic
      kmsg:
        path: /dev/kmsg
        source: /dev/kmsg
        type: unix-char
      cache:
        path: /var/cache/locki
        source: /var/cache/locki
        type: disk
      home:
        path: /root
        source: /root/.locki/home
        type: disk
__LOCKI_EOF__
fi

cat > /usr/local/bin/locki-cleanup << '__LOCKI_EOF__'
#!/usr/bin/env python3
"""One-shot cleanup: stops idle Incus containers, removes orphaned ones, and shuts down the VM after prolonged inactivity."""
import json, logging, pathlib, subprocess, sys, time

IDLE_TIMEOUT = 600
VM_IDLE_TIMEOUT = 600
LAST_ACTIVE_FILE = pathlib.Path("/var/lib/locki/last-active.json")
VM_IDLE_SINCE_FILE = pathlib.Path("/var/lib/locki/vm-idle-since")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout, force=True)
log = logging.getLogger(__name__)

try: last_active = json.loads(LAST_ACTIVE_FILE.read_text())
except (FileNotFoundError, json.JSONDecodeError): last_active = {}

for name in [l.strip() for l in subprocess.run(["incus", "list", "--format=csv", "--columns=n"], capture_output=True, text=True).stdout.splitlines() if l.strip()]:
    r = subprocess.run(["incus", "config", "device", "get", name, "worktree", "source"], capture_output=True, text=True)
    source = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    if source is None: continue
    if not pathlib.Path(source).exists():
        log.info("Deleting orphaned container %r (worktree %s is gone).", name, source)
        subprocess.run(["incus", "delete", "--force", name], capture_output=True)
        last_active.pop(name, None)
running = set()
for line in subprocess.run(["incus", "list", "--format=csv", "--columns=n,s"], capture_output=True, text=True).stdout.splitlines():
    parts = line.split(",", 1)
    if len(parts) == 2 and parts[1].strip() == "RUNNING": running.add(parts[0].strip())
active = set()
r = subprocess.run(["incus", "operation", "list", "--format=json"], capture_output=True, text=True)
if r.returncode == 0 and r.stdout.strip():
    try:
        for op in json.loads(r.stdout):
            if op.get("status") != "Running": continue
            for key in ("containers", "instances"):
                for path in (op.get("resources") or {}).get(key) or []: active.add(path.rsplit("/", 1)[-1])
    except json.JSONDecodeError: pass
now = time.time()
for name in running:
    if name in active: last_active[name] = now
    elif name not in last_active: last_active[name] = now
    elif now - last_active[name] >= IDLE_TIMEOUT:
        log.info("Stopping idle container %r (idle %.0fs).", name, now - last_active[name])
        subprocess.run(["incus", "stop", name], capture_output=True)
        last_active.pop(name, None)
for name in list(last_active):
    if name not in running: last_active.pop(name)

LAST_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
LAST_ACTIVE_FILE.write_text(json.dumps(last_active))

still_running = set()
for line in subprocess.run(["incus", "list", "--format=csv", "--columns=n,s"], capture_output=True, text=True).stdout.splitlines():
    parts = line.split(",", 1)
    if len(parts) == 2 and parts[1].strip() == "RUNNING": still_running.add(parts[0].strip())
if still_running:
    VM_IDLE_SINCE_FILE.unlink(missing_ok=True)
else:
    try: idle_since = float(VM_IDLE_SINCE_FILE.read_text())
    except (FileNotFoundError, ValueError): idle_since = now; VM_IDLE_SINCE_FILE.write_text(str(now))
    if now - idle_since >= VM_IDLE_TIMEOUT:
        log.info("No running containers for %.0fs — shutting down VM.", now - idle_since)
        subprocess.run(["poweroff"])
__LOCKI_EOF__
chmod 755 /usr/local/bin/locki-cleanup

cat > /etc/systemd/system/locki-cleanup.service << '__LOCKI_EOF__'
[Unit]
Description=Locki container cleanup
[Service]
Type=oneshot
ExecStart=/usr/local/bin/locki-cleanup
__LOCKI_EOF__

cat > /etc/systemd/system/locki-cleanup.timer << '__LOCKI_EOF__'
[Unit]
Description=Run locki cleanup every minute
[Timer]
OnBootSec=60
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
__LOCKI_EOF__

systemctl daemon-reload
systemctl enable --now locki-cleanup.timer
