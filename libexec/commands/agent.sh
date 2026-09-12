#!/usr/bin/env bash
# 調査コンテナで Claude Code / Codex を対話で起動する（ホストで実行）
#   aws-survey claude [claude の引数...]   Claude Code を起動する（-c で直前の会話の続き、-r で選んで再開）
#   aws-survey codex  [codex の引数...]    Codex を起動する（resume --last で直前の会話の続き）
# 実体は libexec/commands/agent.sh <claude|codex> [...]。run.sh に exec する薄い皮で、持つのは一時キーの判定・
# 使うエージェントの記録・モデルと effort のフラグ・見出しだけ。棚卸しが out/ にあれば、中のエージェントはそれを手にユーザーに聞くところから始める。
set -euo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/container.sh"
. "$LIBEXEC_DIR/launch.sh"

AGENT="${1:-}"; shift || true
case "$AGENT" in
  claude|codex) ;;
  *) ui_die "エージェントは claude か codex です: ${AGENT:-（指定なし）}" ;;
esac
case "${1:-}" in
  -h|--help|help)
    sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
esac

# 対話で起動するので端末が要る（端末でなければ docker run -it が失敗する）
{ [ -t 0 ] && [ -t 1 ]; } || ui_die "端末で実行してください（$AWS_SURVEY_CMD $AGENT は対話でエージェントを起動します）。"

# 一時キーがあり、期限内で、読み取り専用と確かめ済みか（期限切れなら credentials を案内して止まる）
container_require_key || exit 1

# 最後に使ったエージェントとして記録する（scan の既定と、案内文の「次に打つコマンド」に使う）。
# モデルと effort は environment.json の agent から CLI のフラグにして渡す（libexec/agents.sh）
env_set_agent "$AGENT"
launch_agent_flags "$AGENT"

AWS_SURVEY_RUN_TITLE="aws-survey $AGENT" exec "$LIBEXEC_DIR/commands/run.sh" "$AGENT" "${AGENT_FLAGS[@]}" "$@"
