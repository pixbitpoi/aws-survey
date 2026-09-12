#!/usr/bin/env bash
# 発行した一時キーが本当に読み取り専用かを、ホストで確かめる（AWS 側の判定の検証）
#
#   aws-survey verify   （実体は libexec/commands/verify.sh）
#
# ⚠️ この確認はコンテナ内では代用できません。
#    コンテナ内ではフックが手前で拒否してしまい、AWS 側の判定が試されないためです。
#    AWS 側の判定が効いているかを確かめられるのは、ここだけです。
#
# 書き込みの検証には --dry-run を使います（権限チェックだけ行い、実際には何も起きません）。
# EC2 の中を調べる機能（ssh.hosts）があるときは、SSH の入口（ssm:StartSession）が登録済みインスタンスと
# AWS-StartSSHSession に限られることも確かめる（設計文書の第 7 節）。session-manager-plugin の無い PATH で
# 実行するので、万一許可されていても対話には入らず、CLI が作った直後にセッションを終了する。
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/keys.sh"

# 一時キーが無い・切れている・残りが短ければ発行し直してから確かめる（利用者に credentials を打たせない）。
# 元プロファイルで発行するので、一時キーを指す環境変数を出す前に行う
key_ensure "$KEY_MIN_QUICK" || exit 1

export AWS_CONFIG_FILE="$AWS_DIR/config"
export AWS_SHARED_CREDENTIALS_FILE="$AWS_DIR/credentials"
export AWS_PROFILE=claude-ro
export AWS_PAGER=""

pass=0; fail=0; skip=0
ok()   { ui_ok "$1"; pass=$((pass+1)); }
ng()   { ui_err "$1"; [ -z "${2:-}" ] || ui_raw "$2"; fail=$((fail+1)); }
note() { ui_skip "$1"; skip=$((skip+1)); }

ui_title "aws-survey verify"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "ロール" "$ROLE_NAME"
ui_text "一時キーで実際に AWS を叩き、読めること・書けないこと・強い権限に戻れないことを確かめます。"
echo ""

ui_head "1/7 借りたロールになっているか"
who=$(aws sts get-caller-identity --query Arn --output text 2>&1)
case "$who" in
  *":assumed-role/$ROLE_NAME/"*) ok "$who" ;;
  *) ng "借りたロールになっていません" "$who" ;;
esac

echo ""
ui_head "2/7 読み取りは通るか"
vpc=$(aws ec2 describe-vpcs --max-items 1 --query 'Vpcs[0].VpcId' --output text 2>&1)
case "$vpc" in
  vpc-*) ok "VPC の一覧を読めました（${vpc}）" ;;
  *)     ng "読み取りが通りません" "$vpc" ;;
esac

echo ""
ui_head "3/7 書き込みは AWS が拒否するか"
ui_text "--dry-run で権限だけ確かめます。実際には何も変わりません。"
if [ "${vpc:0:4}" = "vpc-" ]; then
  r=$(aws ec2 create-tags --resources "$vpc" --tags Key=canary,Value=1 --dry-run 2>&1)
  case "$r" in
    *UnauthorizedOperation*) ok "タグの付与は拒否されました（UnauthorizedOperation）" ;;
    *DryRunOperation*)       ng "タグを付けられてしまいます（DryRunOperation）" "ロールに付いているポリシーと libexec/session-guard.json を見直してください" ;;
    *)                       ng "判定できません" "$r" ;;
  esac
else
  note "VPC が取れなかったため飛ばしました"
fi

echo ""
ui_head "4/7 機密の読み出しは拒否されるか"
ui_text "読み取り専用でも読めてはいけないもの（パラメータの値、キューのメッセージ、関数のコードの URL）を確かめます。"
p=$(aws ssm describe-parameters --max-items 1 --query 'Parameters[0].Name' --output text 2>/dev/null)
if [ -n "$p" ] && [ "$p" != "None" ]; then
  r=$(aws ssm get-parameter --name "$p" 2>&1)
  case "$r" in
    *AccessDenied*) ok "パラメータの値の読み出しは拒否されました（AccessDenied）" ;;
    *)              ng "パラメータの値が読めてしまいます" "$r" ;;
  esac
else
  note "パラメータが無いため飛ばしました"
fi

q=$(aws sqs list-queues --max-results 1 --query 'QueueUrls[0]' --output text 2>/dev/null)
if [ -n "$q" ] && [ "$q" != "None" ]; then
  r=$(aws sqs receive-message --queue-url "$q" 2>&1)
  case "$r" in
    *AccessDenied*) ok "キューのメッセージ受信は拒否されました（AccessDenied）" ;;
    *)              ng "キューからメッセージを受信できてしまいます" "$r" ;;
  esac
else
  note "キューが無いため飛ばしました"
fi

# get-function の応答にはコードの署名付き URL（Code.Location）が入る。IAM の評価は関数の有無より先なので、
# 存在しない名前で呼べば関数の無い対象でも確かめられる。拒否されなければ ResourceNotFoundException が返る。
r=$(aws lambda get-function --function-name verify-canary-does-not-exist --query 'Configuration.FunctionName' --output text 2>&1)
case "$r" in
  *AccessDenied*) ok "関数のコードの URL の取得は拒否されました（AccessDenied）" ;;
  *)              ng "関数のコードの URL を取得できてしまいます" "$r" ;;
esac

echo ""
ui_head "5/7 強い権限に戻れないか"
r=$(aws sts assume-role --role-arn "$ROLE_ARN" --role-session-name esc 2>&1)
case "$r" in
  *AccessDenied*) ok "ロールの借り直しは拒否されました（AccessDenied）" ;;
  *)              ng "ロールを借り直せてしまいます" "$r" ;;
esac

# start-session は API が通ると session-manager-plugin を起動して対話に入る。プラグインの無い PATH で叩き、
# API の結果（AccessDeniedException か、通ってしまったか）だけを見る。通ってしまったときは CLI 自身がセッションを終了する。
plugin_free_path() {
  local out="" d dirs
  IFS=: read -ra dirs <<< "$PATH"
  for d in "${dirs[@]}"; do
    [ -x "$d/session-manager-plugin" ] && continue
    out="${out:+$out:}$d"
  done
  printf '%s' "${out:-/var/empty}"
}
AWS_BIN=$(command -v aws)
PLUGIN_FREE_PATH=$(plugin_free_path)
start_session() {
  PATH="$PLUGIN_FREE_PATH" "$AWS_BIN" ssm start-session --target "$1" --document-name "$2" --parameters "$3" 2>&1 </dev/null
}

echo ""
ui_head "6/7 登録したインスタンス以外への SSH 接続は拒否されるか"
if [ -z "$SSH_HOSTS" ]; then
  note "登録済みホストが無いため飛ばしました（$AWS_SURVEY_CMD ssh setup の後に確かめます）"
else
  ui_text "タグ diag:ssh=${SURVEY_NAME} の無いインスタンスへの start-session を試します。"
  other=$(aws ec2 describe-instances \
            --query 'Reservations[].Instances[].{id:InstanceId,tag:Tags[?Key==`diag:ssh`]|[0].Value}' --output json 2>/dev/null \
          | jq -r --arg n "$SURVEY_NAME" 'map(select(.tag != $n)) | .[0].id // empty')
  if [ -n "$other" ]; then
    r=$(start_session "$other" AWS-StartSSHSession portNumber=22)
    case "$r" in
      *AccessDenied*) ok "タグの無いインスタンスへの接続は拒否されました（AccessDenied）" ;;
      *)              ng "タグの無いインスタンス（${other}）に接続できてしまいます" "$r" ;;
    esac
  else
    note "タグの無いインスタンスが無いため飛ばしました"
  fi
fi

echo ""
ui_head "7/7 SSH 以外の経路（対話コマンド・ポート転送）は拒否されるか"
if [ -z "$SSH_HOSTS" ]; then
  note "登録済みホストが無いため飛ばしました"
else
  target=$(jq -r '.ssh.hosts | to_entries[0].value.instance_id // empty' "$ENV_FILE")
  ui_text "登録済みインスタンスに対して、AWS-StartSSHSession 以外のドキュメントを試します。"
  r=$(start_session "$target" AWS-StartInteractiveCommand command=uptime)
  case "$r" in
    *AccessDenied*) ok "対話コマンド（AWS-StartInteractiveCommand）は拒否されました（AccessDenied）" ;;
    *)              ng "対話コマンドを実行できてしまいます" "$r" ;;
  esac
  r=$(start_session "$target" AWS-StartPortForwardingSession portNumber=22,localPortNumber=0)
  case "$r" in
    *AccessDenied*) ok "ポート転送（AWS-StartPortForwardingSession）は拒否されました（AccessDenied）" ;;
    *)              ng "ポート転送ができてしまいます" "$r" ;;
  esac
fi

echo ""
ui_head "結果"
ui_kv "確かめた" "$pass 件"
ui_kv "問題あり" "$fail 件"
ui_kv "飛ばした" "$skip 件"
if [ "$fail" -eq 0 ]; then
  ui_ok "読み取り専用であることを確認しました"
  env_mark_setup readonly_verified "読み取り専用であることを確かめた"
  echo ""
  survey_next_cmd
  exit 0
else
  ui_err "読み取り専用になっていません。environment.json には記録しません。"
  ui_text "libexec/session-guard.json と、ロールに付いているポリシーを見直してください。"
  [ -z "$SSH_HOSTS" ] || ui_text "EC2 への接続が広すぎるときは、ポリシー $DIAG_POLICY_NAME の内容を $AWS_SURVEY_CMD role --create で合わせ直してください。"
  exit 1
fi
