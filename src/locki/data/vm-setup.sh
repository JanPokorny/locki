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

if ! command -v caddy >/dev/null 2>&1; then
  dnf install -y --setopt install_weak_deps=False caddy openssl

  mkdir -p /etc/locki
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
    -subj "/CN=Locki Registry CA" -keyout /etc/locki/ca.key -out /etc/locki/ca.crt
  chgrp caddy /etc/locki/ca.key
  chmod 640 /etc/locki/ca.key

  cat > /etc/caddy/Caddyfile << '__LOCKI_EOF__'
{
	skip_install_trust
	pki {
		ca locki {
			name "Locki Registry CA"
			root {
				cert /etc/locki/ca.crt
				key /etc/locki/ca.key
			}
		}
	}
}

http://10.99.0.1 {
	handle /locki-ca.crt {
		root * /etc/locki
		rewrite * /ca.crt
		file_server
	}
	handle {
		respond 404
	}
}
__LOCKI_EOF__

  for entry in \
    "registry-1.docker.io:5000" \
    "mirror.gcr.io:5000" \
    "ghcr.io:5001" \
    "gcr.io:5002" \
    "quay.io:5003" \
    "registry.access.redhat.com:5004" \
  ; do
    domain="${entry%:*}"; port="${entry#*:}"
    ## keepalive off: idle Caddy connections would keep the socket-activated
    ## mirror backends alive past their idle timeout.
    cat >> /etc/caddy/Caddyfile << __LOCKI_EOF__

https://$domain {
	tls {
		issuer internal {
			ca locki
		}
	}
	@mirrorable {
		method GET HEAD
		path /v2/*
		not header Authorization *
	}
	handle @mirrorable {
		reverse_proxy 10.99.0.1:$port {
			transport http {
				keepalive off
			}
			@miss status 401 403 404 429 500 502 503 504
			handle_response @miss {
				reverse_proxy https://$domain
			}
		}
	}
	handle {
		reverse_proxy https://$domain
	}
}
__LOCKI_EOF__
  done

  caddy validate --config /etc/caddy/Caddyfile
  systemctl enable --now caddy
fi
