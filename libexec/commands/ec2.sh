#!/usr/bin/env bash
# EC2 インスタンスの一覧から 1 台選んで、中を調べる準備へ進む（ホストで実行）。aws-survey ec2 から呼ばれる。
#
#   aws-survey ec2                  一覧を出し、端末なら矢印キーで 1 台選ぶ。
#                                   未登録なら「診断ゲートウェイを導入して、中を調べる準備をする」（ssh setup）、
#                                   登録済みなら「調査へ進む」「接続を確かめる」（ssh verify）「設定を外す」（ssh remove）を選ぶ。
#                                   端末でなければ一覧と、次に打つコマンドの案内だけ
#
# 一覧は調査コンテナと同じイメージの中で、読み取り専用の一時キーが読む（libexec/container.sh の container_inventory）。
# 導入・撤去・確認は元プロファイルの仕事なので、libexec/commands/ssh.sh に exec して渡す（案内の末尾もそちらが出す）。
# 1 台ずつ手で進めるなら、今までどおり ssh setup <instance-id | Name タグ> / ssh verify <host> / ssh remove <host>。
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/container.sh"
. "$LIBEXEC_DIR/menu.sh"

SSH_SH="$LIBEXEC_DIR/commands/ssh.sh"
usage() { sed -n '4,8p' "$0" | sed 's/^# \{0,1\}//'; }
[ $# -eq 0 ] || case "$1" in
  -h|--help) usage; exit 0 ;;
  *) ui_die "ec2 に引数はありません。1 台を指定して進めるなら $AWS_SURVEY_CMD ssh setup <instance-id | Name タグ>。" ;;
esac

ui_title "aws-survey ec2"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "リージョン" "$REGION"
echo ""
container_require_key || exit 1
container_prepare || exit 1
ui_text "調査コンテナの中から、読み取り専用の一時キーで読みます（画面に出すだけで、何も書きません）。"

inv=$(container_inventory ec2)
[ -n "$inv" ] || { echo ""; ui_err "列挙できませんでした（上の出力を確認してください）"; exit 1; }
st=$(jq -r 'select(.kind == "service" and .service == "ec2") | .status' <<< "$inv" | head -n 1)
if [ "$st" != ok ]; then
  echo ""
  ui_err "EC2 の一覧を読めませんでした"
  ui_raw "$(jq -r 'select(.kind == "service" and .service == "ec2") | .error // ""' <<< "$inv" | head -n 1)"
  exit 1
fi
ssm_unknown=0
jq -e 'select(.kind == "service" and .service == "ssm" and .status != "ok")' <<< "$inv" >/dev/null && ssm_unknown=1

# 登録済みホスト（instance-id → 別名）
registered=$(jq -r '.ssh.hosts // {} | to_entries[] | "\(.value.instance_id)\t\(.key)"' "$ENV_FILE")
alias_of() { awk -F'\t' -v id="$1" '$1 == id { print $2 }' <<< "$registered"; }

ids=(); labels=(); aliases=(); states=(); ssms=()
while IFS=$'\t' read -r id name state ssm type; do
  [ -n "$id" ] || continue
  alias=$(alias_of "$id")
  ids+=("$id"); aliases+=("$alias"); states+=("$state"); ssms+=("$ssm")
  labels+=("${id}${name:+  $name}  ${state}  SSM ${ssm}${type:+  $type}${alias:+  登録済み: $alias}")
done < <(jq -r 'select(.kind == "item") | [.id, .name, .state, (.extra.ssm // "?"), (.extra.type // "")] | @tsv' <<< "$inv")

echo ""
ui_head "EC2 インスタンス（${#ids[@]}）"
[ "$ssm_unknown" -eq 0 ] || ui_warn "SSM の管理下かどうかは読めませんでした（ssm の権限が無い）。導入には SSM の管理下（Online）が要ります"
if [ ${#ids[@]} -eq 0 ]; then
  ui_skip "なし"
  exit 0
fi

if ! menu_available; then
  for i in "${!ids[@]}"; do
    if [ "${states[i]}" = running ]; then ui_ok "${labels[i]}"; else ui_skip "${labels[i]}"; fi
  done
  echo ""
  next_cmd "$AWS_SURVEY_CMD ssh setup <instance-id | Name タグ>" "診断ゲートウェイを導入して、中を調べる準備をします（SSM 管理下の running に限る）"
  if [ -n "$registered" ]; then
    also_cmd "$AWS_SURVEY_CMD ssh verify <host>" "調査コンテナから実際に接続して確かめます"
    also_cmd "$AWS_SURVEY_CMD ssh remove <host>" "EC2 から撤去し、タグと記録を消します"
  fi
  also_cmd "$AWS_SURVEY_CMD" "調査へ進みます"
  exit 0
fi

choose_menu picked "中を調べたいインスタンスを選んでください" 0 "${labels[@]}"
sel=""
for i in "${!labels[@]}"; do [ "${labels[i]}" != "$picked" ] || sel=$i; done
[ -n "$sel" ] || ui_die "選択を読めませんでした。"
ui_ok "$picked"
id=${ids[sel]}; alias=${aliases[sel]}
echo ""

if [ -z "$alias" ]; then
  if [ "${states[sel]}" != running ] || [ "${ssms[sel]}" != Online ]; then
    ui_warn "導入には running で SSM の管理下（Online）であることが要ります（${states[sel]} / SSM ${ssms[sel]}）"
    [ "${ssms[sel]}" = "?" ] || exit 1
    ui_text "SSM の状態を読めていないので、このまま進めて ssh setup 側の判定に任せます。"
  fi
  choose_menu act "$id に何をしますか？" 0 \
    "診断ゲートウェイを導入して、中を調べる準備をする" "やめる"
  case "$act" in
    "診断ゲートウェイ"*) ui_ok "$act"; echo ""; exec "$SSH_SH" setup "$id" ;;
    *) ui_text "ここで止めます。"; exit 0 ;;
  esac
fi

choose_menu act "${alias}（${id}）に何をしますか？" 0 \
  "調査へ進む" "調査コンテナから接続を確かめる" "設定を外す（EC2 から撤去して登録を消す）" "やめる"
ui_ok "$act"
echo ""
case "$act" in
  "調査へ進む")        exec "$AWS_SURVEY_HOME/bin/aws-survey" ;;
  "調査コンテナから"*) exec "$SSH_SH" verify "$alias" ;;
  "設定を外す"*)       exec "$SSH_SH" remove "$alias" ;;
  *) ui_text "ここで止めます。"; exit 0 ;;
esac
