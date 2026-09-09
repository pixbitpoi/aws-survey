#!/usr/bin/env bash
# 一時キーを発行できないときの切り分け（ホストで実行・何も変更しない）。aws-survey doctor から呼ばれる。
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"

route_label() {
  case "$1" in
    own_role)      echo "自分で作る" ;;
    existing_role) echo "既存のロールを借りる" ;;
    granted_role)  echo "管理者に信頼してもらう" ;;
    "")            echo "未定" ;;
    *)             echo "$1" ;;
  esac
}

ui_head "対象"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "ロール" "$ROLE_ARN"
ui_kv "ロールの用意" "$(route_label "$AUTH_ROUTE")"
ui_kv "プロファイル" "$PROFILE_SRC"
ui_kv "本体" "$AWS_SURVEY_HOME"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
echo ""

step() { local n="$1" title="$2"; shift 2; ui_head "$n $title"; if out=$("$@" 2>&1); then ui_ok "通りました"; [ -z "$out" ] || ui_raw "$out"; return 0; else ui_err "通りません"; ui_raw "$out"; return 1; fi; }

step "1/4" "プロファイル $PROFILE_SRC でログインできているか" \
  aws sts get-caller-identity --profile "$PROFILE_SRC" --output json \
  || ui_text "ログインし直してから、もう一度実行してください。"
echo ""
step "2/4" "ロール $ROLE_NAME の状態" \
  aws iam get-role --profile "$PROFILE_SRC" --role-name "$ROLE_NAME" \
  --query 'Role.{Arn:Arn,MaxSession:MaxSessionDuration,Trust:AssumeRolePolicyDocument}' --output json \
  || ui_text "ロールが無いか見えません。$AWS_SURVEY_CMD role で状態を確かめてください。"
echo ""
step "3/4" "条件なしでロールを借りられるか" \
  aws sts assume-role --profile "$PROFILE_SRC" \
  --role-arn "$ROLE_ARN" --role-session-name diag \
  --query 'AssumedRoleUser.Arn' --output text \
  || ui_text "信頼ポリシーか MFA の条件が原因です。$AWS_SURVEY_CMD role の判定結果を確かめてください。"
echo ""
step "4/4" "読み取り専用の制限を付けてロールを借りられるか" \
  aws sts assume-role --profile "$PROFILE_SRC" \
  --role-arn "$ROLE_ARN" --role-session-name diag2 \
  --policy-arns arn=arn:aws:iam::aws:policy/ReadOnlyAccess \
  --policy "$(jq -c . "$LIBEXEC_DIR/session-guard.json")" \
  --query 'AssumedRoleUser.Arn' --output text \
  || ui_text "3 が通って 4 が通らないなら、原因は libexec/session-guard.json の大きさです（圧縮後のサイズ上限）。"
