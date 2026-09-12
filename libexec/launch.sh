#!/usr/bin/env bash
# 調査コンテナ一式（一時キー・指示書・method/・out/・code/・Claude Code / Codex の状態のボリューム）を付けて
# イメージを起動する部品。run（対話のシェル / エージェント）と scan（非対話の棚卸し）が source する。
# load-env.sh と docker.sh のあとに読み込む。
#
# libexec/container.sh（一時キーだけで 1 コマンド。out/ も指示書もボリュームも付けない）とは別物。
# あちらは「調査エージェントに渡さない道具」を動かす経路で、こちらは調査エージェントそのものを動かす経路。混ぜない。

VOLUME="${SURVEY_NAME}-claude"
CODEX_VOLUME="${SURVEY_NAME}-codex"
# Claude Code のネイティブ導入先（~/.local）。自分で更新するので、更新後の版を残す。
CLI_VOLUME="${SURVEY_NAME}-cli"
OUT_DIR="$AWS_SURVEY_DIR/out"

# 成果物の置き場。フェーズごとにフォルダを分ける（out/NN_フェーズ名/ と その raw/ log/ report/）。
# out/_環境/ は「この環境自体の記録」＝ 動作確認の結果と監査ログ（ホスト側で回収する証跡）。
# コンテナ内からは mkdir できてもよいが、現在のフェーズの器はホスト側で用意しておく。
# code/（aws-survey lambda pull の取り出し先）は空でも作って常に読み取り専用で渡す。コンテナを動かしたまま
# lambda pull したものが起動し直さずに見える（method/07 の依頼文の前提）
launch_prepare_dirs() {
  mkdir -p "$OUT_DIR" "$OUT_DIR/_環境" "$OUT_DIR/$SURVEY_PHASE_DIR/raw" "$OUT_DIR/$SURVEY_PHASE_DIR/log" "$OUT_DIR/$SURVEY_PHASE_DIR/report"
  mkdir -p "$AWS_SURVEY_DIR/code"
}

# 名前付きボリュームは root 所有で作られることがあるため、毎回そろえておく（既に正しければ何も起きない。冪等）。
# 認証はボリュームに残る（Dockerfile の CLAUDE_CONFIG_DIR）。長期トークンをホストから環境変数で渡さないこと。docker inspect に現れる。
launch_prepare_volumes() {
  local v
  for v in "$VOLUME" "$CODEX_VOLUME"; do
    docker volume inspect "$v" >/dev/null 2>&1 || docker volume create "$v" >/dev/null
    docker run --rm -u 0 -v "$v:/v" "$IMAGE" chown -R node:node /v
  done
}

# 調査コンテナ一式を起動する。docker の追加フラグ（-it / --name など）を -- の前に受け、-- の後ろがコンテナのコマンド。
#   launch_run -it --name "$SURVEY_NAME" -- bash
#   launch_run --name "${SURVEY_NAME}-scan" -- claude -p "..." < /dev/null
launch_run() {
  local -a flags=()
  while [ $# -gt 0 ]; do
    case "$1" in --) shift; break ;; *) flags+=("$1"); shift ;; esac
  done
  docker run --rm ${flags[@]+"${flags[@]}"} \
    --hostname aws-survey \
    -v "$AWS_DIR:/home/node/.aws-claude:ro" \
    -v "$AWS_SURVEY_HOME/container/instructions/survey-claude.md:/home/node/aws-survey/CLAUDE.md:ro" \
    -v "$AWS_SURVEY_HOME/container/instructions/survey-agents.md:/home/node/aws-survey/AGENTS.md:ro" \
    -v "$AWS_SURVEY_HOME/container/method:/home/node/aws-survey/method:ro" \
    -v "$OUT_DIR:/home/node/aws-survey/out" \
    -v "$AWS_SURVEY_DIR/code:/home/node/aws-survey/code:ro" \
    -v "$CODEX_VOLUME:/home/node/.codex" \
    -v "$VOLUME:/home/node/.claude" \
    -v "$CLI_VOLUME:/home/node/.local" \
    -e TZ=Asia/Tokyo \
    -e "AWS_DEFAULT_REGION=$REGION" \
    -e "SURVEY_PHASE_DIR=$SURVEY_PHASE_DIR" \
    "$IMAGE" "$@"
}

# 初回の動作確認が済んでいれば、到達点として environment.json に記録する。
# 記録するのは「調査エージェントが確認結果を書いた」ことまで。中身は判断しない。
launch_record_verified() {
  local note="$OUT_DIR/_環境/00_動作確認.md"
  if [ -s "$note" ] && [ -z "$(jq -r '.setup.container_verified // empty' "$ENV_FILE")" ]; then
    echo ""
    env_mark_setup container_verified "コンテナの中で環境を確かめた"
  fi
}

# 同じ out/ に向いた調査コンテナが動いているか（--name で見る）。台帳と監査ログの書き手が競合するので、2 つ同時には走らせない
#   launch_container_running "$SURVEY_NAME"
launch_container_running() {
  [ -n "$(docker ps -q --filter "name=^/$1\$" 2>/dev/null)" ]
}

# エージェントの CLI だけを動かす（状態のボリューム 3 本だけ。一時キーも out/ も指示書も付けない）。認証の確認と、信頼の記録に使う
#   launch_agent_cli [-it] -- claude auth status --json
launch_agent_cli() {
  local -a flags=()
  while [ $# -gt 0 ]; do
    case "$1" in --) shift; break ;; *) flags+=("$1"); shift ;; esac
  done
  docker run --rm ${flags[@]+"${flags[@]}"} \
    -v "$CODEX_VOLUME:/home/node/.codex" \
    -v "$VOLUME:/home/node/.claude" \
    -v "$CLI_VOLUME:/home/node/.local" \
    "$IMAGE" "$@"
}

# エージェントの認証がボリュームに残っているか。各 CLI 自身に聞く（未認証なら終了コード 1。2026-09-12 に実測）。
# ファイル名を推測しない（Claude Code は .credentials.json、Codex は auth.json だが、置き場と名前は CLI の都合で変わりうる）
#   launch_agent_authenticated claude
launch_agent_authenticated() {
  case "$1" in
    claude) launch_agent_cli -- claude auth status --json >/dev/null 2>&1 ;;
    codex)  launch_agent_cli -- codex login status >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

# 非対話（claude -p）でも作業ディレクトリの settings.json（許可・フック）が効くよう、ワークスペースの信頼を記録する。
# 対話で起動したときは信頼の確認画面で同じ記録ができる。非対話ではその画面が出ず、未信頼だと allow の項目が無視される
# （「Ignoring N permissions.allow entries ... this workspace has not been trusted」。2026-09-12 に実測）。
# 記録先は CLAUDE_CONFIG_DIR の .claude.json（ボリュームの中）。他の項目はそのまま残す
launch_trust_workspace() {
  launch_agent_cli -- sh -c 'f=/home/node/.claude/.claude.json; [ -s "$f" ] || echo "{}" > "$f"; t=$(mktemp) && jq ".projects[\"/home/node/aws-survey\"].hasTrustDialogAccepted = true" "$f" > "$t" && cat "$t" > "$f" && rm -f "$t"'
}

# エージェントに渡すフラグ（モデルと effort。libexec/agents.sh）。environment.json の agent の値で、無ければ既定。
# 配列 AGENT_FLAGS に入れる。run / scan が CLI のコマンド列に並べる
#   launch_agent_flags claude; claude "${AGENT_FLAGS[@]}" ...
launch_agent_flags() {
  if [ "$1" = "$SURVEY_AGENT" ]; then
    agent_flags "$1" "$SURVEY_AGENT_MODEL" "$SURVEY_AGENT_EFFORT"
  else
    agent_flags "$1" "" ""
  fi
}

# エージェントのログインを済ませる（イメージ・ボリュームの用意 → 認証の確認 → 未認証なら端末でログイン → Claude は信頼の記録）。
# init（聞き取りの最後）・login・scan が使う。認証はボリュームに残るので、この対象では初回だけ実際のログインになる。
# 端末でなければ未認証のところで 1 を返す（案内は出す）。docker.sh を source してから呼ぶ（build_image）
#   launch_agent_login claude
launch_agent_login() {
  local agent="$1"
  command -v docker >/dev/null || { ui_err "docker コマンドが見つかりません。Docker Desktop を導入して起動してください。"; return 1; }
  docker_check_shared "$AWS_SURVEY_HOME" "$AWS_SURVEY_DIR" || return 1
  docker_awsarch
  ui_head "コンテナのイメージを用意する（$IMAGE, ${awsarch}）"
  docker image inspect "$IMAGE" >/dev/null 2>&1 || ui_text "初回は数分かかります。2 回目からは差分だけです。"
  build_image
  launch_prepare_volumes
  ui_head "$(agent_label "$agent") の認証を確かめる"
  if launch_agent_authenticated "$agent"; then
    ui_ok "$(agent_label "$agent") の認証は済んでいます"
  else
    if ! { [ -t 0 ] && [ -t 1 ]; }; then
      ui_err "$(agent_label "$agent") のログインがまだです。端末で $AWS_SURVEY_CMD login を実行してログインしてください。"
      return 1
    fi
    ui_warn "$(agent_label "$agent") のログインがまだです。先にログインだけ済ませます（この対象では初回だけ）"
    ui_text "画面の案内に従ってログインしてください。"
    echo ""
    case "$agent" in
      codex)  launch_agent_cli -it --name "${SURVEY_NAME}-login" -- codex login --device-auth || true ;;
      claude) launch_agent_cli -it --name "${SURVEY_NAME}-login" -- claude auth login || true ;;
    esac
    echo ""
    launch_agent_authenticated "$agent" || { ui_err "ログインを確かめられませんでした。$AWS_SURVEY_CMD login をもう一度実行してください。"; return 1; }
    ui_ok "$(agent_label "$agent") にログインしました"
  fi
  # 非対話の claude -p でも作業ディレクトリの許可とフックが効くように、ワークスペースの信頼を記録しておく
  [ "$agent" != claude ] || launch_trust_workspace || { ui_err "ワークスペースの信頼を記録できませんでした。"; return 1; }
  return 0
}
