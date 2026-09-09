#!/usr/bin/env bash
# 導入スクリプト（aws-survey ssh setup --print）をローカルの Docker で root 実行し、
# ファイルの配置・権限・sshd 設定・ForceCommand 経由の許可と拒否・失敗時の巻き戻しを確かめる。
# 実 AWS も実 EC2 も使わない。Docker が要るので unittest には入れない（手で実行する）。
#
#   bash tests/ec2_install_smoke.sh            Ubuntu 24.04 と Amazon Linux 2023
#   bash tests/ec2_install_smoke.sh ubuntu:22.04
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ec2-install.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
chmod 755 "$WORK"

# ---- 導入スクリプトの生成（偽の対象フォルダと鍵） ----
mkdir -p "$WORK/target" "$WORK/home/.aws-survey/smoke/ssh"
cat > "$WORK/target/environment.json" <<'EOF'
{"name":"smoke","account_id":"000000000000","region":"test-region",
 "auth":{"route":"own_role","source_profile":"x","principal_arn":"arn:aws:iam::000000000000:user/x",
         "mfa_required":false,"role_name":"r","refresh_command":null,"duration_seconds":3600},
 "phase_dir":"01_基礎調査","setup":{}}
EOF
ssh-keygen -q -t ed25519 -N '' -C smoke -f "$WORK/home/.aws-survey/smoke/ssh/id_ed25519"
HOME="$WORK/home" "$ROOT/bin/aws-survey" --dir "$WORK/target" ssh setup --print \
  --log 'app=/var/www/app/log/*.log' --deny '/var/www/app/config/*' > "$WORK/install.sh"
cp "$WORK/home/.aws-survey/smoke/ssh/id_ed25519" "$WORK/id_ed25519"
chmod 644 "$WORK/install.sh" "$WORK/id_ed25519"
echo "install.sh: $(wc -c < "$WORK/install.sh" | tr -d ' ') bytes"

# ---- コンテナの中で走らせる検査 ----
cat > "$WORK/check.sh" <<'CHECK'
set -uo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
fails=0
ok()   { printf '  ok   %s\n' "$*"; }
bad()  { printf '  FAIL %s\n' "$*"; fails=$((fails+1)); }
check() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
perm()  { [ "$(stat -c '%U:%G %a' "$1")" = "$2" ] || { echo "    $1 is $(stat -c '%U:%G %a' "$1"), want $2"; return 1; }; }

. /etc/os-release
echo "== $PRETTY_NAME (sshd: $(sshd -V 2>&1 | head -1))"
if command -v apt-get >/dev/null; then
  apt-get update -qq >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server openssh-client sudo python3 procps >/dev/null
  mkdir -p /run/sshd
else
  dnf install -y -q openssh-server openssh-clients sudo python3 util-linux procps-ng shadow-utils >/dev/null 2>&1
fi
ssh-keygen -A >/dev/null 2>&1
# root 専用のログを置いて自動検出を見る（Amazon Linux 相当）
install -m 600 -o root -g root /dev/null /var/log/secure
install -d -m 700 -o root -g root /var/log/httpd
install -m 644 -o root -g root /dev/null /var/log/httpd/access_log
install -m 644 -o root -g root /dev/null /var/log/httpd/access_log-20260101
# 誰でも読めるログは登録されないこと
install -m 644 -o root -g root /dev/null /var/log/world.log

echo "-- install (1st run)"
if bash /work/install.sh > /tmp/install.out 2>&1; then ok "install exit 0"; else bad "install exit $?"; cat /tmp/install.out; fi
grep -q '^HOSTKEY ssh-ed25519 ' /tmp/install.out && ok "HOSTKEY line" || bad "HOSTKEY line missing"
grep -q 'self check passed' /tmp/install.out && ok "self check" || bad "self check"

echo "-- files and permissions"
check "gateway root 755"        perm /usr/local/lib/diag/gateway   'root:root 755'
check "diag-root root 755"      perm /usr/local/lib/diag/diag-root 'root:root 755'
check "diag.conf root 644"      perm /etc/diag/diag.conf           'root:root 644'
check "sudoers root 440"        perm /etc/sudoers.d/diag           'root:root 440'
check "tmpfiles root 644"       perm /etc/tmpfiles.d/diag.conf     'root:root 644'
check "lock root:diag 660"      perm /run/diag.lock                'root:diag 660'
check "~/.ssh diag 700"         perm /home/diag/.ssh               'diag:diag 700'
check "authorized_keys diag 600" perm /home/diag/.ssh/authorized_keys 'diag:diag 600'
check "authorized_keys restricted" grep -q '^restrict,command="/usr/local/lib/diag/gateway" ssh-ed25519 ' /home/diag/.ssh/authorized_keys
check "shell is /bin/bash"      sh -c '[ "$(getent passwd diag | cut -d: -f7)" = /bin/bash ]'
check "password locked"         sh -c 'passwd -S diag 2>/dev/null | grep -qE " L | LK " || getent shadow diag | cut -d: -f2 | grep -q "^!"'
if grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config; then
  check "sshd drop-in root 600"   perm /etc/ssh/sshd_config.d/diag.conf 'root:root 600'
  check "sshd_config untouched"   sh -c '! grep -q "diag begin" /etc/ssh/sshd_config'
else
  check "sshd_config has marker block" grep -q '^# diag begin' /etc/ssh/sshd_config
fi
check "sshd -t"                 sshd -t
check "visudo -c"               visudo -c
check "sudo -l lists diag-root only" sh -c 'runuser -u diag -- sudo -n -l | grep -q "NOPASSWD: /usr/local/lib/diag/diag-root" && ! runuser -u diag -- sudo -n -l | grep -E "\(ALL" '
check "diag.conf: app registered"      sh -c 'python3 -c "import json;c=json.load(open(\"/etc/diag/diag.conf\"));assert c[\"logs\"][\"app\"]==\"/var/www/app/log/*.log\";assert c[\"deny\"]==[\"/var/www/app/config/*\"];assert c[\"strict\"] is False"'
check "diag.conf: secure detected"     sh -c 'python3 -c "import json;c=json.load(open(\"/etc/diag/diag.conf\"));assert c[\"logs\"][\"secure\"]==\"/var/log/secure*\""'
check "diag.conf: httpd-access detected" sh -c 'python3 -c "import json;c=json.load(open(\"/etc/diag/diag.conf\"));assert c[\"logs\"][\"httpd-access\"]==\"/var/log/httpd/access_log*\""'
check "diag.conf: world.log not registered" sh -c 'python3 -c "import json;c=json.load(open(\"/etc/diag/diag.conf\"));assert not any(\"world\" in v for v in c[\"logs\"].values())"'
check "gateway conf loads"      runuser -u diag -- env SSH_ORIGINAL_COMMAND=logs /usr/local/lib/diag/gateway
check "root tier via sudo (cron)" runuser -u diag -- env SSH_ORIGINAL_COMMAND=cron /usr/local/lib/diag/gateway

echo "-- install (2nd run, idempotent)"
before=$(getent passwd diag)
if bash /work/install.sh > /tmp/install2.out 2>&1; then ok "install exit 0"; else bad "install exit $?"; cat /tmp/install2.out; fi
[ "$before" = "$(getent passwd diag)" ] && ok "user kept" || bad "user changed"
[ "$(grep -c '^restrict' /home/diag/.ssh/authorized_keys)" = 1 ] && ok "one authorized key" || bad "authorized_keys duplicated"
[ "$(grep -c 'diag begin' /etc/ssh/sshd_config)" -le 1 ] && ok "marker block not duplicated" || bad "marker block duplicated"
check "sshd -t after 2nd run" sshd -t

echo "-- sshd + ForceCommand (real ssh through the gateway)"
/usr/sbin/sshd -o ListenAddress=127.0.0.1 && sleep 1
install -m 600 /work/id_ed25519 /tmp/id_ed25519
SSH="ssh -i /tmp/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR diag@127.0.0.1"
out=$($SSH uptime 2>&1); rc=$?
[ $rc -eq 0 ] && echo "$out" | grep -q 'load average' && ok "ssh uptime -> allowed" || { bad "ssh uptime rc=$rc"; echo "    $out"; }
out=$($SSH bash 2>&1); rc=$?
[ $rc -eq 2 ] && echo "$out" | grep -q '^denied:' && ok "ssh bash -> denied (rc=2)" || { bad "ssh bash rc=$rc"; echo "    $out"; }
out=$($SSH 'uptime; id' 2>&1); rc=$?
[ $rc -eq 2 ] && ok "ssh 'uptime; id' -> denied" || { bad "ssh 'uptime; id' rc=$rc"; echo "    $out"; }
out=$($SSH 'cat /etc/passwd' 2>&1); rc=$?
[ $rc -eq 0 ] && echo "$out" | grep -q '^root:' && ok "ssh cat /etc/passwd -> allowed (general tier)" || { bad "ssh cat /etc/passwd rc=$rc"; echo "    $out"; }
out=$($SSH 'cat /etc/shadow' 2>&1); rc=$?
[ $rc -ne 0 ] && ok "ssh cat /etc/shadow -> fails (rc=$rc)" || { bad "ssh cat /etc/shadow allowed"; echo "    $out"; }
out=$($SSH 'cat /etc/ssh/sshd_config' 2>&1); rc=$?
[ $rc -eq 2 ] && ok "ssh cat /etc/ssh/sshd_config -> denied" || { bad "ssh cat sshd_config rc=$rc"; echo "    $out"; }
out=$($SSH 'proc 1 environ' 2>&1); rc=$?
[ $rc -eq 2 ] && ok "ssh proc 1 environ -> denied" || { bad "proc 1 environ rc=$rc"; echo "    $out"; }
out=$($SSH 2>&1 < /dev/null); rc=$?
[ $rc -ne 0 ] && ok "ssh (no command, no tty) -> rc=$rc" || { bad "ssh without command succeeded"; echo "    $out"; }
out=$($SSH -tt uptime 2>&1); rc=$?
echo "$out" | grep -qi 'PTY allocation request failed' && ok "ssh -tt -> pty refused" || { bad "ssh -tt rc=$rc"; echo "    $out"; }
out=$($SSH -L 9999:127.0.0.1:22 -O check 2>&1); true
out=$(sftp -i /tmp/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR diag@127.0.0.1 <<< 'ls' 2>&1); rc=$?
[ $rc -ne 0 ] && ok "sftp -> refused (rc=$rc)" || { bad "sftp worked"; echo "    $out"; }

echo "-- rollback when sshd -t fails"
cp -p /etc/ssh/sshd_config /tmp/sshd_config.before
mv /usr/sbin/sshd /usr/sbin/sshd.real
printf '#!/bin/sh\ncase " $* " in *" -t "*) echo "fake: bad config" >&2; exit 255;; esac\nexec /usr/sbin/sshd.real "$@"\n' > /usr/sbin/sshd; chmod 755 /usr/sbin/sshd
rm -f /etc/ssh/sshd_config.d/diag.conf
if bash /work/install.sh > /tmp/install3.out 2>&1; then bad "install should fail when sshd -t fails"; else ok "install exit non-zero ($?)"; fi
grep -q 'previous files restored' /tmp/install3.out && ok "restore message" || { bad "restore message"; cat /tmp/install3.out; }
cmp -s /etc/ssh/sshd_config /tmp/sshd_config.before && ok "sshd_config restored" || bad "sshd_config differs"
[ ! -f /etc/ssh/sshd_config.d/diag.conf ] && ok "drop-in removed" || bad "drop-in left behind"
mv -f /usr/sbin/sshd.real /usr/sbin/sshd

echo "-- result: $fails failure(s)"
exit $fails
CHECK

status=0
images=("$@"); [ ${#images[@]} -gt 0 ] || images=(ubuntu:24.04 amazonlinux:2023)
for image in "${images[@]}"; do
  echo ""
  echo "### $image"
  if docker run --rm -v "$WORK:/work:ro" "$image" bash /work/check.sh; then
    echo "### $image: passed"
  else
    echo "### $image: FAILED"; status=1
  fi
done
exit $status
