#!/usr/bin/env bash
# environment.json を読み込んでシェル変数にする（ホスト側スクリプトから source する）
#
#   . "$(cd "$(dirname "$0")" && pwd)/load-env.sh"      （libexec/ の中のスクリプトから）
#
# パスは 2 系統で解決する。
#   AWS_SURVEY_HOME : 本体（libexec/・templates/・container/ など）。既定はこのファイルから見た libexec/ の親
#   AWS_SURVEY_DIR  : 対象フォルダ（environment.json・out/・trust.json）。既定はカレントディレクトリ
#   AWS_DIR         : 一時キーの置き場。既定は ~/.aws-survey/<name>/
# 本体の場所を $PWD で、対象フォルダの場所をスクリプトの位置で決めないこと。
#
# 環境変数で個別に上書きできる（一時的な試行用）。
# 恒久的な値は environment.json 側を直すこと。

_die() { echo "" >&2; echo "✗ $*" >&2; exit 1; }

AWS_SURVEY_HOME="${AWS_SURVEY_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
[ -d "$AWS_SURVEY_HOME/libexec" ] || _die "AWS_SURVEY_HOME に libexec/ がありません: $AWS_SURVEY_HOME"
LIBEXEC_DIR="$AWS_SURVEY_HOME/libexec"
. "$LIBEXEC_DIR/ui.sh"
. "$LIBEXEC_DIR/agents.sh"
_die() { ui_die "$@"; }

AWS_SURVEY_DIR="${AWS_SURVEY_DIR:-$PWD}"
[ -d "$AWS_SURVEY_DIR" ] || _die "対象フォルダがありません: $AWS_SURVEY_DIR"
AWS_SURVEY_DIR="$(cd "$AWS_SURVEY_DIR" && pwd -P)"
ENV_FILE="${ENV_FILE:-$AWS_SURVEY_DIR/environment.json}"

command -v jq >/dev/null || _die "jq が必要です（brew install jq）。"

if [ ! -f "$ENV_FILE" ]; then
  _die "environment.json がありません: $ENV_FILE
  このフォルダは、まだどの AWS アカウント向けにも設定されていません。
  aws-survey init で AWS への繋ぎ方を聞き取って作ります。"
fi
jq -e . "$ENV_FILE" >/dev/null 2>&1 || _die "environment.json が壊れています: $ENV_FILE"

_get() { jq -r "$1 // empty" "$ENV_FILE"; }

SURVEY_NAME="${SURVEY_NAME:-$(_get .name)}"
ACCOUNT_ID="${ACCOUNT_ID:-$(_get .account_id)}"
REGION="${REGION:-$(_get .region)}"
AUTH_ROUTE="${AUTH_ROUTE:-$(_get .auth.route)}"
PROFILE_SRC="${PROFILE_SRC:-$(_get .auth.source_profile)}"
PRINCIPAL_ARN="${PRINCIPAL_ARN:-$(_get .auth.principal_arn)}"
# 真偽値に _get（jq の //）は使えない。false が空扱いになるため。
MFA_REQUIRED="${MFA_REQUIRED:-$(jq -r '.auth.mfa_required' "$ENV_FILE")}"
ROLE_NAME="${ROLE_NAME:-$(_get .auth.role_name)}"
# 元プロファイルが切れたときに、それを有効にし直すコマンド。
# 環境によって違う（SSO なら aws sso login、MFA なら会社ごとのスクリプト）ため設定で持つ。
# 未設定なら空。
REFRESH_CMD="${REFRESH_CMD:-$(_get .auth.refresh_command)}"
DURATION="${DURATION:-$(_get .auth.duration_seconds)}"
# 最初の仕事は基礎調査（scan が報告まで書く）。その器の名前。別の種類の仕事のフォルダは調査エージェントが out/ に自分で作る。
SURVEY_PHASE_DIR="${SURVEY_PHASE_DIR:-$(_get .phase_dir)}"
SURVEY_PHASE_DIR="${SURVEY_PHASE_DIR:-01_基礎調査}"
# 調査に使うエージェント（claude / codex）とそのモデル・effort（libexec/agents.sh）。init が聞いて書き、最後に使ったものを
# scan と claude / codex の入口が記録する。未定なら空で、scan が矢印キーで聞く。案内文の「次に打つコマンド」もこれで組み立てる。
# 古い形（"agent": "claude"）も名前だけの指定として読む
SURVEY_AGENT="${SURVEY_AGENT:-$(jq -r 'if (.agent | type) == "object" then .agent.name // empty else .agent // empty end' "$ENV_FILE")}"
SURVEY_AGENT_MODEL="${SURVEY_AGENT_MODEL:-$(jq -r 'if (.agent | type) == "object" then .agent.model // empty else empty end' "$ENV_FILE")}"
SURVEY_AGENT_EFFORT="${SURVEY_AGENT_EFFORT:-$(jq -r 'if (.agent | type) == "object" then .agent.effort // empty else empty end' "$ENV_FILE")}"
case "$SURVEY_AGENT" in
  ""|claude|codex) ;;
  *) _die "environment.json の agent.name が不正です: ${SURVEY_AGENT}（claude か codex。未定なら null）" ;;
esac
if [ -n "$SURVEY_AGENT" ]; then
  [ -z "$SURVEY_AGENT_MODEL" ]  || agent_valid_model "$SURVEY_AGENT_MODEL" \
    || _die "environment.json の agent.model が不正です: ${SURVEY_AGENT_MODEL}"
  [ -z "$SURVEY_AGENT_EFFORT" ] || agent_valid_effort "$SURVEY_AGENT" "$SURVEY_AGENT_EFFORT" \
    || _die "environment.json の agent.effort が不正です: ${SURVEY_AGENT_EFFORT}（$(agent_label "$SURVEY_AGENT") は $(agent_effort_choices "$SURVEY_AGENT" | tr ' ' '/')）"
fi

for _v in SURVEY_NAME ACCOUNT_ID REGION PROFILE_SRC ROLE_NAME DURATION SURVEY_PHASE_DIR; do
  [ -n "${!_v}" ] || _die "environment.json に $_v にあたる項目がありません。templates/environment.json と見比べてください。"
done
case "$SURVEY_NAME$ACCOUNT_ID$REGION" in
  *TEMPLATE*) _die "environment.json が雛形のままです。aws-survey init で作り直してください。" ;;
esac

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# EC2 の中を調べる機能（任意）。ログインユーザー名と、登録済みホストの別名だけを読む。
# 既定は diag。init では聞かず、aws-survey ssh setup で決める。
SSH_USER="${SSH_USER:-$(_get .ssh.user)}"
SSH_USER="${SSH_USER:-diag}"
[[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || _die "environment.json の ssh.user が不正です: ${SSH_USER}（小文字英字か _ で始まり、小文字英数字 _ - で 32 字まで）"
SSH_HOSTS="${SSH_HOSTS:-$(jq -r '.ssh.hosts // {} | keys | join(" ")' "$ENV_FILE")}"
# 登録済みホストへ ssm:StartSession を許す顧客管理ポリシー（設計文書の第 6 節）。
# role --create が作って調査用ロールに付け、credentials が --policy-arns に並べる。ssh.hosts が空なら使わない。
DIAG_POLICY_NAME="diag-ssh-${SURVEY_NAME}"
# 一時キーのロールセッション名の接頭辞。SSM のセッション ID は <ロールセッション名>-<乱数> になるので、
# diag-ssh-<name> の ssm:TerminateSession / ResumeSession はこの接頭辞で自分のセッションに限る（${aws:userid} では一致しない）。
SESSION_NAME_PREFIX="claude-survey"
DIAG_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${DIAG_POLICY_NAME}"

# 案内文に書く CLI の名前。インストール済み（PATH 上に aws-survey がある）を前提に、常に短い名前にする。
# 利用者に見せる「次に打つコマンド」はスクリプト名ではなくこれで組み立てる。
AWS_SURVEY_CMD="${AWS_SURVEY_CMD:-aws-survey}"
# 一時キーは対象（name）ごとに分ける。対象を切り替えても上書きされない。
AWS_DIR="${AWS_DIR:-$HOME/.aws-survey/$SURVEY_NAME}"

# ---- Docker Desktop の共有パス ----
# macOS の Docker Desktop は Settings → Resources → File Sharing に登録した場所しかマウントできず、
# 外れていると docker run が「mounts denied」で落ちる。既定は /Users・/Volumes・/private・/tmp・/var/folders で、
# Homebrew 版の本体（/opt/homebrew/Cellar/...）はこの外にある。設定ファイルが読めるときだけ判定し、
# 無ければ（Docker Desktop でない、Linux）判定しない。判定は run / ssh verify が起動前に、doctor が点検で使う。
DOCKER_SHARE_SETTINGS="${DOCKER_SHARE_SETTINGS:-$HOME/Library/Group Containers/group.com.docker/settings-store.json}"
# 共有パスの一覧（1 行 1 つ）。設定が無ければ空
docker_shared_dirs() {
  [ -f "$DOCKER_SHARE_SETTINGS" ] || return 0
  jq -r '(.FilesharingDirectories // .filesharingDirectories // []) | .[]' "$DOCKER_SHARE_SETTINGS" 2>/dev/null
}
# 1 引数の場所をマウントできるか。設定が無ければ 0（判定しない）
docker_path_shared() {
  local p d dirs
  dirs=$(docker_shared_dirs); [ -n "$dirs" ] || return 0
  p=$(cd "$1" 2>/dev/null && pwd -P) || p="$1"
  while IFS= read -r d; do
    d=${d%/}; [ -n "$d" ] || continue
    case "$p/" in "$d"/*) return 0 ;; esac
  done <<< "$dirs"
  return 1
}
# File Sharing に足す場所の提案。Homebrew の keg はその prefix ごと
docker_share_suggest() {
  case "$1" in
    /opt/homebrew/*) echo /opt/homebrew ;;
    /usr/local/Cellar/*|/usr/local/opt/*) echo /usr/local ;;
    *) echo "$1" ;;
  esac
}
# 引数の場所を全部確かめ、マウントできない場所があれば表示して案内し、1 を返す
docker_check_shared() {
  local p bad=()
  for p in "$@"; do docker_path_shared "$p" || bad+=("$p"); done
  [ ${#bad[@]} -eq 0 ] && return 0
  ui_err "Docker Desktop がマウントできない場所があります（File Sharing に入っていません）"
  for p in "${bad[@]}"; do ui_raw "$p"; done
  ui_text "Docker Desktop のメニュー → Settings → Resources → File Sharing で「+」から次を追加し、Apply & restart してから、もう一度実行してください。"
  for p in "${bad[@]}"; do ui_text "  $(ui_cmd "$(docker_share_suggest "$p")")"; done
  return 1
}

# scan の説明。初期調査を行うのは誰か（記録してあるエージェント。未定なら両方の名前）と、時間の目安
scan_desc() { printf '対象アカウントの初期調査を %s が行います（インフラ構成の複雑さによっては数十分かかります）' "$(agent_label "${1:-}")"; }

# 準備が済んだあとの次の 1 手。初期調査（scan）がまだなら scan、済んでいれば記録してあるエージェントとの対話。
# ls / lambda pull / credentials / verify など、準備が済んだ状態で終わるコマンドの末尾が出す。run（素のシェル）は案内しない
survey_next_cmd() {
  if [ -z "$(jq -r '.setup.scanned // empty' "$ENV_FILE")" ]; then
    next_cmd "$AWS_SURVEY_CMD scan" "$(scan_desc "$SURVEY_AGENT")"
  elif [ -n "$SURVEY_AGENT" ]; then
    next_cmd "$AWS_SURVEY_CMD $SURVEY_AGENT" "$(agent_label "$SURVEY_AGENT") と対話で調査を進めます"
  else
    next_cmd "$AWS_SURVEY_CMD claude" "Claude Code と対話で調査を進めます（Codex なら $AWS_SURVEY_CMD codex）"
  fi
}

# セットアップの到達点を environment.json に記録する。2 つ目は利用者に見せる言い換え（省略時はキー名）、
# 3 つ目は記録する値（省略時は今日の日付。分まで要るときは呼ぶ側が渡す）
#   env_mark_setup role_created "ロールを用意した"
#   env_mark_setup scanned "初期調査を済ませた" "$(date '+%FT%H:%M%z')"
env_mark_setup() {
  local key="${1:-}" label="${2:-${1:-}}" value="${3:-$(date +%F)}" tmp
  [ -n "$key" ] || { echo "env_mark_setup: キー名がありません" >&2; return 1; }
  tmp=$(mktemp) || return 0
  jq --arg k "$key" --arg d "$value" '.setup[$k] = $d' "$ENV_FILE" > "$tmp" \
    && mv "$tmp" "$ENV_FILE" \
    && ui_text "environment.json に記録しました: ${label}（${value}）"
}

# 調査に使うエージェントを記録する（最後に使ったもの）。env_set_agent <name> [<model> <effort>]
# 名前だけ渡して別のエージェントに切り替えたときは、model / effort をそのエージェントの既定に戻す（前のものの値は渡せない）。
# 同じ名前で model / effort の指定も無ければ何も書かない
env_set_agent() {
  local agent="${1:-}" model="${2:-}" effort="${3:-}" tmp
  agent_valid "$agent" || { echo "env_set_agent: claude か codex を指定してください" >&2; return 1; }
  if [ -z "$model" ] && [ -z "$effort" ]; then
    [ "$agent" != "$SURVEY_AGENT" ] || return 0
    model=$(agent_default_model "$agent"); effort=$(agent_default_effort "$agent")
  fi
  tmp=$(mktemp) || return 0
  jq --arg a "$agent" --arg m "$model" --arg e "$effort" \
     '.agent = {name: $a, model: (if $m == "" then null else $m end), effort: (if $e == "" then null else $e end)}' "$ENV_FILE" > "$tmp" \
    && mv "$tmp" "$ENV_FILE" && SURVEY_AGENT="$agent" && SURVEY_AGENT_MODEL="$model" && SURVEY_AGENT_EFFORT="$effort"
}
