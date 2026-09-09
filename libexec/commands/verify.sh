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
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"

export AWS_CONFIG_FILE="$AWS_DIR/config"
export AWS_SHARED_CREDENTIALS_FILE="$AWS_DIR/credentials"
export AWS_PROFILE=claude-ro
export AWS_PAGER=""

[ -f "$AWS_SHARED_CREDENTIALS_FILE" ] || ui_die "一時キーがありません。先に $AWS_SURVEY_CMD credentials を実行してください。"

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

ui_head "1/5 借りたロールになっているか"
who=$(aws sts get-caller-identity --query Arn --output text 2>&1)
case "$who" in
  *":assumed-role/$ROLE_NAME/"*) ok "$who" ;;
  *) ng "借りたロールになっていません" "$who" ;;
esac

echo ""
ui_head "2/5 読み取りは通るか"
vpc=$(aws ec2 describe-vpcs --max-items 1 --query 'Vpcs[0].VpcId' --output text 2>&1)
case "$vpc" in
  vpc-*) ok "VPC の一覧を読めました（${vpc}）" ;;
  *)     ng "読み取りが通りません" "$vpc" ;;
esac

echo ""
ui_head "3/5 書き込みは AWS が拒否するか"
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
ui_head "4/5 機密の読み出しは拒否されるか"
ui_text "読み取り専用でも読めてはいけないもの（パラメータの値、キューのメッセージ）を確かめます。"
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

echo ""
ui_head "5/5 強い権限に戻れないか"
r=$(aws sts assume-role --role-arn "$ROLE_ARN" --role-session-name esc 2>&1)
case "$r" in
  *AccessDenied*) ok "ロールの借り直しは拒否されました（AccessDenied）" ;;
  *)              ng "ロールを借り直せてしまいます" "$r" ;;
esac

echo ""
ui_head "結果"
ui_kv "確かめた" "$pass 件"
ui_kv "問題あり" "$fail 件"
ui_kv "飛ばした" "$skip 件"
if [ "$fail" -eq 0 ]; then
  ui_ok "読み取り専用であることを確認しました"
  env_mark_setup readonly_verified "読み取り専用であることを確かめた"
  echo ""
  next_cmd "$AWS_SURVEY_CMD run" "調査コンテナを起動します。中で Claude Code / Codex と調査を進めます"
  exit 0
else
  ui_err "読み取り専用になっていません。environment.json には記録しません。"
  ui_text "libexec/session-guard.json と、ロールに付いているポリシーを見直してください。"
  exit 1
fi
