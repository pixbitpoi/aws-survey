#!/usr/bin/env bash
# EC2 の中を調べるための導入と鍵の管理（ホストで実行）。aws-survey ssh から呼ばれる。
#
#   aws-survey ssh setup <instance-id | Name タグ> [--alias <名前>] [--user <名前>] [--log <名前>=<パス or glob>]... [--deny <glob>]... [--strict]
#                                   SSM 経由で導入する。元プロファイル（auth.source_profile）で実行し、
#                                   成功したら $AWS_DIR/ssh/{config,known_hosts} と environment.json の ssh.hosts に記録する
#   aws-survey ssh setup --print [...]   導入スクリプト（root で実行する 1 本の bash）を標準出力に出す。案内は標準エラー
#   aws-survey ssh list                  登録済みホストと導入状態（AWS に届けばインスタンスの状態とタグも）
#   aws-survey ssh verify <host>         調査コンテナから ec2 --selftest <host> を打つ。通るもの・塞がっているもの・
#                                   終了後に SSM セッションが残らないことを実際に接続して確かめる
#   aws-survey ssh rotate                鍵の作り直しと再導入    （未実装）
#   aws-survey ssh remove <host>         撤去                    （未実装）
#
# 鍵は $AWS_DIR/ssh/id_ed25519（対象ごとに 1 対、無ければ作る）。導入スクリプトに入るのは公開鍵だけ。
# 導入スクリプトの雛形は libexec/ec2/install.sh.tmpl。EC2 に残るものの名前は diag で統一する（設計文書の第 2 節）。
# 導入は元プロファイルの仕事で、ssm:SendCommand を調査用ロールや一時キーに足さない。
# 途中で失敗したら何も記録しない（タグ付け・config・environment.json は導入が成功してから）。
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"

EC2_DIR="$LIBEXEC_DIR/ec2"
TEMPLATE="$EC2_DIR/install.sh.tmpl"
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

usage() { sed -n '4,12p' "$0" | sed 's/^# \{0,1\}//'; }

# ---- 引数 ----
SUB="${1:-}"; [ $# -gt 0 ] && shift
[ -n "$SUB" ] || { usage >&2; die "サブコマンドを指定してください（setup <対象> / setup --print / list / verify <host>）。"; }

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
read_pubkey() {
  local line
  line=$(head -n 1 "$KEY.pub")
  case "$line" in
    "ssh-ed25519 "*) ;;
    *) die "公開鍵が ssh-ed25519 ではありません: $KEY.pub" ;;
  esac
  PUBKEY="$(printf '%s' "$line" | cut -d' ' -f1,2) diag"
  [[ "$PUBKEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+\ diag$ ]] || die "公開鍵の形式を読めません: $KEY.pub"
}

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
prepare_script() {
  ensure_key
  read_pubkey
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
check_ssm() {
  local out ping platform
  out=$(src ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
          --query 'InstanceInformationList[0].{ping:PingStatus,platform:PlatformName,version:PlatformVersion,agent:AgentVersion}' 2>&1) \
    || { ui_raw "$out"; die "SSM の管理状態を読めません（ssm:DescribeInstanceInformation）。"; }
  ping=$(jq -r '.ping // empty' <<< "$out")
  platform=$(jq -r 'if .platform then "\(.platform) \(.version // "")  agent \(.agent // "?")" else "" end' <<< "$out")
  if [ "$ping" != Online ]; then
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
  fi
  ui_ok "SSM 管理下（Online）  ${C_DIM}${platform}${C_RESET}"
}

# AWS-RunShellScript で root 実行し、完了を待つ。COMMAND_ID / RUN_STATUS / RUN_STDOUT / RUN_STDERR を埋める。
# 1 引数はスクリプト本文。送れなければ 2（大きすぎる）か 1 を返す。
send_and_wait() {
  local script="$1" input cmd_id out status waited=0 rc=0
  # $(...) で受けたスクリプトは末尾の改行が落ちているので戻す
  input=$(jq -n --arg id "$INSTANCE_ID" --arg script "$script"$'\n' --arg t "$SSM_TIMEOUT" \
            '{DocumentName: "AWS-RunShellScript", InstanceIds: [$id], Comment: "diag setup",
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
        ui_status "EC2 で導入スクリプトを実行中… ${status}（${waited} 秒）"
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
  [ -n "$HOSTKEY_LINES" ] || die "導入は成功しましたが、出力に HOSTKEY 行がありません。known_hosts を作れないため記録しません。"
}

# $AWS_DIR/ssh/config を environment.json の ssh.hosts 全体から作り直す（コンテナの ssh -F が読む）
write_ssh_config() {
  local tmp
  tmp=$(mktemp) || die "一時ファイルを作れません。"
  {
    echo "# aws-survey が生成。手で直さない（aws-survey ssh setup で作り直す）"
    jq -r --arg dir "$CONTAINER_SSH_DIR" --arg default_user "$SSH_USER" '
      .ssh.hosts | to_entries[] | .key as $alias | .value |
      "\nHost \($alias)\n    HostName \(.instance_id)\n    User \(.user // $default_user)\n    IdentityFile \($dir)/id_ed25519\n    IdentitiesOnly yes\n    ProxyCommand aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p\n    StrictHostKeyChecking yes\n    UserKnownHostsFile \($dir)/known_hosts\n    BatchMode yes\n    RequestTTY no\n    ForwardAgent no\n    ServerAliveInterval 15"' "$ENV_FILE"
  } > "$tmp" || { rm -f "$tmp"; die "config を組み立てられません。"; }
  mv "$tmp" "$SSH_CONFIG" && chmod 644 "$SSH_CONFIG" || die "書き込めません: $SSH_CONFIG"
}

# known_hosts: このインスタンスの行を入れ替え、他のホストの行は残す
write_known_hosts() {
  local tmp
  tmp=$(mktemp) || die "一時ファイルを作れません。"
  { [ ! -f "$KNOWN_HOSTS" ] || grep -v -E "^$INSTANCE_ID " "$KNOWN_HOSTS"; printf '%s\n' "$HOSTKEY_LINES"; } > "$tmp"
  mv "$tmp" "$KNOWN_HOSTS" && chmod 644 "$KNOWN_HOSTS" || die "書き込めません: $KNOWN_HOSTS"
}

record_host() {
  local tmp now
  now=$(date +%Y-%m-%dT%H:%M:%S%z | sed -E 's/([0-9]{2})$/:\1/')
  tmp=$(mktemp) || die "一時ファイルを作れません。"
  jq --arg a "$HOST_ALIAS" --arg id "$INSTANCE_ID" --arg user "$SSH_USER" --arg now "$now" \
     --argjson logs "$LOGS_JSON" --argjson deny "$DENY_JSON" --argjson strict "$STRICT" '
     .ssh = (.ssh // {}) | .ssh.user = (.ssh.user // $user) |
     .ssh.hosts = (.ssh.hosts // {}) |
     .ssh.hosts[$a] = {instance_id: $id, user: $user, installed_at: $now, logs: $logs, deny: $deny, strict: $strict}' \
     "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE" || { rm -f "$tmp"; die "environment.json に記録できません。"; }
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
  local form=plain rc=2 packed packed_bytes
  if [ "$SSH_PACK" = 1 ]; then
    ui_text "AWS_SURVEY_SSH_PACK=1: 最初から gzip + base64 に畳んで送ります。"
  else
    send_and_wait "$SCRIPT"; rc=$?
  fi
  if [ "$rc" -eq 2 ]; then
    if [ "$SSH_PACK" != 1 ]; then
      ui_warn "素のスクリプトは SSM のパラメータ上限に掛かりました。gzip + base64 に畳んで送り直します。"
      ui_raw "$(printf '%s\n' "$SEND_ERROR" | head -3)"
    fi
    packed=$(pack_install "$SCRIPT") || die "スクリプトを圧縮できません（gzip / base64 が要ります）。"
    packed_bytes=$(printf '%s\n' "$packed" | wc -c | tr -d ' ')
    ui_kv "圧縮後" "${packed_bytes} バイト"
    form=packed
    send_and_wait "$packed"; rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
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
    die "何も記録していません（タグ・config・environment.json）。"
  fi
  ui_ok "導入スクリプトが成功しました${C_DIM}（送った形: ${form}）${C_RESET}"
  ui_raw "$(printf '%s\n' "$RUN_STDOUT" | grep -E '^\[diag\]' | tail -12)"
  collect_hostkeys
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
  next_cmd "$AWS_SURVEY_CMD ssh list" "登録済みホストと導入状態を確かめます"
  if [ "$AUTH_ROUTE" = own_role ] || [ -z "$AUTH_ROUTE" ]; then
    also_cmd "$AWS_SURVEY_CMD role --create" "このインスタンスへの SSH 接続を許すポリシーを調査用ロールに付けます（初めての登録のとき）"
  else
    also_cmd "$AWS_SURVEY_CMD role --create" "SSH 接続を許すポリシーの JSON を表示します（管理者に付けてもらいます）"
  fi
  also_cmd "$AWS_SURVEY_CMD credentials" "その権限を含めて一時キーを発行し直し、$AWS_SURVEY_CMD verify で確かめます"
  also_cmd "$AWS_SURVEY_CMD ssh verify $HOST_ALIAS" "一時キーを発行し直したあと、調査コンテナから実際に接続して確かめます"
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
  ui_kv "鍵" "$([ -f "$KEY.pub" ] && echo "$KEY" || echo "（まだ無い）")"
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
  echo ""

  ui_head "2/3 調査コンテナのイメージを用意する"
  command -v docker >/dev/null || die "docker コマンドが見つかりません。"
  local arch awsarch image out
  arch=$(uname -m)
  case "$arch" in
    arm64|aarch64) awsarch=aarch64 ;;
    x86_64|amd64)  awsarch=x86_64  ;;
    *) die "未対応のアーキテクチャ: $arch" ;;
  esac
  image="${SURVEY_NAME}:latest"
  ui_status "docker build（初回は数分かかります）…"
  if ! out=$(docker build -q --build-arg "AWSCLI_ARCH=$awsarch" -t "$image" -f "$AWS_SURVEY_HOME/Dockerfile" "$AWS_SURVEY_HOME/container" 2>&1); then
    ui_status_done
    ui_err "イメージのビルドに失敗しました"
    ui_raw "$(printf '%s\n' "$out" | tail -20)"
    exit 1
  fi
  ui_status_done
  ui_ok "イメージ $image ${C_DIM}（${awsarch}）${C_RESET}"
  echo ""

  ui_head "3/3 コンテナから ec2 --selftest $host を打つ"
  local rc=0
  docker run --rm \
    -v "$AWS_DIR:/home/node/.aws-claude:ro" \
    -e TZ=Asia/Tokyo \
    -e "AWS_DEFAULT_REGION=$REGION" \
    "$image" ec2 --selftest "$host" 2>&1 | sed 's/^/    /' || rc=${PIPESTATUS[0]}
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
  else
    ui_err "検証に失敗した項目があります（上の ✗ を見てください）"
  fi
  return "$rc"
}

not_implemented() {
  ui_title "aws-survey ssh $1"
  ui_warn "$2 はまだ実装されていません。"
  ui_text "いま使えるのは $AWS_SURVEY_CMD ssh setup（導入）・setup --print（スクリプトの書き出し）・list（一覧）・verify（検証）です。"
  exit 1
}

case "$SUB" in
  setup)  cmd_setup "$@" ;;
  list)   cmd_list "$@" ;;
  verify) cmd_verify "$@" ;;
  rotate) not_implemented rotate "鍵の作り直しと再導入" ;;
  remove) not_implemented remove "撤去" ;;
  -h|--help|help) usage ;;
  *) usage >&2; die "ssh の不明なサブコマンド: $SUB" ;;
esac
