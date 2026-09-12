#!/usr/bin/env bash
# アカウントで使っているリソースの一覧（ホストで実行）。aws-survey ls から呼ばれる。
#
#   aws-survey ls [サービス名...] [--json]
#                                   ec2 lambda vpc s3 rds ecs elb cloudfront を固定順で並べる。無指定なら全部
#                                   --json は 1 行 1 JSON のまま出す
#
# 列挙は調査コンテナと同じイメージの中で、読み取り専用の一時キーが行う（libexec/container.sh の container_inventory）。
# ホストの元プロファイルは使わない。読むだけで、out/ にも他のファイルにも書かない。
# 何を調べるかを利用者が決めるための画面で、調査エージェントに渡す棚卸しではない（それは調査エージェントが白紙から作る）。
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/container.sh"

usage() { sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//'; }

JSON=0; PICK=()
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) ui_die "ls の不明なオプション: $1" ;;
    *) PICK+=("$1"); shift ;;
  esac
done

# 見出しの言い換え。サービス名はそのまま出さない
service_label() {
  case "$1" in
    ec2)        echo "EC2 インスタンス" ;;
    lambda)     echo "Lambda 関数" ;;
    vpc)        echo "VPC" ;;
    s3)         echo "S3 バケット" ;;
    rds)        echo "RDS（インスタンスとクラスター）" ;;
    ecs)        echo "ECS クラスター" ;;
    elb)        echo "ロードバランサー" ;;
    cloudfront) echo "CloudFront ディストリビューション" ;;
    *)          echo "$1" ;;
  esac
}

# 1 サービス分を描く。<NDJSON> <service>
render_service() {
  local inv="$1" svc="$2" st cnt err id name state extra
  st=$(jq -r --arg s "$svc" 'select(.kind == "service" and .service == $s) | .status' <<< "$inv" | head -n 1)
  [ -n "$st" ] || return 0
  cnt=$(jq -r --arg s "$svc" 'select(.kind == "service" and .service == $s) | .count // 0' <<< "$inv" | head -n 1)
  echo ""
  case "$st" in
    ok)
      ui_head "$(service_label "$svc")（${cnt}）"
      if [ "$cnt" -eq 0 ]; then ui_skip "なし"; fi
      while IFS=$'\t' read -r id name state extra; do
        [ -n "$id" ] || continue
        [ "$name" = "$id" ] && name=""
        case "$state" in
          stopped|stopping|terminated|shutting-down|INACTIVE|deleting|deleted) ui_skip "${id}${name:+  $name}  ${state}  ${extra}" ;;
          *) ui_ok "${id}${name:+  $name}  ${C_DIM}${state}${state:+  }${extra}${C_RESET}" ;;
        esac
      done < <(jq -r --arg s "$svc" 'select(.kind == "item" and .service == $s)
                 | [.id, .name, .state, (.extra | to_entries | map(select(.value != null and .value != "" and .value != false)) | map("\(.key) \(.value)") | join("  "))] | @tsv' <<< "$inv")
      if [ "$svc" = ec2 ] && jq -e 'select(.kind == "service" and .service == "ssm" and .status != "ok")' <<< "$inv" >/dev/null; then
        ui_warn "SSM の管理下かどうかは読めませんでした（ssm の権限が無い）"
      fi ;;
    denied)
      ui_head "$(service_label "$svc")"
      ui_warn "読めません（この一時キーに ${svc} の読み取りの権限がありません）" ;;
    *)
      err=$(jq -r --arg s "$svc" 'select(.kind == "service" and .service == $s) | .error // ""' <<< "$inv" | head -n 1)
      ui_head "$(service_label "$svc")"
      ui_err "読めませんでした"
      [ -z "$err" ] || ui_raw "$err" ;;
  esac
}

ui_title "aws-survey ls"
ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
ui_kv "アカウント" "$ACCOUNT_ID"
ui_kv "リージョン" "$REGION"
echo ""
container_require_key || exit 1
container_prepare || exit 1
ui_text "調査コンテナの中から、読み取り専用の一時キーで読みます（画面に出すだけで、何も書きません）。"

inv=$(container_inventory_spin ${PICK[@]+"${PICK[@]}"}); rc=$?
if [ -z "$inv" ]; then
  echo ""
  ui_err "列挙できませんでした（上の出力を確認してください）"
  exit "${rc:-1}"
fi
if [ "$JSON" -eq 1 ]; then printf '%s\n' "$inv"; exit "$rc"; fi

for svc in $(jq -r 'select(.kind == "service") | .service' <<< "$inv" | grep -vx ssm); do
  render_service "$inv" "$svc"
done
echo ""
also_cmd "$AWS_SURVEY_CMD ec2"    "EC2 を一覧から選んで、中を調べる準備をします"
also_cmd "$AWS_SURVEY_CMD lambda" "Lambda 関数を一覧から選んで、コードを取り出します"
also_cmd "$AWS_SURVEY_CMD run"    "調査コンテナを起動します。中で Claude Code / Codex と調査を進めます"
exit "$rc"
