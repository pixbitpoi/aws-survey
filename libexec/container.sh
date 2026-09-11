#!/usr/bin/env bash
# 読み取り専用の一時キーだけを渡して、調査コンテナと同じイメージで 1 コマンドを走らせる部品。
# ホストの ls / ec2 / lambda（一覧）と ssh verify（ec2 --selftest）が source する。load-env.sh のあとに読み込む。
#
# 渡すのは $AWS_DIR の読み取り専用マウント（一時キー・鍵・接続設定）とリージョンだけ。out/ も code/ も
# 元プロファイルも渡さず、Claude Code / Codex の状態のボリュームも付けない。中で走るのは aws / jq / ec2 ラッパーで、
# 一時キーの Deny 付きセッションポリシーに縛られたまま読むだけになる。画面に出すだけで、ファイルには何も書かない。
# ホスト側のスクリプトを借りたいときは /x/ に読み取り専用でマウントする（lambda pull が抽出器を借りるのと同じ型。
# イメージには焼かず、調査エージェントには届かない）。
. "$LIBEXEC_DIR/docker.sh"
. "$LIBEXEC_DIR/keys.sh"

# 一時キーでコンテナに 1 コマンド走らせられる状態か。ファイルだけで判定し AWS は叩かない。
# 一時キーがあり、期限内で、読み取り専用であることを確かめ済み（段階 4 まで済んでいる）なら 0。
# 満たさなければ理由と次の 1 手を出して 1 を返す。
container_require_key() {
  key_state
  case "$KEY_STATE" in
    missing)
      ui_err "読み取り専用の一時キーがありません（$AWS_DIR/credentials）"
      next_cmd "$AWS_SURVEY_CMD" "準備の続きを順に進めます（一時キーの発行まで）"
      return 1 ;;
    expired)
      ui_err "一時キーが期限切れです（${KEY_EXP}${KEY_NOTE}）"
      next_cmd "$AWS_SURVEY_CMD credentials" "一時キーを発行し直します"
      return 1 ;;
    unknown)
      ui_err "一時キーの期限が読めません（${KEY_EXP:-記録なし}）"
      next_cmd "$AWS_SURVEY_CMD credentials" "一時キーを発行し直します"
      return 1 ;;
  esac
  if [ -z "$(jq -r '.setup.readonly_verified // empty' "$ENV_FILE")" ]; then
    ui_err "一時キーが読み取り専用であることを、まだ確かめていません"
    next_cmd "$AWS_SURVEY_CMD" "準備の続きを順に進めます（読み取り専用の検証まで）"
    return 1
  fi
  return 0
}

# イメージを用意する。docker の有無、Docker Desktop がマウントできる場所か（本体と一時キー）、アーキテクチャ、ビルド（毎回。全段キャッシュなら 1 秒未満）
container_prepare() {
  command -v docker >/dev/null || ui_die "docker コマンドが見つかりません。Docker Desktop を導入して起動してください。"
  docker_check_shared "$AWS_SURVEY_HOME" "$AWS_DIR" || return 1
  docker_awsarch
  build_image
}

# 1 コマンドを走らせる。標準出力はそのまま返す（呼ぶ側が読む）。端末は渡さない（-it 無し）。
#   container_run [-v <マウント>]... -- <コマンド>...
container_run() {
  local -a mounts=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -v) mounts+=(-v "$2"); shift 2 ;;
      --) shift; break ;;
      *) break ;;
    esac
  done
  docker run --rm \
    -v "$AWS_DIR:/home/node/.aws-claude:ro" \
    ${mounts[@]+"${mounts[@]}"} \
    -e TZ=Asia/Tokyo \
    -e "AWS_DEFAULT_REGION=$REGION" \
    "$IMAGE" "$@"
}

# アカウントで使っているリソースを一時キーで列挙する（libexec/inventory.sh をコンテナに貸して走らせる）。
# 出力は 1 行 1 JSON。引数はサービス名の絞り込み（無指定なら全部）。
#   container_inventory [ec2|lambda|...]
container_inventory() {
  container_run -v "$LIBEXEC_DIR/inventory.sh:/x/inventory.sh:ro" -- bash /x/inventory.sh --region "$REGION" "$@"
}
