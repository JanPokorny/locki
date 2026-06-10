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
  addr: 10.99.0.1:$port
proxy:
  remoteurl: $url
  ttl: 168h
EOF
  done

  cat > /etc/systemd/system/locki-registry@.service << 'EOF'
[Unit]
Description=Locki registry mirror (%i)
After=network-online.target incus.service
Wants=network-online.target

[Service]
Environment=OTEL_TRACES_EXPORTER=none
ExecStart=/usr/bin/registry serve /etc/locki/registry-%i.yml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  for reg in docker ghcr gcr quay redhat; do
    systemctl enable --now "locki-registry@${reg}.service"
  done
fi
