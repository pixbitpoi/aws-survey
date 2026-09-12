#!/usr/bin/env bash
# 調査に使うエージェント（Claude Code / Codex）のログインを済ませる（ホストで実行）
#   aws-survey login                   記録してあるエージェントでログインする（init が聞き取りの最後に呼ぶ）
#   aws-survey login codex             エージェントを指定する（記録も更新する）
# 実体は libexec/commands/login.sh。認証はコンテナ内のボリュームに残るので、対象ごとに初回だけ実際のログインになる。
# 一時キーは要らない（付けない）。イメージのビルドとログインの実体は libexec/launch.sh の launch_agent_login。
set -euo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/docker.sh"
. "$LIBEXEC_DIR/launch.sh"

AGENT="${1:-}"
case "$AGENT" in
  -h|--help|help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") AGENT="$SURVEY_AGENT" ;;
  claude|codex) ;;
  *) ui_die "エージェントは claude か codex です: $AGENT" ;;
esac
if [ -z "$AGENT" ]; then
  ui_err "調査に使うエージェントがまだ決まっていません"
  next_cmd "$AWS_SURVEY_CMD login claude" "Claude Code でログインします（codex なら Codex）"
  exit 1
fi
env_set_agent "$AGENT"

ui_title "aws-survey login"
ui_kv "エージェント" "$(agent_summary "$SURVEY_AGENT" "$SURVEY_AGENT_MODEL" "$SURVEY_AGENT_EFFORT")"
echo ""
launch_agent_login "$AGENT" || exit 1
