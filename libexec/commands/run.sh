#!/usr/bin/env bash
# 調査用コンテナをビルドして起動する（ホストで実行）
#   aws-survey run          → シェルに入る（そこから claude / codex を起動する）
#   aws-survey run bash     → 同上（明示）
#   aws-survey run codex    → codex を直接起動する
# 実体は libexec/commands/run.sh。通常は aws-survey 経由で呼ぶ。
#
# 本体は AWS_SURVEY_HOME、対象フォルダ（out/・environment.json）は AWS_SURVEY_DIR から解決する
# （libexec/load-env.sh）。カレントディレクトリに依存するのは AWS_SURVEY_DIR の既定だけ。
set -euo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"

IMAGE="${SURVEY_NAME}:latest"
VOLUME="${SURVEY_NAME}-claude"
CODEX_VOLUME="${SURVEY_NAME}-codex"
# Claude Code のネイティブ導入先（~/.local）。自分で更新するので、更新後の版を残す。
CLI_VOLUME="${SURVEY_NAME}-cli"

arch=$(uname -m)
case "$arch" in
  arm64|aarch64) awsarch=aarch64 ;;
  x86_64|amd64)  awsarch=x86_64  ;;
  *) ui_die "未対応のアーキテクチャ: $arch" ;;
esac

[ -f "$AWS_DIR/credentials" ] || ui_die "読み取り専用の一時キーがありません。先に $AWS_SURVEY_CMD credentials を実行してください。"

# 成果物の置き場。フェーズごとにフォルダを分ける（out/NN_フェーズ名/ と その raw/ log/ report/）。
# out/_環境/ は「この環境自体の記録」＝ 動作確認の結果と監査ログ（ホスト側で回収する証跡）。
# コンテナ内からは mkdir できてもよいが、現在のフェーズの器はホスト側で用意しておく。
OUT_DIR="$AWS_SURVEY_DIR/out"
mkdir -p "$OUT_DIR" "$OUT_DIR/_環境" "$OUT_DIR/$SURVEY_PHASE_DIR/raw" "$OUT_DIR/$SURVEY_PHASE_DIR/log" "$OUT_DIR/$SURVEY_PHASE_DIR/report"

# ビルドは毎回走らせる。焼き込んだ設定とガード（settings.json・hooks・bashrc・survey-status）は
# 再ビルドしないと古いまま渡るので、「ビルドし忘れたまま起動する」を構造的に無くしている。
# ほとんどの回は全段キャッシュで 1 秒未満、見せるものが何も無い。一方、初回や apt / npm の段が
# 変わった回は数分かかるので、黙ったままにはできない。そこで --progress=plain を読み、
# 2 秒を超えたときだけ進行中の 1 行を書き替え、終わったら結果の 1 行だけ残す。
# 失敗したときは畳んだものを捨てず、記録の末尾を見せる。
#
# docker build の出力を読み、数えた段数と、そのうちキャッシュだった数を $1 に書く。
render_build() {
  local counts=$1 line rest cur='' steps=0 cached=0 tick=-1 text
  # 1 秒で区切って読む。出力の無い段（ネットワーク待ちなど）でも経過を進めたい。
  # read が 128 より大きい値を返したのが時間切れ、それ以外の失敗が出力の終わり。
  while true; do
    line=''
    IFS= read -r -t 1 line || { [ $? -gt 128 ] || break; }
    case $line in
      '#'[0-9]*)
        rest=${line#* }
        case $rest in
          '[internal]'*) ;;                    # 文脈や定義の読み込み。段ではない
          '['*'/'*']'*)
            cur=$rest
            # ベースイメージの段は、キャッシュに当たっても CACHED とは出ない。数に入れない
            case ${rest#*] } in 'FROM '*) ;; *) steps=$(( steps + 1 )) ;; esac ;;
          CACHED) cached=$(( cached + 1 )) ;;
        esac ;;
    esac

    # 1 秒に 1 回だけ書き替える。2 秒未満で終わる回（全段キャッシュ）はここへ来ない
    if [ "$SECONDS" -ge 2 ] && [ "$SECONDS" != "$tick" ]; then
      tick=$SECONDS
      text=${cur:-準備中}
      [ ${#text} -gt 56 ] && text="${text:0:55}…"
      ui_status "$text  $(ui_duration "$tick")"
    fi
  done
  ui_status_done
  printf '%s %s\n' "$steps" "$cached" > "$counts"
}

build_image() {
  local log counts steps=0 cached=0 elapsed suffix=''
  log=$(mktemp)    || ui_die "一時ファイルを作れません。"
  counts=$(mktemp) || ui_die "一時ファイルを作れません。"
  SECONDS=0
  if ! docker build --progress=plain --build-arg "AWSCLI_ARCH=$awsarch" \
         -t "$IMAGE" -f "$AWS_SURVEY_HOME/Dockerfile" "$AWS_SURVEY_HOME/container" 2>&1 \
       | tee "$log" | render_build "$counts"; then
    ui_err "イメージのビルドに失敗しました"
    # 段ごとの行を落とすと、docker が最後にまとめる失敗の説明（どの段の何行目か、その出力）
    # だけが残る。ERROR の行と、段に属さない行は残す。
    ui_raw "$(awk '/^#[0-9]+ / && !/ERROR/ { next }
                   /^$/ || /^View build details:/ { next }
                   { print }' "$log" | tail -n 25)"
    rm -f "$log" "$counts"
    exit 1
  fi
  elapsed=$SECONDS
  read -r steps cached < "$counts" || true
  rm -f "$log" "$counts"
  [ "$elapsed" -gt 0 ] && suffix="（$(ui_duration "$elapsed")）"
  if [ "${steps:-0}" -gt 0 ] && [ "${cached:-0}" -ge "$steps" ]; then
    ui_ok "イメージは最新です$suffix"
  else
    ui_ok "イメージを用意しました$suffix"
  fi
}

ui_title "aws-survey run"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "本体" "$AWS_SURVEY_HOME"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "リージョン" "$REGION"
ui_kv "フェーズ" "$SURVEY_PHASE_DIR"
echo ""
ui_head "1/3 コンテナのイメージを用意する（$IMAGE, ${awsarch}）"
docker image inspect "$IMAGE" >/dev/null 2>&1 || ui_text "初回は数分かかります。2 回目からは差分だけです。"
build_image

# 名前付きボリュームは root 所有で作られることがあるため、毎回そろえておく。
# （既に正しければ何も起きない。冪等）
echo ""
ui_head "2/3 Claude Code / Codex の状態を保つ領域を用意する"
docker volume inspect "$VOLUME" >/dev/null 2>&1 || docker volume create "$VOLUME" >/dev/null
docker run --rm -u 0 -v "$VOLUME:/v" "$IMAGE" chown -R node:node /v

docker volume inspect "$CODEX_VOLUME" >/dev/null 2>&1 || docker volume create "$CODEX_VOLUME" >/dev/null
docker run --rm -u 0 -v "$CODEX_VOLUME:/v" "$IMAGE" chown -R node:node /v

# 認証はボリュームに残る（Dockerfile の CLAUDE_CONFIG_DIR）。
# 長期トークンをホストから環境変数で渡さないこと。docker inspect に現れる。
ui_ok "用意できました"
echo ""
ui_head "3/3 調査コンテナを起動する"
ui_text "中では AWS を読むことしかできません。調査の目的と進め方は、中で Claude Code / Codex と決めます。"
echo ""
# exec しない。コンテナが終わったあとに到達点を記録するため（下）。終了コードはそのまま返す。
status=0
docker run --rm -it \
  --name "$SURVEY_NAME" \
  --hostname aws-survey \
  -v "$AWS_DIR:/home/node/.aws-claude:ro" \
  -v "$AWS_SURVEY_HOME/container/instructions/survey-claude.md:/home/node/aws-survey/CLAUDE.md:ro" \
  -v "$AWS_SURVEY_HOME/container/instructions/survey-agents.md:/home/node/aws-survey/AGENTS.md:ro" \
  -v "$AWS_SURVEY_HOME/container/method:/home/node/aws-survey/method:ro" \
  -v "$OUT_DIR:/home/node/aws-survey/out" \
  -v "$CODEX_VOLUME:/home/node/.codex" \
  -v "$VOLUME:/home/node/.claude" \
  -v "$CLI_VOLUME:/home/node/.local" \
  -e TZ=Asia/Tokyo \
  -e "AWS_DEFAULT_REGION=$REGION" \
  -e "SURVEY_PHASE_DIR=$SURVEY_PHASE_DIR" \
  "$IMAGE" "$@" || status=$?

# 初回の動作確認が済んでいれば、到達点として environment.json に記録する。
# 記録するのは「調査エージェントが確認結果を書いた」ことまで。中身は判断しない。
VERIFIED_NOTE="$OUT_DIR/_環境/00_動作確認.md"
if [ -s "$VERIFIED_NOTE" ] && [ -z "$(jq -r '.setup.container_verified // empty' "$ENV_FILE")" ]; then
  echo ""
  env_mark_setup container_verified "コンテナの中で環境を確かめた"
fi
exit "$status"
