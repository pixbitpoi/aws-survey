#!/usr/bin/env bash
# 表示の共通部品。aws-survey（ディスパッチャ）と libexec/*.sh（load-env.sh 経由）が source する。
#
# 見た目の規則:
#   ◆ 見出し（太字）      ✔ 済んだこと（緑）     ⚠ 注意（黄）     ✗ 失敗（赤）
#   補足は薄い色、コマンドはシアン。利用者向けの文に内部の値名（own_role など）を書かない。
# 色と記号の装飾は標準出力が端末で NO_COLOR が無いときだけ。端末でなければ素の文字列（テスト・エージェント実行環境）。

UI_TTY=0; C_BOLD=''; C_DIM=''; C_CYAN=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_RESET=''
if [ -t 1 ]; then
  UI_TTY=1
  if [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
    C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RESET=$'\033[0m'
  fi
fi

ui_title() { printf '%s◆ %s%s\n' "$C_BOLD" "$1" "$C_RESET"; }                 # 画面の見出し
ui_head()  { printf '  %s◆ %s%s\n' "$C_BOLD" "$1" "$C_RESET"; }               # 区切り・質問の見出し
ui_text()  { printf '    %s%s%s\n' "$C_DIM" "$1" "$C_RESET"; }                # 補足
ui_raw()   { printf '%s\n' "$1" | sed "s/^/      ${C_DIM}/; s/\$/${C_RESET}/"; } # 生の出力（エラー文など）を薄く字下げ
ui_ok()    { printf '  %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
ui_warn()  { printf '  %s⚠ %s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
ui_err()   { printf '  %s✗ %s%s\n' "$C_RED" "$1" "$C_RESET"; }
ui_skip()  { printf '  %s－ %s%s\n' "$C_DIM" "$1" "$C_RESET"; }
ui_cmd()   { printf '%s%s%s' "$C_CYAN" "$1" "$C_RESET"; }
# 直前の n 行を消して、カーソルをその先頭に置く（端末のときだけ）
ui_erase() { [ "$UI_TTY" = 1 ] || return 0; printf '\033[%dA\033[J' "$1"; }

# 進行中の 1 行を同じ場所に書き替え続ける（端末のときだけ）。終わったら ui_status_done で消す。
# 端末でなければ何も出さない。長い処理の途中経過は、あとから読む記録には要らない。
ui_status()      { [ "$UI_TTY" = 1 ] || return 0; printf '\r    %s%s%s\033[K' "$C_DIM" "$1" "$C_RESET"; }
ui_status_done() { [ "$UI_TTY" = 1 ] || return 0; printf '\r\033[K'; }

# 秒数を読める長さにする。ui_duration <秒>
ui_duration() { [ "$1" -lt 60 ] && { echo "$1 秒"; return 0; }; echo "$(( $1 / 60 )) 分 $(( $1 % 60 )) 秒"; }

# 表示幅。3 バイト文字（日本語）を幅 2 とみなす
ui_width() {
  local chars bytes
  chars=${#1}; bytes=$(LC_ALL=C; printf '%s' "$1" | wc -c | tr -d ' ')
  echo $(( chars + (bytes - chars) / 2 ))
}
# ラベルと値。ラベルは幅 16 に揃える。ui_kv <ラベル> <値>
ui_kv() {
  local w pad; w=$(ui_width "$1"); pad=$(( 18 - w )); [ "$pad" -ge 2 ] || pad=2
  printf '    %s%s%*s%s%s\n' "$C_DIM" "$1" "$pad" '' "$C_RESET" "$2"
}

# 次に打つコマンド。next_cmd が見出し付きの 1 本目、also_cmd がその続き。説明は「何をするか」を利用者の言葉で書く。
# AWS_SURVEY_CHAIN=1 のときは黙る。引数なしの aws-survey が続けて実行しているときで、
# 次の案内は、実行後にもう一度判定し直した aws-survey 側が出す。
next_cmd() { [ "${AWS_SURVEY_CHAIN:-0}" != 1 ] || return 0
             printf '  %s次に打つコマンド%s\n' "$C_BOLD" "$C_RESET"; also_cmd "$@"; }
also_cmd() { [ "${AWS_SURVEY_CHAIN:-0}" != 1 ] || return 0
             printf '    %s%-28s%s' "$C_CYAN" "$1" "$C_RESET"; [ -z "${2:-}" ] || printf '  %s%s%s' "$C_DIM" "$2" "$C_RESET"; echo ""; }

# 失敗して終わる。標準エラーが端末なら赤
ui_die() {
  echo "" >&2
  if [ -t 2 ] && [ -n "$C_RED" ]; then printf '%s✗ %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; else echo "✗ $*" >&2; fi
  exit 1
}
