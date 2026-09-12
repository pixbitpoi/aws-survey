#!/usr/bin/env bash
# Claude Code のインフラ調査用に、読み取り専用の一時キーを発行する（ホストで実行）
#
#   前提: environment.json が記入済みで、そこに書いたロールが作られていること（aws-survey role）
#         元プロファイル（environment.json の auth.source_profile）が有効であること
#   使い方: aws-survey credentials   （実体は libexec/commands/credentials.sh）
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"

GUARD="$LIBEXEC_DIR/session-guard.json"
OUT="$AWS_DIR"

die() { ui_die "$@"; }

# ---- 前提チェック ----
command -v aws >/dev/null || die "aws コマンドが見つかりません。"
[ -f "$GUARD" ] || die "session-guard.json が見つかりません: $GUARD"
jq -e . "$GUARD" >/dev/null 2>&1 || die "session-guard.json が壊れています。"

# 元プロファイルの確認。使えなければ、設定された更新コマンドで一度だけ直しにいく。
#
# ⚠️ 元プロファイルの有効化のしかたは環境ごとに違う（SSO のブラウザ認証、MFA コードの入力、
#    会社ごとのスクリプト…）。ここに書き分けず、environment.json の auth.refresh_command で持つ。
#    未設定なら、何を実行すべきかを伝えて終わる。
check_source() {
  who=$(aws sts get-caller-identity --profile "$PROFILE_SRC" --query Arn --output text 2>&1)
}

ui_title "aws-survey credentials"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "リージョン" "$REGION"
ui_kv "ロール" "$ROLE_NAME"
echo ""

ui_head "1/3 ホストのプロファイル $PROFILE_SRC でログインできているか"
if ! check_source; then
  ui_warn "使えません"
  ui_raw "$who"
  if [ -n "${REFRESH_CMD:-}" ] && [ "$REFRESH_CMD" != "null" ]; then
    echo ""
    ui_text "environment.json に書いてあるログインのコマンドを実行します。MFA コードの入力やブラウザでの認証を求められることがあります。"
    ui_pause
    ui_tty "    $(ui_cmd "$REFRESH_CMD")
"
    # 利用者自身が environment.json に書いたコマンド。対話的でよい（簡潔表示では端末に直接つなぐ）
    if ui_compact; then eval "$REFRESH_CMD" > /dev/tty 2>&1; else eval "$REFRESH_CMD"; fi
    ui_resume
    echo ""
    ui_text "もう一度確かめます。"
    check_source || { ui_raw "$who"; die "ログインのコマンドを実行しても $PROFILE_SRC が使えるようになりませんでした。
  コマンド: $REFRESH_CMD
  environment.json の auth.refresh_command を見直してください。"; }
  else
    case "$who" in
      *ExpiredToken*|*expired*) reason="$PROFILE_SRC のログインが期限切れです。" ;;
      *"could not be found"*|*"The config profile"*) reason="プロファイル $PROFILE_SRC がありません。" ;;
      *) reason="$PROFILE_SRC が使えません（上のエラーを確認してください）。" ;;
    esac
    die "$reason
  $PROFILE_SRC でログインし直してから、もう一度実行してください。
  毎回この手間をかけたくない場合は、そのログインのコマンドを environment.json の auth.refresh_command に
  書いておくと、次からここで自動的に実行します（例: \"aws-login --profile ${PROFILE_SRC%-mfa}\"）。"
  fi
fi
ui_ok "$who"
# 借りたロール（Identity Center を含む）からのログインは、AWS の決まりで 1 時間まで。ロール側を延ばしても変わらないので、
# environment.json の auth.duration_seconds をその場で直して続けられるようにする（聞いて「はい」のときだけ。読めなければ案内だけで止まる）
fix_duration_or_die() {
  local ans tmp
  ui_pause
  ui_tty "$(printf '  %s❯%s environment.json の auth.duration_seconds を %s → %s に直して、そのまま発行しますか？ %s(Y/n)%s: ' \
    "$C_CYAN" "$C_RESET" "$DURATION" "$CHAIN_MAX_SECONDS" "$C_DIM" "$C_RESET")"
  if read_line ans; then
    ui_resume
    [ -t 0 ] || echo ""
    case "$ans" in
      ""|y|Y|yes|YES)
        tmp=$(mktemp) && jq --argjson d "$CHAIN_MAX_SECONDS" '.auth.duration_seconds = $d' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE" \
          || die "environment.json を書き換えられませんでした: $ENV_FILE"
        DURATION=$CHAIN_MAX_SECONDS
        ui_ok "environment.json の auth.duration_seconds を ${CHAIN_MAX_SECONDS} にしました"
        return 0 ;;
    esac
  fi
  ui_resume
  echo ""
  ui_text "environment.json の auth.duration_seconds を ${CHAIN_MAX_SECONDS} にしてから、もう一度実行してください: $ENV_FILE"
  die "一時キーを発行できませんでした。$OUT は変更していません。"
}
if is_chained_arn "$who" && [ "$DURATION" -gt "$CHAIN_MAX_SECONDS" ]; then
  ui_chain_limit "$DURATION"
  fix_duration_or_die
fi
echo ""

# ---- ロールの存在確認 ----
ui_head "2/3 ロール $ROLE_NAME があるか"
if ! aws iam get-role --profile "$PROFILE_SRC" --role-name "$ROLE_NAME" \
       --query 'Role.Arn' --output text >/dev/null 2>&1; then
  case "$AUTH_ROUTE" in
    own_role) die "ロール $ROLE_NAME がありません。$AWS_SURVEY_CMD role --create を先に実行してください。" ;;
    *)        die "ロール $ROLE_NAME を確認できません。この出力を Claude Code / Codex に伝えてください。" ;;
  esac
fi
ui_ok "あります"
echo ""

# ---- 一時キーの発行 ----
# --policy-arns は必須。session-guard.json は Deny しか書いていないため、
# これを外すとセッションポリシー側に Allow が無くなり全 API が拒否される。
# EC2 の中を調べる機能（ssh.hosts）があるときは、登録済みインスタンスへの SSH 接続だけを許す
# diag-ssh-<name>（role --create が作る）を ReadOnlyAccess と並べる。無いときは渡さない（ポリシーが無くても発行できる）。
ui_head "3/3 読み取り専用の一時キーを発行する（$((DURATION / 60)) 分）"
extra_policy_arns=()
if [ -n "$SSH_HOSTS" ]; then
  extra_policy_arns+=("arn=$DIAG_POLICY_ARN")
  ui_text "登録済みホスト（${SSH_HOSTS}）への SSH 接続を許す $DIAG_POLICY_NAME も付けます。"
fi
if ! json=$(
  aws sts assume-role \
    --profile "$PROFILE_SRC" \
    --role-arn "$ROLE_ARN" \
    --role-session-name "${SESSION_NAME_PREFIX}-$(date +%Y%m%d-%H%M%S)" \
    --duration-seconds "$DURATION" \
    --policy-arns arn=arn:aws:iam::aws:policy/ReadOnlyAccess ${extra_policy_arns[@]+"${extra_policy_arns[@]}"} \
    --policy "$(jq -c . "$GUARD")" \
    --output json 2>&1
); then
  ui_err "発行できませんでした"
  ui_raw "$json"
  echo ""
  ui_head "対処の目安"
  case "$json" in
    *PackedPolicyTooLarge*)
      ui_text "セッションポリシーが上限（圧縮後のサイズ）を超えています。"
      ui_text "libexec/session-guard.json の Action を減らしてください。" ;;
    *ExpiredToken*)
      ui_text "$PROFILE_SRC のログインが期限切れです。ログインし直してから、もう一度実行してください。" ;;
    *"$DIAG_POLICY_NAME"*)
      ui_text "EC2 の中を調べる権限（${DIAG_POLICY_NAME}）がまだ無いか、付いていません。"
      ui_text "$AWS_SURVEY_CMD role --create で作って付けてから、もう一度実行してください。" ;;
    *"not authorized to perform: sts:AssumeRole"*|*AccessDenied*)
      if is_sso_arn "$PRINCIPAL_ARN"; then
        ui_text "Identity Center のログインでは、信頼ポリシーに「MFA 済みの人だけ」の条件があると借りられません。"
        ui_text "$AWS_SURVEY_CMD role で確かめられます（⚠ が出たら、その案内に沿って直します）。"
      fi
      ui_text "ロールを作った直後なら、IAM の反映待ちかもしれません。10 秒ほど待ってもう一度実行してください。"
      ui_text "それでも駄目なら信頼ポリシーを確認してください:"
      ui_text "  aws iam get-role --profile $PROFILE_SRC --role-name $ROLE_NAME --query 'Role.AssumeRolePolicyDocument'"
      ui_text "$AWS_SURVEY_CMD doctor でも切り分けられます。" ;;
    *"role chaining"*)
      ui_chain_limit "$DURATION"
      fix_duration_or_die
      echo ""
      ui_text "直した設定で発行し直します。"
      echo ""
      exec bash "$0" "$@" ;;
    *DurationSeconds*|*duration*)
      ui_text "ロールのセッション上限より長い時間を要求しています。"
      ui_text "environment.json の auth.duration_seconds を短くするか、ロール側を延ばしてください:"
      ui_text "  aws iam update-role --profile $PROFILE_SRC --role-name $ROLE_NAME --max-session-duration $DURATION" ;;
    *)
      ui_text "上のエラーメッセージを確認してください。" ;;
  esac
  die "一時キーを発行できませんでした。$OUT は変更していません。"
fi

packed=$(echo "$json" | jq -r '.PackedPolicySize // "n/a"')
ui_ok "一時キーを発行しました（$((DURATION / 60)) 分）"
ui_kv "セッションポリシー" "上限の ${packed}%（100% を超えると発行できません）"

# ---- ここまで来て初めて書き込む ----
mkdir -p "$OUT"
chmod 700 "$OUT"

cat > "$OUT/config" <<CFG
[profile claude-ro]
region = $REGION
output = json
cli_pager =
max_attempts = 3
CFG

umask 077
echo "$json" | jq -r '.Credentials |
  "[claude-ro]\naws_access_key_id = \(.AccessKeyId)\naws_secret_access_key = \(.SecretAccessKey)\naws_session_token = \(.SessionToken)"' \
  > "$OUT/credentials"
chmod 600 "$OUT/credentials"

# 期限切れのとき、コンテナ内の survey-status がユーザーに伝える文言。
# 何を実行すればよいかを具体的に出せるようにしておく。
RENEW_HINT="cd $AWS_SURVEY_DIR && $AWS_SURVEY_CMD credentials"

# コンテナ内の survey-status が「残り時間」と「いま何をすべきか」を出すために使う。
# duration_seconds を渡すのが要点で、これが無いと残り時間を判断の閾値に換算できない。
# ssh_hosts は発行時の登録済みホスト。引数なしの aws-survey が、いまの登録と比べて発行し直しを案内する。
echo "$json" | jq \
  --argjson dur "$DURATION" \
  --arg region "$REGION" \
  --arg account "$ACCOUNT_ID" \
  --arg role "$ROLE_NAME" \
  --arg renew "$RENEW_HINT" \
  --arg ssh_hosts "$SSH_HOSTS" \
  '{expiration: .Credentials.Expiration, duration_seconds: $dur,
    region: $region, account_id: $account, role_name: $role,
    renew_hint: $renew, ssh_hosts: $ssh_hosts}' \
  > "$OUT/session.json"
chmod 644 "$OUT/session.json"
rm -f "$OUT/expires"

ui_kv "プロファイル" "claude-ro（読み取り専用）"
ui_kv "置き場" "$OUT"
ui_kv "期限" "$(echo "$json" | jq -r .Credentials.Expiration)"
echo ""
# 読み取り専用の検証が済んでいなければ、run ではなく verify を案内する
if [ -z "$(jq -r '.setup.readonly_verified // empty' "$ENV_FILE")" ]; then
  next_cmd "$AWS_SURVEY_CMD verify" "この一時キーが読み取り専用であることを確かめます（省略できません）"
else
  survey_next_cmd
fi
