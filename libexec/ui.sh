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

# ---- 簡潔表示 ----
# 引数なしの aws-survey が端末で続けて実行するとき、各コマンドは画面に 1 行だけを持つ（AWS_SURVEY_COMPACT=1）。
# 親（aws-survey）が回転する印付きの 1 行を描き続け、子（各コマンド）は見出しと ✔ でその行の文言を入れ替え、
# 補足・生の出力は記録（log）に残すだけ。⚠ ✗ はその行の上に残す（keep）。失敗したら親が記録を見せる。
# 画面に書くのは親の回転ループだけ（子の標準出力は親が記録へ向ける）。子が端末を使う（質問する・ログインする）
# あいだは ui_pause / ui_resume で回転を止め、/dev/tty に直接書く。
# AWS_SURVEY_COMPACT=1 でも受け渡しの場所（AWS_SURVEY_SPIN_DIR）が無いときは「静か」なだけ:
# 見出し・補足・表は出さず、✔ ⚠ ✗ だけを出す（run のように端末を渡すコマンド向け）。
UI_COMPACT="${AWS_SURVEY_COMPACT:-0}"
UI_SPIN_DIR="${AWS_SURVEY_SPIN_DIR:-}"
ui_quiet()   { [ "$UI_COMPACT" = 1 ]; }
ui_compact() { [ "$UI_COMPACT" = 1 ] && [ -n "$UI_SPIN_DIR" ]; }
ui_log()     { [ -n "$UI_SPIN_DIR" ] || return 0; printf '%s\n' "$1" >> "$UI_SPIN_DIR/log"; }
ui_spin_msg()  { printf '%s' "$1" > "$UI_SPIN_DIR/msg.tmp" && mv -f "$UI_SPIN_DIR/msg.tmp" "$UI_SPIN_DIR/msg"; }
ui_spin_keep() { printf '%s\n' "$1" >> "$UI_SPIN_DIR/keep"; }
ui_pause()   { ui_compact || return 0; touch "$UI_SPIN_DIR/pause"; sleep 0.25; }
ui_resume()  { ui_compact || return 0; rm -f "$UI_SPIN_DIR/pause"; }
# 端末に直接書く（簡潔表示では標準出力が記録へ向いているため）。質問の文などに使う
ui_tty()     { if ui_compact; then printf '%s' "$1" > /dev/tty; else printf '%s' "$1"; fi; }

ui_title() { ui_log "◆ $1"; ui_quiet && return 0; printf '%s◆ %s%s\n' "$C_BOLD" "$1" "$C_RESET"; }                 # 画面の見出し
ui_head()  { ui_log "◆ $1"; ui_compact && { ui_spin_msg "${1#[0-9]*/[0-9]* }"; return 0; }; ui_quiet && return 0
             printf '  %s◆ %s%s\n' "$C_BOLD" "$1" "$C_RESET"; }                # 区切り・質問の見出し（簡潔表示では回転する行の文言。1/7 の番号は落とす）
ui_text()  { ui_log "  $1"; ui_quiet && return 0; printf '    %s%s%s\n' "$C_DIM" "$1" "$C_RESET"; }                # 補足
ui_raw()   { ui_log "$1"; ui_quiet && return 0; printf '%s\n' "$1" | sed "s/^/      ${C_DIM}/; s/\$/${C_RESET}/"; } # 生の出力（エラー文など）を薄く字下げ
ui_ok()    { ui_log "✔ $1"; ui_compact && { ui_spin_msg "$1"; return 0; }; printf '  %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
ui_warn()  { ui_log "⚠ $1"; ui_compact && { ui_spin_keep "⚠ $1"; return 0; }; printf '  %s⚠ %s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
ui_err()   { ui_log "✗ $1"; ui_compact && { ui_spin_keep "✗ $1"; return 0; }; printf '  %s✗ %s%s\n' "$C_RED" "$1" "$C_RESET"; }
ui_skip()  { ui_log "－ $1"; ui_quiet && return 0; printf '  %s－ %s%s\n' "$C_DIM" "$1" "$C_RESET"; }
ui_cmd()   { printf '%s%s%s' "$C_CYAN" "$1" "$C_RESET"; }
# 直前の n 行を消して、カーソルをその先頭に置く（端末のときだけ）
ui_erase() { [ "$UI_TTY" = 1 ] || return 0; printf '\033[%dA\033[J' "$1"; }

# 進行中の 1 行を同じ場所に書き替え続ける（端末のときだけ）。終わったら ui_status_done で消す。
# 端末でなければ何も出さない。長い処理の途中経過は、あとから読む記録には要らない。簡潔表示では回転する行の文言にする
ui_status()      { ui_compact && { ui_spin_msg "$1"; return 0; }; [ "$UI_TTY" = 1 ] || return 0; printf '\r    %s%s%s\033[K' "$C_DIM" "$1" "$C_RESET"; }
ui_status_done() { ui_compact && return 0; [ "$UI_TTY" = 1 ] || return 0; printf '\r\033[K'; }

# 秒数を読める長さにする。ui_duration <秒>
ui_duration() { [ "$1" -lt 60 ] && { echo "$1 秒"; return 0; }; echo "$(( $1 / 60 )) 分 $(( $1 % 60 )) 秒"; }

# ロールの名前か ARN から「誰に貸すか」を言い換える。Identity Center のロール（AWSReservedSSO_<権限セット>_<識別子>）は権限セット名で言う
ui_role_audience() {
  local r="${1##*/}" ps
  case "$r" in
    AWSReservedSSO_*_*) ps=${r#AWSReservedSSO_}; echo "権限セット ${ps%_*} でログインした人なら誰でも" ;;
    *) echo "ロール ${r} を借りている人なら誰でも" ;;
  esac
}

# エージェント名の言い換え。空なら両方
agent_label() { case "${1:-}" in claude) echo "Claude Code" ;; codex) echo "Codex" ;; *) echo "Claude Code / Codex" ;; esac; }

# Identity Center（SSO）のログインを表す ARN か。セッション ARN とパス付きのロール ARN のどちらも受ける。
# このログインのセッションには aws:MultiFactorAuthPresent が付かないため、信頼ポリシーの MFA 条件を満たせない（init・role・credentials で使う）
is_sso_arn() {
  case "$1" in
    *:assumed-role/AWSReservedSSO_*|*:role/*AWSReservedSSO_*) return 0 ;;
    *) return 1 ;;
  esac
}

# ロールを借りた状態でログインしているか（Identity Center のログインも assumed-role）。そこから調査用ロールを借りると
# AWS の決まり（ロールチェーン）で一時キーは 1 時間が上限になり、ロール側の MaxSessionDuration を延ばしても変わらない。
# 判定は sts get-caller-identity の Arn で行う（init は principal_arn で行う。is_chained_principal）
CHAIN_MAX_SECONDS=3600
is_chained_arn() { case "$1" in *:assumed-role/*) return 0 ;; *) return 1 ;; esac; }
# 貸す相手（principal_arn）から見て、そのログインが借りたロールか。セッション ARN でもロール ARN でもロールを借りてログインしている
is_chained_principal() { case "$1" in *:assumed-role/*|*:role/*) return 0 ;; *) return 1 ;; esac; }
# 上限の説明。ui_chain_limit <environment.json の duration_seconds>
ui_chain_limit() {
  ui_warn "いまのログインは借りたロールなので、ここから借りる一時キーは 1 時間が上限です（environment.json は $(( $1 / 60 )) 分）"
  ui_text "AWS の決まり（ロールチェーン）で、調査用ロールの上限（MaxSessionDuration）を延ばしても変わりません。Identity Center のログインも同じです。"
  ui_text "1 時間を超える長さにするには、IAM ユーザーの長期キー（MFA 付き）でログインする経路が要ります。"
}

# ホームフォルダの下のパスを ~ で省略する（画面に出すときだけ。ファイルやコマンドに書く値には使わない）
ui_path() {
  local p="$1"
  if [ -n "${HOME:-}" ] && [ "$HOME" != / ]; then
    case "$p" in
      "$HOME") p='~' ;;
      "$HOME"/*) p="~${p#"$HOME"}" ;;
    esac
  fi
  printf '%s' "$p"
}

# 表示幅。3 バイト文字（日本語）を幅 2 とみなす
ui_width() {
  local chars bytes
  chars=${#1}; bytes=$(LC_ALL=C; printf '%s' "$1" | wc -c | tr -d ' ')
  echo $(( chars + (bytes - chars) / 2 ))
}
# ラベルと値。ラベルは幅 16 に揃える。値がホームフォルダの下のパスなら ~ で省略する。ui_kv <ラベル> <値>
ui_kv() {
  local w pad v; w=$(ui_width "$1"); pad=$(( 18 - w )); [ "$pad" -ge 2 ] || pad=2
  v=$(ui_path "$2")
  ui_log "  $1  $v"; ui_quiet && return 0
  printf '    %s%s%*s%s%s\n' "$C_DIM" "$1" "$pad" '' "$C_RESET" "$v"
}

# 次に打つコマンド。next_cmd が見出し付きの 1 本目、also_cmd がその続き。説明は「何をするか」を利用者の言葉で書く。
# AWS_SURVEY_CHAIN=1 のときは黙る。引数なしの aws-survey が続けて実行しているときで、
# 次の案内は、実行後にもう一度判定し直した aws-survey 側が出す。
next_cmd() { ui_log "次に打つコマンド: $1  ${2:-}"; [ "${AWS_SURVEY_CHAIN:-0}" != 1 ] || return 0
             printf '  %s次に打つコマンド%s\n' "$C_BOLD" "$C_RESET"; also_cmd "$@"; }
also_cmd() { ui_log "  $1  ${2:-}"; [ "${AWS_SURVEY_CHAIN:-0}" != 1 ] || return 0
             printf '    %s%-28s%s' "$C_CYAN" "$1" "$C_RESET"; [ -z "${2:-}" ] || printf '  %s%s%s' "$C_DIM" "$2" "$C_RESET"; echo ""; }

# 失敗して終わる。標準エラーが端末なら赤。簡潔表示では残す行にして親に見せてもらう
ui_die() {
  ui_log "✗ $*"
  if ui_compact; then ui_spin_keep "✗ $*"; exit 1; fi
  echo "" >&2
  if [ -t 2 ] && [ -n "$C_RED" ]; then printf '%s✗ %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; else echo "✗ $*" >&2; fi
  exit 1
}

# ---- 簡潔表示の親側 ----
# 回転する 1 行を描き続ける。ui_spin_loop <受け渡しの場所>。keep に足された行は先に出して残し、pause があるあいだは何も描かない。
# 親は & で起動し、子が終わったら stop を置いて待つ（ui_spin_end）。ループは stop を見たら keep の残りを出し、行を消して終わる。
# kill で止めると、keep の行を出している最中に切られて出し直しが二重に出ることがあるので、止め方は stop で揃える。
ui_spin_loop() {
  local dir="$1" i=0 n=0 total line msg drawn=0
  local -a frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
  trap 'exit 0' TERM
  while :; do
    total=$(wc -l < "$dir/keep" 2>/dev/null | tr -d ' ')
    if [ "${total:-0}" -gt "$n" ]; then
      [ "$drawn" -eq 0 ] || printf '\r\033[K'
      sed -n "$((n + 1)),${total}p" "$dir/keep" | while IFS= read -r line; do
        case "$line" in
          "⚠ "*) printf '  %s%s%s\n' "$C_YELLOW" "$line" "$C_RESET" ;;
          "✗ "*) printf '  %s%s%s\n' "$C_RED" "$line" "$C_RESET" ;;
          *)     printf '  %s\n' "$line" ;;
        esac
      done
      n=$total; drawn=0
      printf '%s' "$n" > "$dir/printed"
    fi
    if [ -e "$dir/stop" ]; then
      [ "$drawn" -eq 0 ] || printf '\r\033[K'
      exit 0
    fi
    if [ -e "$dir/pause" ]; then
      [ "$drawn" -eq 0 ] || { printf '\r\033[K'; drawn=0; }
    else
      msg=$(cat "$dir/msg" 2>/dev/null)
      printf '\r\033[K  %s%s%s %s' "$C_CYAN" "${frames[i % 10]}" "$C_RESET" "$msg"
      drawn=1; i=$((i + 1))
    fi
    sleep 0.1
  done
}
# 回転を止めて行を消す。ループに stop を伝えて待つので、keep の行はループ側が出し切る。ループが応じないときだけ kill して、
# 出していない keep の行をこちらで出す。ui_spin_end <受け渡しの場所> <ループの PID>
ui_spin_end() {
  local dir="$1" pid="$2" line n total i
  touch "$dir/stop"
  for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
  kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true
  printf '\r\033[K'
  n=$(cat "$dir/printed" 2>/dev/null || true); n=${n:-0}
  total=$(wc -l < "$dir/keep" 2>/dev/null | tr -d ' '); total=${total:-0}
  [ "$total" -gt "$n" ] || return 0
  sed -n "$((n + 1)),${total}p" "$dir/keep" | while IFS= read -r line; do
    case "$line" in
      "⚠ "*) printf '  %s%s%s\n' "$C_YELLOW" "$line" "$C_RESET" ;;
      "✗ "*) printf '  %s%s%s\n' "$C_RED" "$line" "$C_RESET" ;;
      *)     printf '  %s\n' "$line" ;;
    esac
  done
}

# 1 コマンドを回転する 1 行に畳んで走らせる（親側の入口）。説明 → 子の見出しと ✔ の文言に入れ替わり、終わったら ✔ の 1 行になる。
# 失敗したら ✗ と、記録しておいた出力の全部を見せる。引数なしの aws-survey が各 1 手に、key_ensure（libexec/keys.sh）が
# 一時キーの発行し直しに使う。標準出力が端末のときに呼ぶ（$(...) の中では呼ばない。回転の印が結果に混ざる）。
#   ui_fold <説明> <失敗したときに出す名前> <コマンド>...
ui_fold() {
  local desc="$1" name="$2" rc dir spid; shift 2
  dir=$(mktemp -d) || ui_die "一時ディレクトリを作れません。"
  printf '%s' "$desc" > "$dir/msg"; : > "$dir/log"; : > "$dir/keep"
  ui_spin_loop "$dir" &
  spid=$!
  AWS_SURVEY_CHAIN=1 AWS_SURVEY_COMPACT=1 AWS_SURVEY_SPIN_DIR="$dir" "$@" >> "$dir/log" 2>&1
  rc=$?
  ui_spin_end "$dir" "$spid"
  if [ "$rc" -eq 0 ]; then
    ui_ok "$(cat "$dir/msg")"
  else
    ui_err "$name が失敗しました"
    echo ""
    ui_text "そのときの出力:"
    ui_raw "$(cat "$dir/log")"
  fi
  rm -rf "$dir"
  return "$rc"
}

# 1 行読む。端末なら待ち続け、端末でなければ数秒で諦める（エージェントの実行環境では標準入力が閉じず、待つと止まる）。
# 読めなければ 1 を返す。
read_line() {
  if [ -t 0 ]; then read -r "$1"; else read -t 5 -r "$1"; fi
}
