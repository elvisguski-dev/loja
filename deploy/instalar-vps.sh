#!/usr/bin/env bash
# Instala a Loja em um servidor Ubuntu/Debian (ex.: Oracle Cloud Always Free) com HTTPS automatico.
# Uso (como root):   bash instalar-vps.sh meu-dominio.duckdns.org
# Sem dominio:       bash instalar-vps.sh          (fica em http://IP, sem HTTPS - so para teste)
set -euo pipefail
DOMINIO="${1:-}"
REPO="https://github.com/elvisguski-dev/loja"
[ "$(id -u)" = 0 ] || { echo "Rode como root (sudo bash $0 ...)"; exit 1; }

echo "==> Instalando pacotes"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 git curl debian-keyring debian-archive-keyring apt-transport-https gnupg iptables-persistent
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

echo "==> Baixando o programa"
id loja >/dev/null 2>&1 || useradd --system --home /opt/loja --shell /usr/sbin/nologin loja
if [ -d /opt/loja/.git ]; then git -C /opt/loja pull --ff-only; else git clone --depth 1 "$REPO" /opt/loja; fi
mkdir -p /var/lib/loja /var/backups/loja
chown -R loja:loja /var/lib/loja

if [ ! -f /etc/loja.env ]; then
  SENHA="$(python3 -c 'import secrets;print(secrets.token_urlsafe(14))')"
  cat > /etc/loja.env <<ENV
ADMIN_SENHA=$SENHA
PORT=8082
HOST=127.0.0.1
LOJA_DB=/var/lib/loja/loja.db
TRUST_PROXY=1
MAX_UPLOAD_MB=500
ENV
  chmod 600 /etc/loja.env
  echo "==> SENHA DO ADMIN (anote agora): $SENHA   (fica salva em /etc/loja.env)"
fi

cat > /etc/systemd/system/loja.service <<'UNIT'
[Unit]
Description=Loja de programas
After=network.target

[Service]
User=loja
WorkingDirectory=/opt/loja
EnvironmentFile=/etc/loja.env
ExecStart=/usr/bin/python3 /opt/loja/server.py
Restart=always
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/loja
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

if [ -n "$DOMINIO" ]; then
  printf '%s {\n    encode gzip\n    reverse_proxy 127.0.0.1:8082\n}\n' "$DOMINIO" > /etc/caddy/Caddyfile
else
  printf ':80 {\n    reverse_proxy 127.0.0.1:8082\n}\n' > /etc/caddy/Caddyfile
fi

echo "==> Backup diario (7 dias)"
cat > /etc/cron.daily/loja-backup <<'CRON'
#!/bin/sh
set -e
d=/var/backups/loja; mkdir -p "$d"
python3 - <<'PY'
import sqlite3
s=sqlite3.connect('/var/lib/loja/loja.db'); t=sqlite3.connect('/var/backups/loja/loja.db'); s.backup(t); t.close(); s.close()
PY
tar -czf "$d/arquivos-$(date +%F).tar.gz" -C /var/lib/loja arquivos 2>/dev/null || true
find "$d" -name 'arquivos-*.tar.gz' -mtime +7 -delete
CRON
chmod +x /etc/cron.daily/loja-backup

echo "==> Liberando portas 80/443 no firewall do servidor"
iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
netfilter-persistent save >/dev/null 2>&1 || true

systemctl daemon-reload
systemctl enable --now loja caddy
systemctl restart caddy
sleep 2
systemctl is-active loja caddy
echo
if [ -n "$DOMINIO" ]; then echo "Pronto! https://$DOMINIO  (admin em /admin)"; else echo "Pronto! http://$(curl -fsS ifconfig.me 2>/dev/null || echo SEU-IP)  (admin em /admin)"; fi
echo "Lembre de liberar as portas 80 e 443 tambem no painel da Oracle (Security List / Network Security Group)."
echo "Para atualizar depois:  git -C /opt/loja pull && systemctl restart loja"
