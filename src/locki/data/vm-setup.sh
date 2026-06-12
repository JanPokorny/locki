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

if ! command -v nginx >/dev/null 2>&1; then
  dnf install -y --setopt install_weak_deps=False nginx openssl

  mkdir -p /etc/locki /var/cache/locki/registry-cache

  if ! test -f /etc/locki/ca.crt; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
      -subj "/CN=Locki Registry CA" -keyout /etc/locki/ca.key -out /etc/locki/ca.crt
  fi
  sans="DNS:registry-1.docker.io,DNS:mirror.gcr.io,DNS:ghcr.io,DNS:gcr.io,DNS:quay.io,DNS:registry.access.redhat.com"
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -keyout /etc/locki/registry.key -subj "/CN=Locki Registry" \
    -addext "subjectAltName=$sans" -addext "extendedKeyUsage=serverAuth" \
    -out /etc/locki/registry.csr
  openssl x509 -req -in /etc/locki/registry.csr -CA /etc/locki/ca.crt -CAkey /etc/locki/ca.key \
    -CAcreateserial -days 3650 -copy_extensions copyall -out /etc/locki/registry.crt
  rm -f /etc/locki/registry.csr
  chgrp nginx /etc/locki/ca.key /etc/locki/registry.key
  chmod 640 /etc/locki/ca.key /etc/locki/registry.key

  resolvers=$(awk '/^nameserver/ && $2 !~ /:/ {print $2}' /etc/resolv.conf | paste -sd' ')
  [ -n "$resolvers" ] || resolvers="1.1.1.1 8.8.8.8"

  cat > /etc/nginx/nginx.conf << '__LOCKI_NGINX__'
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /run/nginx.pid;

events {
	worker_connections 4096;
}

http {
	access_log off;
	server_tokens off;

	proxy_cache_path /var/cache/locki/registry-cache levels=1:2 keys_zone=registry:64m inactive=365d max_size=20g use_temp_path=off;

	resolver __RESOLVERS__ ipv6=off valid=300s;
	resolver_timeout 10s;

	proxy_http_version 1.1;
	proxy_ssl_server_name on;
	proxy_read_timeout 300s;
	proxy_max_temp_file_size 8192m;

	server {
		listen 10.99.0.1:80;
		location = /locki-ca.crt {
			default_type application/x-x509-ca-cert;
			alias /etc/locki/ca.crt;
		}
		location / { return 404; }
	}

	server {
		listen 10.99.0.1:443 ssl;
		http2 on;
		server_name registry-1.docker.io mirror.gcr.io ghcr.io gcr.io quay.io registry.access.redhat.com;

		ssl_certificate     /etc/locki/registry.crt;
		ssl_certificate_key /etc/locki/registry.key;

		location ~ "^/v2/.+/blobs/(sha256:[0-9a-f]{64})$" {
			set $cache_key "blob:$1";
			proxy_set_header Host $host;
			proxy_pass https://$host;
			proxy_cache registry;
			proxy_cache_key $cache_key;
			proxy_cache_valid 200 365d;
			proxy_cache_lock on;
			proxy_cache_lock_timeout 120s;
			proxy_cache_use_stale error timeout updating;
			proxy_ignore_headers Cache-Control Expires Set-Cookie X-Accel-Expires Vary;
			proxy_intercept_errors on;
			error_page 301 302 307 = @follow_redirect;
			add_header X-Locki-Cache $upstream_cache_status always;
		}

		location ~ "^/v2/.+/manifests/sha256:[0-9a-f]{64}$" {
			proxy_set_header Host $host;
			proxy_pass https://$host;
			proxy_cache registry;
			proxy_cache_key "manifest:$host:$request_uri";
			proxy_cache_valid 200 365d;
			proxy_cache_lock on;
			proxy_ignore_headers Cache-Control Expires Set-Cookie X-Accel-Expires Vary;
			add_header X-Locki-Cache $upstream_cache_status always;
		}

		location / {
			proxy_set_header Host $host;
			proxy_pass https://$host;
		}

		location @follow_redirect {
			set $redirect_target $upstream_http_location;
			if ($redirect_target !~ "^https?://") {
				set $redirect_target "https://$host$upstream_http_location";
			}
			proxy_set_header Authorization "";
			proxy_pass $redirect_target;
			proxy_cache registry;
			proxy_cache_key $cache_key;
			proxy_cache_valid 200 365d;
			proxy_cache_lock on;
			proxy_cache_lock_timeout 120s;
			proxy_ignore_headers Cache-Control Expires Set-Cookie X-Accel-Expires Vary;
			add_header X-Locki-Cache $upstream_cache_status always;
		}
	}
}
__LOCKI_NGINX__
  sed -i "s|__RESOLVERS__|$resolvers|" /etc/nginx/nginx.conf

  setsebool -P httpd_can_network_connect 1 || true
  chcon -R -t httpd_cache_t /var/cache/locki/registry-cache 2>/dev/null || true
  echo 'net.ipv4.ip_nonlocal_bind=1' > /etc/sysctl.d/99-locki.conf
  sysctl -w net.ipv4.ip_nonlocal_bind=1 || true
  mkdir -p /etc/systemd/system/nginx.service.d
  cat > /etc/systemd/system/nginx.service.d/locki.conf << 'EOF'
[Unit]
After=incus.service network-online.target
Wants=network-online.target
EOF

  nginx -t
  systemctl daemon-reload
  systemctl enable --now nginx
fi

if ! command -v buildkitd >/dev/null 2>&1; then
  dnf install -y --setopt install_weak_deps=False docker-buildkit runc
  mkdir -p /etc/buildkit /var/cache/locki/buildkit
  cat > /etc/buildkit/buildkitd.toml << 'EOF'
root = "/var/cache/locki/buildkit"
[worker.oci]
  enabled = true
  gc = true
  [[worker.oci.gcpolicy]]
    all = true
    maxUsedSpace = "20GB"
EOF

  cat > /etc/systemd/system/locki-buildkit.service << 'EOF'
[Unit]
Description=Locki shared BuildKit daemon
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/buildkitd --addr unix:///var/cache/locki/buildkit.sock --config /etc/buildkit/buildkitd.toml
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now locki-buildkit
fi
