#!/usr/bin/env bash
# 調査コンテナでエージェントを非対話で 1 回走らせ、白紙の棚卸しだけを行わせる（ホストで実行）
#   aws-survey scan                    記録してあるエージェント（初回は矢印キーで選ぶ）で棚卸しを行う
#   aws-survey scan --agent codex      エージェントを指定する（記録も更新する）
# 実体は libexec/commands/scan.sh。通常は aws-survey 経由で呼ぶ。
#
# ユーザーは待つだけ。中のエージェントは環境の確認（初回）と白紙の棚卸しをして、見つけたものと聞きたいことを out/ に
# 残して終わる。目的の聞き取りと深掘りはしない（それは aws-survey claude / codex で対話して進める）。
# ホストから渡す指示は「棚卸しだけ・ユーザーに聞かずに終える」に限る。対象の概要・調査項目・サービス名は渡さない
# （AGENTS.md「ホストと調査コンテナ」。白紙で棚卸しさせる設計は、先に教えると発見ではなく確認になるため）。
# Claude / Codex の認証はコンテナ内のボリュームに残る。無ければ先に対話でログインだけ済ませ（claude auth login / codex login --device-auth）、
# そのあと非対話で続ける。Claude はさらにワークスペースの信頼を記録する（未信頼だと -p で settings.json の許可が無視される）。
set -euo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/container.sh"
. "$LIBEXEC_DIR/launch.sh"
. "$LIBEXEC_DIR/menu.sh"

AGENT_OPT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --agent)   [ $# -ge 2 ] || ui_die "--agent には claude か codex を指定してください。"; AGENT_OPT="$2"; shift 2 ;;
    --agent=*) AGENT_OPT="${1#--agent=}"; shift ;;
    -h|--help|help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) ui_die "scan の不明なオプション: $1（--agent claude|codex）" ;;
  esac
done
case "$AGENT_OPT" in ""|claude|codex) ;; *) ui_die "--agent は claude か codex です: $AGENT_OPT" ;; esac

# ホストから渡す指示。棚卸しだけ・聞かずに終える、以外を書かない（対象・調査項目・サービス名を含めない）
SCAN_PROMPT='この回はユーザーが応答できません。質問・目的の聞き取り・承認が要ることは行わないでください。
AGENTS.md と method/00 の順で、環境の確認（初回なら method/03）と、method/04 の工程 1「白紙で棚卸しする」だけを行って終えてください。
工程 2 以降（ユーザーに聞く・領域ごとの深掘り）には入らないでください。
ユーザーに聞きたいことは out/ に書き残し、次の回にユーザーと話せるようにしておいてください。'

on_terminal() { [ -t 0 ] && [ -t 1 ]; }

# 端末で待っているあいだの進み具合。回転する印の行の文言を 1 秒ごとに書き替える（ui.sh の簡潔表示の部品を借りる）。
# エージェントは何分も黙って働くので、経過時間と、out/ に書かれた記録の数・最新のものを出して、固まっていないことを見せる。
#   scan_progress <受け渡しの場所>   （& で起動し、終わったら kill する）
scan_progress() {
  local dir="$1" start now el n last msg
  set +e; set +o pipefail          # 背景の 1 ループ。find が無い・読めないときも黙って回り続ける
  start=$(date +%s)
  while :; do
    now=$(date +%s); el=$((now - start))
    n=$(find "$OUT_DIR" -type f -newer "$dir/keep" 2>/dev/null | wc -l | tr -d ' ')
    last=$(find "$OUT_DIR" -type f -newer "$dir/keep" -exec ls -t {} + 2>/dev/null | head -n 1)
    last=${last#"$OUT_DIR/"}
    msg="棚卸し中  経過 $(ui_duration "$el")  out/ に書いた記録 ${n:-0} 件${last:+  最新 ${last}}"
    printf '%s' "$msg" > "$dir/msg.tmp" && mv -f "$dir/msg.tmp" "$dir/msg"
    sleep 1
  done
}

ui_title "aws-survey scan"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "リージョン" "$REGION"
ui_kv "フェーズ" "$SURVEY_PHASE_DIR"
echo ""

# ---- 1. 一時キー ----
# あり・期限内・読み取り専用と確かめ済みか。棚卸しは時間がかかるので、残りが短ければ発行し直してから始める。
# 閾値（総時間の半分、上限 30 分）はホスト側だけの値。調査エージェント向けの文書には書かない（survey-status が計算する）
container_require_key || exit 1
SCAN_MIN_LEFT=$(( KEY_TOTAL_MIN / 2 )); [ "$SCAN_MIN_LEFT" -le 30 ] || SCAN_MIN_LEFT=30
if [ "$KEY_LEFT_MIN" -lt "$SCAN_MIN_LEFT" ]; then
  ui_warn "一時キーの残りが約 ${KEY_LEFT_MIN} 分です。棚卸しの途中で切れるおそれがあります"
  ans=""
  if on_terminal; then
    printf '  %s❯%s いま %s を実行して発行し直しますか？ %s(Y/n)%s: ' "$C_CYAN" "$C_RESET" "$(ui_cmd "$AWS_SURVEY_CMD credentials")" "$C_DIM" "$C_RESET"
    read_line ans || ans=n
    echo ""
  else
    ans=n
  fi
  case "$ans" in
    ""|y|Y|yes|YES)
      AWS_SURVEY_CHAIN=1 bash "$LIBEXEC_DIR/commands/credentials.sh" || ui_die "一時キーを発行し直せませんでした。"
      echo ""
      container_require_key || exit 1 ;;
    *)
      next_cmd "$AWS_SURVEY_CMD credentials" "一時キーを発行し直してから、もう一度 $AWS_SURVEY_CMD scan を実行します"
      exit 1 ;;
  esac
fi

# ---- 2. エージェント ----
AGENT="${AGENT_OPT:-$SURVEY_AGENT}"
if [ -z "$AGENT" ]; then
  if menu_available; then
    choose_menu pick "調査に使うエージェントを選んでください" 0 "Claude Code（claude）" "Codex（codex）"
    case "$pick" in Codex*) AGENT=codex ;; *) AGENT=claude ;; esac
  else
    ui_err "調査に使うエージェントがまだ決まっていません"
    next_cmd "$AWS_SURVEY_CMD scan --agent claude" "Claude Code で棚卸しを行います（--agent codex なら Codex）"
    exit 1
  fi
fi
env_set_agent "$AGENT"
ui_ok "エージェント: $(agent_label "$AGENT")"

# ---- 3. 同じ out/ に向いたコンテナが動いていないか ----
command -v docker >/dev/null || ui_die "docker コマンドが見つかりません。Docker Desktop を導入して起動してください。"
for n in "$SURVEY_NAME" "${SURVEY_NAME}-scan"; do
  if launch_container_running "$n"; then
    ui_die "調査コンテナ（${n}）が動いています。同じ out/ に 2 つのエージェントを同時には走らせられません。中の会話を終えてから実行してください。"
  fi
done

# ---- 4. イメージとボリューム ----
docker_check_shared "$AWS_SURVEY_HOME" "$AWS_SURVEY_DIR" "$AWS_DIR" || exit 1
docker_awsarch
ui_head "1/3 コンテナのイメージを用意する（$IMAGE, ${awsarch}）"
docker image inspect "$IMAGE" >/dev/null 2>&1 || ui_text "初回は数分かかります。2 回目からは差分だけです。"
build_image
launch_prepare_dirs
launch_prepare_volumes
ui_ok "用意できました"
echo ""

# ---- 5. 認証 ----
ui_head "2/3 $(agent_label "$AGENT") の認証を確かめる"
if launch_agent_authenticated "$AGENT"; then
  ui_ok "認証は済んでいます"
else
  on_terminal || ui_die "$(agent_label "$AGENT") のログインがまだです。端末で $AWS_SURVEY_CMD $AGENT を一度起動してログインしてください。"
  ui_warn "$(agent_label "$AGENT") のログインがまだです。先にログインだけ済ませます（この対象では初回だけ）"
  ui_text "画面の案内に従ってログインしてください。終わると棚卸しに進みます。"
  echo ""
  case "$AGENT" in
    codex)  launch_agent_cli -it --name "${SURVEY_NAME}-login" -- codex login --device-auth || true ;;
    claude) launch_agent_cli -it --name "${SURVEY_NAME}-login" -- claude auth login || true ;;
  esac
  echo ""
  launch_agent_authenticated "$AGENT" || ui_die "ログインを確かめられませんでした。$AWS_SURVEY_CMD $AGENT で起動して、ログインできているか見てください。"
  ui_ok "ログインしました"
fi
# 非対話の claude -p でも作業ディレクトリの許可とフックが効くように、ワークスペースの信頼を記録しておく（launch.sh）
[ "$AGENT" != claude ] || launch_trust_workspace || ui_die "ワークスペースの信頼を記録できませんでした。"
echo ""

# ---- 6. 実行 ----
ui_head "3/3 白紙の棚卸しを行わせる（$(agent_label "$AGENT")）"
ui_text "ユーザーへの質問はせず、見つけたものと聞きたいことを out/ に残して終わります。数分から数十分かかります。"
ui_text "止めるときは Ctrl-C（もう一度 $AWS_SURVEY_CMD scan で最初からになります）。"
echo ""
# 出力は画面に流しつつ、失敗の原因（ログイン切れなど）を読むために一時ファイルにも残す。out/ には書かない（out/ は調査エージェントの領分）
SCAN_LOG=$(mktemp) || ui_die "一時ファイルを作れません。"
trap 'rm -f "$SCAN_LOG"' EXIT
agent_cmd() {
  case "$AGENT" in
    claude) launch_run --name "${SURVEY_NAME}-scan" -- claude -p "$SCAN_PROMPT" --output-format text ;;
    codex)  launch_run --name "${SURVEY_NAME}-scan" -- codex exec --skip-git-repo-check --color never "$SCAN_PROMPT" ;;
  esac
}
status=0
if on_terminal; then
  # 端末では回転する印と進み具合を 1 行に出し続け、エージェントの出力はその上に流す
  SPIN_DIR=$(mktemp -d) || ui_die "一時ディレクトリを作れません。"
  : > "$SPIN_DIR/keep"; printf '%s' "棚卸しを始めています" > "$SPIN_DIR/msg"
  ui_spin_loop "$SPIN_DIR" &
  spid=$!
  scan_progress "$SPIN_DIR" &
  ppid=$!
  agent_cmd < /dev/null >> "$SPIN_DIR/keep" 2>&1 || status=$?
  kill "$ppid" 2>/dev/null || true; wait "$ppid" 2>/dev/null || true
  ui_spin_end "$SPIN_DIR" "$spid"
  cp "$SPIN_DIR/keep" "$SCAN_LOG"
  rm -rf "$SPIN_DIR"
else
  agent_cmd < /dev/null 2>&1 | tee "$SCAN_LOG" || status=${PIPESTATUS[0]}
fi
echo ""
launch_record_verified

if [ "$status" -ne 0 ]; then
  ui_err "棚卸しが途中で終わりました（終了コード ${status}）"
  if grep -qiE 'not logged in|log ?in|unauthorized|authentication' "$SCAN_LOG" 2>/dev/null; then
    ui_text "ログインが切れているようです。$(ui_cmd "$AWS_SURVEY_CMD $AGENT") で起動して入り直してから、もう一度 $AWS_SURVEY_CMD scan を実行してください。"
  else
    ui_text "上の出力を確認してから、もう一度 $(ui_cmd "$AWS_SURVEY_CMD scan") を実行してください。"
  fi
  exit "$status"
fi

raw_count=0
for f in "$OUT_DIR/$SURVEY_PHASE_DIR"/raw/raw-*; do [ -f "$f" ] && raw_count=$((raw_count + 1)); done
env_mark_setup scanned "白紙の棚卸しを済ませた" "$(date '+%FT%H:%M%z')"
ui_ok "棚卸しを終えました（生データ ${raw_count} 件: out/$SURVEY_PHASE_DIR/raw/）"
echo ""
next_cmd "$AWS_SURVEY_CMD $AGENT" "棚卸しを手に、何を明らかにしたいかを中で決めて調査を進めます"
