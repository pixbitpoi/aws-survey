#!/usr/bin/env bash
# EC2 の中を調べるための導入と鍵の管理（ホストで実行）。aws-survey ssh から呼ばれる。
#
#   aws-survey ssh setup <instance-id | Name タグ> [--alias <名前>] [--user <名前>] [--log <名前>=<パス or glob>]... [--deny <glob>]... [--strict]
#                                   SSM 経由で導入する。元プロファイル（auth.source_profile）で実行し、
#                                   成功したら $AWS_DIR/ssh/{config,known_hosts} と environment.json の ssh.hosts に記録する
#   aws-survey ssh setup --print [...]   導入スクリプト（root で実行する 1 本の bash）を標準出力に出す。案内は標準エラー
#   aws-survey ssh list                  登録済みホスト・鍵の作成日時・導入日時（AWS に届けばインスタンスの状態とタグも）
#   aws-survey ssh verify <host>         調査コンテナから ec2 --selftest <host> を打つ。通るもの・塞がっているもの・
#                                   終了後に SSM セッションが残らないことを実際に接続して確かめる
#   aws-survey ssh rotate                鍵を作り直し、登録済みホスト全部に再導入する。1 台でも失敗したら古い鍵と記録を残す
#   aws-survey ssh remove <host>         EC2 からユーザー・sshd 設定・sudoers・ゲートウェイ・設定・tmpfiles・ロックを撤去し、
#                                   タグを外して記録から消す。最後のホストなら diag-ssh-<name> ポリシーも調査用ロールから外して消す
#                                   （自分で作ったロールのときだけ。借りたロールでは管理者向けのコマンドを表示する）。
#                                   remove --print <host> は撤去スクリプトを出すだけ
#
# 鍵は $AWS_DIR/ssh/id_ed25519（対象ごとに 1 対、無ければ作る）。導入スクリプトに入るのは公開鍵だけ。
# 導入スクリプトの雛形は libexec/ec2/install.sh.tmpl、撤去は remove.sh.tmpl。EC2 に残るものの名前は diag で統一する
# （設計文書の第 2 節）。導入と撤去は元プロファイルの仕事で、ssm:SendCommand を調査用ロールや一時キーに足さない。
# 途中で失敗したら何も記録しない（タグ付け・config・environment.json は導入が成功してから。撤去も EC2 側が済んでから）。
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/container.sh"

EC2_DIR="$LIBEXEC_DIR/ec2"
TEMPLATE="$EC2_DIR/install.sh.tmpl"
REMOVE_TEMPLATE="$EC2_DIR/remove.sh.tmpl"
SSH_DIR="$AWS_DIR/ssh"
KEY="$SSH_DIR/id_ed25519"
SSH_CONFIG="$SSH_DIR/config"
KNOWN_HOSTS="$SSH_DIR/known_hosts"
# コンテナから見た鍵の置き場（$AWS_DIR の読み取り専用マウント先）
CONTAINER_SSH_DIR='~/.aws-claude/ssh'
# RunCommand の待ち時間（秒）。導入スクリプトは数十秒で終わる
SSM_TIMEOUT="${AWS_SURVEY_SSM_TIMEOUT:-600}"
SSM_POLL="${AWS_SURVEY_SSM_POLL:-5}"
# 1 にすると最初から gzip + base64 に畳んで送る（上限に掛かったときの経路を確かめる開発用）。
SSH_PACK="${AWS_SURVEY_SSH_PACK:-0}"

die() { ui_die "$@"; }

usage() { sed -n '4,13p' "$0" | sed 's/^# \{0,1\}//'; }

# ---- 引数 ----
SUB="${1:-}"; [ $# -gt 0 ] && shift
[ -n "$SUB" ] || { usage >&2; die "サブコマンドを指定してください（setup <対象> / setup --print / list / verify <host> / rotate / remove <host>）。"; }

TARGET=""; PRINT=0; USER_OPT=""; ALIAS_OPT=""; STRICT=false
LOGS_JSON='{}'; DENY_JSON='[]'

valid_user()  { [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; }
valid_alias() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; }
valid_instance_id() { [[ "$1" =~ ^i-[0-9a-f]{8,17}$ ]]; }
valid_log_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$ ]]; }
# 登録するログの glob。絶対パスで、ディレクトリ部分が確定している（* ? [ と .. を含まない）こと。
valid_log_glob() {
  local p="$1" dir name
  [[ "$p" == /* ]] || return 1
  [[ "$p" != *[[:cntrl:]]* ]] || return 1
  case "$p/" in */../*|*/./*|*//*) return 1 ;; esac
  dir=${p%/*}; name=${p##*/}
  [ -n "$name" ] || return 1
  case "$dir" in *'*'*|*'?'*|*'['*) return 1 ;; esac
  return 0
}
valid_deny_glob() {
  local p="$1"
  [[ "$p" == /* ]] || return 1
  [[ "$p" != *[[:cntrl:]]* ]] || return 1
  case "$p/" in */../*) return 1 ;; esac
  return 0
}

add_log() {
  local spec="$1" name glob
  case "$spec" in *=*) ;; *) die "--log は <名前>=<パス or glob> の形で指定してください: $spec" ;; esac
  name=${spec%%=*}; glob=${spec#*=}
  valid_log_name "$name" || die "--log の名前が不正です: ${name}（英数字で始まり、英数字 . _ - で 40 字まで）"
  valid_log_glob "$glob" || die "--log のパスが不正です: ${glob}（絶対パスで、ディレクトリ部分に * ? [ と .. を含めない）"
  LOGS_JSON=$(jq -c --arg k "$name" --arg v "$glob" '. + {($k): $v}' <<< "$LOGS_JSON")
}
add_deny() {
  valid_deny_glob "$1" || die "--deny のパターンが不正です: ${1}（絶対パスの glob。.. は含めない）"
  DENY_JSON=$(jq -c --arg v "$1" '. + [$v]' <<< "$DENY_JSON")
}

parse_setup_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --print)  PRINT=1; shift ;;
      --user)   [ $# -ge 2 ] || die "--user には名前を指定してください。"; USER_OPT="$2"; shift 2 ;;
      --user=*) USER_OPT="${1#--user=}"; shift ;;
      --alias)  [ $# -ge 2 ] || die "--alias には名前を指定してください。"; ALIAS_OPT="$2"; shift 2 ;;
      --alias=*) ALIAS_OPT="${1#--alias=}"; shift ;;
      --log)    [ $# -ge 2 ] || die "--log には <名前>=<パス or glob> を指定してください。"; add_log "$2"; shift 2 ;;
      --log=*)  add_log "${1#--log=}"; shift ;;
      --deny)   [ $# -ge 2 ] || die "--deny には glob を指定してください。"; add_deny "$2"; shift 2 ;;
      --deny=*) add_deny "${1#--deny=}"; shift ;;
      --strict) STRICT=true; shift ;;
      -*) die "setup の不明なオプション: $1" ;;
      *)  [ -z "$TARGET" ] || die "対象は 1 つだけ指定してください: $TARGET と $1"; TARGET="$1"; shift ;;
    esac
  done
}

# ---- 鍵 ----
ensure_key() {
  if [ -f "$KEY.pub" ]; then return 0; fi
  command -v ssh-keygen >/dev/null || die "ssh-keygen が見つかりません（鍵を作れません）。"
  mkdir -p "$SSH_DIR" && chmod 700 "$SSH_DIR" || die "鍵の置き場を作れません: $SSH_DIR"
  # コメントに対象の名前を入れない（authorized_keys に残る）
  ssh-keygen -q -t ed25519 -N '' -C diag -f "$KEY" || die "鍵を作れませんでした: $KEY"
  ui_ok "鍵を作りました: ${KEY}${C_DIM}（対象ごとに 1 対。コンテナには読み取り専用で渡ります）${C_RESET}" >&2
}
# 1 引数は秘密鍵のパス（省略時は $KEY）。PUBKEY を埋める
read_pubkey() {
  local key="${1:-$KEY}" line
  line=$(head -n 1 "$key.pub")
  case "$line" in
    "ssh-ed25519 "*) ;;
    *) die "公開鍵が ssh-ed25519 ではありません: $key.pub" ;;
  esac
  PUBKEY="$(printf '%s' "$line" | cut -d' ' -f1,2) diag"
  [[ "$PUBKEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+\ diag$ ]] || die "公開鍵の形式を読めません: $key.pub"
}
# ファイルの更新日時を ISO 8601（ローカル時刻）で。鍵の作成日時に使う（作った・差し替えた時刻がそのまま残る）
file_mtime() {
  local t
  t=$(stat -c %Y "$1" 2>/dev/null) || t=$(stat -f %m "$1" 2>/dev/null) || { echo "不明"; return 0; }
  { date -r "$t" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null || date -d "@$t" +%Y-%m-%dT%H:%M:%S%z; } | sed -E 's/([0-9]{2})$/:\1/'
}
now_iso() { date +%Y-%m-%dT%H:%M:%S%z | sed -E 's/([0-9]{2})$/:\1/'; }

# ---- 導入スクリプトの組み立て ----
# 雛形の @@GATEWAY_PY@@ / @@DIAG_ROOT_PY@@ / @@CONF_JSON@@ の行を本文に置き換え、
# それ以外の行の @@USER@@ / @@PUBKEY@@ を埋める。heredoc の終端行が本文に無いことは先に確かめる。
render_install() {
  local line f delim
  for f in gateway.py diag-root.py; do
    for delim in __DIAG_GATEWAY__ __DIAG_ROOT__ __DIAG_CONF__ __DIAG_CONF_PY__; do
      ! grep -qxF "$delim" "$EC2_DIR/$f" || die "$f に heredoc の終端行 $delim が含まれています。"
    done
  done
  shopt -u patsub_replacement 2>/dev/null || true
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      '@@GATEWAY_PY@@')   cat "$EC2_DIR/gateway.py" ;;
      '@@DIAG_ROOT_PY@@') cat "$EC2_DIR/diag-root.py" ;;
      '@@CONF_JSON@@')    printf '%s\n' "$CONF_JSON" ;;
      *)
        line=${line//@@USER@@/$SSH_USER}
        line=${line//@@PUBKEY@@/$PUBKEY}
        printf '%s\n' "$line" ;;
    esac
  done < "$TEMPLATE"
}

# 撤去スクリプト。埋めるのはユーザー名だけ
render_remove() {
  local line
  shopt -u patsub_replacement 2>/dev/null || true
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "${line//@@USER@@/$SSH_USER}"
  done < "$REMOVE_TEMPLATE"
}

# 導入スクリプトを gzip + base64 で 1 つの heredoc に畳み、EC2 側で展開して実行する短い bash にする。
# SSM のパラメータ上限に掛かったときの逃げ道（設計文書の第 4.3 節）。
pack_install() {
  local b64
  b64=$(printf '%s\n' "$1" | gzip -9 -n | base64 | tr -d '\n' | fold -w 76) || return 1
  cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
t=\$(mktemp)
trap 'rm -f "\$t"' EXIT
base64 -d <<'__DIAG_B64__' | gzip -dc > "\$t"
$b64
__DIAG_B64__
bash "\$t"
EOF
}

build_conf() {
  CONF_JSON=$(jq -n --argjson strict "$STRICT" --argjson logs "$LOGS_JSON" --argjson deny "$DENY_JSON" \
                '{strict: $strict, logs: $logs, deny: $deny}')
}
show_conf() {
  ui_kv "登録するログ" "$(jq -r 'to_entries | map("\(.key)=\(.value)") | join(" ") | if . == "" then "（なし。root 専用のログは実行時に自動検出）" else . end' <<< "$LOGS_JSON")"
  ui_kv "拒否パターン" "$(jq -r 'if length == 0 then "（追加なし）" else join(" ") end' <<< "$DENY_JSON")"
  ui_kv "strict" "$STRICT"
}
# 1 引数は使う秘密鍵（省略時は $KEY。無ければ作る）
prepare_script() {
  local key="${1:-$KEY}"
  [ "$key" != "$KEY" ] || ensure_key
  read_pubkey "$key"
  build_conf
  SCRIPT=$(render_install) || die "導入スクリプトを組み立てられませんでした。"
  bash -n <(printf '%s\n' "$SCRIPT") || die "組み立てた導入スクリプトが bash として読めません。"
  SCRIPT_BYTES=$(printf '%s\n' "$SCRIPT" | wc -c | tr -d ' ')
}

# ---- 元プロファイルで AWS を叩く ----
src() { aws --profile "$PROFILE_SRC" --region "$REGION" --output json "$@"; }

check_source_profile() {
  local who
  if ! who=$(aws sts get-caller-identity --profile "$PROFILE_SRC" --query Arn --output text 2>&1); then
    ui_err "元プロファイル $PROFILE_SRC が使えません"
    ui_raw "$who"
    [ -z "${REFRESH_CMD:-}" ] || [ "$REFRESH_CMD" = null ] || ui_text "ログインし直すコマンド: $(ui_cmd "$REFRESH_CMD")"
    die "導入は元プロファイル（強い権限）で行います。$PROFILE_SRC でログインしてから、もう一度実行してください。"
  fi
  ui_ok "$who"
}

# 対象を instance-id に解決する。Name タグなら running のものが 1 台だけ見つかること。
resolve_target() {
  local out n
  if valid_instance_id "$TARGET"; then
    out=$(src ec2 describe-instances --instance-ids "$TARGET" \
            --query 'Reservations[].Instances[].{id:InstanceId,state:State.Name,name:Tags[?Key==`Name`]|[0].Value,arch:Architecture,image:ImageId}' 2>&1) \
      || { ui_raw "$out"; die "インスタンス $TARGET を読めません。"; }
  else
    out=$(src ec2 describe-instances --filters "Name=tag:Name,Values=$TARGET" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
            --query 'Reservations[].Instances[].{id:InstanceId,state:State.Name,name:Tags[?Key==`Name`]|[0].Value,arch:Architecture,image:ImageId}' 2>&1) \
      || { ui_raw "$out"; die "Name タグ $TARGET のインスタンスを探せません。"; }
  fi
  n=$(jq 'length' <<< "$out")
  [ "$n" -ge 1 ] || die "対象が見つかりません: ${TARGET}（instance-id か Name タグ。停止中でも指定できますが、導入には running が要ります）"
  [ "$n" -eq 1 ] || die "Name タグ $TARGET のインスタンスが $n 台あります。instance-id で指定してください: $(jq -r 'map(.id) | join(" ")' <<< "$out")"
  INSTANCE_ID=$(jq -r '.[0].id' <<< "$out")
  INSTANCE_STATE=$(jq -r '.[0].state' <<< "$out")
  INSTANCE_NAME=$(jq -r '.[0].name // empty' <<< "$out")
  INSTANCE_ARCH=$(jq -r '.[0].arch // empty' <<< "$out")
  ui_kv "インスタンス" "${INSTANCE_ID}${INSTANCE_NAME:+ (Name: $INSTANCE_NAME)}  ${C_DIM}${INSTANCE_STATE}${INSTANCE_ARCH:+ / $INSTANCE_ARCH}${C_RESET}"
  [ "$INSTANCE_STATE" = running ] || die "インスタンスが running ではありません（${INSTANCE_STATE}）。起動してから、もう一度実行してください。"
}

# config の Host 名。--alias > Name タグ（使える文字なら） > instance-id
decide_alias() {
  if [ -n "$ALIAS_OPT" ]; then
    valid_alias "$ALIAS_OPT" || die "--alias が不正です: ${ALIAS_OPT}（英数字で始まり、英数字 . _ - で 64 字まで）"
    HOST_ALIAS="$ALIAS_OPT"
  elif [ -n "$INSTANCE_NAME" ] && valid_alias "$INSTANCE_NAME"; then
    HOST_ALIAS="$INSTANCE_NAME"
  else
    HOST_ALIAS="$INSTANCE_ID"
  fi
  # 同じ別名が別のインスタンスに使われていたら止める（上書き事故を防ぐ）
  local used
  used=$(jq -r --arg a "$HOST_ALIAS" '.ssh.hosts[$a].instance_id // empty' "$ENV_FILE")
  if [ -n "$used" ] && [ "$used" != "$INSTANCE_ID" ]; then
    die "別名 $HOST_ALIAS は既に $used に使われています。--alias で別の名前を付けてください。"
  fi
  ui_kv "別名（Host）" "$HOST_ALIAS"
}

# SSM 管理下（PingStatus=Online）でなければ、必要なものを案内して終わる。ここは自動化しない。
# SSM_PING / SSM_PLATFORM を埋める。Online なら 0
ssm_online() {
  local out
  out=$(src ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
          --query 'InstanceInformationList[0].{ping:PingStatus,platform:PlatformName,version:PlatformVersion,agent:AgentVersion}' 2>&1) \
    || { ui_raw "$out"; die "SSM の管理状態を読めません（ssm:DescribeInstanceInformation）。"; }
  SSM_PING=$(jq -r '.ping // empty' <<< "$out")
  SSM_PLATFORM=$(jq -r 'if .platform then "\(.platform) \(.version // "")  agent \(.agent // "?")" else "" end' <<< "$out")
  [ "$SSM_PING" = Online ]
}
check_ssm() {
  local ping platform
  ssm_online && { ui_ok "SSM 管理下（Online）  ${C_DIM}${SSM_PLATFORM}${C_RESET}"; return 0; }
  ping="$SSM_PING"
  {
    ui_err "SSM の管理下にありません（PingStatus: ${ping:-なし}）"
    echo ""
    ui_text "導入は SSM RunCommand で行うため、対象が SSM 管理下（Online）である必要があります。次を確かめてください。"
    ui_text "  - インスタンスプロファイルに AmazonSSMManagedInstanceCore が付いているか（付けるのは利用者の判断で行います）"
    ui_text "  - SSM Agent が動いているか（Amazon Linux / Ubuntu の公式 AMI には入っています）"
    ui_text "  - ssm.$REGION.amazonaws.com などへ出られるか（パブリック IP + IGW、NAT、または VPC エンドポイント）"
    ui_text "起動直後なら 1〜2 分待つと Online になります。"
    echo ""
    ui_text "SSM を使えない対象には、スクリプトを書き出して root で手実行する方法があります。"
    also_cmd "$AWS_SURVEY_CMD ssh setup --print > install-diag.sh" "導入スクリプトを 1 本の bash として書き出します"
    exit 1
  }
}

# AWS-RunShellScript で root 実行し、完了を待つ。COMMAND_ID / RUN_STATUS / RUN_STDOUT / RUN_STDERR を埋める。
# 1 引数はスクリプト本文、2 引数は RunCommand のコメント（省略時は diag setup）。送れなければ 2（大きすぎる）か 1 を返す。
send_and_wait() {
  local script="$1" comment="${2:-diag setup}" input cmd_id out status waited=0 rc=0
  RUN_STATUS=""; RUN_STDOUT=""; RUN_STDERR=""; SEND_ERROR=""
  # $(...) で受けたスクリプトは末尾の改行が落ちているので戻す
  input=$(jq -n --arg id "$INSTANCE_ID" --arg script "$script"$'\n' --arg t "$SSM_TIMEOUT" --arg c "$comment" \
            '{DocumentName: "AWS-RunShellScript", InstanceIds: [$id], Comment: $c,
              Parameters: {commands: [$script], executionTimeout: [$t]}}')
  if ! out=$(src ssm send-command --cli-input-json "$input" 2>&1); then
    SEND_ERROR="$out"
    case "$out" in
      *MaxDocumentSizeExceeded*|*"too large"*|*"Too large"*|*"exceed"*|*"exceeds"*|*"length less than or equal"*|*"Member must have length"*) return 2 ;;
      *) return 1 ;;
    esac
  fi
  COMMAND_ID=$(jq -r '.Command.CommandId // empty' <<< "$out")
  [ -n "$COMMAND_ID" ] || { SEND_ERROR="$out"; return 1; }
  ui_kv "RunCommand" "$COMMAND_ID"
  while :; do
    out=$(src ssm get-command-invocation --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" 2>&1) || out='{"Status":"Pending"}'
    status=$(jq -r '.Status // "Pending"' <<< "$out" 2>/dev/null || echo Pending)
    case "$status" in
      Pending|InProgress|Delayed)
        [ "$waited" -lt "$SSM_TIMEOUT" ] || { RUN_STATUS=HostTimeout; return 1; }
        ui_status "EC2 でスクリプトを実行中… ${status}（${waited} 秒）"
        sleep "$SSM_POLL"; waited=$((waited + SSM_POLL)) ;;
      *) break ;;
    esac
  done
  ui_status_done
  RUN_STATUS="$status"
  RUN_STDOUT=$(jq -r '.StandardOutputContent // ""' <<< "$out")
  RUN_STDERR=$(jq -r '.StandardErrorContent // ""' <<< "$out")
  [ "$status" = Success ] || return 1
  return 0
}

# 出力の末尾の HOSTKEY 行を known_hosts の行（<instance-id> <type> <base64>）にする
collect_hostkeys() {
  HOSTKEY_LINES=$(printf '%s\n' "$RUN_STDOUT" | grep -E '^HOSTKEY (ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+) [A-Za-z0-9+/=]+$' \
                    | sed -E "s/^HOSTKEY /$INSTANCE_ID /") || true
  [ -n "$HOSTKEY_LINES" ]
}

# $AWS_DIR/ssh/config を environment.json の ssh.hosts 全体から作り直す（コンテナの ssh -F が読む）
write_ssh_config() {
  local tmp
  tmp=$(mktemp) || die "一時ファイルを作れません。"
  {
    echo "# aws-survey が生成。手で直さない（aws-survey ssh setup で作り直す）"
    jq -r --arg dir "$CONTAINER_SSH_DIR" '
      (.ssh.user // "diag") as $default_user |
      .ssh.hosts | to_entries[] | .key as $alias | .value |
      "\nHost \($alias)\n    HostName \(.instance_id)\n    User \(.user // $default_user)\n    IdentityFile \($dir)/id_ed25519\n    IdentitiesOnly yes\n    ProxyCommand aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p\n    StrictHostKeyChecking yes\n    UserKnownHostsFile \($dir)/known_hosts\n    BatchMode yes\n    RequestTTY no\n    ForwardAgent no\n    ServerAliveInterval 15"' "$ENV_FILE"
  } > "$tmp" || { rm -f "$tmp"; die "config を組み立てられません。"; }
  mv "$tmp" "$SSH_CONFIG" && chmod 644 "$SSH_CONFIG" || die "書き込めません: $SSH_CONFIG"
}

# known_hosts: 1 引数のインスタンスの行を 2 引数の行（空なら削除だけ）に入れ替え、他のホストの行は残す
write_known_hosts() {
  local id="${1:-$INSTANCE_ID}" lines="${2-$HOSTKEY_LINES}" tmp
  tmp=$(mktemp) || die "一時ファイルを作れません。"
  { [ ! -f "$KNOWN_HOSTS" ] || grep -v -E "^$id " "$KNOWN_HOSTS"; [ -z "$lines" ] || printf '%s\n' "$lines"; } > "$tmp"
  mv "$tmp" "$KNOWN_HOSTS" && chmod 644 "$KNOWN_HOSTS" || die "書き込めません: $KNOWN_HOSTS"
}

record_host() {
  local tmp now
  now=$(now_iso)
  tmp=$(mktemp) || die "一時ファイルを作れません。"
  jq --arg a "$HOST_ALIAS" --arg id "$INSTANCE_ID" --arg user "$SSH_USER" --arg now "$now" \
     --argjson logs "$LOGS_JSON" --argjson deny "$DENY_JSON" --argjson strict "$STRICT" '
     .ssh = (.ssh // {}) | .ssh.user = (.ssh.user // $user) |
     .ssh.hosts = (.ssh.hosts // {}) |
     .ssh.hosts[$a] = {instance_id: $id, user: $user, installed_at: $now, logs: $logs, deny: $deny, strict: $strict}' \
     "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE" || { rm -f "$tmp"; die "environment.json に記録できません。"; }
}

# 導入スクリプトを INSTANCE_ID に送って完了を待つ。素のまま送り、大きさで拒否されたら畳んで送り直す。FORM に送った形が入る
run_install() {
  local script="$1" rc=2 packed packed_bytes
  FORM=plain
  if [ "$SSH_PACK" = 1 ]; then
    ui_text "AWS_SURVEY_SSH_PACK=1: 最初から gzip + base64 に畳んで送ります。"
  else
    send_and_wait "$script"; rc=$?
  fi
  if [ "$rc" -eq 2 ]; then
    if [ "$SSH_PACK" != 1 ]; then
      ui_warn "素のスクリプトは SSM のパラメータ上限に掛かりました。gzip + base64 に畳んで送り直します。"
      ui_raw "$(printf '%s\n' "$SEND_ERROR" | head -3)"
    fi
    packed=$(pack_install "$script") || die "スクリプトを圧縮できません（gzip / base64 が要ります）。"
    packed_bytes=$(printf '%s\n' "$packed" | wc -c | tr -d ' ')
    ui_kv "圧縮後" "${packed_bytes} バイト"
    FORM=packed
    send_and_wait "$packed"; rc=$?
  fi
  return "$rc"
}
show_install_failure() {
  if [ -n "${RUN_STATUS:-}" ]; then
    ui_err "導入スクリプトが失敗しました（${RUN_STATUS}）"
    [ -z "${RUN_STDOUT:-}" ] || ui_raw "$(printf '%s\n' "$RUN_STDOUT" | tail -20)"
    [ -z "${RUN_STDERR:-}" ] || ui_raw "$(printf '%s\n' "$RUN_STDERR" | tail -20)"
    ui_text "sshd と sudoers は元に戻しています。ゲートウェイの置き場（/usr/local/lib/diag）は残ることがありますが、ログインは有効になっていません。"
    ui_text "原因を直して同じコマンドを実行すれば、続きから導入し直せます（冪等）。"
  else
    ui_err "RunCommand を送れませんでした"
    ui_raw "$SEND_ERROR"
  fi
}

cmd_setup() {
  parse_setup_args "$@"
  if [ -n "$USER_OPT" ]; then
    valid_user "$USER_OPT" || die "--user が不正です: ${USER_OPT}（小文字英字か _ で始まり、小文字英数字 _ - で 32 字まで）"
    SSH_USER="$USER_OPT"
  fi
  [ -f "$TEMPLATE" ] || die "導入スクリプトの雛形がありません: $TEMPLATE"
  for f in gateway.py diag-root.py; do [ -f "$EC2_DIR/$f" ] || die "見つかりません: $EC2_DIR/$f"; done

  if [ "$PRINT" -eq 1 ]; then cmd_setup_print; return; fi
  [ -n "$TARGET" ] || die "対象（instance-id か Name タグ）を指定してください。スクリプトだけ出すなら --print。"
  command -v aws >/dev/null || die "aws コマンドが見つかりません。"

  ui_title "aws-survey ssh setup"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "アカウント" "$ACCOUNT_ID"
  ui_kv "リージョン" "$REGION"
  ui_kv "ログインユーザー" "$SSH_USER"
  show_conf
  echo ""

  ui_head "1/5 元プロファイル $PROFILE_SRC で導入する"
  ui_text "導入（SSM RunCommand）は強い権限の仕事です。調査用の一時キーには渡しません。"
  check_source_profile
  echo ""

  ui_head "2/5 対象と SSM の管理状態"
  resolve_target
  decide_alias
  check_ssm
  echo ""

  ui_head "3/5 鍵と導入スクリプト"
  prepare_script
  ui_kv "鍵" "$KEY"
  ui_kv "導入スクリプト" "${SCRIPT_BYTES} バイト"
  echo ""

  ui_head "4/5 EC2 で root 実行する（AWS-RunShellScript）"
  run_install "$SCRIPT" || { show_install_failure; die "何も記録していません（タグ・config・environment.json）。"; }
  ui_ok "導入スクリプトが成功しました${C_DIM}（送った形: ${FORM}）${C_RESET}"
  ui_raw "$(printf '%s\n' "$RUN_STDOUT" | grep -E '^\[diag\]' | tail -12)"
  collect_hostkeys || die "導入は成功しましたが、出力に HOSTKEY 行がありません。known_hosts を作れないため記録しません。"
  ui_kv "ホスト鍵" "$(printf '%s\n' "$HOSTKEY_LINES" | wc -l | tr -d ' ') 件"
  echo ""

  ui_head "5/5 タグと接続設定"
  local out
  if ! out=$(src ec2 create-tags --resources "$INSTANCE_ID" --tags "Key=diag:ssh,Value=$SURVEY_NAME" 2>&1); then
    ui_raw "$out"
    die "タグ diag:ssh=$SURVEY_NAME を付けられませんでした。導入は済んでいるので、同じコマンドをもう一度実行すればここからやり直せます。"
  fi
  ui_ok "タグを付けました: diag:ssh=$SURVEY_NAME"
  record_host
  write_known_hosts
  write_ssh_config
  ui_ok "environment.json に記録しました: ssh.hosts.$HOST_ALIAS"
  ui_kv "config" "$SSH_CONFIG"
  ui_kv "known_hosts" "$KNOWN_HOSTS"
  echo ""
  ui_text "調査コンテナからは ec2 $HOST_ALIAS <動詞> で使います（一覧: ec2 $HOST_ALIAS help）。"
  ui_text "使えるようにするには、あと 3 手（接続を許すポリシー → 一時キーの発行し直し → 接続の確認）が要ります。"
  ui_text "引数なしの $AWS_SURVEY_CMD を 1 回打てば、3 手とも順に進みます（1 手ごとに Enter で確認するだけ）。"
  next_cmd "$AWS_SURVEY_CMD" "残りの 3 手を順に進めます"
  also_cmd "$AWS_SURVEY_CMD ssh list" "登録済みホストと導入状態を確かめます"
  ui_text "手で 1 手ずつ進めるなら: $AWS_SURVEY_CMD role --create → $AWS_SURVEY_CMD credentials → $AWS_SURVEY_CMD ssh verify $HOST_ALIAS"
  if [ "$AUTH_ROUTE" != own_role ] && [ -n "$AUTH_ROUTE" ]; then
    ui_text "ロールを借りているので、1 手目はポリシーの JSON を表示するだけです。管理者に付けてもらってから、もう一度 $AWS_SURVEY_CMD を打ちます。"
  fi
  setup_offer_continue
}

# 端末なら、その場で引数なしの aws-survey に進む（残りの 3 手を順に聞いて実行する）。
# 端末でなければ（エージェントの実行環境・テスト）案内だけで終わる。黙って AWS を叩かないため。
setup_offer_continue() {
  [ "${AWS_SURVEY_CHAIN:-0}" != 1 ] || return 0
  { [ -t 0 ] && [ -t 1 ]; } || return 0
  local ans
  echo ""
  printf '  %s❯%s 続けて %s を実行しますか？ %s(Y/n)%s: ' \
    "$C_CYAN" "$C_RESET" "$(ui_cmd "$AWS_SURVEY_CMD")" "$C_DIM" "$C_RESET"
  read -r ans || { echo ""; return 0; }
  case "$ans" in
    ""|y|Y|yes|YES) ;;
    *) ui_text "ここで止めます。続けるときは上のコマンドを打ってください。"; return 0 ;;
  esac
  echo ""
  exec "$AWS_SURVEY_HOME/bin/aws-survey"
}

cmd_setup_print() {
  # 案内は標準エラー、スクリプトは標準出力（> ファイル でそのまま使える）
  exec 3>&1 1>&2
  ui_title "aws-survey ssh setup --print"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "ログインユーザー" "$SSH_USER"
  [ -z "$TARGET" ] || ui_kv "対象" "${TARGET}${C_DIM}（--print では使いません）${C_RESET}"
  prepare_script
  show_conf
  printf '%s\n' "$SCRIPT" >&3
  echo ""
  ui_ok "導入スクリプトを標準出力に出しました${C_DIM}（$SCRIPT_BYTES バイト）${C_RESET}"
  ui_text "対象の EC2 に root で実行してください。末尾の HOSTKEY 行は known_hosts に使います。"
  ui_text "sshd の設定は sshd -t で検証してから reload します。検証に失敗したら元に戻して非ゼロで終わります。"
}

# ---- list ----
cmd_list() {
  [ $# -eq 0 ] || die "list に引数はありません。"
  ui_title "aws-survey ssh list"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "ログインユーザー" "$SSH_USER"
  if [ -f "$KEY" ]; then
    ui_kv "鍵" "$KEY  ${C_DIM}作成 $(file_mtime "$KEY")${C_RESET}"
  else
    ui_kv "鍵" "（まだ無い。ssh setup で作られます）"
  fi
  local n
  n=$(jq -r '.ssh.hosts // {} | length' "$ENV_FILE")
  if [ "$n" -eq 0 ]; then
    echo ""
    ui_skip "登録済みホストはありません"
    next_cmd "$AWS_SURVEY_CMD ssh setup <instance-id | Name タグ>" "EC2 に診断ゲートウェイを導入して登録します"
    return 0
  fi
  # 生きた状態（インスタンスの状態・タグ・SSM）は元プロファイルで読めるときだけ足す
  local live=0 ids states tags ssm
  if command -v aws >/dev/null && aws sts get-caller-identity --profile "$PROFILE_SRC" --query Arn --output text >/dev/null 2>&1; then
    ids=$(jq -r '.ssh.hosts | map(.instance_id) | join(" ")' "$ENV_FILE")
    # shellcheck disable=SC2086
    states=$(src ec2 describe-instances --instance-ids $ids \
               --query 'Reservations[].Instances[].{id:InstanceId,state:State.Name,tag:Tags[?Key==`diag:ssh`]|[0].Value}' 2>/dev/null) || states='[]'
    ssm=$(src ssm describe-instance-information --filters "Key=InstanceIds,Values=$(printf '%s' "$ids" | tr ' ' ',')" \
            --query 'InstanceInformationList[].{id:InstanceId,ping:PingStatus}' 2>/dev/null) || ssm='[]'
    live=1
  fi
  echo ""
  ui_head "登録済みホスト（${n}）"
  local alias id user at strict logs state tag ping line
  while IFS=$'\t' read -r alias id user at strict logs; do
    line="$alias  ${C_DIM}$id  user $user  導入 $at${C_RESET}"
    if [ "$live" -eq 1 ]; then
      state=$(jq -r --arg id "$id" 'map(select(.id == $id)) | .[0].state // "不明"' <<< "$states")
      tag=$(jq -r --arg id "$id" 'map(select(.id == $id)) | .[0].tag // ""' <<< "$states")
      ping=$(jq -r --arg id "$id" 'map(select(.id == $id)) | .[0].ping // "なし"' <<< "$ssm")
      if [ "$state" = running ] && [ "$tag" = "$SURVEY_NAME" ] && [ "$ping" = Online ]; then
        ui_ok "$line"
      else
        ui_warn "$line"
      fi
      ui_text "状態 $state / タグ diag:ssh=${tag:-（無し）} / SSM $ping"
    else
      ui_ok "$line"
    fi
    [ "$strict" != true ] || ui_text "strict（一般読み取り段は無効）"
    [ -z "$logs" ] || ui_text "登録済みログ: $logs"
  done < <(jq -r '.ssh.hosts | to_entries[] |
             [.key, .value.instance_id, (.value.user // "?"), (.value.installed_at // "?"), (.value.strict // false | tostring),
              (.value.logs // {} | to_entries | map("\(.key)=\(.value)") | join(" "))] | @tsv' "$ENV_FILE")
  [ "$live" -eq 1 ] || { echo ""; ui_text "元プロファイル $PROFILE_SRC が使えないため、インスタンスの状態とタグは確かめていません。"; }
  echo ""
  ui_kv "config" "$([ -f "$SSH_CONFIG" ] && echo "$SSH_CONFIG" || echo "（無い。ssh setup で作られます）")"
  ui_kv "known_hosts" "$([ -f "$KNOWN_HOSTS" ] && echo "$KNOWN_HOSTS" || echo "（無い）")"
  also_cmd "$AWS_SURVEY_CMD ssh rotate" "鍵を作り直して、登録済みホスト全部に再導入します"
  also_cmd "$AWS_SURVEY_CMD ssh remove <host>" "そのホストから撤去し、タグと記録を消します"
}

# ---- verify ----
# 調査コンテナのイメージ（aws-survey run と同じもの）を用意し、一時キーと鍵を読み取り専用で渡して
# ec2 --selftest <host> を打つ。ホストの Mac に session-manager-plugin は要らない。
# 中の判定（ゲートウェイの拒否・sshd の制限・セッションの後片付け）はラッパーが持ち、ここは結果を読むだけ。
# 終わったら元プロファイルが使えるときだけ、SSM 側にセッションが残っていないことをもう一度見る。
cmd_verify() {
  [ $# -eq 1 ] || die "verify には <host>（ssh list に出る別名）を 1 つ指定してください。"
  local host="$1" id user
  id=$(jq -r --arg a "$host" '.ssh.hosts[$a].instance_id // empty' "$ENV_FILE")
  [ -n "$id" ] || die "未登録のホストです: ${host}（登録済み: ${SSH_HOSTS:-なし}）"
  user=$(jq -r --arg a "$host" '.ssh.hosts[$a].user // empty' "$ENV_FILE")
  ui_title "aws-survey ssh verify"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "ホスト" "$host  ${C_DIM}${id}  user ${user:-$SSH_USER}${C_RESET}"
  ui_text "調査コンテナから実際に接続し、通るもの・塞がっているもの・終了後にセッションが残らないことを確かめます。"
  echo ""

  ui_head "1/3 一時キーと鍵"
  [ -f "$AWS_DIR/credentials" ] || { ui_err "一時キーがありません"; next_cmd "$AWS_SURVEY_CMD credentials" "SSH 接続の権限を含めて一時キーを発行します"; exit 1; }
  [ -f "$SSH_CONFIG" ] && [ -f "$KEY" ] || die "接続設定か鍵がありません（${SSH_DIR}）。$AWS_SURVEY_CMD ssh setup で作られます。"
  local exp now
  exp=$(jq -r '.expiration // empty' "$AWS_DIR/session.json" 2>/dev/null)
  if [ -n "$exp" ]; then
    now=$(date -u +%Y-%m-%dT%H:%M:%S)
    if [[ "${exp:0:19}" < "$now" ]]; then
      ui_err "一時キーは期限切れです（${exp}）"
      next_cmd "$AWS_SURVEY_CMD credentials" "一時キーを発行し直します"
      exit 1
    fi
    ui_ok "一時キー ${C_DIM}（$exp まで）${C_RESET}"
  else
    ui_ok "一時キー ${C_DIM}（期限は不明）${C_RESET}"
  fi
  ui_ok "鍵と接続設定 ${C_DIM}$SSH_DIR${C_RESET}"
  # 一時キーと鍵の置き場を Docker Desktop がマウントできるか（load-env.sh）
  docker_check_shared "$AWS_DIR" || exit 1
  echo ""

  ui_head "2/3 調査コンテナのイメージを用意する"
  container_prepare || exit 1
  echo ""

  ui_head "3/3 コンテナから ec2 --selftest $host を打つ"
  local rc=0 out
  container_run -- ec2 --selftest "$host" 2>&1 | sed 's/^/    /' || rc=${PIPESTATUS[0]}
  echo ""
  # 元プロファイルで読めるなら、SSM 側にこの対象へのセッションが残っていないことをもう一度見る
  if command -v aws >/dev/null && aws sts get-caller-identity --profile "$PROFILE_SRC" --query Arn --output text >/dev/null 2>&1; then
    sleep 2
    out=$(aws --profile "$PROFILE_SRC" --region "$REGION" --output text ssm describe-sessions --state Active \
            --filters "key=Target,value=$id" \
            --query "Sessions[?starts_with(SessionId, \`${SESSION_NAME_PREFIX}-\`)].[SessionId, StartDate]" 2>/dev/null) || out=''
    if [ -z "$out" ]; then
      ui_ok "SSM のセッションは残っていません（元プロファイルで確認）"
    else
      ui_err "SSM のセッションが残っています"
      ui_raw "$out"
      rc=1
    fi
  else
    ui_skip "元プロファイル $PROFILE_SRC が使えないため、SSM 側のセッションの確認は省きました"
  fi
  echo ""
  if [ "$rc" -eq 0 ]; then
    ui_ok "検証が通りました。調査コンテナからは ec2 $host <動詞> で使えます（一覧: ec2 $host help）"
    # 引数なしの aws-survey が「確かめ済み」と見る記録。setup / rotate が installed_at を進めると、また確かめる案内になる
    local tmp
    tmp=$(mktemp) && jq --arg a "$host" --arg now "$(now_iso)" '.ssh.hosts[$a].verified_at = $now' "$ENV_FILE" > "$tmp" \
      && mv "$tmp" "$ENV_FILE" && ui_text "environment.json に記録しました: ssh.hosts.${host}.verified_at"
  else
    ui_err "検証に失敗した項目があります（上の ✗ を見てください）"
  fi
  return "$rc"
}

# ---- rotate ----
# 新しい鍵を別名（id_ed25519.next）で作り、登録済みホスト全部に再導入（冪等）してから差し替える。
# 1 台でも失敗したらそこで止め、新しい鍵は捨てて古い鍵・記録・config を残す（半端な状態を残さない方針は setup と同じ）。
# 失敗までに新しい鍵になったホストは古い鍵では届かなくなるので、どれがそうかを表示する。
load_host() {  # 1 引数の別名から INSTANCE_ID / SSH_USER / LOGS_JSON / DENY_JSON / STRICT を埋める
  local rec
  rec=$(jq -c --arg a "$1" '.ssh.hosts[$a] // empty' "$ENV_FILE")
  [ -n "$rec" ] || die "未登録のホストです: ${1}（登録済み: ${SSH_HOSTS:-なし}）"
  INSTANCE_ID=$(jq -r '.instance_id' <<< "$rec")
  valid_instance_id "$INSTANCE_ID" || die "environment.json の ssh.hosts.$1.instance_id が不正です: $INSTANCE_ID"
  SSH_USER=$(jq -r --arg d "$SSH_USER" '.user // $d' <<< "$rec")
  valid_user "$SSH_USER" || die "environment.json の ssh.hosts.$1.user が不正です: $SSH_USER"
  LOGS_JSON=$(jq -c '.logs // {}' <<< "$rec")
  DENY_JSON=$(jq -c '.deny // []' <<< "$rec")
  STRICT=$(jq -r '.strict // false' <<< "$rec")
}

cmd_rotate() {
  [ $# -eq 0 ] || die "rotate に引数はありません（登録済みホスト全部に再導入します）。"
  [ -n "$SSH_HOSTS" ] || die "登録済みホストがありません。鍵は $AWS_SURVEY_CMD ssh setup が作ります。"
  command -v aws >/dev/null || die "aws コマンドが見つかりません。"
  command -v ssh-keygen >/dev/null || die "ssh-keygen が見つかりません（鍵を作れません）。"
  [ -f "$TEMPLATE" ] || die "導入スクリプトの雛形がありません: $TEMPLATE"
  local hosts n
  hosts=$(jq -r '.ssh.hosts | keys[]' "$ENV_FILE")
  n=$(printf '%s\n' "$hosts" | wc -l | tr -d ' ')
  # trap から参照するので local にしない
  NEW_KEY="$SSH_DIR/id_ed25519.next"
  ROTATE_WORK=$(mktemp -d) || die "一時ディレクトリを作れません。"
  # 途中で止まったら新しい鍵は残さない（差し替え後は既に移動済みで、消すものが無い）
  trap 'rm -rf "$ROTATE_WORK" "$NEW_KEY" "$NEW_KEY.pub"' EXIT
  local new_key="$NEW_KEY" work="$ROTATE_WORK"

  ui_title "aws-survey ssh rotate"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "アカウント" "$ACCOUNT_ID"
  ui_kv "リージョン" "$REGION"
  ui_kv "いまの鍵" "$([ -f "$KEY" ] && echo "$KEY  ${C_DIM}作成 $(file_mtime "$KEY")${C_RESET}" || echo "（無い）")"
  ui_kv "登録済みホスト" "$(printf '%s' "$hosts" | tr '\n' ' ')  ${C_DIM}（${n} 台）${C_RESET}"
  echo ""

  ui_head "1/4 元プロファイル $PROFILE_SRC で再導入する"
  ui_text "再導入（SSM RunCommand）は強い権限の仕事です。調査用の一時キーには渡しません。"
  check_source_profile
  echo ""

  ui_head "2/4 新しい鍵"
  mkdir -p "$SSH_DIR" && chmod 700 "$SSH_DIR" || die "鍵の置き場を作れません: $SSH_DIR"
  rm -f "$new_key" "$new_key.pub"
  ssh-keygen -q -t ed25519 -N '' -C diag -f "$new_key" || die "鍵を作れませんでした: $new_key"
  ui_ok "新しい鍵を作りました: ${new_key}${C_DIM}（全ホストに入るまで、いまの鍵はそのままです）${C_RESET}"
  echo ""

  ui_head "3/4 登録済みホスト全部に再導入する（${n} 台）"
  local alias i=0 done_hosts=() failed_host="" rc
  for alias in $hosts; do
    i=$((i + 1))
    load_host "$alias"
    ui_text "[$i/$n] $alias  $INSTANCE_ID  user $SSH_USER"
    if ! ssm_online; then
      ui_err "$alias は SSM の管理下にありません（PingStatus: ${SSM_PING:-なし}）"
      failed_host="$alias"; break
    fi
    prepare_script "$new_key"
    if ! run_install "$SCRIPT"; then
      show_install_failure
      failed_host="$alias"; break
    fi
    if ! collect_hostkeys; then
      ui_err "$alias の出力に HOSTKEY 行がありません"
      failed_host="$alias"; break
    fi
    printf '%s\n' "$HOSTKEY_LINES" > "$work/$INSTANCE_ID"
    done_hosts+=("$alias")
    ui_ok "$alias に新しい鍵を入れました${C_DIM}（送った形: ${FORM}）${C_RESET}"
  done
  echo ""

  ui_head "4/4 鍵と記録の差し替え"
  if [ -n "$failed_host" ]; then
    rm -f "$new_key" "$new_key.pub"
    ui_err "$failed_host で失敗したので、鍵は差し替えていません（いまの鍵・config・known_hosts・environment.json はそのまま）"
    if [ "${#done_hosts[@]}" -gt 0 ]; then
      ui_warn "新しい鍵になったホスト: ${done_hosts[*]}"
      ui_text "この鍵は捨てたので、上のホストにはいまの鍵では届きません。原因を直して、次のどちらかで揃えてください。"
      next_cmd "$AWS_SURVEY_CMD ssh rotate" "もう一度、新しい鍵を全ホストに入れ直します"
      also_cmd "$AWS_SURVEY_CMD ssh setup <host>" "そのホストだけ、いまの鍵で再導入します（1 台ずつ）"
    else
      ui_text "どのホストも変わっていません。原因を直して、もう一度実行してください。"
      next_cmd "$AWS_SURVEY_CMD ssh rotate" "もう一度、新しい鍵を全ホストに入れ直します"
    fi
    exit 1
  fi
  mv "$new_key" "$KEY" && mv "$new_key.pub" "$KEY.pub" && chmod 600 "$KEY" && chmod 644 "$KEY.pub" \
    || die "鍵を差し替えられませんでした: $KEY"
  ui_ok "鍵を差し替えました: ${KEY}${C_DIM}（作成 $(file_mtime "$KEY")）${C_RESET}"
  local tmp now id
  now=$(now_iso)
  for alias in "${done_hosts[@]}"; do
    id=$(jq -r --arg a "$alias" '.ssh.hosts[$a].instance_id' "$ENV_FILE")
    write_known_hosts "$id" "$(cat "$work/$id")"
    tmp=$(mktemp) || die "一時ファイルを作れません。"
    jq --arg a "$alias" --arg now "$now" '.ssh.hosts[$a].installed_at = $now' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE" \
      || { rm -f "$tmp"; die "environment.json に記録できません。"; }
  done
  write_ssh_config
  ui_ok "environment.json の導入日時を更新しました（${n} 台）"
  ui_kv "known_hosts" "$KNOWN_HOSTS"
  echo ""
  ui_text "調査コンテナには次の起動から新しい鍵が渡ります（起動中のコンテナは読み取り専用マウント越しに同じ場所を見ています）。"
  next_cmd "$AWS_SURVEY_CMD ssh verify <host>" "新しい鍵で実際に接続できることを確かめます"
}

# ---- remove ----
# setup の逆順。EC2 側の撤去（RunCommand）が済んでからタグを外し、記録・config・known_hosts から消す。
# EC2 側で失敗したらタグも記録も触らない（同じコマンドで続きからやり直せる。撤去スクリプトは冪等）。
# 最後のホストを消したときは diag-ssh-<name> ポリシーも片付ける（下）。

# 元プロファイルで IAM を叩く（role.sh と同じ形）
iam() { aws iam "$@" --profile "$PROFILE_SRC"; }

# 借りたロールの経路と、自分で作ったロールで片付けに失敗したときに、手で打つコマンドを表示する
show_policy_cleanup_by_hand() {
  ui_text "ポリシー $DIAG_POLICY_NAME を片付けるコマンド（ロールから外し、既定でない版を消してから、ポリシーを消します）:"
  ui_text "  aws iam detach-role-policy --role-name $ROLE_NAME --policy-arn $DIAG_POLICY_ARN"
  ui_text "  aws iam list-policy-versions --policy-arn $DIAG_POLICY_ARN --query 'Versions[?!IsDefaultVersion].VersionId'"
  ui_text "  aws iam delete-policy-version --policy-arn $DIAG_POLICY_ARN --version-id <既定でない版>"
  ui_text "  aws iam delete-policy --policy-arn $DIAG_POLICY_ARN"
}

# 自分で作ったロールから diag-ssh-<name> を外し、既定でない版を消してから、ポリシーを消す。
# ポリシーが無ければ何もしない。途中で失敗したら、残っているものと手で打つコマンドを表示して 1 を返す
# （EC2 側・タグ・記録は済んでいるので、やり直しはこの片付けだけ）。
cleanup_diag_policy() {
  local out v n=0
  if ! out=$(iam get-policy --policy-arn "$DIAG_POLICY_ARN" --query Policy.Arn --output text 2>&1); then
    case "$out" in
      *NoSuchEntity*) ui_skip "ポリシー $DIAG_POLICY_NAME はありません（片付けるものはありません）"; return 0 ;;
      *) ui_raw "$out"; ui_err "ポリシー $DIAG_POLICY_NAME の状態を読めません"; show_policy_cleanup_by_hand; return 1 ;;
    esac
  fi
  if ! out=$(iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$DIAG_POLICY_ARN" 2>&1); then
    case "$out" in
      *NoSuchEntity*) ui_skip "ロール $ROLE_NAME に $DIAG_POLICY_NAME は付いていません" ;;
      *) ui_raw "$out"; ui_err "ロール $ROLE_NAME から $DIAG_POLICY_NAME を外せませんでした"; show_policy_cleanup_by_hand; return 1 ;;
    esac
  else
    ui_ok "ロール $ROLE_NAME から $DIAG_POLICY_NAME を外しました"
  fi
  for v in $(iam list-policy-versions --policy-arn "$DIAG_POLICY_ARN" \
               --query 'Versions[?!IsDefaultVersion].VersionId' --output text 2>/dev/null); do
    [ "$v" != None ] || continue
    if ! out=$(iam delete-policy-version --policy-arn "$DIAG_POLICY_ARN" --version-id "$v" 2>&1); then
      ui_raw "$out"; ui_err "ポリシー $DIAG_POLICY_NAME の版 $v を消せませんでした"; show_policy_cleanup_by_hand; return 1
    fi
    n=$((n + 1))
  done
  [ "$n" -eq 0 ] || ui_ok "既定でない版を消しました（${n}）"
  if ! out=$(iam delete-policy --policy-arn "$DIAG_POLICY_ARN" 2>&1); then
    ui_raw "$out"; ui_err "ポリシー $DIAG_POLICY_NAME を消せませんでした（ロールから外したあとです）"; show_policy_cleanup_by_hand; return 1
  fi
  ui_ok "ポリシー $DIAG_POLICY_NAME を消しました"
}

cmd_remove() {
  local host="" print=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --print) print=1; shift ;;
      -*) die "remove の不明なオプション: $1" ;;
      *) [ -z "$host" ] || die "ホストは 1 つだけ指定してください: $host と $1"; host="$1"; shift ;;
    esac
  done
  [ -n "$host" ] || die "remove には <host>（ssh list に出る別名）を 1 つ指定してください。"
  [ -f "$REMOVE_TEMPLATE" ] || die "撤去スクリプトの雛形がありません: $REMOVE_TEMPLATE"
  load_host "$host"
  local script
  script=$(render_remove) || die "撤去スクリプトを組み立てられませんでした。"
  bash -n <(printf '%s\n' "$script") || die "組み立てた撤去スクリプトが bash として読めません。"

  if [ "$print" -eq 1 ]; then
    exec 3>&1 1>&2
    ui_title "aws-survey ssh remove --print"
    ui_kv "ホスト" "$host  ${C_DIM}${INSTANCE_ID}  user ${SSH_USER}${C_RESET}"
    printf '%s\n' "$script" >&3
    ui_ok "撤去スクリプトを標準出力に出しました"
    ui_text "対象の EC2 に root で実行してください。最後の行が REMOVED clean なら何も残っていません。"
    ui_text "タグ diag:ssh と、このフォルダの記録（environment.json・config・known_hosts）は消していません。"
    return 0
  fi
  command -v aws >/dev/null || die "aws コマンドが見つかりません。"

  ui_title "aws-survey ssh remove"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "アカウント" "$ACCOUNT_ID"
  ui_kv "リージョン" "$REGION"
  ui_kv "ホスト" "$host  ${C_DIM}${INSTANCE_ID}  user ${SSH_USER}${C_RESET}"
  echo ""

  # 最後のホストなら、ポリシーの片付けが 5/5 に入る
  local steps=4 last=0
  [ "$(jq -r '.ssh.hosts | length' "$ENV_FILE")" -gt 1 ] || { steps=5; last=1; }

  ui_head "1/$steps 元プロファイル $PROFILE_SRC で撤去する"
  ui_text "撤去（SSM RunCommand）は強い権限の仕事です。調査用の一時キーには渡しません。"
  check_source_profile
  echo ""

  ui_head "2/$steps 対象と SSM の管理状態"
  local out state gone=0
  if out=$(src ec2 describe-instances --instance-ids "$INSTANCE_ID" \
             --query 'Reservations[].Instances[].{id:InstanceId,state:State.Name}' 2>&1); then
    state=$(jq -r '.[0].state // "terminated"' <<< "$out")
  else
    case "$out" in
      *InvalidInstanceID.NotFound*|*InvalidInstanceID.Malformed*) state=terminated ;;
      *) ui_raw "$out"; die "インスタンス $INSTANCE_ID を読めません。" ;;
    esac
  fi
  if [ "$state" = terminated ]; then
    gone=1
    ui_warn "インスタンス $INSTANCE_ID はもうありません（terminated）。EC2 側の撤去とタグ外しは省き、記録だけ消します。"
  else
    ui_kv "インスタンス" "${INSTANCE_ID}  ${C_DIM}${state}${C_RESET}"
    [ "$state" = running ] || die "インスタンスが running ではありません（${state}）。起動してから実行するか、撤去スクリプトを手で実行してください: $AWS_SURVEY_CMD ssh remove --print $host"
    if ! ssm_online; then
      ui_err "SSM の管理下にありません（PingStatus: ${SSM_PING:-なし}）"
      ui_text "撤去は SSM RunCommand で行います。SSM を使えないなら、撤去スクリプトを書き出して root で手実行してください。"
      also_cmd "$AWS_SURVEY_CMD ssh remove --print $host > remove-diag.sh" "撤去スクリプトを 1 本の bash として書き出します"
      exit 1
    fi
    ui_ok "SSM 管理下（Online）  ${C_DIM}${SSM_PLATFORM}${C_RESET}"
  fi
  echo ""

  ui_head "3/$steps EC2 で撤去スクリプトを root 実行する（AWS-RunShellScript）"
  if [ "$gone" -eq 1 ]; then
    ui_skip "インスタンスが無いので省きます"
  else
    if ! send_and_wait "$script" "diag remove"; then
      if [ -n "${RUN_STATUS:-}" ]; then
        ui_err "撤去スクリプトが失敗しました（${RUN_STATUS}）"
        [ -z "${RUN_STDOUT:-}" ] || ui_raw "$(printf '%s\n' "$RUN_STDOUT" | tail -20)"
        [ -z "${RUN_STDERR:-}" ] || ui_raw "$(printf '%s\n' "$RUN_STDERR" | tail -20)"
        ui_text "撤去スクリプトは冪等です。原因を直して同じコマンドを実行すれば、残ったものだけ消して続きへ進みます。"
      else
        ui_err "RunCommand を送れませんでした"
        ui_raw "$SEND_ERROR"
      fi
      die "タグと記録（environment.json・config・known_hosts）はそのままです。"
    fi
    if ! printf '%s\n' "$RUN_STDOUT" | grep -qx 'REMOVED clean'; then
      ui_err "撤去スクリプトは終わりましたが、EC2 に残っているものがあります"
      ui_raw "$(printf '%s\n' "$RUN_STDOUT" | grep -E '^(\[diag\]|REMOVED)' | tail -20)"
      die "タグと記録はそのままです。残ったものを確かめて、同じコマンドをもう一度実行してください。"
    fi
    ui_ok "EC2 から撤去しました（REMOVED clean）"
    ui_raw "$(printf '%s\n' "$RUN_STDOUT" | grep -E '^\[diag\]' | tail -12)"
  fi
  echo ""

  ui_head "4/$steps タグと記録"
  if [ "$gone" -eq 1 ]; then
    ui_skip "タグ diag:ssh=$SURVEY_NAME はインスタンスと一緒に消えています"
  else
    if ! out=$(src ec2 delete-tags --resources "$INSTANCE_ID" --tags "Key=diag:ssh,Value=$SURVEY_NAME" 2>&1); then
      ui_raw "$out"
      die "タグ diag:ssh=$SURVEY_NAME を外せませんでした。EC2 側の撤去は済んでいるので、同じコマンドをもう一度実行すればここからやり直せます。"
    fi
    ui_ok "タグを外しました: diag:ssh=$SURVEY_NAME"
  fi
  local tmp others
  tmp=$(mktemp) || die "一時ファイルを作れません。"
  jq --arg a "$host" 'del(.ssh.hosts[$a])' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE" \
    || { rm -f "$tmp"; die "environment.json から消せませんでした。"; }
  # 同じインスタンスを別の別名でも登録しているときは、known_hosts の行を残す
  others=$(jq -r --arg id "$INSTANCE_ID" '.ssh.hosts | to_entries | map(select(.value.instance_id == $id)) | length' "$ENV_FILE")
  [ "$others" -gt 0 ] || [ ! -f "$KNOWN_HOSTS" ] || write_known_hosts "$INSTANCE_ID" ""
  write_ssh_config
  ui_ok "environment.json・config・known_hosts から消しました: $host"
  echo ""
  local remaining
  remaining=$(jq -r '.ssh.hosts | keys | join(" ")' "$ENV_FILE")
  if [ -n "$remaining" ]; then
    ui_kv "残っている登録" "$remaining"
    next_cmd "$AWS_SURVEY_CMD ssh list" "登録済みホストと導入状態を確かめます"
    return 0
  fi

  ui_head "5/$steps ポリシー ${DIAG_POLICY_NAME}"
  ui_text "登録済みホストが無くなりました。鍵（${KEY}）は残しています（次の ssh setup がそのまま使います）。"
  local policy_rc=0
  if [ "$AUTH_ROUTE" = own_role ] || [ -z "$AUTH_ROUTE" ]; then
    ui_text "調査用ロールは自分で作ったものなので、SSH 接続を許すポリシーも片付けます。"
    cleanup_diag_policy || policy_rc=1
    # ポリシーは無くなったので、次に ssh setup したときは role --create からやり直す
    tmp=$(mktemp) && jq '.setup.ssh_policy_attached = null' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
  else
    ui_text "調査用ロールは借りたものなので、ポリシー ${DIAG_POLICY_NAME} は触りません。タグ付きのインスタンスが無いので、付いたままでも何も許しません。"
    ui_text "管理者に、要らなくなったことを伝えてください。"
    show_policy_cleanup_by_hand
  fi
  echo ""
  if [ "$AUTH_ROUTE" = own_role ] || [ -z "$AUTH_ROUTE" ]; then
    ui_text "いまの一時キーは、発行時に渡した ${DIAG_POLICY_NAME} が無くなったので使えません（読み取りも通りません）。発行し直してください。"
  fi
  next_cmd "$AWS_SURVEY_CMD" "一時キーの発行し直しを案内します（登録が無いときは ${DIAG_POLICY_NAME} を渡しません）"
  also_cmd "$AWS_SURVEY_CMD credentials" "SSH 接続の権限を含めない一時キーを、いますぐ発行し直します"
  also_cmd "$AWS_SURVEY_CMD role --create" "ロールの状態を確かめます（登録が無いときは ${DIAG_POLICY_NAME} を付けません）"
  if [ "$policy_rc" -ne 0 ]; then
    ui_err "ポリシーの片付けが途中で止まりました。EC2 側・タグ・記録は済んでいるので、残りは上のコマンドを手で打つか、原因を直してから同じ手順で片付けてください。"
    return 1
  fi
}

case "$SUB" in
  setup)  cmd_setup "$@" ;;
  list)   cmd_list "$@" ;;
  verify) cmd_verify "$@" ;;
  rotate) cmd_rotate "$@" ;;
  remove) cmd_remove "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; die "ssh の不明なサブコマンド: $SUB" ;;
esac
