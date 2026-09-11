#!/bin/bash
# 動作テスト用 EC2 の中を構成する（Amazon Linux 2023）
#
# EC2 の上で（root）:
#   bash configure.sh
#   launch.sh が --payload の出力を user-data として渡すので、既定では起動時に自動で走る。何度走らせても同じ状態になる。
#   ログは /var/log/shop-configure.log。
#
# ホストから（state/<名前>.env のプロファイル・リージョン・インスタンスを使う）:
#   ./configure.sh --send  <名前>   EC2 側の部分を SSM の RunCommand で送って実行する（--bare のあと・再構成）
#   ./configure.sh --check <名前>   構成の完了を待ち、サービス・待ち受け・応答を見る
#   ./configure.sh --payload        EC2 に渡す部分（「EC2 側」から下。コメント行を落としたもの）を標準出力に出す
#
# EC2 に渡るのは --payload の出力だけ。user-data は調査エージェントが describe-instance-attribute や IMDS で読めるので、
# テストの意図が分かる文（このヘッダーやコメント行）は EC2 側に残さない。EC2 側の部分にはコメント行以外で意図を書かないこと。
#
# できあがる中身:
#   nginx :80（vhost shop.internal と既定のサイト）→ shop-api（Python、127.0.0.1:8000、ユーザー shop）
#   → Redis 6（127.0.0.1:6379）/ PostgreSQL 15（127.0.0.1:5432、DB shop）
#   shop-order.timer（5 分ごとに注文を投入）、shop-sync.timer（宛先が無く失敗し続ける）、
#   cron の日次 pg_dump、logrotate、rsyslog（/var/log/messages）、スワップ 1GB
#   秘密: /etc/shop/shop.env（0640 root:shop）、ログ: /var/log/shop/*.log（0640 shop）
set -euo pipefail

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

# ---- ホスト側 ----

self=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")

payload() {  # 「EC2 側」から下を、コメント行（#! は残す）を落として出す
  printf '#!/bin/bash\nset -euo pipefail\n'
  sed -n '/^# ---- EC2 側 ----$/,$p' "$self" | grep -vE '^[[:space:]]*#($|[^!])'
}

remote() {  # remote <コメント> <commands の JSON 配列>
  local cmd status
  cmd=$(aws ssm send-command --instance-ids "$INSTANCE_ID" --document-name AWS-RunShellScript \
    --comment "$1" --parameters "{\"commands\":$2}" --query Command.CommandId --output text)
  echo "command: ${cmd}（終わるまで待ちます）"
  while :; do
    status=$(aws ssm get-command-invocation --command-id "$cmd" --instance-id "$INSTANCE_ID" \
      --query Status --output text 2>/dev/null || echo Pending)
    case $status in Pending|InProgress|Delayed) sleep 10 ;; *) break ;; esac
  done
  aws ssm get-command-invocation --command-id "$cmd" --instance-id "$INSTANCE_ID" \
    --query StandardOutputContent --output text
  if [[ $status != Success ]]; then
    aws ssm get-command-invocation --command-id "$cmd" --instance-id "$INSTANCE_ID" \
      --query StandardErrorContent --output text >&2
    echo "status: $status" >&2
    return 1
  fi
  echo "status: $status"
}

host_mode() {
  local mode=$1 name=${2:-web2} b64 check
  local state=${self%/*}/state/$name.env
  [[ -f $state ]] || { echo "$state がありません（launch.sh で作ったインスタンスだけ扱えます）" >&2; exit 1; }
  # shellcheck disable=SC1090
  source "$state"
  export AWS_PROFILE AWS_REGION
  case $mode in
    --send)
      b64=$(payload | base64 | tr -d '\n')
      remote "configure $name" \
        "[\"cloud-init status --wait >/dev/null || true\",\"echo $b64 | base64 -d > /root/configure.sh\",\"bash /root/configure.sh\"]"
      ;;
    --check)
      check=$(cat <<'EOF'
["cloud-init status --wait --long || true",
 "echo; echo '== サービス'; systemctl is-active nginx postgresql redis6 shop-api crond rsyslog || true",
 "echo; echo '== 失敗しているユニット'; systemctl --failed --no-legend --no-pager",
 "echo; echo '== 待ち受け'; ss -ltnp",
 "echo; echo '== タイマー'; systemctl list-timers 'shop-*' --no-pager",
 "echo; echo '== 構成ログの末尾'; tail -n 5 /var/log/shop-configure.log 2>/dev/null || echo 'まだありません'",
 "echo; echo '== 応答'; curl -sSi http://shop.internal/"]
EOF
)
      remote "check $name" "$check"
      ;;
  esac
}

case ${1:-} in
  --payload) payload; exit ;;
  --send|--check) host_mode "$@"; exit ;;
  "") ;;
  *) usage ;;
esac

# ---- EC2 側 ----

[[ $EUID -eq 0 ]] || { echo "root で実行してください" >&2; exit 1; }
exec > >(tee -a /var/log/shop-configure.log) 2>&1
cd /
step() { printf '\n== %s  %s\n' "$(date -Is)" "$*"; }

step "スワップ 1GB"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  [[ -f /swapfile ]] || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap defaults 0 0' >> /etc/fstab

step "パッケージ"
# redis6 が見つからないときは dnf search redis valkey で名前を確かめる
dnf install -y -q nginx postgresql15-server redis6 cronie rsyslog
systemctl enable --now rsyslog crond redis6

step "PostgreSQL"
[[ -f /var/lib/pgsql/data/PG_VERSION ]] || postgresql-setup --initdb
sed -i -E 's/^(host\s+all\s+all\s+\S+\s+)ident$/\1scram-sha-256/' /var/lib/pgsql/data/pg_hba.conf
systemctl enable --now postgresql
systemctl reload postgresql

install -d -o root -g root -m 755 /etc/shop
if [[ -f /etc/shop/shop.env ]]; then
  db_password=$(sed -n 's/^PGPASSWORD=//p' /etc/shop/shop.env)
else
  db_password=$(openssl rand -hex 16)
fi
pg() { runuser -u postgres -- psql -v ON_ERROR_STOP=1 -qtAc "$1"; }
[[ $(pg "SELECT 1 FROM pg_roles WHERE rolname='shop'") == 1 ]] || pg "CREATE ROLE shop LOGIN"
pg "ALTER ROLE shop PASSWORD '$db_password'"
[[ $(pg "SELECT 1 FROM pg_database WHERE datname='shop'") == 1 ]] || pg "CREATE DATABASE shop OWNER shop"
PGPASSWORD=$db_password psql -h 127.0.0.1 -U shop -d shop -v ON_ERROR_STOP=1 -qc \
  "CREATE TABLE IF NOT EXISTS orders (id serial PRIMARY KEY, item text NOT NULL, created_at timestamptz DEFAULT now());"

step "アプリのユーザー・置き場・秘密"
id shop >/dev/null 2>&1 || useradd --system --home-dir /opt/shop --shell /sbin/nologin shop
install -d -m 755 /opt/shop /opt/shop/bin
install -d -o root -g shop -m 750 /etc/shop
install -d -o shop -g shop -m 750 /var/log/shop
install -d -o postgres -g postgres -m 700 /var/backups/shop
cat > /etc/shop/shop.env <<EOF
PGHOST=127.0.0.1
PGDATABASE=shop
PGUSER=shop
PGPASSWORD=$db_password
EOF
chown root:shop /etc/shop/shop.env
chmod 640 /etc/shop/shop.env

step "API とジョブのスクリプト"
# 標準ライブラリだけ。Redis は生の RESP、PostgreSQL は psql を呼ぶ（接続先は PG* の環境変数）
cat > /opt/shop/app.py <<'EOF'
import http.server, json, logging, socket, subprocess

logging.basicConfig(filename="/var/log/shop/api.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

def redis_incr(key):
    with socket.create_connection(("127.0.0.1", 6379), timeout=2) as s:
        s.sendall(f"INCR {key}\r\n".encode())
        return int(s.recv(64).decode().strip().lstrip(":"))

def order_count():
    r = subprocess.run(["psql", "-tAc", "SELECT count(*) FROM orders"],
                       capture_output=True, text=True, timeout=5, check=True)
    return int(r.stdout)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            code, body = 200, {"hits": redis_incr("hits"), "orders": order_count()}
        except Exception as e:
            logging.exception("request failed")
            code, body = 500, {"error": str(e)}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        logging.info("%s %s", self.address_string(), fmt % args)

http.server.ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
EOF

cat > /opt/shop/bin/order.sh <<'EOF'
#!/bin/bash
set -euo pipefail
item="item-$((RANDOM % 100))"
psql -qc "INSERT INTO orders(item) VALUES ('$item')"
echo "$(date -Is) inserted $item" >> /var/log/shop/order.log
EOF

cat > /opt/shop/bin/backup.sh <<'EOF'
#!/bin/bash
set -euo pipefail
pg_dump shop | gzip > "/var/backups/shop/shop-$(date +%F).sql.gz"
find /var/backups/shop -name '*.sql.gz' -mtime +7 -delete
EOF
chmod 755 /opt/shop/bin/*.sh

step "systemd のユニット"
cat > /etc/systemd/system/shop-api.service <<'EOF'
[Unit]
Description=Shop API
After=network-online.target postgresql.service redis6.service
Wants=postgresql.service redis6.service

[Service]
User=shop
EnvironmentFile=/etc/shop/shop.env
ExecStart=/usr/bin/python3 /opt/shop/app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/shop-order.service <<'EOF'
[Unit]
Description=Insert a sample order
After=postgresql.service

[Service]
Type=oneshot
User=shop
EnvironmentFile=/etc/shop/shop.env
ExecStart=/opt/shop/bin/order.sh
EOF

cat > /etc/systemd/system/shop-order.timer <<'EOF'
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

# 宛先（sync.shop.internal）が無いので失敗し続ける。調査で「失敗しているユニット」として見つかる
cat > /etc/systemd/system/shop-sync.service <<'EOF'
[Unit]
Description=Push orders to sync server

[Service]
Type=oneshot
User=shop
ExecStart=/usr/bin/curl -fsS --max-time 5 http://sync.shop.internal:9000/push
EOF

cat > /etc/systemd/system/shop-sync.timer <<'EOF'
[Timer]
OnBootSec=3min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
EOF

step "cron と logrotate"
echo '30 3 * * * postgres /opt/shop/bin/backup.sh' > /etc/cron.d/shop-backup

cat > /etc/logrotate.d/shop <<'EOF'
/var/log/shop/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    su shop shop
    create 0640 shop shop
    copytruncate
}
EOF

step "nginx"
grep -q 'shop\.internal' /etc/hosts || echo '127.0.0.1 shop.internal' >> /etc/hosts
cat > /etc/nginx/conf.d/shop.conf <<'EOF'
server {
    listen 80;
    server_name shop.internal;
    access_log /var/log/nginx/shop.access.log;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    location /nginx_status { stub_status; allow 127.0.0.1; deny all; }
}
EOF
nginx -t

step "起動"
systemctl daemon-reload
systemctl enable shop-api nginx
systemctl restart shop-api
systemctl reload-or-restart nginx
systemctl enable --now shop-order.timer shop-sync.timer

step "確認"
curl -fsS --retry 10 --retry-connrefused --retry-delay 2 http://shop.internal/
echo
ss -ltnp
step "完了"
