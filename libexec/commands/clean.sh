#!/usr/bin/env bash
# 調査を終えたあとの後片付け（ホストで実行）
#   aws-survey clean          残っているものを並べ、1 つずつ聞いてから消す（Enter は「消す」）
#   aws-survey clean --yes    聞かずに全部消す（端末でないときはこれが要る）
#   aws-survey clean --list   残っているものを並べるだけ（何も消さない。AWS も叩かない）
# 実体は libexec/commands/clean.sh。通常は aws-survey 経由で呼ぶ。
#
# 消すのは、この一式が対象アカウントとこのホストに置いたものだけ。out/（報告・生データ）と environment.json は消さない
# （AGENTS.md「out/ を削除・整理しない」。もう一度 aws-survey を打てば、同じ記録を土台に準備からやり直せる）。
# 順は、依存の逆: EC2 の診断ゲートウェイ（ssh remove。最後のホストで diag-ssh-<name> ポリシーも消える）→ ホストが無いのに残った
# ポリシー（ssh clean-policy）→ 調査用ロール（自分で作ったものだけ。借りたロールは触らない）→ ホスト側（一時キーと鍵・Docker の
# イメージとボリューム・取り出した Lambda のコード）。AWS 側は元プロファイル（強い権限）の仕事で、権限が無ければその項目を飛ばして
# 手で打つコマンドを出し、止まらずに次へ進む（利用者に権限が無いことは普通にある）。
# 判定はファイルだけで行い（AWS は叩かない）、消すときだけ AWS を叩く。同じ out/ に向いた調査コンテナが動いていれば止まる。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/docker.sh"   # IMAGE
. "$LIBEXEC_DIR/launch.sh"   # ボリューム名・launch_container_running

usage() { sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; }

YES=0; LIST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1; shift ;;
    --list)   LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) ui_die "clean の不明なオプション: $1（--yes / --list）" ;;
  esac
done

on_terminal() { [ -t 0 ] && [ -t 1 ]; }
TRUST_OUT="$AWS_SURVEY_DIR/trust.json"

# ---- 残っているものの判定（ファイルだけ）----
HOSTS="$SSH_HOSTS"
POLICY_LEFT=0; [ -n "$(jq -r '.setup.ssh_policy_attached // empty' "$ENV_FILE")" ] && [ -z "$HOSTS" ] && POLICY_LEFT=1
ROLE_OWN=0; { [ "$AUTH_ROUTE" = own_role ] || [ -z "$AUTH_ROUTE" ]; } && [ -n "$(jq -r '.setup.role_created // empty' "$ENV_FILE")" ] && ROLE_OWN=1
KEYS_LEFT=0; [ -d "$AWS_DIR" ] && KEYS_LEFT=1
code_count() {
  local m n=0
  for m in "$AWS_SURVEY_DIR"/code/lambda/*/*/_manifest.json; do [ -f "$m" ] && n=$((n + 1)); done
  echo "$n"
}
CODE_N=$(code_count)
DOCKER_LEFT=0
if command -v docker >/dev/null 2>&1; then
  for v in "$VOLUME" "$CODEX_VOLUME" "$CLI_VOLUME" "$NPM_VOLUME"; do
    docker volume inspect "$v" >/dev/null 2>&1 && DOCKER_LEFT=1
  done
  docker image inspect "$IMAGE" >/dev/null 2>&1 && DOCKER_LEFT=1
fi

# 残っているものを、AWS 側とホスト側に分けて出す。status と引数なしの aws-survey も同じ形で出したいので、ここで完結させる
show_left() {
  local any=0
  ui_head "対象アカウントに残っているもの"
  if [ -n "$HOSTS" ]; then any=1; ui_warn "EC2 の診断ゲートウェイ: ${HOSTS}（ユーザー・sshd と sudoers の設定・タグ）"; fi
  [ "$POLICY_LEFT" -eq 0 ] || { any=1; ui_warn "接続を許すポリシー diag-ssh-${SURVEY_NAME}（登録済みホストは無い）"; }
  if [ "$ROLE_OWN" -eq 1 ]; then any=1; ui_warn "調査用ロール ${ROLE_NAME}（自分で作ったもの。信頼ポリシーと ReadOnlyAccess つき）"; fi
  [ "$any" -eq 1 ] || ui_ok "なし"
  if [ "$ROLE_OWN" -eq 0 ] && [ -n "$(jq -r '.setup.role_created // empty' "$ENV_FILE")" ]; then
    ui_text "調査用ロール ${ROLE_NAME} は借りたものなので、ここでは触りません。要らなくなったことを管理者に伝えてください。"
  fi
  echo ""
  ui_head "このホストに残っているもの"
  any=0
  [ "$KEYS_LEFT" -eq 0 ] || { any=1; ui_warn "一時キーと SSH の鍵: $(ui_path "$AWS_DIR")"; }
  [ "$DOCKER_LEFT" -eq 0 ] || { any=1; ui_warn "Docker のイメージ ${IMAGE} とボリューム（Claude Code / Codex のログインを含む）"; }
  [ "$CODE_N" -eq 0 ] || { any=1; ui_warn "取り出した Lambda のコード: ${CODE_N} 関数（$(ui_path "$AWS_SURVEY_DIR/code")）"; }
  [ "$any" -eq 1 ] || ui_ok "なし"
  ui_text "報告と生データ（$(ui_path "$AWS_SURVEY_DIR/out")）と environment.json は消しません。"
  echo ""
}

ui_title "aws-survey clean"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "ロール" "$ROLE_NAME"
echo ""
show_left
if [ "$LIST" -eq 1 ]; then exit 0; fi
if [ -z "$HOSTS" ] && [ "$POLICY_LEFT" -eq 0 ] && [ "$ROLE_OWN" -eq 0 ] && [ "$KEYS_LEFT" -eq 0 ] && [ "$DOCKER_LEFT" -eq 0 ] && [ "$CODE_N" -eq 0 ]; then
  ui_ok "片付けるものはありません"
  exit 0
fi
if [ "$YES" -eq 0 ] && ! on_terminal; then
  ui_text "端末ではないので聞けません。全部消すなら $(ui_cmd "$AWS_SURVEY_CMD clean --yes") を実行してください。"
  exit 1
fi

# 1 項目ずつ聞く。Enter は「消す」。--yes なら聞かない
ask() {
  local ans
  [ "$YES" -eq 0 ] || return 0
  printf '  %s❯%s %s %s(Y/n)%s: ' "$C_CYAN" "$C_RESET" "$1" "$C_DIM" "$C_RESET"
  read_line ans || { echo ""; return 1; }
  case "$ans" in ""|y|Y|yes|YES) return 0 ;; *) ui_skip "残します"; return 1 ;; esac
}

# 同じ out/ に向いた調査コンテナが動いていれば、ボリュームもロールも消せない
if command -v docker >/dev/null 2>&1; then
  for n in "$SURVEY_NAME" "${SURVEY_NAME}-scan"; do
    ! launch_container_running "$n" || ui_die "調査コンテナ（${n}）が動いています。中の会話を終えてから実行してください。"
  done
fi

LEFT=()      # 消せなかったもの（最後にまとめて出し、1 で終わる）
KEPT=()      # 利用者が残すと答えたもの（最後にまとめて出すだけ。失敗ではない）
SRC_OK=""    # 元プロファイルが使えるか（AWS 側の項目で初めて確かめる）
source_ok() {
  if [ -n "$SRC_OK" ]; then [ "$SRC_OK" = 1 ]; return; fi
  if aws sts get-caller-identity --profile "$PROFILE_SRC" >/dev/null 2>&1; then SRC_OK=1; return 0; fi
  if [ -n "${REFRESH_CMD:-}" ] && [ "$REFRESH_CMD" != null ] && on_terminal; then
    ui_text "元プロファイル $PROFILE_SRC のログインが切れているので、environment.json のログインのコマンドを実行します。"
    ui_text "  $(ui_cmd "$REFRESH_CMD")"
    eval "$REFRESH_CMD"
    if aws sts get-caller-identity --profile "$PROFILE_SRC" >/dev/null 2>&1; then SRC_OK=1; return 0; fi
  fi
  SRC_OK=0
  ui_warn "元プロファイル $PROFILE_SRC が使えないので、対象アカウント側は片付けられません（ホスト側だけ進めます）"
  return 1
}
iam_src() { aws iam "$@" --profile "$PROFILE_SRC" --output text 2>&1; }

# ---- 1. EC2 の診断ゲートウェイ ----
if [ -n "$HOSTS" ]; then
  ui_head "1. EC2 の診断ゲートウェイ（${HOSTS}）"
  ui_text "各ホストから撤去し、タグと記録を消します。最後のホストで、接続を許すポリシーも消します。"
  if ! ask "EC2 から撤去しますか？"; then KEPT+=("EC2 の診断ゲートウェイ ${HOSTS}: $AWS_SURVEY_CMD ssh remove <host>")
  elif ! source_ok; then LEFT+=("EC2 の診断ゲートウェイ ${HOSTS}: $AWS_SURVEY_CMD ssh remove <host>")
  else
    for h in $HOSTS; do
      if AWS_SURVEY_CHAIN=1 bash "$LIBEXEC_DIR/commands/ssh.sh" remove "$h"; then
        ui_ok "撤去しました: $h"
      else
        ui_err "撤去できませんでした: $h（上の出力を確認してください）"
        LEFT+=("EC2 の診断ゲートウェイ ${h}: $AWS_SURVEY_CMD ssh remove $h")
      fi
    done
    HOSTS=$(jq -r '.ssh.hosts // {} | keys | join(" ")' "$ENV_FILE")
    POLICY_LEFT=0; [ -n "$(jq -r '.setup.ssh_policy_attached // empty' "$ENV_FILE")" ] && [ -z "$HOSTS" ] && POLICY_LEFT=1
  fi
  echo ""
fi

# ---- 2. ホストが無いのに残ったポリシー ----
if [ "$POLICY_LEFT" -eq 1 ]; then
  ui_head "2. 接続を許すポリシー diag-ssh-${SURVEY_NAME}"
  if [ "$ROLE_OWN" -eq 1 ]; then
    if ! ask "ロールから外して消しますか？"; then KEPT+=("ポリシー diag-ssh-${SURVEY_NAME}: $AWS_SURVEY_CMD ssh clean-policy")
    elif ! source_ok; then LEFT+=("ポリシー diag-ssh-${SURVEY_NAME}: $AWS_SURVEY_CMD ssh clean-policy")
    elif AWS_SURVEY_CHAIN=1 bash "$LIBEXEC_DIR/commands/ssh.sh" clean-policy; then POLICY_LEFT=0
    else LEFT+=("ポリシー diag-ssh-${SURVEY_NAME}: $AWS_SURVEY_CMD ssh clean-policy"); fi
  else
    ui_text "調査用ロールは借りたものなので触りません。管理者に、ポリシー diag-ssh-${SURVEY_NAME} が要らなくなったことを伝えてください。"
  fi
  echo ""
fi

# ---- 3. 調査用ロール ----
role_by_hand() {
  ui_text "手で消すコマンド（付いているポリシーを外してから消します）:"
  ui_text "  aws iam list-attached-role-policies --role-name $ROLE_NAME"
  ui_text "  aws iam detach-role-policy --role-name $ROLE_NAME --policy-arn <付いているポリシーの ARN>"
  ui_text "  aws iam delete-role --role-name $ROLE_NAME"
}
if [ "$ROLE_OWN" -eq 1 ]; then
  ui_head "3. 調査用ロール ${ROLE_NAME}"
  ui_text "自分で作ったロールです。付いているポリシー（ReadOnlyAccess など）を外してから消します。一時キーはこのロールから借りたものなので使えなくなります。"
  if ! ask "ロールを消しますか？"; then KEPT+=("調査用ロール ${ROLE_NAME}: $AWS_SURVEY_CMD clean（もう一度）")
  elif ! source_ok; then LEFT+=("調査用ロール ${ROLE_NAME}: 元プロファイルでログインしてから $AWS_SURVEY_CMD clean")
  else
    role_rc=0
    if out=$(iam_src get-role --role-name "$ROLE_NAME" --query Role.Arn); then
      for arn in $(iam_src list-attached-role-policies --role-name "$ROLE_NAME" --query 'AttachedPolicies[].PolicyArn'); do
        [ "$arn" != None ] || continue
        if out=$(iam_src detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$arn"); then ui_ok "外しました: ${arn##*/}"
        else ui_raw "$out"; ui_err "外せませんでした: ${arn##*/}"; role_rc=1; fi
      done
      for pname in $(iam_src list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames[]'); do
        [ "$pname" != None ] || continue
        if out=$(iam_src delete-role-policy --role-name "$ROLE_NAME" --policy-name "$pname"); then ui_ok "インラインポリシーを消しました: $pname"
        else ui_raw "$out"; ui_err "インラインポリシーを消せませんでした: $pname"; role_rc=1; fi
      done
      if [ "$role_rc" -eq 0 ]; then
        if out=$(iam_src delete-role --role-name "$ROLE_NAME"); then
          ui_ok "ロール ${ROLE_NAME} を消しました"
        else
          ui_raw "$out"; ui_err "ロール ${ROLE_NAME} を消せませんでした（権限が無いか、まだ何かが付いています）"; role_rc=1
        fi
      fi
    else
      case "$out" in
        *NoSuchEntity*) ui_skip "ロール ${ROLE_NAME} はもうありません" ;;
        *) ui_raw "$out"; ui_err "ロール ${ROLE_NAME} を読めません（権限が無い可能性があります）"; role_rc=1 ;;
      esac
    fi
    if [ "$role_rc" -eq 0 ]; then
      # 次に aws-survey を打てば、ロールの用意からやり直す（out/ の記録はそのまま）
      tmp=$(mktemp) && jq '.setup.role_created = null | .setup.readonly_verified = null | .setup.ssh_policy_attached = null' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
      rm -f "$TRUST_OUT"
      ui_text "environment.json の記録を戻しました（次に $AWS_SURVEY_CMD を打てば、ロールの用意からやり直せます）。"
    else
      role_by_hand
      LEFT+=("調査用ロール ${ROLE_NAME}: 上の aws iam のコマンドを、消せる権限のある人が実行")
    fi
  fi
  echo ""
fi

# ---- 4. ホスト側 ----
ui_head "4. このホストに残っているもの"
if [ "$KEYS_LEFT" -eq 1 ]; then
  if ask "一時キーと SSH の鍵（$(ui_path "$AWS_DIR")）を消しますか？"; then
    rm -rf "$AWS_DIR" && ui_ok "消しました: $(ui_path "$AWS_DIR")" || LEFT+=("一時キー: rm -rf $AWS_DIR")
  else KEPT+=("一時キーと鍵: rm -rf $AWS_DIR"); fi
fi
if [ "$DOCKER_LEFT" -eq 1 ]; then
  if ask "Docker のイメージ ${IMAGE} とボリューム（Claude Code / Codex のログインを含む）を消しますか？"; then
    for v in "$VOLUME" "$CODEX_VOLUME" "$CLI_VOLUME" "$NPM_VOLUME"; do
      docker volume inspect "$v" >/dev/null 2>&1 || continue
      docker volume rm "$v" >/dev/null 2>&1 && ui_ok "ボリュームを消しました: $v" || { ui_err "ボリュームを消せませんでした: $v"; LEFT+=("Docker ボリューム: docker volume rm $v"); }
    done
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
      docker rmi "$IMAGE" >/dev/null 2>&1 && ui_ok "イメージを消しました: $IMAGE" || { ui_err "イメージを消せませんでした: $IMAGE"; LEFT+=("Docker イメージ: docker rmi $IMAGE"); }
    fi
    ui_text "次に調査コンテナを起動するときは、イメージの作り直しと Claude Code / Codex のログインからになります。"
  else KEPT+=("Docker のイメージとボリューム: $AWS_SURVEY_CMD clean（もう一度）"); fi
fi
if [ "$CODE_N" -gt 0 ]; then
  if ask "取り出した Lambda のコード（${CODE_N} 関数）を消しますか？"; then
    AWS_SURVEY_CHAIN=1 bash "$LIBEXEC_DIR/commands/lambda.sh" remove --all >/dev/null 2>&1 && ui_ok "取り出したコードを消しました" \
      || { ui_err "取り出したコードを消せませんでした"; LEFT+=("Lambda のコード: $AWS_SURVEY_CMD lambda remove --all"); }
  else KEPT+=("Lambda のコード: $AWS_SURVEY_CMD lambda remove --all"); fi
fi
echo ""

# ---- まとめ ----
if [ ${#KEPT[@]} -gt 0 ]; then
  ui_text "残すと答えたもの（消すときのコマンド）:"
  for l in "${KEPT[@]}"; do ui_text "  ・$l"; done
fi
if [ ${#LEFT[@]} -eq 0 ]; then
  ui_ok "後片付けを終えました。報告と生データ（$(ui_path "$AWS_SURVEY_DIR/out")）はそのままです"
  ui_text "もう一度調べるときは、同じフォルダで $(ui_cmd "$AWS_SURVEY_CMD") を打てば準備からやり直せます。"
  exit 0
fi
ui_warn "消せなかったものがあります"
for l in "${LEFT[@]}"; do ui_text "  ・$l"; done
exit 1
