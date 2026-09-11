#!/usr/bin/env bash
# 調査用コンテナをビルドして起動する（ホストで実行）
#   aws-survey run          → シェルに入る（そこから claude / codex を起動する）
#   aws-survey run bash     → 同上（明示）
#   aws-survey run codex    → codex を直接起動する
# 実体は libexec/commands/run.sh。通常は aws-survey 経由で呼ぶ。
#
# 本体は AWS_SURVEY_HOME、対象フォルダ（out/・environment.json）は AWS_SURVEY_DIR から解決する
# （libexec/load-env.sh）。カレントディレクトリに依存するのは AWS_SURVEY_DIR の既定だけ。
# イメージのビルドは libexec/docker.sh（lambda pull と共有）。
set -euo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/docker.sh"

VOLUME="${SURVEY_NAME}-claude"
CODEX_VOLUME="${SURVEY_NAME}-codex"
# Claude Code のネイティブ導入先（~/.local）。自分で更新するので、更新後の版を残す。
CLI_VOLUME="${SURVEY_NAME}-cli"

docker_awsarch

[ -f "$AWS_DIR/credentials" ] || ui_die "読み取り専用の一時キーがありません。先に $AWS_SURVEY_CMD credentials を実行してください。"

# 成果物の置き場。フェーズごとにフォルダを分ける（out/NN_フェーズ名/ と その raw/ log/ report/）。
# out/_環境/ は「この環境自体の記録」＝ 動作確認の結果と監査ログ（ホスト側で回収する証跡）。
# コンテナ内からは mkdir できてもよいが、現在のフェーズの器はホスト側で用意しておく。
OUT_DIR="$AWS_SURVEY_DIR/out"
mkdir -p "$OUT_DIR" "$OUT_DIR/_環境" "$OUT_DIR/$SURVEY_PHASE_DIR/raw" "$OUT_DIR/$SURVEY_PHASE_DIR/log" "$OUT_DIR/$SURVEY_PHASE_DIR/report"

ui_title "aws-survey run"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "本体" "$AWS_SURVEY_HOME"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "リージョン" "$REGION"
ui_kv "フェーズ" "$SURVEY_PHASE_DIR"
echo ""
# マウントする 3 か所（本体の指示書と method/、対象フォルダの out/、一時キー）が Docker Desktop から見えるか（load-env.sh）
docker_check_shared "$AWS_SURVEY_HOME" "$AWS_SURVEY_DIR" "$AWS_DIR" || exit 1
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
# 取り出した Lambda のコード（aws-survey lambda pull）。あるときだけ読み取り専用で渡す
code_mount=()
if [ -d "$AWS_SURVEY_DIR/code" ]; then
  code_mount=(-v "$AWS_SURVEY_DIR/code:/home/node/aws-survey/code:ro")
  ui_text "取り出した Lambda のコード（code/）も読み取り専用で渡します。"
fi
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
  ${code_mount[@]+"${code_mount[@]}"} \
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
