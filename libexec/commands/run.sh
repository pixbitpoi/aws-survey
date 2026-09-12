#!/usr/bin/env bash
# 調査用コンテナをビルドして起動する（ホストで実行）
#   aws-survey run          → 素のシェルに入る（通常は aws-survey scan / claude / codex から起動するので、直接は使わない）
#   aws-survey run bash     → 同上（明示）
#   aws-survey run claude   → claude を直接起動する（aws-survey claude がこれを呼ぶ。codex も同じ）
# 実体は libexec/commands/run.sh。通常は aws-survey 経由で呼ぶ。
#
# 本体は AWS_SURVEY_HOME、対象フォルダ（out/・environment.json）は AWS_SURVEY_DIR から解決する
# （libexec/load-env.sh）。カレントディレクトリに依存するのは AWS_SURVEY_DIR の既定だけ。
# イメージのビルドは libexec/docker.sh（lambda pull と共有）、マウント列は libexec/launch.sh（scan と共有）。
set -euo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/docker.sh"
. "$LIBEXEC_DIR/launch.sh"

docker_awsarch

[ -f "$AWS_DIR/credentials" ] || ui_die "読み取り専用の一時キーがありません。先に $AWS_SURVEY_CMD credentials を実行してください。"

launch_prepare_dirs

# 見出しは呼び元が差し替えられる（aws-survey claude / codex は自分の名前で出す）
ui_title "${AWS_SURVEY_RUN_TITLE:-aws-survey run}"
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

echo ""
ui_head "2/3 Claude Code / Codex の状態を保つ領域を用意する"
launch_prepare_volumes
ui_ok "用意できました"
echo ""
ui_head "3/3 調査コンテナを起動する"
ui_text "中では AWS を読むことしかできません。調査の目的と進め方は、中で Claude Code / Codex と決めます。"
if [ -d "$AWS_SURVEY_DIR/code/lambda" ]; then
  ui_text "取り出した Lambda のコード（code/）も読み取り専用で渡します。"
fi
echo ""
# exec しない。コンテナが終わったあとに到達点を記録するため（下）。終了コードはそのまま返す。
status=0
launch_run -it --name "$SURVEY_NAME" -- "$@" || status=$?

launch_record_verified
exit "$status"
