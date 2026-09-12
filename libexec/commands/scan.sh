#!/usr/bin/env bash
# 調査コンテナでエージェントを非対話で 1 回走らせ、対象アカウントの初期調査だけを行わせる（ホストで実行）
#   aws-survey scan                    記録してあるエージェント（未定なら矢印キーで選ぶ）で初期調査を行う
#   aws-survey scan --agent codex      エージェントを指定する（記録も更新する）
# 実体は libexec/commands/scan.sh。通常は aws-survey 経由で呼ぶ。
#
# ユーザーは待つだけ。中のエージェントは環境の確認（初回）と白紙の棚卸しをして、見つけたものと聞きたいことを out/ に
# 残して終わる。目的の聞き取りと深掘りはしない（それは aws-survey claude / codex で対話して進める）。
# 「白紙の棚卸し」は開発側の言葉で、利用者に見せる文と表示では「初期調査」で通す（.agents/rules/credentials.md）。
# 端末では、始める前に ls と同じ列挙（Cost Explorer 込み）で調査の量を見積もり、待っているあいだは推定の進捗をプログレスバーで出す。
# 見積もりは画面に出すだけで、ファイルにもエージェントにも渡さない。終わったら out/ に残したものを言い換えて示す。
# ホストから渡す指示は「棚卸しだけ・ユーザーに聞かずに終える」に限る。対象の概要・調査項目・サービス名は渡さない
# （AGENTS.md「ホストと調査コンテナ」。白紙で棚卸しさせる設計は、先に教えると発見ではなく確認になるため）。
# Claude / Codex の認証はコンテナ内のボリュームに残る。無ければ先に対話でログインだけ済ませ（launch.sh の launch_agent_login。
# 通常は init が済ませている）、そのあと非対話で続ける。モデルと effort は environment.json の agent から渡す（libexec/agents.sh）。
set -euo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/container.sh"
. "$LIBEXEC_DIR/launch.sh"   # container.sh が docker.sh を読む
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

# ---- 調査の量の見積もり ----
# 端末で進捗を出すときだけ。ls と同じ列挙（libexec/inventory.sh）を Cost Explorer 込みで一時キーに読ませ、課金のあるサービスと
# リソースの数から、生データ（raw/raw-*）の見込み数 EST_FILES と所要時間の目安 EST_SECONDS を決める。読めなければ一般的な目安。
# 係数は実測に合わせて直す値で、ここにだけ置く: 生データは「4 + サービスごとに 2 + リソース 5 件ごとに 1」（6〜60 件）、
# 時間は「150 秒 + 生データ 1 件あたり 50 秒」、環境の確認（初回）があれば 120 秒足す。
EST_FILES=12; EST_SECONDS=$((150 + 50 * 12))
scan_estimate() {
  local inv billed cost_ok active denied items s note
  inv=$(container_inventory_spin cost ec2 lambda vpc s3 rds ecs elb cloudfront 2>/dev/null) || true
  if [ -z "$inv" ] || ! jq -e 'select(.kind == "service" and .status == "ok")' <<< "$inv" >/dev/null 2>&1; then
    ui_text "調査の量を見積もれなかったので、一般的な目安（$(ui_duration "$EST_SECONDS")ほど）で進捗を出します。"
  else
    cost_ok=$(jq -r 'select(.kind == "service" and .service == "cost" and .status == "ok") | .service' <<< "$inv" | wc -l | tr -d ' ')
    billed=$(jq -r 'select(.kind == "item" and .service == "cost") | select(.id | test("Tax|Support|Refund|Credit") | not) | .id' <<< "$inv" | wc -l | tr -d ' ')
    active=$(jq -r 'select(.kind == "service" and .status == "ok" and .service != "cost" and .service != "ssm" and (.count // 0) > 0) | .service' <<< "$inv" | wc -l | tr -d ' ')
    denied=$(jq -r 'select(.kind == "service" and .status != "ok" and .service != "cost" and .service != "ssm") | .service' <<< "$inv" | wc -l | tr -d ' ')
    items=$(jq -r 'select(.kind == "service" and .status == "ok" and .service != "cost") | .count // 0' <<< "$inv" | awk '{ s += $1 } END { print s + 0 }')
    s=$((active + denied)); [ "$s" -ge "$billed" ] || s=$billed
    EST_FILES=$((4 + 2 * s + items / 5)); [ "$EST_FILES" -ge 6 ] || EST_FILES=6; [ "$EST_FILES" -le 60 ] || EST_FILES=60
    if [ "$cost_ok" -gt 0 ]; then note="課金のあるサービス ${billed}、リソース ${items} 件"; else note="リソース ${items} 件（Cost Explorer は読めず）"; fi
    [ "$denied" -eq 0 ] || note="${note}、読めないサービス ${denied}"
    EST_SECONDS=$((150 + 50 * EST_FILES))
    [ -f "$OUT_DIR/_環境/00_動作確認.md" ] || EST_SECONDS=$((EST_SECONDS + 120))
    ui_ok "見積もり: ${note}。目安は $(ui_duration "$EST_SECONDS")ほど（進捗は推定です）"
  fi
}

# ---- 進捗 ----
# 端末で待っているあいだ、回転する印の行に、推定の進捗（プログレスバーと %）・経過時間・out/ に書かれた記録を 1 秒ごとに出す。
# エージェントは何分も黙って働くので、固まっていないことと、あとどれくらいかを見せる（ui.sh の簡潔表示の部品を借りる）。
# 前半（生データが見込み数に届くまで）は 2 つを混ぜて直線的に進める: 生データの数 ÷ 見込み数（7 割）と、経過時間 ÷ 目安の時間（3 割）。
# 見込み数に届いたら「思ったより量が多い」局面で、そのときの値から 94% に向けて、届いてからの生データの数と時間に比例して
# 半分ずつ詰めていく（同じ量をもう一度こなすと残りの半分。止まらず、100% にも届かない）。表示は戻さず、上限は 95%。
# 生データは raw/ の下の新しいファイルを名前を問わず数える（method は raw- で始めるよう求めるが、進捗はそれに依らない）。
# まとめ（log/01_棚卸し.md）が書かれたら 90% 以上。100% は終わったときの ✔ が担う。
#   scan_progress <受け渡しの場所>   （& で起動し、終わったら kill する。keep が開始の目印）
scan_bar() {  # <percent> → ████████░░░░░░░░░░░░  40%
  local p="$1" i bar=''
  for ((i = 0; i < 20; i++)); do if [ $((i * 5)) -lt "$p" ]; then bar+='█'; else bar+='░'; fi; done
  printf '%s %3d%%' "$bar" "$p"
}
# 推定の % を 1 つ返す。状態（前回の値・後半に入った時点）は呼ぶ側の変数で持つ。
#   scan_percent <生データの数> <経過秒> → PCT を更新（PREV / B_N / B_EL / B_BASE を読み書き）
scan_percent() {
  local n="$1" el="$2" pf pt p k x kf kt
  if [ "$B_EL" -lt 0 ] && [ "$n" -ge "$EST_FILES" ]; then
    B_N=$n; B_EL=$el; B_BASE=$PREV; [ "$B_N" -ge 1 ] || B_N=1; [ "$B_EL" -ge 1 ] || B_EL=1
  fi
  if [ "$B_EL" -lt 0 ]; then
    pf=$((100 * n / EST_FILES)); pt=$((100 * el / EST_SECONDS)); [ "$pt" -le 100 ] || pt=100
    p=$(( (7 * pf + 3 * pt) / 10 ))
  else
    k=$((n - B_N)); x=$((el - B_EL))
    kf=$((100 * k / (k + B_N))); kt=$((100 * x / (x + B_EL)))
    p=$(( B_BASE + (94 - B_BASE) * (7 * kf + 3 * kt) / 1000 ))
  fi
  [ "$p" -le 95 ] || p=95
  [ "$p" -ge "$PREV" ] || p=$PREV
  PREV=$p; PCT=$p
}
scan_progress() {
  local dir="$1" raw="$OUT_DIR/$SURVEY_PHASE_DIR/raw" log="$OUT_DIR/$SURVEY_PHASE_DIR/log/01_棚卸し.md"
  local start now el n m last msg PCT=0 PREV=0 B_N=0 B_EL=-1 B_BASE=0
  set +e; set +o pipefail          # 背景の 1 ループ。find が無い・読めないときも黙って回り続ける
  start=$(date +%s)
  while :; do
    now=$(date +%s); el=$((now - start))
    n=$(find "$raw" -type f -newer "$dir/keep" 2>/dev/null | wc -l | tr -d ' '); n=${n:-0}
    m=$(find "$OUT_DIR" -type f -newer "$dir/keep" 2>/dev/null | wc -l | tr -d ' '); m=${m:-0}
    last=$(find "$OUT_DIR" -type f -newer "$dir/keep" -exec ls -t {} + 2>/dev/null | head -n 1); last=${last##*/}
    if [ -f "$log" ] && [ "$log" -nt "$dir/keep" ] && [ "$PREV" -lt 90 ]; then PREV=90; fi
    scan_percent "$n" "$el"
    msg="初期調査中 $(scan_bar "$PCT")  経過 $(ui_duration "$el")  記録 ${m} 件${last:+  最新 ${last}}"
    printf '%s' "$msg" > "$dir/msg.tmp" && mv -f "$dir/msg.tmp" "$dir/msg"
    sleep 1
  done
}

# ---- 終わったあとの案内 ----
# out/ に何が残ったかを、この回に書かれた（開始の目印より新しい）ファイルだけ、置き場所ごとに言い換えて示す。
# 生データは件数と名前、まとめ（log/01_棚卸し.md）は見出しを抜き出す。中身の要約はしない（読むのは対話の回のエージェント）。
#   scan_summary <開始の目印>
scan_summary() (   # サブシェル。無いフォルダや空の grep で止まらないよう、set -e と pipefail を外す
  set +e; set +o pipefail
  local mark="$1" phase="$OUT_DIR/$SURVEY_PHASE_DIR" log="$OUT_DIR/$SURVEY_PHASE_DIR/log/01_棚卸し.md" raw_n names line
  raw_n=$(find "$phase/raw" -type f -newer "$mark" 2>/dev/null | wc -l | tr -d ' ')
  names=$(find "$phase/raw" -type f -newer "$mark" 2>/dev/null | sed 's|.*/||' | sort | head -n 5 | tr '\n' ' ')
  ui_head "out/ に残したもの"
  if [ "${raw_n:-0}" -gt 0 ]; then
    ui_ok "生データ ${raw_n} 件  $SURVEY_PHASE_DIR/raw/"
    ui_text "AWS の API の出力そのまま（${names% }$([ "$raw_n" -le 5 ] || echo " …")）。あとから jq で引き直せます。"
  else
    ui_skip "生データ（$SURVEY_PHASE_DIR/raw/）は書かれていません"
  fi
  if [ -f "$log" ] && [ "$log" -nt "$mark" ]; then
    ui_ok "見つけたものの一覧と、次に聞きたいこと  $SURVEY_PHASE_DIR/log/01_棚卸し.md"
    grep -E '^#{1,3} ' "$log" | head -n 12 | sed 's/^#* *//' | while IFS= read -r line; do ui_text "  ・$line"; done
  else
    ui_skip "まとめ（$SURVEY_PHASE_DIR/log/01_棚卸し.md）は書かれていません。他のファイルに書いたか、途中で終わった可能性があります"
  fi
  find "$OUT_DIR" -type f -newer "$mark" 2>/dev/null | grep -v "^$phase/raw/" | grep -vx "$log" | sed "s|^$OUT_DIR/||" | sort \
    | while IFS= read -r line; do
        case "$line" in
          _環境/00_動作確認.md) ui_ok "環境の確認の記録  $line" ;;
          *) ui_ok "$line" ;;
        esac
      done
  ui_text "報告書（report/）はまだありません。次の対話の回で、何を明らかにしたいかを決めてから書きます。"
)

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
    next_cmd "$AWS_SURVEY_CMD scan --agent claude" "Claude Code で初期調査を行います（--agent codex なら Codex）"
    exit 1
  fi
fi
env_set_agent "$AGENT"
launch_agent_flags "$AGENT"
ui_ok "エージェント: $(agent_summary "$AGENT" "$SURVEY_AGENT_MODEL" "$SURVEY_AGENT_EFFORT")"

# ---- 3. 同じ out/ に向いたコンテナが動いていないか ----
command -v docker >/dev/null || ui_die "docker コマンドが見つかりません。Docker Desktop を導入して起動してください。"
for n in "$SURVEY_NAME" "${SURVEY_NAME}-scan"; do
  if launch_container_running "$n"; then
    ui_die "調査コンテナ（${n}）が動いています。同じ out/ に 2 つのエージェントを同時には走らせられません。中の会話を終えてから実行してください。"
  fi
done

# ---- 4. イメージ・ボリューム・認証（launch.sh） ----
docker_check_shared "$AWS_DIR" || exit 1
launch_prepare_dirs
launch_agent_login "$AGENT" || exit 1
echo ""

# ---- 5. 実行 ----
ui_head "初期調査（$(agent_label "$AGENT")）"
ui_text "対象アカウントに何があるかを洗い出し、リソースの一覧と気づいたことを out/ に書きます。質問はしないので、待つだけです。"
ui_text "止めるときは Ctrl-C（もう一度 $AWS_SURVEY_CMD scan で最初からになります）。"
echo ""
# 出力は画面に流しつつ、失敗の原因（ログイン切れなど）を読むために一時ファイルにも残す。out/ には書かない（out/ は調査エージェントの領分）
SCAN_LOG=$(mktemp) || ui_die "一時ファイルを作れません。"
START_MARK=$(mktemp) || ui_die "一時ファイルを作れません。"     # この回に書かれたファイルを見分ける目印（進捗とまとめが -newer で見る）
trap 'rm -f "$SCAN_LOG" "$START_MARK"' EXIT
agent_cmd() {
  case "$AGENT" in
    claude) launch_run --name "${SURVEY_NAME}-scan" -- claude "${AGENT_FLAGS[@]}" -p "$SCAN_PROMPT" --output-format text ;;
    codex)  launch_run --name "${SURVEY_NAME}-scan" -- codex "${AGENT_FLAGS[@]}" exec --skip-git-repo-check --color never "$SCAN_PROMPT" ;;
  esac
}
status=0
if on_terminal; then
  # 端末では先に量を見積もり、回転する印と推定の進捗を 1 行に出し続ける。エージェントの出力はその上に流す（keep）
  scan_estimate
  scan_start=$(date +%s)
  SPIN_DIR=$(mktemp -d) || ui_die "一時ディレクトリを作れません。"
  : > "$SPIN_DIR/keep"; touch "$START_MARK"; printf '%s' "初期調査中 $(scan_bar 0)  始めています" > "$SPIN_DIR/msg"
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
  scan_start=$(date +%s)
  touch "$START_MARK"
  agent_cmd < /dev/null 2>&1 | tee "$SCAN_LOG" || status=${PIPESTATUS[0]}
fi
scan_elapsed=$(( $(date +%s) - scan_start ))
echo ""
launch_record_verified

if [ "$status" -ne 0 ]; then
  ui_err "初期調査が途中で終わりました（終了コード ${status}）"
  if grep -qiE 'not logged in|log ?in|unauthorized|authentication' "$SCAN_LOG" 2>/dev/null; then
    ui_text "ログインが切れているようです。$(ui_cmd "$AWS_SURVEY_CMD $AGENT") で起動して入り直してから、もう一度 $AWS_SURVEY_CMD scan を実行してください。"
  else
    ui_text "上の出力を確認してから、もう一度 $(ui_cmd "$AWS_SURVEY_CMD scan") を実行してください。"
  fi
  exit "$status"
fi

ui_ok "初期調査を終えました（$(ui_duration "$scan_elapsed")）"
echo ""
scan_summary "$START_MARK"
echo ""
env_mark_setup scanned "初期調査を済ませた" "$(date '+%FT%H:%M%z')"
echo ""
next_cmd "$AWS_SURVEY_CMD $AGENT" "初期調査の結果を手に、何を明らかにしたいかを中で決めて調査を進めます"
