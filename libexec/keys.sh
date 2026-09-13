#!/usr/bin/env bash
# 一時キーの状態の判定と、足りなければ発行し直す入口。aws-survey（段階の判定・status）と、一時キーを使う各コマンド
# （libexec/container.sh 経由の ls / ec2 / lambda / scan / claude / codex / run と、verify / ssh verify）が source する。
# key_state はファイルだけを見て AWS は叩かない。key_ensure だけが credentials.sh を走らせる。
# AWS_DIR と DURATION と SSH_HOSTS を使うので、load-env.sh のあとで呼ぶ（読み込むのは先でもよい）。

# ISO 8601（AWS の Expiration: 2026-01-02T03:04:05+00:00 / ...Z）を epoch 秒に。読めなければ空。
iso_to_epoch() {
  local s="$1" n
  n=$(printf '%s' "$s" | sed -E 's/\.[0-9]+//; s/\+00:00$/Z/; s/UTC$/Z/')
  case "$n" in
    *Z) jq -rn --arg t "$n" '$t | fromdateiso8601' 2>/dev/null && return 0 ;;
  esac
  date -d "$s" +%s 2>/dev/null && return 0
  date -j -f '%Y-%m-%dT%H:%M:%S%z' "${s/%Z/+0000}" +%s 2>/dev/null
}
mtime_of() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

# 一時キーの状態。KEY_STATE = missing | expired | valid | unknown、KEY_EXP / KEY_LEFT_MIN / KEY_TOTAL_MIN / KEY_NOTE
key_state() {
  local cred="$AWS_DIR/credentials" session="$AWS_DIR/session.json" exp="" dur="" mt exp_epoch now
  KEY_STATE=missing; KEY_EXP=""; KEY_LEFT_MIN=""; KEY_TOTAL_MIN=""; KEY_NOTE=""
  [ -f "$cred" ] || return 0
  if [ -f "$session" ]; then
    exp=$(jq -r '.expiration // empty' "$session" 2>/dev/null)
    dur=$(jq -r '.duration_seconds // empty' "$session" 2>/dev/null)
  fi
  [ -n "$dur" ] || dur="$DURATION"
  if [ -z "$exp" ]; then
    # 発行元の情報が無いときは、credentials の更新時刻 + duration_seconds で推定する
    if mt=$(mtime_of "$cred") && [ -n "$mt" ]; then
      exp_epoch=$(( mt + dur )); KEY_NOTE="（credentials の更新時刻から推定）"
      exp=$(date -u -r "$exp_epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$exp_epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
    fi
  else
    exp_epoch=$(iso_to_epoch "$exp")
  fi
  KEY_EXP="$exp"
  if [ -z "${exp_epoch:-}" ]; then KEY_STATE=unknown; return 0; fi
  now=$(date +%s)
  KEY_LEFT_MIN=$(( (exp_epoch - now) / 60 ))
  KEY_TOTAL_MIN=$(( dur / 60 ))
  if [ "$exp_epoch" -le "$now" ]; then KEY_STATE=expired; else KEY_STATE=valid; fi
}

# ---- 元プロファイルの種類 ----
# aws-login の MFA 運用: 長期キーの <名> に mfa_serial を持ち、MFA を通した一時キーを <名>-mfa に保存する。
# 信頼ポリシーに MFA の条件があるロールは、長期キーのままでは借りられないので、借りる元は <名>-mfa。
# ここでは ~/.aws の設定だけ読み、AWS は叩かない。
profile_conf() { aws configure get "$1" --profile "$2" 2>/dev/null; }
# 長期キー（aws_access_key_id があり aws_session_token が無く、SSO / role_arn / credential_process でもない）なら 0
profile_is_long_term_key() {
  local p="$1"
  [ -n "$(profile_conf aws_access_key_id "$p")" ] || return 1
  [ -z "$(profile_conf aws_session_token "$p")" ] || return 1
  [ -z "$(profile_conf sso_session "$p")$(profile_conf sso_start_url "$p")$(profile_conf role_arn "$p")$(profile_conf credential_process "$p")" ]
}
# MFA を求めるときにロールを借りる元。長期キーの <名> なら aws-login の保存先 <名>-mfa、それ以外はそのまま
mfa_source_profile() {
  local p="$1"
  case "$p" in *-mfa) echo "$p"; return 0 ;; esac
  if profile_is_long_term_key "$p"; then echo "${p}-mfa"; else echo "$p"; fi
}

# ---- 元プロファイル（強い権限）を使える状態にする ----
# 元プロファイルを使うコマンド（credentials / role / ssh setup・rotate・remove / lambda pull）は入口でここを通る。
# 使えなければ（無い・期限切れ）environment.json の refresh_command（既定は aws-login。MFA コードの入力やブラウザでの認証を
# 求めることがある）を 1 度だけ走らせて確かめ直す。簡潔表示（ui_fold の中）では端末に直接つなぐ。
# 使えれば SOURCE_ARN に身元を入れて 0。駄目なら SOURCE_ERR に aws の出力を入れて 1（止め方と案内は呼ぶ側）
source_check() {
  SOURCE_ARN=$(aws sts get-caller-identity --profile "$PROFILE_SRC" --query Arn --output text 2>&1) && return 0
  SOURCE_ERR="$SOURCE_ARN"; SOURCE_ARN=""; return 1
}
source_ensure() {
  SOURCE_ARN=""; SOURCE_ERR=""; SOURCE_REFRESHED=0
  source_check && return 0
  [ -n "${REFRESH_CMD:-}" ] && [ "$REFRESH_CMD" != null ] || return 1
  ui_warn "$PROFILE_SRC が使えません"
  ui_raw "$SOURCE_ERR"
  echo ""
  ui_text "environment.json に書いてあるログインのコマンドを実行します。MFA コードの入力やブラウザでの認証を求められることがあります。"
  ui_pause
  ui_tty "    $(ui_cmd "$REFRESH_CMD")
"
  # 利用者自身が environment.json に書いたコマンド。対話的でよい
  if ui_compact; then eval "$REFRESH_CMD" > /dev/tty 2>&1; else eval "$REFRESH_CMD"; fi
  ui_resume
  SOURCE_REFRESHED=1
  echo ""
  ui_text "もう一度確かめます。"
  source_check
}
# source_ensure が 1 のときの案内文
source_hint() {
  if [ "${SOURCE_REFRESHED:-0}" = 1 ]; then
    printf '%s' "ログインのコマンドを実行しても $PROFILE_SRC が使えるようになりませんでした。
  コマンド: $REFRESH_CMD
  environment.json の auth.refresh_command を見直してください。"
    return 0
  fi
  local reason
  case "${SOURCE_ERR:-}" in
    *ExpiredToken*|*expired*) reason="$PROFILE_SRC のログインが期限切れです。" ;;
    *"could not be found"*|*"The config profile"*) reason="プロファイル $PROFILE_SRC がありません。" ;;
    *) reason="$PROFILE_SRC が使えません（上のエラーを確認してください）。" ;;
  esac
  printf '%s' "$reason
  $PROFILE_SRC でログインし直してから、もう一度実行してください。
  毎回この手間をかけたくない場合は、そのログインのコマンドを environment.json の auth.refresh_command に
  書いておくと、次からここで自動的に実行します（例: \"aws-login --profile ${PROFILE_SRC%-mfa}\"）。"
}

# ---- 足りなければ発行し直す ----
# 一時キーを使うコマンドは、利用者に credentials を打たせず、入口でここを通る。発行し直すのは次のどれか:
# 無い・期限切れ・期限を読めない・残りが要る分数に足りない・登録済みホスト（ssh.hosts）と発行時の記録（session.json の
# ssh_hosts）が食い違う（接続を許すポリシーがロールに付いている記録があるときだけ。無ければ発行しても借りられない）。
# 要る分数は呼ぶ側が渡す。一覧のような短い処理は KEY_MIN_QUICK、調査コンテナを起動する長い処理は key_session_min
# （総時間の半分、上限 30 分）。この絶対値はホスト側だけに置き、調査エージェント向けの文書には書かない（survey-status が計算する）。
KEY_MIN_QUICK=5
key_session_min() {
  local m; m=$(( ${KEY_TOTAL_MIN:-$(( DURATION / 60 ))} / 2 ))
  [ "$m" -le 30 ] || m=30
  echo "$m"
}
# 発行し直す理由を KEY_WHY に入れて 0 を返す。要らなければ空にして 1。key_state を呼び直すので、あとの KEY_* も新しい
#   key_needs_reissue <要る分数>
key_needs_reissue() {
  local need="${1:-0}" key_hosts
  key_state
  KEY_WHY=""
  case "$KEY_STATE" in
    missing) KEY_WHY="一時キーがありません" ;;
    expired) KEY_WHY="一時キーが期限切れです（${KEY_EXP}${KEY_NOTE}）" ;;
    unknown) KEY_WHY="一時キーの期限を読めません（${KEY_EXP:-記録なし}）" ;;
    valid)
      if [ "$KEY_LEFT_MIN" -lt "$need" ]; then
        KEY_WHY="一時キーの残りが約 ${KEY_LEFT_MIN} 分です（${need} 分は要ります）"
      elif [ -n "$(jq -r '.setup.ssh_policy_attached // empty' "$ENV_FILE" 2>/dev/null)" ] || [ -z "${SSH_HOSTS:-}" ]; then
        key_hosts=$(jq -r '.ssh_hosts // empty' "$AWS_DIR/session.json" 2>/dev/null)
        if [ "$key_hosts" != "${SSH_HOSTS:-}" ]; then
          if [ -n "${SSH_HOSTS:-}" ]; then KEY_WHY="いまの一時キーは、登録済みホスト（${SSH_HOSTS}）への接続の権限を含んでいません"
          else KEY_WHY="登録を消したホスト（${key_hosts}）の権限が一時キーに残っています"; fi
        fi
      fi ;;
  esac
  [ -n "$KEY_WHY" ]
}
# 要る分数に足りなければ発行し直す。端末なら回転する 1 行に畳み（ui_fold）、端末でなければそのまま走らせる。
# 既に畳まれている中（引数なしの aws-survey の 1 手）では、credentials の見出しがその行の文言になる。
# 発行し直したあと、もう一度判定して有効でなければ 1。何も要らなければ黙って 0
#   key_ensure <要る分数>
key_ensure() {
  local need="${1:-0}" rc=0
  key_needs_reissue "$need" || return 0
  ui_text "${KEY_WHY}。一時キーを発行し直します。"
  if [ "$UI_TTY" = 1 ] && ! ui_compact; then
    ui_fold "一時キーを発行し直します" "$AWS_SURVEY_CMD credentials" bash "$LIBEXEC_DIR/commands/credentials.sh" || rc=$?
  else
    AWS_SURVEY_CHAIN=1 bash "$LIBEXEC_DIR/commands/credentials.sh" || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    ui_err "一時キーを発行し直せませんでした"
    next_cmd "$AWS_SURVEY_CMD doctor" "発行できない原因を切り分けます"
    return 1
  fi
  key_state
  [ "$KEY_STATE" = valid ] && return 0
  ui_err "発行し直した一時キーを読めません（${KEY_STATE}）"
  return 1
}
