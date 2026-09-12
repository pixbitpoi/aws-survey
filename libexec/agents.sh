#!/usr/bin/env bash
# 調査に使うエージェント（Claude Code / Codex）の名前・モデル・effort の扱い。aws-survey（init）と load-env.sh が source する。
# ui.sh のあとに読み込む（agent_label を使う）。
#
# environment.json の agent は {name, model, effort}。name が使うエージェント（最後に使ったもの）、model と effort は
# そのエージェントに渡す値。init が聞いて書き、aws-survey claude / codex と scan --agent が name を上書きする
# （別のエージェントに切り替えたときは model / effort もそのエージェントの既定に戻る。前のものの値は渡せないため）。
# 古い形（"agent": "claude"）も name だけの指定として読む。
#
# モデルの候補はここに並べるだけで、イメージには焼かない。名前は各 CLI の都合で増減するので、候補に無ければ手で入れる。
# CLI に渡す形: Claude Code は --model <m> --effort <e>、Codex は -m <m> -c model_reasoning_effort=<e>（どちらも対話・非対話で同じ）。

AGENT_NAMES="claude codex"

agent_valid() { case "${1:-}" in claude|codex) return 0 ;; *) return 1 ;; esac; }

# 既定。Claude Code は opus の medium、Codex は gpt-5.6-sol の low
agent_default_model()  { case "$1" in claude) echo opus ;;   codex) echo gpt-5.6-sol ;; esac; }
agent_default_effort() { case "$1" in claude) echo medium ;; codex) echo low ;; esac; }

# 候補（空白区切り）。既定は agent_default_* で、候補の並びとは独立（effort は低い順に並べ、メニューのカーソルを既定に置く）
agent_model_choices()  { case "$1" in claude) echo "opus sonnet haiku" ;; codex) echo "gpt-5.6-sol gpt-6-astra gpt-5.6-terra gpt-5.6-luna gpt-5.5" ;; esac; }
agent_effort_choices() { case "$1" in claude) echo "low medium high xhigh max" ;; codex) echo "low medium high xhigh" ;; esac; }

# モデル名は CLI に引数で渡す文字列。英数字と . - _ [ ] だけ
agent_valid_model()  { local re='^[][A-Za-z0-9._-]{1,64}$'; [[ "${1:-}" =~ $re ]]; }
agent_valid_effort() {
  local e
  for e in $(agent_effort_choices "$1"); do [ "$e" != "${2:-}" ] || return 0; done
  return 1
}

# CLI に渡すフラグを配列 AGENT_FLAGS に組む。agent_flags <agent> <model> <effort>（model / effort が空なら既定）
agent_flags() {
  local agent="$1" model="${2:-}" effort="${3:-}"
  [ -n "$model" ]  || model=$(agent_default_model "$agent")
  [ -n "$effort" ] || effort=$(agent_default_effort "$agent")
  case "$agent" in
    claude) AGENT_FLAGS=(--model "$model" --effort "$effort" --strict-mcp-config) ;;   # claude.ai のコネクタ（MCP）を調査コンテナに持ち込まない
    codex)  AGENT_FLAGS=(-m "$model" -c "model_reasoning_effort=$effort") ;;
    *)      AGENT_FLAGS=() ;;
  esac
}

# 利用者に見せる 1 行。agent_summary <agent> <model> <effort> → "Claude Code（opus / medium）"
agent_summary() {
  local model="${2:-}" effort="${3:-}"
  [ -n "$model" ]  || model=$(agent_default_model "$1")
  [ -n "$effort" ] || effort=$(agent_default_effort "$1")
  printf '%s（%s / %s）' "$(agent_label "$1")" "$model" "$effort"
}
