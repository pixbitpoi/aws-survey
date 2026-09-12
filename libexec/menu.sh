#!/usr/bin/env bash
# 矢印キーで 1 つ（choose_menu）か複数（choose_multi）選ぶメニュー。aws-survey（init）と、一覧から選ばせるコマンド（ec2 / lambda）が source する。
# ui.sh の色（C_CYAN など）と ui_die を使うので、ui.sh のあとに読み込む。
#
# ⚠️ choose_menu は EXIT トラップを張って（カーソルを戻すため）決定後に外す。呼ぶ側の `trap ... EXIT` を潰すので、
#    一時ファイルの後片付けなどのトラップを張る前に呼ぶこと。

# 標準入力と標準出力の両方が端末のときだけメニューを出せる。端末でなければ呼ぶ側は案内だけ出す
menu_available() { [ -t 0 ] && [ "$UI_TTY" = 1 ]; }

# ↑↓ / j k で移動、1〜9 で番号へ、Enter で決定、q または Ctrl-D で中断。描画は ANSI（色・カーソル移動・消去）だけ。
# choose_menu <結果の変数名> <問いの文> <初期カーソル（0 始まり）> <候補>...  決定後はメニューを消して 1 行に畳む（呼ぶ側が ui_ok を出す）。
# 色は標準出力が端末で NO_COLOR が無いときだけ。
choose_menu() {
  local var="$1" title="$2" cursor="$3"; shift 3
  local -a entries=("$@")
  local count=${#entries[@]} i key rest
  # 描く行数 = 候補 + ヒント 1 行。見出しは動かさない
  draw_menu() {
    local i
    for i in "${!entries[@]}"; do
      if [ "$i" = "$cursor" ]; then
        printf '\033[K  %s❯ %s%s\n' "$C_CYAN" "${entries[i]}" "$C_RESET"
      else
        printf '\033[K  %s  %s%s\n' "$C_DIM" "${entries[i]}" "$C_RESET"
      fi
    done
    printf '\033[K  %s↑↓ 移動 · Enter 決定 · q 中断%s\n' "$C_DIM" "$C_RESET"
  }
  show_cursor() { printf '\033[?25h'; }
  trap show_cursor EXIT
  printf '\033[?25l'
  printf '  %s❯%s %s\n' "$C_CYAN" "$C_RESET" "$title"
  draw_menu
  while :; do
    IFS= read -rsn1 key || { show_cursor; echo ""; ui_die "${title}: 選択が中断されました。"; }
    case "$key" in
      $'\e')
        read -rsn2 -t 0.2 rest || rest=
        case "$rest" in
          '[A') [ "$cursor" -le 0 ] || cursor=$((cursor-1)) ;;
          '[B') [ "$cursor" -ge $((count-1)) ] || cursor=$((cursor+1)) ;;
        esac
        ;;
      k) [ "$cursor" -le 0 ] || cursor=$((cursor-1)) ;;
      j) [ "$cursor" -ge $((count-1)) ] || cursor=$((cursor+1)) ;;
      [1-9]) [ "$key" -gt "$count" ] || cursor=$((key-1)) ;;
      '') break ;;
      q) show_cursor; echo ""; ui_die "${title}: 選択が中断されました。" ;;
    esac
    printf '\033[%dA' "$((count+1))"
    draw_menu
  done
  printf -v "$var" '%s' "${entries[cursor]}"
  # 問い + 候補 + ヒント + Enter で bash が出す改行 1 行を消して、呼ぶ側の結果 1 行に畳む
  printf '\033[%dA' "$((count+3))"
  printf '\033[J'
  show_cursor
  trap - EXIT
}

# 複数選ぶ。↑↓ / j k で移動、Space で印を付け外し、a で全部、Enter で決定、q または Ctrl-D で中断。
# choose_multi <結果の変数名> <問いの文> <候補>...  結果は選んだ候補の番号（0 始まり）を空白区切りで入れる。
# 1 つも選ばずに Enter は、いまカーソルのある 1 つを選んだことにする（1 つだけならメニューと同じ手数）。
choose_multi() {
  local var="$1" title="$2"; shift 2
  local -a entries=("$@") marks=()
  local count=${#entries[@]} i key rest cursor=0 picked=""
  for i in "${!entries[@]}"; do marks[i]=0; done
  draw_multi() {
    local i m
    for i in "${!entries[@]}"; do
      if [ "${marks[i]}" = 1 ]; then m="${C_GREEN}◉${C_RESET}"; else m="${C_DIM}○${C_RESET}"; fi
      if [ "$i" = "$cursor" ]; then
        printf '\033[K  %s❯%s %s %s%s%s\n' "$C_CYAN" "$C_RESET" "$m" "$C_CYAN" "${entries[i]}" "$C_RESET"
      else
        printf '\033[K    %s %s%s%s\n' "$m" "$C_DIM" "${entries[i]}" "$C_RESET"
      fi
    done
    printf '\033[K  %s↑↓ 移動 · Space 選ぶ · a 全部 · Enter 決定 · q 中断%s\n' "$C_DIM" "$C_RESET"
  }
  show_cursor() { printf '\033[?25h'; }
  trap show_cursor EXIT
  printf '\033[?25l'
  printf '  %s❯%s %s\n' "$C_CYAN" "$C_RESET" "$title"
  draw_multi
  while :; do
    IFS= read -rsn1 key || { show_cursor; echo ""; ui_die "${title}: 選択が中断されました。"; }
    case "$key" in
      $'\e')
        read -rsn2 -t 0.2 rest || rest=
        case "$rest" in
          '[A') [ "$cursor" -le 0 ] || cursor=$((cursor-1)) ;;
          '[B') [ "$cursor" -ge $((count-1)) ] || cursor=$((cursor+1)) ;;
        esac
        ;;
      k) [ "$cursor" -le 0 ] || cursor=$((cursor-1)) ;;
      j) [ "$cursor" -ge $((count-1)) ] || cursor=$((cursor+1)) ;;
      ' ') if [ "${marks[cursor]}" = 1 ]; then marks[cursor]=0; else marks[cursor]=1; fi ;;
      a) for i in "${!entries[@]}"; do marks[i]=1; done ;;
      '') break ;;
      q) show_cursor; echo ""; ui_die "${title}: 選択が中断されました。" ;;
    esac
    printf '\033[%dA' "$((count+1))"
    draw_multi
  done
  for i in "${!entries[@]}"; do [ "${marks[i]}" = 1 ] && picked="${picked:+$picked }$i"; done
  [ -n "$picked" ] || picked="$cursor"
  printf -v "$var" '%s' "$picked"
  printf '\033[%dA' "$((count+3))"
  printf '\033[J'
  show_cursor
  trap - EXIT
}
