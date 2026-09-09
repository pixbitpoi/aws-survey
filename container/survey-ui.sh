# 調査コンテナの表示部品。bashrc（ログイン画面）と survey-status が source する。
# この 2 つはログイン時に続けて 1 画面に出るので、記号・字下げ・色をここにまとめる。
#
# 見た目の規則:
#   ◆ 見出し（太字）   → いま何をすべきか（太字）   ⚠ 注意（黄）
#   状態は素の色、補足は薄い色、打つコマンドはシアン。本文は 4 字下げ。
# 装飾は標準出力が端末で NO_COLOR が無いときだけ。端末でなければ素の文字列。
# survey-status は調査エージェントも実行する。読むのは人だけではない。

S_BOLD=''; S_DIM=''; S_CYAN=''; S_YELLOW=''; S_RESET=''
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
  S_BOLD=$'\033[1m'; S_DIM=$'\033[2m'; S_CYAN=$'\033[36m'
  S_YELLOW=$'\033[33m'; S_RESET=$'\033[0m'
fi

# 見出し。第 2 引数を渡すと同じ行に状態を続ける（例: sui_head 資格情報 "残り 約 52 分"）
sui_head() { printf '%s◆ %s%s' "$S_BOLD" "$1" "$S_RESET"; [ -z "${2:-}" ] || printf '  %s' "$2"; echo ""; }
sui_warn() { printf '%s⚠ %s%s\n' "$S_YELLOW" "$1" "$S_RESET"; }   # 見出しの位置に出す注意
sui_next() { printf '    %s→ %s%s\n' "$S_BOLD" "$1" "$S_RESET"; } # いま何をすべきか
sui_line() { printf '    %s\n' "$1"; }                            # 状態そのもの
sui_text() { printf '    %s%s%s\n' "$S_DIM" "$1" "$S_RESET"; }    # 補足
sui_note() { printf '      %s%s%s\n' "$S_DIM" "$1" "$S_RESET"; }  # 直前の行に続く補足
sui_cmd()  { if [ -n "${2:-}" ]
             then printf '    %s%-21s%s %s%s%s\n' "$S_CYAN" "$1" "$S_RESET" "$S_DIM" "$2" "$S_RESET"
             else printf '    %s%s%s\n' "$S_CYAN" "$1" "$S_RESET"; fi; }
