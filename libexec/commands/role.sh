#!/usr/bin/env bash
# 調査用の読み取り専用ロールを、作れるかどうか判定して作る（ホストで実行）
#
#   aws-survey role            判定のみ。何も変更しない（既定）
#   aws-survey role --create   ロールを作る／不足を補う
# 実体は libexec/commands/role.sh。通常は aws-survey 経由で呼ぶ。
#
# 判定は IAM のシミュレーション機能を使う。実行せずに「この操作は許可されるか」だけを見る。
# environment.json に ssh.hosts（EC2 の中を調べる機能）があれば、登録済みインスタンスへの ssm:StartSession だけを
# 許す顧客管理ポリシー diag-ssh-<name> も作ってロールに付ける（設計文書の第 6 節）。冪等。
# ロールを借りる経路（existing_role / granted_role）では作れないので、管理者に渡す JSON を表示して終わる。
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

# セッション ARN（arn:…:sts::<ID>:assumed-role/<ロール>/<人>）から、借りているロールの名前を取り出す
principal_role() { local r="${1#*:assumed-role/}"; printf '%s' "${r%%/*}"; }

# $1 のセッション ARN が、$2 のロール ARN（パスは問わない）を借りているセッションか
session_in_role() {
  case "$1" in arn:*:sts::*:assumed-role/*/*) ;; *) return 1 ;; esac
  case "$2" in arn:*:iam::*:role/*) ;; *) return 1 ;; esac
  [ "$(printf '%s' "$1" | cut -d: -f2,5)" = "$(printf '%s' "$2" | cut -d: -f2,5)" ] \
    && [ "$(principal_role "$1")" = "${2##*/}" ]
}

# principal_arn の相手が、信頼ポリシーの貸す相手（改行区切りの Principal.AWS）に含まれるか。$3 はいまのログイン。
#   exact   そのまま載っている
#   role    principal_arn がセッション ARN で、借りているロール（パスは問わない）が載っている（信頼ポリシーのほうが広い）
#   narrow  principal_arn がロール ARN で、そのロールを借りているいまのログインが載っている（信頼ポリシーのほうが狭い）
#   account principal_arn のアカウント全体（:root かアカウント ID）が載っている
#   none    含まれない
principal_coverage() {
  local me="$1" list="$2" now="${3:-}" p part acct
  if printf '%s\n' "$list" | grep -qxF -- "$me"; then echo exact; return 0; fi
  while IFS= read -r p; do
    ! session_in_role "$me" "$p" || { echo role; return 0; }
  done <<< "$list"
  if [ -n "$now" ] && session_in_role "$now" "$me" && printf '%s\n' "$list" | grep -qxF -- "$now"; then
    echo narrow; return 0
  fi
  part=${me#arn:}; part=${part%%:*}
  acct=$(printf '%s' "$me" | cut -d: -f5)
  if printf '%s\n' "$list" | grep -qxF -e "arn:$part:iam::$acct:root" -e "$acct"; then echo account; return 0; fi
  echo none
}

# EC2 の中を調べるための顧客管理ポリシー。許すのは diag:ssh=<name> タグ付きインスタンスへの
# AWS-StartSSHSession だけ。ssm:SendCommand（導入）は元プロファイルの仕事で、ここには足さない。
# セッションの終了・再開は自分のセッション（ID が <ロールセッション名>-<乱数>）に限る。
diag_policy_json() {
  jq -n --arg account "$ACCOUNT_ID" --arg name "$SURVEY_NAME" --arg session "$SESSION_NAME_PREFIX" '{
    Version: "2012-10-17",
    Statement: [
      {Effect: "Allow", Action: "ssm:StartSession",
       Resource: "arn:aws:ec2:*:\($account):instance/*",
       Condition: {StringEquals: {"ssm:resourceTag/diag:ssh": $name}}},
      {Effect: "Allow", Action: "ssm:StartSession",
       Resource: "arn:aws:ssm:*:*:document/AWS-StartSSHSession"},
      {Effect: "Allow", Action: ["ssm:TerminateSession", "ssm:ResumeSession"],
       Resource: "arn:aws:ssm:*:*:session/\($session)-*"}
    ]}'
}

# ポリシーを作る／内容を合わせる／ロールに付ける。何度実行しても同じ状態になる。
ensure_diag_policy() {
  local tmp ver cur
  tmp=$(mktemp) || die "一時ファイルを作れません。"
  diag_policy_json > "$tmp"
  if ver=$(aws iam get-policy --profile "$PROFILE_SRC" --policy-arn "$DIAG_POLICY_ARN" \
             --query Policy.DefaultVersionId --output text 2>/dev/null) && [ -n "$ver" ]; then
    cur=$(aws iam get-policy-version --profile "$PROFILE_SRC" --policy-arn "$DIAG_POLICY_ARN" \
            --version-id "$ver" --query PolicyVersion.Document --output json 2>/dev/null)
    if [ "$(jq -cS . <<< "$cur" 2>/dev/null)" = "$(jq -cS . "$tmp")" ]; then
      ui_ok "ポリシー $DIAG_POLICY_NAME はあります（内容も同じ）"
    else
      # 版は 5 つまでしか持てない。既定でない版を消してから、新しい版を既定にする
      local v
      for v in $(aws iam list-policy-versions --profile "$PROFILE_SRC" --policy-arn "$DIAG_POLICY_ARN" \
                   --query 'Versions[?!IsDefaultVersion].VersionId' --output text 2>/dev/null); do
        [ "$v" != None ] || continue
        aws iam delete-policy-version --profile "$PROFILE_SRC" --policy-arn "$DIAG_POLICY_ARN" --version-id "$v" >/dev/null 2>&1
      done
      aws iam create-policy-version --profile "$PROFILE_SRC" --policy-arn "$DIAG_POLICY_ARN" \
        --policy-document "file://$tmp" --set-as-default >/dev/null \
        || { rm -f "$tmp"; die "ポリシー $DIAG_POLICY_NAME を更新できませんでした。"; }
      ui_ok "ポリシー $DIAG_POLICY_NAME の内容を合わせました"
    fi
  else
    aws iam create-policy --profile "$PROFILE_SRC" --policy-name "$DIAG_POLICY_NAME" \
      --policy-document "file://$tmp" --description "Session Manager SSH to tagged instances" \
      --query Policy.Arn --output text >/dev/null \
      || { rm -f "$tmp"; die "ポリシー $DIAG_POLICY_NAME を作れませんでした。"; }
    ui_ok "ポリシー $DIAG_POLICY_NAME を作りました"
  fi
  rm -f "$tmp"
  aws iam attach-role-policy --profile "$PROFILE_SRC" \
    --role-name "$ROLE_NAME" --policy-arn "$DIAG_POLICY_ARN" >/dev/null \
    || die "ポリシー $DIAG_POLICY_NAME をロールに付けられませんでした。"
  ui_ok "$DIAG_POLICY_NAME を付けました（登録済みインスタンスへの SSH 接続だけを許します）"
  env_mark_setup ssh_policy_attached "EC2 への接続を許すポリシーを付けた"
}

# ロールを借りる経路では作れない。管理者に渡す形で表示する
show_diag_policy_for_admin() {
  ui_head "管理者に頼むもの（EC2 の中を調べる権限）"
  ui_text "登録済みインスタンス（タグ diag:ssh=${SURVEY_NAME}）への SSH 接続だけを許すポリシーです。"
  ui_text "次の内容で $DIAG_POLICY_NAME を作り、ロール $ROLE_NAME に付けてもらってください。"
  ui_raw "$(diag_policy_json)"
  ui_text "管理者が打つコマンドの例:"
  ui_text "  aws iam create-policy --policy-name $DIAG_POLICY_NAME --policy-document file://diag-ssh.json"
  ui_text "  aws iam attach-role-policy --role-name $ROLE_NAME --policy-arn $DIAG_POLICY_ARN"
}

if [ "$DO_CREATE" -eq 1 ]; then ui_title "aws-survey role --create"; else ui_title "aws-survey role"; fi
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "ロール" "$ROLE_NAME"
ui_kv "ロールの用意" "$(route_label "$AUTH_ROUTE")"
if [ "$DO_CREATE" -eq 1 ]; then
  ui_text "まず、いまの状態（直す前）を確かめます。そのあと、足りないところを直します。"
fi
echo ""

# ---- 1. 元プロファイルが生きているか ----
ui_head "1/3 ホストのプロファイル $PROFILE_SRC でログインできているか"
if ! who=$(aws sts get-caller-identity --profile "$PROFILE_SRC" --query Arn --output text 2>&1); then
  ui_raw "$who"
  die "$PROFILE_SRC が使えません。ログインし直してから、もう一度実行してください。"
fi
ui_ok "$who"
# 借りたロールからのログインは、ロール側の上限にかかわらず 1 時間まで。ロールを直しても変わらないので、ここで environment.json 側を案内する
if is_chained_arn "$who" && [ "$DURATION" -gt "$CHAIN_MAX_SECONDS" ]; then
  ui_chain_limit "$DURATION"
  ui_text "environment.json の auth.duration_seconds を ${CHAIN_MAX_SECONDS} にしてください（$AWS_SURVEY_CMD credentials がその場で直すこともできます）。"
fi
# 「同じロールでログインした人なら誰でも」（ロール ARN）を選んだときは、いまのログインがそのロールのセッションなら一致とみなす
if [ -n "$PRINCIPAL_ARN" ] && [ "$who" != "$PRINCIPAL_ARN" ] && ! session_in_role "$who" "$PRINCIPAL_ARN"; then
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
  max_session=$(echo "$role_json" | jq -r '.Role.MaxSessionDuration // 3600')
  ui_kv "セッション上限" "${max_session} 秒（environment.json は $DURATION 秒）"
  ui_kv "信頼ポリシー" "$(echo "$role_json" | jq -c '.Role.AssumeRolePolicyDocument.Statement')"
  attached=$(aws iam list-attached-role-policies --profile "$PROFILE_SRC" --role-name "$ROLE_NAME" \
               --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null)
  ui_kv "付いているポリシー" "${attached:-（なし）}"
  # 不足の洗い出し。揃っていれば「作る」のではなく「このロールを使う」案内にする
  # 1 回のセッションの長さ（init の 8 問目）は、ロールの上限を超えられない。自分のロールなら --create が上限を合わせる。
  # 借りたロールの上限は変えられないので、environment.json 側を下げてもらう
  if [ "$DURATION" -gt "$max_session" ]; then
    if [ -z "$AUTH_ROUTE" ] || [ "$AUTH_ROUTE" = own_role ]; then
      shortfalls+=("セッション上限（${max_session} 秒）が environment.json の duration_seconds（${DURATION} 秒）より短いです")
    else
      shortfalls+=("セッション上限（${max_session} 秒）が environment.json の duration_seconds（${DURATION} 秒）より短いです。借りるロールの上限は変えられないので、auth.duration_seconds を ${max_session} 以下にしてください（${ENV_FILE}）")
    fi
  fi
  case "$attached" in
    *ReadOnlyAccess*) ;;
    *) shortfalls+=("ReadOnlyAccess が付いていません") ;;
  esac
  if [ -n "$SSH_HOSTS" ]; then
    case "$attached" in
      *"$DIAG_POLICY_ARN"*) ;;
      *) shortfalls+=("EC2 の中を調べる権限（${DIAG_POLICY_NAME}）が付いていません") ;;
    esac
  fi
  # 信頼ポリシーに MFA の条件があるか。Identity Center のログインはこの条件を満たせないので、あると借りられない
  trust_mfa=0
  if echo "$role_json" | jq -e \
       '[.Role.AssumeRolePolicyDocument.Statement[] | .Condition.Bool."aws:MultiFactorAuthPresent"] | index("true") != null' >/dev/null 2>&1; then
    trust_mfa=1
  fi
  can_borrow="あなたはこのロールを借りられます"
  if [ "$trust_mfa" -eq 1 ] && is_sso_arn "$PRINCIPAL_ARN"; then
    can_borrow="貸す相手には、あなたが入っています（ただし下の ⚠ のため、今は借りられません）"
  fi
  # 貸す相手は文字の一致ではなく「principal_arn の相手が含まれるか」で見る。含まれていれば書き方の違いは不足にしない
  trust_principals=$(echo "$role_json" | jq -r '[.Role.AssumeRolePolicyDocument.Statement[] | select(.Effect=="Allow")
                       | .Principal | objects | .AWS // empty] | flatten | .[] | strings' 2>/dev/null)
  if [ -n "$PRINCIPAL_ARN" ]; then
    case "$(principal_coverage "$PRINCIPAL_ARN" "$trust_principals" "$who")" in
      exact)
        ui_ok "$can_borrow"
        ui_text "信頼ポリシーの貸す相手は environment.json と同じです。" ;;
      role)
        ui_ok "$can_borrow"
        ui_text "信頼ポリシーは「$(ui_role_audience "$(principal_role "$PRINCIPAL_ARN")")」に貸す書き方で、"
        ui_text "environment.json の「自分だけ」より広い範囲です。このままでも使えます。"
        ui_text "自分だけに絞るなら $AWS_SURVEY_CMD role --create で書き換えます。" ;;
      narrow)
        ui_ok "$can_borrow"
        ui_text "信頼ポリシーは「自分だけ」に貸す書き方で、environment.json の"
        ui_text "「$(ui_role_audience "$PRINCIPAL_ARN")」より狭い範囲です。ほかの人は借りられません。"
        ui_text "ほかの人にも貸すなら $AWS_SURVEY_CMD role --create で書き換えます。" ;;
      account)
        ui_ok "$can_borrow"
        ui_text "信頼ポリシーはアカウント全体に貸す書き方です。借りられるかは、あなた側の権限で決まります。" ;;
      *)
        shortfalls+=("信頼ポリシーの貸す相手に、あなた（${PRINCIPAL_ARN}）が入っていません。このままでは借りられません") ;;
    esac
  fi
  if is_sso_arn "$PRINCIPAL_ARN"; then
    if [ "$MFA_REQUIRED" = "true" ]; then
      shortfalls+=("environment.json で MFA 必須（mfa_required: true）になっていますが、Identity Center のログインではロールの側で MFA を確かめられません。false にしてください")
    elif [ "$trust_mfa" -eq 1 ]; then
      shortfalls+=("信頼ポリシーに「MFA 済みの人だけ」という条件があるため、Identity Center のログインでは借りられません")
    fi
  elif [ "$MFA_REQUIRED" = "true" ] && [ "$trust_mfa" -eq 0 ]; then
    shortfalls+=("environment.json では MFA 必須ですが、信頼ポリシーに「MFA 済みの人だけ」という条件がありません")
  fi
  for sf in "${shortfalls[@]}"; do ui_warn "$sf"; done
  # --create はこのあと直す。ただし直さずに止まる場合（借りたロール、Identity Center で MFA 必須）はそう言わない
  if [ "$DO_CREATE" -eq 1 ] && [ "${#shortfalls[@]}" -gt 0 ] \
     && { [ -z "$AUTH_ROUTE" ] || [ "$AUTH_ROUTE" = own_role ]; } \
     && ! { [ "$MFA_REQUIRED" = "true" ] && is_sso_arn "$PRINCIPAL_ARN"; }; then
    ui_text "ここまでは直す前のいまの状態です。⚠ の点は、このあと「ロールを整えます」で直します。"
  fi
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
    # 借りたロールのセッションは simulate-principal-policy の対象に指定できない
    if [ "$role_exists" -eq 1 ]; then
      ui_skip "ロールはもうあるので、この確認は要りません"
    else
      ui_skip "この確認は飛ばします（ロールを借りた状態でログインしているため、確認する相手を指定できません）"
      ui_text "作れるかどうかは、$AWS_SURVEY_CMD role --create を実際に試して判断してください。"
    fi
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
    ui_warn "ロールはありますが、environment.json と合っていないところがあります（上の ⚠）"
    if [ "$MFA_REQUIRED" = "true" ] && is_sso_arn "$PRINCIPAL_ARN"; then
      ui_text "先に environment.json の auth.mfa_required を false に書き換えてください（${ENV_FILE}）。"
    fi
    echo ""
    if [ -n "$AUTH_ROUTE" ] && [ "$AUTH_ROUTE" != own_role ]; then
      ui_text "借りるロールは、このコマンドでは直せません。⚠ の案内に沿って environment.json を直すか、管理者に頼んでから、もう一度 $AWS_SURVEY_CMD role で確かめてください。"
    else
      next_cmd "$AWS_SURVEY_CMD role --create" "合っていないところを直します（信頼ポリシーを environment.json の内容に書き換え、足りないポリシーを付けます。ロールは作り直しません）"
    fi
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
  ui_warn "environment.json では「$(route_label "$AUTH_ROUTE")」になっています。ロールは作りません。"
  ui_text "借りるロールが使えるかは $AWS_SURVEY_CMD doctor で確かめてください。"
  if [ -n "$SSH_HOSTS" ]; then
    echo ""
    # 借りたロールに管理者が付けてくれたかは、上で読んだ「付いているポリシー」で分かる。付いていれば記録し、無ければ頼む内容を出す
    case "${attached:-}" in
      *"$DIAG_POLICY_ARN"*)
        ui_ok "$DIAG_POLICY_NAME は付いています（登録済みインスタンスへの SSH 接続だけを許します）"
        env_mark_setup ssh_policy_attached "EC2 への接続を許すポリシーが付いていることを確かめた"
        echo ""
        next_cmd "$AWS_SURVEY_CMD credentials" "その権限を含めて一時キーを発行し直します" ;;
      *)
        show_diag_policy_for_admin
        ui_text "付いたら、もう一度 $AWS_SURVEY_CMD を実行してください（付いたことを記録して、一時キーの発行し直しへ進みます）。" ;;
    esac
    exit 0
  fi
  exit 1
fi

# Identity Center のログインに MFA の条件を付けると、誰も借りられないロールになる。書き込む前に止める
if [ "$MFA_REQUIRED" = "true" ] && is_sso_arn "$PRINCIPAL_ARN"; then
  ui_err "environment.json で MFA 必須（mfa_required: true）になっていますが、Identity Center のログインではロールの側で MFA を確かめられません。"
  ui_text "このまま信頼ポリシーに条件を付けると、借りられなくなります（MFA はログインのときに求められています）。"
  ui_text "environment.json の auth.mfa_required を false に書き換えてから、もう一度実行してください（${ENV_FILE}）。"
  exit 1
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

# EC2 の中を調べる機能（任意）。登録済みホストがあるときだけ、そこへの SSH 接続を許すポリシーを付ける
if [ -n "$SSH_HOSTS" ]; then
  ensure_diag_policy
else
  ui_skip "EC2 の中を調べる権限は付けていません（登録済みホストが無いため。$AWS_SURVEY_CMD ssh setup の後に、もう一度 role --create）"
fi

if [ "$ROUTE_UNSET" -eq 1 ]; then
  _tmp=$(mktemp) && jq '.auth.route = "own_role"' "$ENV_FILE" > "$_tmp" && mv "$_tmp" "$ENV_FILE" \
    && ui_text "environment.json に記録しました（ロールの用意のしかた: 自分で作る）"
fi
env_mark_setup route_decided "ロールの用意のしかたを決めた"
env_mark_setup role_created "ロールを用意した"

echo ""
if [ "${#shortfalls[@]}" -gt 0 ]; then
  ui_ok "直す前にあった ⚠ の点（${#shortfalls[@]} 件）は直しました"
fi
ui_ok "ロールの準備ができました"
echo ""
next_cmd "$AWS_SURVEY_CMD credentials" "読み取り専用の一時キーを発行します"
also_cmd "$AWS_SURVEY_CMD verify" "続けて、その一時キーが読み取り専用であることを確かめます（省略できません）"
