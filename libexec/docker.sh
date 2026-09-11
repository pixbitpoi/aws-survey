#!/usr/bin/env bash
# 調査用イメージの用意。run（調査コンテナの起動）と lambda pull（展開に Python の実行環境として借りる）が source する。
# load-env.sh の後に読み込む。IMAGE がイメージ名、docker_awsarch が awsarch（aws CLI のアーキテクチャ）を決め、
# build_image が IMAGE をビルドする。表示は libexec/ui.sh の部品で組む。

IMAGE="${SURVEY_NAME}:latest"

docker_awsarch() {
  local arch
  arch=$(uname -m)
  case "$arch" in
    arm64|aarch64) awsarch=aarch64 ;;
    x86_64|amd64)  awsarch=x86_64  ;;
    *) ui_die "未対応のアーキテクチャ: $arch" ;;
  esac
}

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
