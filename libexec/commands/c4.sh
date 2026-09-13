#!/usr/bin/env bash
# 報告の C4 図（Structurizr DSL）を PNG にする（ホストで実行）
#   aws-survey c4            out/report/c4/workspace.dsl を読み、同じ場所に <ビューのキー>.png と <ビューのキー>-key.png（凡例）を置く
#   aws-survey c4 --force    PNG が DSL より新しくても描き直す
#   aws-survey c4 --auto     DSL が無い・PNG が最新なら黙って 0 で終わる（scan と run が終わりに呼ぶ）
# 実体は libexec/commands/c4.sh。通常は scan / claude / codex の終わりに自動で走るので、直接打つのは描き直したいときだけ。
#
# DSL は調査エージェントが書く（container/method/report.md「構成図」）。調査コンテナには docker も Java も無いので、PNG はホストが作る。
# 描くのは Structurizr の公式イメージ（Playwright 同梱の -playwright タグ。export -format png が DSL → 静的サイト → 同梱の Chromium で
# PNG を書く。2026-09-13 に実測）。渡すのは out/report/c4/ のマウントだけで、一時キーも元プロファイルも渡さず、
# ネットワークも切る（--network none。DSL の theme や !include の URL は効かないので、method は styles の直書きを求める）。
# 出力はいったん c4/.new/ に書き、成功したときだけ古い PNG と入れ替える（ビューを消したときに古い PNG が残らないため）。
# 失敗したら c4/_render-error.txt に docker の出力の ERROR 行を残す。調査エージェントは survey-status でそれを知り、次の回に DSL を直す
# （調査コンテナでは DSL の文法を確かめられないので、これが唯一の戻り道）。成功したら消す。
# イメージの版はここにだけ置く（AWS_SURVEY_C4_IMAGE で上書き可。テストは偽の docker を使うので版は見ない）。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"

C4_IMAGE="${AWS_SURVEY_C4_IMAGE:-structurizr/structurizr:2026.06.28-playwright}"
OUT_DIR="$AWS_SURVEY_DIR/out"
C4_DIR="$OUT_DIR/report/c4"
C4_DSL="$C4_DIR/workspace.dsl"
C4_ERR="$C4_DIR/_render-error.txt"
C4_NEW="$C4_DIR/.new"

usage() { sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; }

FORCE=0; AUTO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --auto)  AUTO=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) ui_die "c4 の不明なオプション: $1" ;;
  esac
done

# PNG が DSL より新しいか（1 枚でも古い・無ければ描き直す）
c4_up_to_date() {
  local p n=0
  for p in "$C4_DIR"/*.png; do
    [ -f "$p" ] || return 1
    [ "$p" -nt "$C4_DSL" ] || return 1
    n=$((n + 1))
  done
  [ "$n" -gt 0 ]
}

# 本体の図（凡例 -key.png を除く）の数
c4_count() {
  local p n=0
  for p in "$C4_DIR"/*.png; do
    [ -f "$p" ] || continue
    case "$p" in *-key.png) ;; *) n=$((n + 1)) ;; esac
  done
  echo "$n"
}

if [ ! -f "$C4_DSL" ]; then
  [ "$AUTO" -eq 0 ] || exit 0
  ui_title "aws-survey c4"
  ui_err "C4 図の DSL がありません: $(ui_path "$C4_DSL")"
  ui_text "調査エージェントが報告と一緒に書きます。初期調査（$AWS_SURVEY_CMD scan）か対話（$AWS_SURVEY_CMD claude / codex）のあとに実行してください。"
  exit 1
fi
if [ "$FORCE" -eq 0 ] && [ ! -f "$C4_ERR" ] && c4_up_to_date; then
  [ "$AUTO" -eq 0 ] || exit 0
  ui_title "aws-survey c4"
  ui_ok "C4 図は最新です（$(c4_count) 枚。$(ui_path "$C4_DIR")）"
  ui_text "描き直すなら $(ui_cmd "$AWS_SURVEY_CMD c4 --force") です。"
  exit 0
fi

[ "$AUTO" -eq 1 ] || ui_title "aws-survey c4"
command -v docker >/dev/null || ui_die "docker コマンドが見つかりません。Docker Desktop を導入して起動してください。"
docker_check_shared "$AWS_SURVEY_DIR" || exit 1
if ! docker image inspect "$C4_IMAGE" >/dev/null 2>&1; then
  ui_text "図を描くイメージ（$C4_IMAGE）を初回だけ取得します。1〜2 分かかります。"
fi

rm -rf "$C4_NEW"
mkdir -p "$C4_NEW" || ui_die "書き込めません: $C4_NEW"
LOG=$(mktemp) || ui_die "一時ファイルを作れません。"
trap 'rm -f "$LOG"; rm -rf "$C4_NEW"' EXIT
ui_status "C4 図を描いています"
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$C4_DIR:/usr/local/structurizr" \
  "$C4_IMAGE" export -workspace workspace.dsl -format png -output /usr/local/structurizr/.new > "$LOG" 2>&1
rc=$?
ui_status_done

made=0
for p in "$C4_NEW"/*.png; do [ -f "$p" ] && made=$((made + 1)); done
if [ "$rc" -ne 0 ] || [ "$made" -eq 0 ]; then
  ui_err "C4 図を描けませんでした（DSL に誤りがあるか、イメージを取得できません）"
  { echo "workspace.dsl から PNG を作れませんでした（$(date '+%F %H:%M')）。この行の下が原因です。直したら、このファイルは自動で消えます。"
    grep -E 'ERROR|Exception|Error|error:' "$LOG" | sed 's/^[0-9:. ]*\[main\] //' | head -n 20
    [ "$made" -eq 0 ] && [ "$rc" -eq 0 ] && echo "PNG が 1 枚も書かれませんでした（views にビューが無いか、export が図を書けなかった）"
  } > "$C4_ERR"
  ui_raw "$(grep -E 'ERROR|Exception' "$LOG" | sed 's/^[0-9:. ]*\[main\] //' | head -n 5)"
  ui_text "原因を $(ui_path "$C4_ERR") に残しました。次の回の調査エージェントが読んで DSL を直します（$AWS_SURVEY_CMD scan か $AWS_SURVEY_CMD claude / codex）。"
  exit 1
fi

rm -f "$C4_DIR"/*.png "$C4_ERR"
mv "$C4_NEW"/*.png "$C4_DIR"/
ui_ok "C4 図を描きました（$(c4_count) 枚。$(ui_path "$C4_DIR")）"
[ "$AUTO" -eq 1 ] || ui_text "報告（$(ui_path "$OUT_DIR/report/構成報告.md")）から c4/<ビューのキー>.png で参照されています。凡例は -key.png です。"
exit 0
