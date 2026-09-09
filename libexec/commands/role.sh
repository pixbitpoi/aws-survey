#!/usr/bin/env bash
# 調査用の読み取り専用ロールを、作れるかどうか判定して作る（ホストで実行）
#
#   aws-survey role            判定のみ。何も変更しない（既定）
#   aws-survey role --create   ロールを作る／不足を補う
# 実体は libexec/commands/role.sh。通常は aws-survey 経由で呼ぶ。
#
# 判定は IAM のシミュレーション機能を使う。実行せずに「この操作は許可されるか」だけを見る。
# 表示は libexec/ui.sh の部品で組む。利用者向けの文に own_role などの内部の値名を書かない。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"

# 信頼ポリシーは対象固有の生成物なので、本体ではなく対象フォルダに置く
TRUST_OUT="$AWS_SURVEY_DIR/trust.json"
RO_POLICY="arn:aws:iam::aws:policy/ReadOnlyAccess"
DO_CREATE=0
case "${1:-}" in
  --create) DO_CREATE=1 ;;
  ""|--check) ;;
  *) echo "使い方: $AWS_SURVEY_CMD role [--create]" >&2; exit 1 ;;
esac

die() { ui_die "$@"; }
command -v aws >/dev/null || die "aws コマンドが見つかりません。"

# 利用者向けの言い換え
route_label() {
  case "$1" in
    own_role)      echo "自分で作る" ;;
    existing_role) echo "既存のロールを借りる" ;;
    granted_role)  echo "管理者に信頼してもらう" ;;
    "")            echo "未定" ;;
    *)             echo "$1" ;;
  esac
}

if [ "$DO_CREATE" -eq 1 ]; then ui_title "aws-survey role --create"; else ui_title "aws-survey role"; fi
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "ロール" "$ROLE_NAME"
ui_kv "ロールの用意" "$(route_label "$AUTH_ROUTE")"
echo ""

# ---- 1. 元プロファイルが生きているか ----
ui_head "1/3 ホストのプロファイル $PROFILE_SRC でログインできているか"
if ! who=$(aws sts get-caller-identity --profile "$PROFILE_SRC" --query Arn --output text 2>&1); then
  ui_raw "$who"
  die "$PROFILE_SRC が使えません。ログインし直してから、もう一度実行してください。"
fi
ui_ok "$who"
if [ -n "$PRINCIPAL_ARN" ] && [ "$who" != "$PRINCIPAL_ARN" ]; then
  ui_warn "environment.json に書いた実体（auth.principal_arn）と一致しません"
  ui_kv "environment.json" "$PRINCIPAL_ARN"
  ui_kv "実際" "$who"
  ui_text "信頼ポリシーは environment.json の値で作られます。意図が違うなら environment.json を直してください。"
fi
echo ""

# ---- 2. ロールは既にあるか ----
ui_head "2/3 ロール $ROLE_NAME はもうあるか"
role_exists=0
shortfalls=()
if role_json=$(aws iam get-role --profile "$PROFILE_SRC" --role-name "$ROLE_NAME" --output json 2>/dev/null); then
  role_exists=1
  ui_ok "あります"
  ui_kv "セッション上限" "$(echo "$role_json" | jq -r '.Role.MaxSessionDuration') 秒（environment.json は $DURATION 秒）"
  ui_kv "信頼ポリシー" "$(echo "$role_json" | jq -c '.Role.AssumeRolePolicyDocument.Statement')"
  attached=$(aws iam list-attached-role-policies --profile "$PROFILE_SRC" --role-name "$ROLE_NAME" \
               --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null)
  ui_kv "付いているポリシー" "${attached:-（なし）}"
  # 不足の洗い出し。揃っていれば「作る」のではなく「このロールを使う」案内にする
  case "$attached" in
    *ReadOnlyAccess*) ;;
    *) shortfalls+=("ReadOnlyAccess が付いていません") ;;
  esac
  if [ -n "$PRINCIPAL_ARN" ] && ! echo "$role_json" | jq -e --arg p "$PRINCIPAL_ARN" \
       '[.Role.AssumeRolePolicyDocument.Statement[] | select(.Effect=="Allow") | .Principal.AWS] | flatten | index($p) != null' >/dev/null 2>&1; then
    shortfalls+=("信頼ポリシーに自分（${PRINCIPAL_ARN}）が入っていません")
  fi
  if [ "$MFA_REQUIRED" = "true" ] && ! echo "$role_json" | jq -e \
       '[.Role.AssumeRolePolicyDocument.Statement[] | .Condition.Bool."aws:MultiFactorAuthPresent"] | index("true") != null' >/dev/null 2>&1; then
    shortfalls+=("信頼ポリシーに MFA の条件がありません")
  fi
  for sf in "${shortfalls[@]}"; do ui_warn "$sf"; done
else
  ui_skip "まだありません"
fi
echo ""

# ---- 3. 作れるか（シミュレーション） ----
ui_head "3/3 このアカウントでロールを自分で作れるか"
ui_text "IAM のシミュレーション機能で「許可されるか」だけを見ます。実際には何も実行しません。"
sim_src="$who"
can_create=""
case "$who" in
  *:assumed-role/*)
    ui_warn "ログイン元がすでに借りたロールのため、シミュレーションの対象を特定できません。"
    ui_text "判定は飛ばします。実際に $AWS_SURVEY_CMD role --create を試した結果で判断してください。"
    sim_src="" ;;
esac
if [ -n "$sim_src" ]; then
  ctx=()
  if [ "$MFA_REQUIRED" = "true" ]; then
    ctx=(--context-entries ContextKeyName=aws:MultiFactorAuthPresent,ContextKeyValues=true,ContextKeyType=boolean)
    ui_text "MFA ありとして判定します（environment.json で MFA 必須としているため）。"
  fi
  if ! sim=$(aws iam simulate-principal-policy --profile "$PROFILE_SRC" \
        --policy-source-arn "$sim_src" \
        --action-names iam:CreateRole iam:AttachRolePolicy iam:UpdateRole sts:AssumeRole ec2:DescribeVpcs \
        "${ctx[@]}" \
        --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}' \
        --output text 2>&1); then
    ui_raw "$sim"
    ui_warn "シミュレーション自体が許可されていません（iam:SimulatePrincipalPolicy がありません）。"
    ui_text "判定は飛ばします。実際に $AWS_SURVEY_CMD role --create を試した結果で判断してください。"
  else
    while IFS=$'\t ' read -r action decision; do
      [ -n "$action" ] && [ -n "$decision" ] || continue
      case "$decision" in
        allowed) ui_ok "$action  ${C_DIM}許可${C_RESET}" ;;
        *)       ui_err "$action  ${C_DIM}拒否（${decision}）${C_RESET}" ;;
      esac
    done <<< "$sim"
    if echo "$sim" | grep -q '^ec2:DescribeVpcs[[:space:]]*explicitDeny'; then
      echo ""
      ui_warn "単なる VPC 一覧の閲覧まで拒否と出ています。個別の権限ではなく、"
      ui_text "アカウント全体にかかる条件（MFA 必須など）が原因の可能性が高いです。"
      ui_text "environment.json の auth.mfa_required を確認してください。"
    fi
    if echo "$sim" | grep -q '^iam:CreateRole[[:space:]]*allowed'; then can_create=yes; else can_create=no; fi
  fi
fi
echo ""

# ---- 判定のまとめ ----
recorded=$(jq -r '[.setup.route_decided, .setup.role_created] | map(. // empty) | length' "$ENV_FILE")
if [ "$DO_CREATE" -eq 0 ]; then
  if [ "$role_exists" -eq 1 ] && [ "${#shortfalls[@]}" -eq 0 ]; then
    ui_ok "ロールは用意できています"
    if [ "$recorded" -lt 2 ] || [ -z "$AUTH_ROUTE" ]; then
      ui_text "ただし、このロールを使うことがまだ environment.json に記録されていません。"
      echo ""
      next_cmd "$AWS_SURVEY_CMD role --create" "このロールを使うことを記録します（信頼ポリシーとセッション上限も確かめ直します。作り直しはしません）"
    else
      echo ""
      next_cmd "$AWS_SURVEY_CMD credentials" "読み取り専用の一時キーを発行します"
    fi
  elif [ "$role_exists" -eq 1 ]; then
    ui_warn "ロールはありますが、不足があります（上の ⚠）"
    echo ""
    next_cmd "$AWS_SURVEY_CMD role --create" "不足を補います（ReadOnlyAccess を付け、信頼ポリシーを environment.json の内容に合わせます）"
  else
    case "$can_create" in
      yes)
        ui_ok "ロールを自分で作れます"
        echo ""
        next_cmd "$AWS_SURVEY_CMD role --create" "読み取り専用ロールを作ります（作成後、ロールの用意のしかたを記録します）" ;;
      no)
        ui_err "ロールを自分で作る権限がありません"
        ui_text "次のどちらかで進めます。"
        ui_text "  既存のロールを借りる: 読み取り専用で、自分を信頼しているロールがあれば使えます。"
        ui_text "  管理者に頼む: 信頼ポリシーに自分を追加してもらいます。"
        ui_text "借りられるロールを探すには:"
        ui_text "  aws iam list-roles --profile $PROFILE_SRC --query 'Roles[].RoleName'"
        ui_text "借りるロールには ReadOnlyAccess 相当が付いている必要があります。"
        ui_text "セッションポリシーは禁止しか書いておらず、許可はロール側から来るためです。"
        ui_text "1 回のセッションの長さも、そのロールの上限（MaxSessionDuration）を超えられません。"
        ui_text "決めたら environment.json を書き換えます（借りる経路では --create は動きません）:"
        ui_text "  auth.route      借りるなら existing_role、管理者に信頼してもらうなら granted_role"
        ui_text "  auth.role_name  そのロール名"
        ui_text "  setup.route_decided と setup.role_created  今日の日付（YYYY-MM-DD）"
        ui_text "書けたら $AWS_SURVEY_CMD credentials に進みます。" ;;
      *)
        ui_warn "判定できませんでした"
        echo ""
        next_cmd "$AWS_SURVEY_CMD role --create" "実際に作ってみて判断します" ;;
    esac
  fi
  exit 0
fi

# ---- 4. 作る ----
# init は auth.route を聞かず null で書く。--create で作れたら own_role として記録する
ROUTE_UNSET=0
if [ -z "$AUTH_ROUTE" ]; then
  ROUTE_UNSET=1; AUTH_ROUTE=own_role
fi
if [ "$AUTH_ROUTE" != "own_role" ]; then
  die "environment.json では「$(route_label "$AUTH_ROUTE")」になっています。作るものはありません。
  借りるロールが使えるかは $AWS_SURVEY_CMD doctor で確かめてください。"
fi

if [ "$role_exists" -eq 1 ]; then ui_head "ロールを整えます"; else ui_head "ロールを作ります"; fi

# 信頼ポリシーを environment.json から生成する（trust.json は生成物）
[ -n "$PRINCIPAL_ARN" ] || die "environment.json の auth.principal_arn が空です。信頼ポリシーを作れません。"
if [ "$MFA_REQUIRED" = "true" ]; then
  jq -n --arg p "$PRINCIPAL_ARN" '{Version:"2012-10-17",Statement:[{Effect:"Allow",
    Principal:{AWS:$p},Action:"sts:AssumeRole",
    Condition:{Bool:{"aws:MultiFactorAuthPresent":"true"}}}]}' > "$TRUST_OUT"
else
  jq -n --arg p "$PRINCIPAL_ARN" '{Version:"2012-10-17",Statement:[{Effect:"Allow",
    Principal:{AWS:$p},Action:"sts:AssumeRole"}]}' > "$TRUST_OUT"
fi
ui_ok "信頼ポリシーを書きました: $TRUST_OUT"
ui_raw "$(jq -c . "$TRUST_OUT")"

if [ "$role_exists" -eq 1 ]; then
  ui_text "ロールは既にあるので、信頼ポリシーとセッション上限を environment.json の内容に合わせます。"
  aws iam update-assume-role-policy --profile "$PROFILE_SRC" \
    --role-name "$ROLE_NAME" --policy-document "file://$TRUST_OUT" >/dev/null \
    || die "信頼ポリシーを更新できませんでした。"
  ui_ok "信頼ポリシーを更新しました"
else
  role_arn=$(aws iam create-role --profile "$PROFILE_SRC" \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$TRUST_OUT" \
    --max-session-duration "$DURATION" \
    --description "Read-only role" \
    --query 'Role.Arn' --output text) \
    || die "ロールを作れませんでした。上の判定結果を確認してください。"
  ui_ok "ロールを作りました: $role_arn"
fi

# create-role では作成時に設定済みなので、既存ロールを引き継いだときだけ合わせ直す
if [ "$role_exists" -eq 1 ]; then
  aws iam update-role --profile "$PROFILE_SRC" \
    --role-name "$ROLE_NAME" --max-session-duration "$DURATION" >/dev/null 2>&1 \
    && ui_ok "セッション上限を $DURATION 秒に合わせました"
fi

aws iam attach-role-policy --profile "$PROFILE_SRC" \
  --role-name "$ROLE_NAME" --policy-arn "$RO_POLICY" >/dev/null \
  || die "ReadOnlyAccess を付けられませんでした。"
ui_ok "ReadOnlyAccess を付けました"

if [ "$ROUTE_UNSET" -eq 1 ]; then
  _tmp=$(mktemp) && jq '.auth.route = "own_role"' "$ENV_FILE" > "$_tmp" && mv "$_tmp" "$ENV_FILE" \
    && ui_text "environment.json に記録しました（ロールの用意のしかた: 自分で作る）"
fi
env_mark_setup route_decided "ロールの用意のしかたを決めた"
env_mark_setup role_created "ロールを用意した"

echo ""
ui_ok "ロールの準備ができました"
echo ""
next_cmd "$AWS_SURVEY_CMD credentials" "読み取り専用の一時キーを発行します"
also_cmd "$AWS_SURVEY_CMD verify" "続けて、その一時キーが読み取り専用であることを確かめます（省略できません）"
