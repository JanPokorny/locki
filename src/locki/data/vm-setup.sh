#!/bin/sh
set -eux

if ! command -v incus >/dev/null 2>&1; then
  echo "root:1000000:1000000000" >> /etc/subuid
  echo "root:1000000:1000000000" >> /etc/subgid
  dnf install -y --setopt install_weak_deps=False incus incus-client btrfs-progs
  systemctl enable --now incus
  mkdir -p /var/cache/locki
  incus admin init --preseed << '__LOCKI_EOF__'
storage_pools:
  - name: default
    driver: btrfs
    config:
      btrfs.mount_options: compress=zstd:1,noatime
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

if ! test -f /usr/bin/registry; then
  dnf install -y --setopt install_weak_deps=False docker-distribution

  mkdir -p /etc/locki /var/cache/locki/registry-cache
  for entry in \
    "docker:5000:https://registry-1.docker.io" \
    "ghcr:5001:https://ghcr.io" \
    "gcr:5002:https://gcr.io" \
    "quay:5003:https://quay.io" \
    "redhat:5004:https://registry.access.redhat.com" \
  ; do
    name="${entry%%:*}"; rest="${entry#*:}"
    port="${rest%%:*}"; url="${rest#*:}"
    mkdir -p "/var/cache/locki/registry-cache/$name"
    cat > "/etc/locki/registry-${name}.yml" << EOF
version: 0.1
log:
  level: warn
storage:
  filesystem:
    rootdirectory: /var/cache/locki/registry-cache/$name
  delete:
    enabled: true
http:
  addr: 10.99.0.1:1$port
proxy:
  remoteurl: $url
  ttl: 168h
EOF

    cat > "/etc/systemd/system/locki-registry-${name}.socket" << EOF
[Unit]
Description=Locki registry mirror socket ($name)

[Socket]
ListenStream=10.99.0.1:$port
FreeBind=true

[Install]
WantedBy=sockets.target
EOF

    cat > "/etc/systemd/system/locki-registry-${name}.service" << EOF
[Unit]
Description=Locki registry mirror proxy ($name)
Requires=locki-registry-${name}-backend.service
After=locki-registry-${name}-backend.service

[Service]
ExecStart=/usr/lib/systemd/systemd-socket-proxyd --exit-idle-time=1min 10.99.0.1:1$port
EOF

    ## ExecStartPost blocks until the registry accepts connections, so the proxy never
    ## races a backend that is still starting up.
    cat > "/etc/systemd/system/locki-registry-${name}-backend.service" << EOF
[Unit]
Description=Locki registry mirror ($name)
StopWhenUnneeded=true

[Service]
Environment=OTEL_TRACES_EXPORTER=none GOMEMLIMIT=64MiB
ExecStart=/usr/bin/registry serve /etc/locki/registry-${name}.yml
ExecStartPost=/usr/bin/bash -c 'for _ in {1..100}; do (exec 3<>/dev/tcp/10.99.0.1/1$port) 2>/dev/null && exit 0; sleep 0.1; done; exit 1'
Restart=on-failure
RestartSec=2
EOF
  done

  setsebool -P systemd_socket_proxyd_bind_any=1 systemd_socket_proxyd_connect_any=1 || true

  systemctl daemon-reload
  for reg in docker ghcr gcr quay redhat; do
    systemctl enable --now "locki-registry-${reg}.socket"
  done
fi
