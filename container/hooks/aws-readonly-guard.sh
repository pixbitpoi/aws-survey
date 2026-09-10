#!/usr/bin/env bash
# Claude Code PreToolUse フック（Bash 用）: AWS 読み取り専用ガード
#
# 方針:
#   - 許可リスト方式。明示的に許した形以外はすべて拒否する。
#   - aws を含むコマンドは「単独の aws コマンド」であることを必須にし、
#     bash -c / python3 -c / パイプ / コマンド連結による迂回を封じる。
#   - AWS 側のセッションポリシーと二重に、危険な読み取り API を塞ぐ。
#   - EC2 の中を調べる入口は ec2 ラッパーだけ。ssh / scp / sftp / session-manager-plugin の
#     直接実行は拒否し、ec2 は aws と同じ「単独コマンド・保存は > out/… だけ」の規則で通す。
#     EC2 側の境界は IAM・sshd・ゲートウェイにあり、ここは事故防止の層。
#
# 終了コード: 0=許可, 2=拒否（stderr の内容が Claude に返る）

set -uo pipefail

# ~/.aws-claude は読み取り専用マウントなので、書き込める out/ に出す。
# 置き場は out/_環境/ = 「この環境自体の記録」の区画（フェーズの成果物とは分ける）。
LOG="${SURVEY_AUDIT_LOG:-$HOME/aws-survey/out/_環境/aws-audit.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

# 例示に使う保存先。run.sh が現在のフェーズを環境変数で渡す。
# 渡っていない場合でも、拒否理由が古いフェーズ名を指さないようにしておく。
PHASE="${SURVEY_PHASE_DIR:-<フェーズ>}"

payload=$(cat)
cmd=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null || true)
[ -z "$cmd" ] && exit 0

norm=$(printf '%s' "$cmd" | tr '\n' ' ' | tr -s ' ' | sed 's/^ *//; s/ *$//')

log() { printf '%s\t%s\t%s\n' "$(date -Is)" "$1" "$norm" >> "$LOG" 2>/dev/null || true; }

deny() {
  log "DENY($1)"
  cat >&2 <<MSG
[aws-readonly-guard] このコマンドは実行できません。
理由: $1
コマンド: $norm

このセッションは AWS の読み取り専用調査に限定されています。
・aws と ec2 は単独で実行してください（パイプ・連結・\$() は不可）
・EC2 の中は ec2 <host> <動詞> [引数...] で調べます（ssh / scp / sftp の直接実行は不可）
・生の JSON を保存するときは次の形が使えます:
    aws ec2 describe-instances --max-items 100 > out/${PHASE}/raw/raw-ec2.json
  保存先は out/ 配下（サブフォルダ可・.. は不可）・拡張子は json / txt / csv のみです。
・保存した JSON は jq で必要な部分だけ取り出してください:
    jq '[.Reservations[].Instances[] | {id:.InstanceId}]' out/${PHASE}/raw/raw-ec2.json
・軽い絞り込みは --query と --output でも構いません
MSG
  exit 2
}

has() { printf '%s' "$norm" | grep -qE -- "$1"; }

# ---- 1. 常に禁止（aws に言及していなくても） ----
has '(^|[^[:alnum:]_./-])sudo([^[:alnum:]_-]|$)' && deny "sudo は使用できません"
has '\.aws(-claude)?/(credentials|config|ssh)'    && deny "AWS 認証情報・SSH 鍵のファイルへのアクセスは禁止です"
has 'aws-readonly-guard|settings\.local\.json|settings\.json' && deny "ガード設定そのものへの操作は禁止です"
# 監査ログは、この環境自身が付ける記録。中から手を加えられないことが前提になっている。
# 注意: 下の 2. の単語境界は「aws-audit.log」に掛からない（aws の直後がハイフンのため）。
#       ここで明示的に落とさないと、素通りして通常の権限設定に委ねられてしまう。
has 'aws-audit' && deny "監査ログへの操作は禁止です"
has 'AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN|PROFILE|SHARED_CREDENTIALS_FILE|CONFIG_FILE)=' \
  && deny "AWS 認証関連の環境変数は変更できません"

# ---- 2. EC2 の中を調べる経路 ----
# 入口は ec2 ラッパーだけ。ssh / scp / sftp / session-manager-plugin をコマンドの位置
# （行頭・; & | ( ` $( の後ろ・env / exec / xargs / sh -c などの後ろ）に書いたものは拒否する。
# 引数の中の語（grep の --pattern ssh など）までは見ない。ここは事故防止の層で、
# 直接 ssh を打っても EC2 側ではゲートウェイしか動かない。
Q="'\""  # 引用符 2 種（正規表現の文字クラスに入れる）
CMDPOS="(^|[;&|(\`{]|\\\$\\(|(^|[[:space:]])(env|exec|xargs|nohup|nice|command|eval|time|then|do|else)[[:space:]]|(^|[[:space:]])(ba|z|da|k)?sh[[:space:]]+-c[[:space:]])[[:space:]]*[$Q]?[[:space:]]*"
has "${CMDPOS}(ssh|scp|sftp|session-manager-plugin)([[:space:]]|$|[$Q])" \
  && deny "ssh / scp / sftp / session-manager-plugin は直接実行できません。EC2 の中は ec2 <host> <動詞> で調べます（一覧: ec2 <host> help）"
# ec2 は行頭に書いた単独コマンドだけ。他の位置に出てきたら迂回として拒否する（aws ec2 … は aws の規則が見る）。
case "$norm" in
  ec2|ec2\ *) EC2=1 ;;
  *) EC2=0
     if ! has '(^|[^[:alnum:]_-])aws([^[:alnum:]_-]|$)' && has "${CMDPOS}ec2([[:space:]]|$)"; then
       deny "ec2 は単独のコマンドとしてのみ実行できます（bash -c / パイプ / 連結の経由は不可）"
     fi ;;
esac

# ---- 3. aws にも ec2 にも言及していなければ、通常の権限設定に委ねる ----
if [ "$EC2" -eq 0 ] && ! has '(^|[^[:alnum:]_-])(aws|awscli|boto3)([^[:alnum:]_-]|$)'; then
  exit 0
fi

# ---- 4. 以降は aws / ec2 関連。単独コマンドのみ許可 ----
# 例外として、末尾の「> out/.../名前.json」だけは通す。
# 生の JSON をファイルに落とせないと、出力が必ずエージェントの文脈を経由してしまい
# トークンを浪費するため。out/ 以外は root 所有で書き込めないので、書ける場所は増えない。
#
# out/ 配下のサブディレクトリも許可する（フェーズごとに out/NN_名前/raw/ に分けるため）。
# 文字クラスは「危険な ASCII を除く」形にしてある。日本語のフォルダ名を通すためで、
# 代わりに .. による脱出を明示的に塞いでいる。
body="$norm"
if [[ "$norm" =~ ^(.*[^[:space:]\>])[[:space:]]*\>[[:space:]]*(out/[^[:space:]\;\&\|\<\>\`\$\(\)\'\"\*\?]+\.(json|txt|csv))$ ]]; then
  case "${BASH_REMATCH[2]}" in
    *..*|*//*) deny "保存先のパスに .. や // は使えません" ;;
  esac
  body="${BASH_REMATCH[1]}"
fi

if [ "$EC2" -eq 1 ]; then
  # ec2 <host> <動詞> [引数...]。動詞と引数の検証は EC2 側のゲートウェイが行う。ここでは形だけ見る。
  printf '%s' "$body" | grep -qE -- '[;&|<>`]|\$\(' \
    && deny "ec2 での連結・パイプ・リダイレクトは禁止です（保存は「> out/…/名前.txt」の形だけ使えます）"
  case "$body" in
    ec2\ --selftest*|ec2\ --list|ec2\ --help|ec2\ -h|ec2) log "ALLOW"; exit 0 ;;
    ec2\ -*) deny "ec2 にオプションはありません（ec2 <host> <動詞> [引数...]）" ;;
    ec2\ *\ *) log "ALLOW"; exit 0 ;;
    *) deny "ec2 は ec2 <host> <動詞> [引数...] の形で実行します（一覧: ec2 <host> help）" ;;
  esac
fi

printf '%s' "$body" | grep -qE -- '[;&|<>`]|\$\(' \
  && deny "aws での連結・パイプ・リダイレクトは禁止です（保存は「> out/…/名前.json」の形だけ使えます）"
case "$body" in
  aws\ *) ;;
  *) deny "aws は単独のコマンドとしてのみ実行できます（bash -c / python3 -c 等の経由は不可）" ;;
esac

svc=$(printf '%s' "$body" | awk '{print $2}')
op=$(printf '%s'  "$body" | awk '{print $3}')

case "$svc" in
  --*) deny "グローバルオプションはサービス名の後ろに書いてください（例: aws ec2 describe-vpcs --region ...）" ;;
esac

# ---- 5. プロファイルの固定 ----
if has '--profile' && ! has '--profile[ =]claude-ro([^[:alnum:]-]|$)'; then
  deny "--profile は claude-ro のみ指定できます"
fi

# ---- 6. 危険な読み取り API（セッションポリシーと二重の防御） ----
DANGER='get-secret-value|batch-get-secret-value'
DANGER="$DANGER"'|get-parameter|get-parameters|get-parameters-by-path'
DANGER="$DANGER"'|receive-message'
DANGER="$DANGER"'|get-object|get-log-events|filter-log-events|start-live-tail|start-query'
DANGER="$DANGER"'|download-db-log-file-portion'
DANGER="$DANGER"'|decrypt|generate-data-key|re-encrypt'
DANGER="$DANGER"'|get-credentials-for-identity|get-open-id-token|get-authorization-token'
DANGER="$DANGER"'|assume-role|get-session-token|get-federation-token'
if printf '%s' "$op" | grep -qE "^($DANGER)$"; then
  deny "$svc $op は機密や業務データが出るため禁止です"
fi
[ "$svc" = "kms" ] && [ "$op" = "sign" ] && deny "kms sign は禁止です"

# ---- 7. サービス個別の制限 ----
case "$svc" in
  s3)
    [ "$op" = "ls" ] || deny "aws s3 は ls のみ許可です（cp/sync/mv/rm は不可）"
    log "ALLOW"; exit 0 ;;
  configure)
    case "$op" in
      list|list-profiles) ;;
      *) deny "aws configure は list / list-profiles のみ許可です" ;;
    esac
    exit 0 ;;
  help|"")
    deny "サービス名を指定してください" ;;
esac

# ---- 8. オペレーションは読み取り系の動詞のみ ----
if ! printf '%s' "$op" | grep -qE '^(describe|list|get|lookup|search|batch-get|batch-describe|estimate|validate|simulate|check|preview|view|test|generate)-'; then
  case "$op" in
    help) ;;
    *) deny "読み取り系のオペレーションのみ許可です（describe- / list- / get- / lookup- / search- など）" ;;
  esac
fi

log "ALLOW"
exit 0
