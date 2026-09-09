#!/usr/bin/env bash
# EC2 の中を調べるための導入と鍵の管理（ホストで実行）。aws-survey ssh から呼ばれる。
#
#   aws-survey ssh setup --print [--user <名前>] [--log <名前>=<パス or glob>]... [--deny <glob>]... [--strict]
#                                   導入スクリプト（root で実行する 1 本の bash）を標準出力に出す。案内は標準エラー
#   aws-survey ssh setup <instance-id | Name タグ> [...]   SSM 経由の導入        （未実装）
#   aws-survey ssh list                                    登録済みホストと導入状態（未実装）
#   aws-survey ssh verify <host>                           コンテナからの自己診断  （未実装）
#   aws-survey ssh rotate                                  鍵の作り直しと再導入    （未実装）
#   aws-survey ssh remove <host>                           撤去                    （未実装）
#
# 鍵は $AWS_DIR/ssh/id_ed25519（対象ごとに 1 対、無ければ作る）。導入スクリプトに入るのは公開鍵だけ。
# 導入スクリプトの雛形は libexec/ec2/install.sh.tmpl。EC2 に残るものの名前は diag で統一する（設計文書の第 2 節）。
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"

EC2_DIR="$LIBEXEC_DIR/ec2"
TEMPLATE="$EC2_DIR/install.sh.tmpl"
KEY="$AWS_DIR/ssh/id_ed25519"

die() { ui_die "$@"; }

usage() { sed -n '4,10p' "$0" | sed 's/^# \{0,1\}//'; }

# ---- 引数 ----
SUB="${1:-}"; [ $# -gt 0 ] && shift
[ -n "$SUB" ] || { usage >&2; die "サブコマンドを指定してください（setup --print など）。"; }

TARGET=""; PRINT=0; USER_OPT=""; STRICT=false
LOGS_JSON='{}'; DENY_JSON='[]'

valid_user()  { [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; }
valid_log_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$ ]]; }
# 登録するログの glob。絶対パスで、ディレクトリ部分が確定している（* ? [ と .. を含まない）こと。
valid_log_glob() {
  local p="$1" dir name
  [[ "$p" == /* ]] || return 1
  [[ "$p" != *[[:cntrl:]]* ]] || return 1
  case "$p/" in */../*|*/./*|*//*) return 1 ;; esac
  dir=${p%/*}; name=${p##*/}
  [ -n "$name" ] || return 1
  case "$dir" in *'*'*|*'?'*|*'['*) return 1 ;; esac
  return 0
}
valid_deny_glob() {
  local p="$1"
  [[ "$p" == /* ]] || return 1
  [[ "$p" != *[[:cntrl:]]* ]] || return 1
  case "$p/" in */../*) return 1 ;; esac
  return 0
}

add_log() {
  local spec="$1" name glob
  case "$spec" in *=*) ;; *) die "--log は <名前>=<パス or glob> の形で指定してください: $spec" ;; esac
  name=${spec%%=*}; glob=${spec#*=}
  valid_log_name "$name" || die "--log の名前が不正です: ${name}（英数字で始まり、英数字 . _ - で 40 字まで）"
  valid_log_glob "$glob" || die "--log のパスが不正です: ${glob}（絶対パスで、ディレクトリ部分に * ? [ と .. を含めない）"
  LOGS_JSON=$(jq -c --arg k "$name" --arg v "$glob" '. + {($k): $v}' <<< "$LOGS_JSON")
}
add_deny() {
  valid_deny_glob "$1" || die "--deny のパターンが不正です: ${1}（絶対パスの glob。.. は含めない）"
  DENY_JSON=$(jq -c --arg v "$1" '. + [$v]' <<< "$DENY_JSON")
}

parse_setup_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --print)  PRINT=1; shift ;;
      --user)   [ $# -ge 2 ] || die "--user には名前を指定してください。"; USER_OPT="$2"; shift 2 ;;
      --user=*) USER_OPT="${1#--user=}"; shift ;;
      --log)    [ $# -ge 2 ] || die "--log には <名前>=<パス or glob> を指定してください。"; add_log "$2"; shift 2 ;;
      --log=*)  add_log "${1#--log=}"; shift ;;
      --deny)   [ $# -ge 2 ] || die "--deny には glob を指定してください。"; add_deny "$2"; shift 2 ;;
      --deny=*) add_deny "${1#--deny=}"; shift ;;
      --strict) STRICT=true; shift ;;
      -*) die "setup の不明なオプション: $1" ;;
      *)  [ -z "$TARGET" ] || die "対象は 1 つだけ指定してください: $TARGET と $1"; TARGET="$1"; shift ;;
    esac
  done
}

# ---- 鍵 ----
ensure_key() {
  if [ -f "$KEY.pub" ]; then return 0; fi
  command -v ssh-keygen >/dev/null || die "ssh-keygen が見つかりません（鍵を作れません）。"
  mkdir -p "$AWS_DIR/ssh" && chmod 700 "$AWS_DIR/ssh" || die "鍵の置き場を作れません: $AWS_DIR/ssh"
  # コメントに対象の名前を入れない（authorized_keys に残る）
  ssh-keygen -q -t ed25519 -N '' -C diag -f "$KEY" || die "鍵を作れませんでした: $KEY"
  ui_ok "鍵を作りました: ${KEY}${C_DIM}（対象ごとに 1 対。コンテナには読み取り専用で渡ります）${C_RESET}" >&2
}
read_pubkey() {
  local line
  line=$(head -n 1 "$KEY.pub")
  case "$line" in
    "ssh-ed25519 "*) ;;
    *) die "公開鍵が ssh-ed25519 ではありません: $KEY.pub" ;;
  esac
  PUBKEY="$(printf '%s' "$line" | cut -d' ' -f1,2) diag"
  [[ "$PUBKEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+\ diag$ ]] || die "公開鍵の形式を読めません: $KEY.pub"
}

# ---- 導入スクリプトの組み立て ----
# 雛形の @@GATEWAY_PY@@ / @@DIAG_ROOT_PY@@ / @@CONF_JSON@@ の行を本文に置き換え、
# それ以外の行の @@USER@@ / @@PUBKEY@@ を埋める。heredoc の終端行が本文に無いことは先に確かめる。
render_install() {
  local line f delim
  for f in gateway.py diag-root.py; do
    for delim in __DIAG_GATEWAY__ __DIAG_ROOT__ __DIAG_CONF__ __DIAG_CONF_PY__; do
      ! grep -qxF "$delim" "$EC2_DIR/$f" || die "$f に heredoc の終端行 $delim が含まれています。"
    done
  done
  shopt -u patsub_replacement 2>/dev/null || true
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      '@@GATEWAY_PY@@')   cat "$EC2_DIR/gateway.py" ;;
      '@@DIAG_ROOT_PY@@') cat "$EC2_DIR/diag-root.py" ;;
      '@@CONF_JSON@@')    printf '%s\n' "$CONF_JSON" ;;
      *)
        line=${line//@@USER@@/$SSH_USER}
        line=${line//@@PUBKEY@@/$PUBKEY}
        printf '%s\n' "$line" ;;
    esac
  done < "$TEMPLATE"
}

cmd_setup() {
  parse_setup_args "$@"
  if [ -n "$USER_OPT" ]; then
    valid_user "$USER_OPT" || die "--user が不正です: ${USER_OPT}（小文字英字か _ で始まり、小文字英数字 _ - で 32 字まで）"
    SSH_USER="$USER_OPT"
  fi
  [ -f "$TEMPLATE" ] || die "導入スクリプトの雛形がありません: $TEMPLATE"
  for f in gateway.py diag-root.py; do [ -f "$EC2_DIR/$f" ] || die "見つかりません: $EC2_DIR/$f"; done

  if [ "$PRINT" -ne 1 ]; then
    [ -n "$TARGET" ] || die "対象（instance-id か Name タグ）を指定してください。スクリプトだけ出すなら --print。"
    ui_title "aws-survey ssh setup"
    ui_kv "対象" "$TARGET"
    ui_kv "ログインユーザー" "$SSH_USER"
    echo ""
    ui_warn "SSM 経由の導入はまだ実装されていません。"
    ui_text "いまできるのは導入スクリプトを出すところまでです。出したスクリプトを対象の EC2 に root で実行してください。"
    next_cmd "$AWS_SURVEY_CMD ssh setup --print${USER_OPT:+ --user $USER_OPT} > install-diag.sh" "導入スクリプトを 1 本の bash として書き出します"
    exit 1
  fi

  # 案内は標準エラー、スクリプトは標準出力（> ファイル でそのまま使える）
  exec 3>&1 1>&2
  ui_title "aws-survey ssh setup --print"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "ログインユーザー" "$SSH_USER"
  [ -z "$TARGET" ] || ui_kv "対象" "${TARGET}${C_DIM}（--print では使いません）${C_RESET}"
  ensure_key
  read_pubkey
  CONF_JSON=$(jq -n --argjson strict "$STRICT" --argjson logs "$LOGS_JSON" --argjson deny "$DENY_JSON" \
                '{strict: $strict, logs: $logs, deny: $deny}')
  ui_kv "登録するログ" "$(jq -r 'to_entries | map("\(.key)=\(.value)") | join(" ") | if . == "" then "（なし。root 専用のログは実行時に自動検出）" else . end' <<< "$LOGS_JSON")"
  ui_kv "拒否パターン" "$(jq -r 'if length == 0 then "（追加なし）" else join(" ") end' <<< "$DENY_JSON")"
  ui_kv "strict" "$STRICT"
  local script bytes
  script=$(render_install) || die "導入スクリプトを組み立てられませんでした。"
  bash -n <(printf '%s\n' "$script") || die "組み立てた導入スクリプトが bash として読めません。"
  printf '%s\n' "$script" >&3
  bytes=$(printf '%s\n' "$script" | wc -c | tr -d ' ')
  echo ""
  ui_ok "導入スクリプトを標準出力に出しました${C_DIM}（$bytes バイト）${C_RESET}"
  ui_text "対象の EC2 に root で実行してください。末尾の HOSTKEY 行は known_hosts に使います。"
  ui_text "sshd の設定は sshd -t で検証してから reload します。検証に失敗したら元に戻して非ゼロで終わります。"
}

not_implemented() {
  ui_title "aws-survey ssh $1"
  ui_warn "$2 はまだ実装されていません。"
  ui_text "いま使えるのは $AWS_SURVEY_CMD ssh setup --print（導入スクリプトの書き出し）だけです。"
  exit 1
}

case "$SUB" in
  setup)  cmd_setup "$@" ;;
  list)   not_implemented list "登録済みホストの一覧" ;;
  verify) not_implemented verify "コンテナからの自己診断" ;;
  rotate) not_implemented rotate "鍵の作り直しと再導入" ;;
  remove) not_implemented remove "撤去" ;;
  -h|--help|help) usage ;;
  *) usage >&2; die "ssh の不明なサブコマンド: $SUB" ;;
esac
