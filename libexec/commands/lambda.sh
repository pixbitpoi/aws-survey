#!/usr/bin/env bash
# Lambda 関数にデプロイされているコードを取り出す（ホストで実行）。aws-survey lambda から呼ばれる。
#
#   aws-survey lambda                  一覧（調査コンテナの中で一時キーが読む）から関数を選んで、取り出す・消す（端末でなければ案内だけ）
#   aws-survey lambda pull <関数名>... [--region <r>] [--with-deps]
#   aws-survey lambda pull --all [--region <r>] [--with-deps]
#                                   取り出し専用の一時キー（Lambda の読み取り 4 つだけ・15 分・ファイルに書かない）でコードを落とし、
#                                   ネットワークの無い使い捨てのコンテナで展開・絞り込んで <対象フォルダ>/code/ に置く
#   aws-survey lambda list               取り出してある関数とレイヤー（AWS は叩かない）
#   aws-survey lambda remove <関数名>... [--region <r>] | --all
#                                   取り出したコードを消す（AWS には何も置いていないので、AWS 側の片付けは無い）
#
# 設計は docs/design-lambda-code.md（第 4 節・第 6.2 節）。get-function の応答には環境変数の値とコードの署名付き URL が入るので、
# --query で要る項目だけ取り、URL は変数に持ったまま curl に標準入力で渡す（画面・ファイル・ps に出さない）。
# 一時キーの値も変数にだけ持ち、aws を起動するときの環境変数で渡す。生の zip は一時ディレクトリにだけ置き、trap で消す。
# 表示は libexec/ui.sh の部品で組む。
set -uo pipefail

. "$(cd "$(dirname "$0")/.." && pwd)/load-env.sh"
. "$LIBEXEC_DIR/docker.sh"
. "$LIBEXEC_DIR/container.sh"
. "$LIBEXEC_DIR/menu.sh"

CODE_DIR="$AWS_SURVEY_DIR/code"
EXTRACT="$LIBEXEC_DIR/lambda/extract.py"
GATEWAY="$LIBEXEC_DIR/ec2/gateway.py"
# 取り出し専用の一時キーのロールセッション名の接頭辞。CloudTrail で調査の API 呼び出し（SESSION_NAME_PREFIX）と見分ける
PULL_SESSION_PREFIX="lambda-pull"
PULL_DURATION=900
PULL_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["lambda:GetFunction","lambda:GetLayerVersion","lambda:ListFunctions","lambda:ListAliases"],"Resource":"*"}]}'
# zip の上限。展開後の上限（250 MB）を超える zip は無い。直接アップロードの 50 MB は S3 経由のデプロイには掛からない
ZIP_LIMIT=$((250 * 1024 * 1024))
# 何件落としたら展開するか。一時ディレクトリに生の zip を溜めすぎない
BATCH="${AWS_SURVEY_LAMBDA_BATCH:-20}"
# 環境変数は名前だけ（keys）。Code.Location（署名付き URL）は location に分けて持ち、控えには入れない
GF_QUERY='{c:Configuration.{FunctionName:FunctionName,FunctionArn:FunctionArn,Runtime:Runtime,Handler:Handler,PackageType:PackageType,CodeSha256:CodeSha256,CodeSize:CodeSize,LastModified:LastModified,Version:Version,Architectures:Architectures,Layers:Layers[].Arn,EnvironmentVariableNames:keys(Environment.Variables || `{}`),ImageConfig:ImageConfigResponse.ImageConfig},image:Code.{ImageUri:ImageUri,ResolvedImageUri:ResolvedImageUri},location:Code.Location}'
GL_QUERY='{c:{LayerVersionArn:LayerVersionArn,Version:Version,CompatibleRuntimes:CompatibleRuntimes,CompatibleArchitectures:CompatibleArchitectures,CreatedDate:CreatedDate,CodeSha256:Content.CodeSha256,CodeSize:Content.CodeSize},location:Content.Location}'

die() { ui_die "$@"; }
usage() { sed -n '4,11p' "$0" | sed 's/^# \{0,1\}//'; }

valid_function() { [[ "$1" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; }
# 取り出し先のパスの 1 段になるので、形だけ見る（実在しないリージョンは AWS が拒否する）
valid_region()   { [[ "$1" =~ ^[a-z][a-z0-9-]{1,31}$ ]]; }
valid_layer()    { [[ "$1" =~ ^[A-Za-z0-9_-]{1,140}$ ]]; }
now_iso() { date +%Y-%m-%dT%H:%M:%S%z | sed -E 's/([0-9]{2})$/:\1/'; }
# エラー文に URL が混ざっても表示しない（署名付き URL の形を伏せる）
hide_urls() { sed -E 's#https?://[^[:space:]"]+#<URL>#g'; }

# ---- 引数 ----
SUB="${1:-}"; [ $# -gt 0 ] && shift
ARGS_TEXT="$*"
ALL=0; WITH_DEPS=false; PULL_REGION="$REGION"; NAMES=()

parse_args() {
  local mode="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --all) ALL=1; shift ;;
      --with-deps) [ "$mode" = pull ] || die "--with-deps は pull だけのオプションです。"; WITH_DEPS=true; shift ;;
      --region) [ $# -ge 2 ] || die "--region にはリージョンを指定してください。"; PULL_REGION="$2"; shift 2 ;;
      --region=*) PULL_REGION="${1#--region=}"; shift ;;
      -*) die "$mode の不明なオプション: $1" ;;
      *) valid_function "$1" || die "関数名が不正です: ${1}（英数字 - _ で 64 字まで。ARN ではなく名前で指定します）"
         NAMES+=("$1"); shift ;;
    esac
  done
  valid_region "$PULL_REGION" || die "リージョンが不正です: $PULL_REGION"
  [ "$ALL" -eq 0 ] || [ ${#NAMES[@]} -eq 0 ] || die "--all と関数名は一緒に指定できません。"
  [ "$ALL" -eq 1 ] || [ ${#NAMES[@]} -gt 0 ] || die "関数名か --all を指定してください。"
}

# ---- 元プロファイルと取り出し専用の一時キー ----
check_source_profile() {
  local who
  if ! who=$(aws sts get-caller-identity --profile "$PROFILE_SRC" --query Arn --output text 2>&1); then
    ui_err "元プロファイル $PROFILE_SRC が使えません"
    ui_raw "$who"
    [ -z "${REFRESH_CMD:-}" ] || [ "$REFRESH_CMD" = null ] || ui_text "ログインし直すコマンド: $(ui_cmd "$REFRESH_CMD")"
    die "取り出し専用の一時キーは元プロファイルで発行します。$PROFILE_SRC でログインしてから、もう一度実行してください。"
  fi
  ui_ok "$who"
}

# 調査用ロールを、Lambda の 4 つだけを許すインラインポリシーで借りる。--policy-arns は渡さないので、有効な権限は
# 「ロール（ReadOnlyAccess）∩ この 4 つ」になる。応答は変数にだけ持ち、bash の文字列操作で分ける（ファイルにも ps にも出さない）。
PULL_AK=""; PULL_SK=""; PULL_ST=""
issue_pull_key() {
  local out rest
  if ! out=$(aws sts assume-role --profile "$PROFILE_SRC" --role-arn "$ROLE_ARN" \
               --role-session-name "${PULL_SESSION_PREFIX}-$(date +%Y%m%d-%H%M%S)" \
               --duration-seconds "$PULL_DURATION" --policy "$PULL_POLICY" \
               --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text 2>&1); then
    ui_err "取り出し専用の一時キーを発行できませんでした"
    ui_raw "$out"
    ui_text "調査用の一時キーを発行できているなら（$AWS_SURVEY_CMD credentials）、同じロールで発行できるはずです。$AWS_SURVEY_CMD doctor で切り分けられます。"
    exit 1
  fi
  out=${out%$'\n'}
  PULL_AK=${out%%$'\t'*}; rest=${out#*$'\t'}
  PULL_SK=${rest%%$'\t'*}; PULL_ST=${rest#*$'\t'}
  [ -n "$PULL_AK" ] && [ -n "$PULL_SK" ] && [ -n "$PULL_ST" ] && [ "$PULL_ST" != "$rest" ] \
    || die "取り出し専用の一時キーの応答を読めません。"
  ui_ok "発行しました${C_DIM}（ロールセッション名 ${PULL_SESSION_PREFIX}-…。ファイルには書きません）${C_RESET}"
}

# 取り出し専用の一時キーで aws を叩く。元プロファイルや ~/.aws の設定を読ませない
pull_aws() {
  ( unset AWS_PROFILE AWS_DEFAULT_PROFILE
    AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_PAGER="" \
    AWS_ACCESS_KEY_ID="$PULL_AK" AWS_SECRET_ACCESS_KEY="$PULL_SK" AWS_SESSION_TOKEN="$PULL_ST" \
      aws --region "$PULL_REGION" "$@" )
}

# 同じキーで Lambda 以外（VPC の一覧）が拒否されることを 1 回見る。読めてしまったら取り出さない
self_check() {
  local out
  if out=$(pull_aws ec2 describe-vpcs --max-items 1 --query 'Vpcs[0].VpcId' --output text 2>&1); then
    die "取り出し専用の一時キーで Lambda 以外（VPC の一覧）が読めてしまいます。取り出しを止めます。"
  fi
  case "$out" in
    *UnauthorizedOperation*|*AccessDenied*) ui_ok "Lambda 以外は拒否されます（VPC の一覧で確認）" ;;
    *) ui_raw "$out"; die "取り出し専用の一時キーの権限を確かめられません。" ;;
  esac
}

# ---- 取り出し ----
# 1 引数は関数名、2 引数は版（省略時は $LATEST）。応答（URL を含む）を標準出力に出す。失敗したらエラー文を出して 1
get_function() {
  if [ -n "${2:-}" ]; then
    pull_aws lambda get-function --function-name "$1" --qualifier "$2" --query "$GF_QUERY" --output json 2>&1
  else
    pull_aws lambda get-function --function-name "$1" --query "$GF_QUERY" --output json 2>&1
  fi
}

PULLED=0; UNCHANGED=0; FAILED=0; EXPIRED=0; JOB_SEQ=0; LAYERS=""

# 展開の回ごとに、新しい入れ物（$WORK/in-<n>）を用意する。同じディレクトリで jobs.json を消して作り直すと、
# Docker Desktop のファイル共有が消えたままの状態を見せることがある（2026-09-11 に再現。2 回目のマウントで ENOENT）。
# 名前もマウント元も使い回さない。
BATCH_NO=0; IN_DIR=""
new_batch() {
  BATCH_NO=$((BATCH_NO + 1))
  IN_DIR="$WORK/in-$BATCH_NO"
  mkdir "$IN_DIR" || die "一時ディレクトリを作れません。"
}

# 取り出し専用の一時キーが切れたら、そこで止める（もう一度打てば、取り出し済みは飛ばして残りから進む）
api_failed() {
  local label="$1" out="$2"
  case "$out" in
    *ExpiredToken*|*"token included in the request is expired"*)
      EXPIRED=1
      ui_warn "取り出し専用の一時キーが切れました（${label} の手前で止めます）" ;;
    *)
      FAILED=$((FAILED + 1))
      ui_err "$label  情報を読めません"
      ui_raw "$(printf '%s\n' "$out" | hide_urls | tail -5)" ;;
  esac
}

# 取り出し先の控えと同じ CodeSha256 で、同じ依存の扱いで展開済みなら取り直さない
unchanged() {
  local manifest="$CODE_DIR/$1/_manifest.json"
  [ -f "$manifest" ] && jq -e --arg sha "$2" --argjson deps "$WITH_DEPS" \
    '.CodeSha256 == $sha and .contents.status == "ok" and ((.contents.with_deps // false) == $deps)' "$manifest" >/dev/null 2>&1
}

# URL は標準入力の curl 設定で渡す（引数に出さない）。https 以外は落とさない
download() {
  local url="$1" out="$2" err
  case "$url" in https://*) ;; *) echo "コードの場所が https ではありません"; return 1 ;; esac
  if ! err=$(printf 'url = "%s"\n' "$url" | curl -sS --fail --proto =https --max-filesize "$ZIP_LIMIT" --max-time 600 \
                -o "$out" -K - 2>&1); then
    printf '%s\n' "$err" | hide_urls
    return 1
  fi
}

add_job() {  # 1 取り出し先 2 zip の名前（無ければ空） 3 控え（JSON）
  jq -cn --arg dest "$1" --arg zip "$2" --argjson meta "$3" \
    '{dest: $dest, zip: (if $zip == "" then null else $zip end), meta: $meta}' >> "$WORK/jobs.jsonl"
}

# 1 表示名 2 取り出し先 3 get-function / get-layer-version-by-arn の応答（URL を含む） 4 控えの上位に足す JSON
pull_code() {
  local label="$1" dest="$2" answer="$3" extra="$4" sha pkg meta url zip="" err tmp
  sha=$(printf '%s' "$answer" | jq -r '.c.CodeSha256 // empty')
  pkg=$(printf '%s' "$answer" | jq -r '.c.PackageType // "Zip"')
  if unchanged "$dest" "$sha"; then
    # コードは同じでも、エイリアスの向き先は変わりうる。控えだけ書き換える
    tmp=$(mktemp) || die "一時ファイルを作れません。"
    jq --argjson extra "$extra" --arg at "$(now_iso)" '. + $extra + {checked_at: $at}' "$CODE_DIR/$dest/_manifest.json" > "$tmp" \
      && mv "$tmp" "$CODE_DIR/$dest/_manifest.json" || rm -f "$tmp"
    ui_skip "$label  変わっていません（CodeSha256 ${sha:0:12}…）"
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  meta=$(printf '%s' "$answer" | jq -c --arg region "$PULL_REGION" --arg at "$(now_iso)" --argjson deps "$WITH_DEPS" --argjson extra "$extra" \
           '.c + {Region: $region, pulled_at: $at, with_deps: $deps}
            + (if .c.PackageType == "Image" then .image else {} end) + $extra')
  if [ "$pkg" = Zip ]; then
    url=$(printf '%s' "$answer" | jq -r '.location // empty')
    JOB_SEQ=$((JOB_SEQ + 1)); zip="$JOB_SEQ.zip"
    if ! err=$(download "$url" "$IN_DIR/$zip"); then
      rm -f "$IN_DIR/$zip"
      ui_err "$label  コードを落とせませんでした"
      ui_raw "$err"
      FAILED=$((FAILED + 1))
      return 1
    fi
    url=""
  else
    ui_text "$label  コンテナイメージ形式です。中身は取り出さず、イメージの場所だけを控えます"
  fi
  add_job "$dest" "$zip" "$meta"
  [ "$(wc -l < "$WORK/jobs.jsonl" | tr -d ' ')" -lt "$BATCH" ] || flush_jobs
}

# 溜めた zip を、ネットワークも資格情報も無い使い捨てのコンテナで展開する（libexec/lambda/extract.py）。
# ホストの利用者の uid:gid で書かせるので、Linux でも取り出し先を消せる。終わったら生の zip を消す
flush_jobs() {
  [ -s "$WORK/jobs.jsonl" ] || return 0
  local n out rc=0 line fields status dest error ok=0
  n=$(wc -l < "$WORK/jobs.jsonl" | tr -d ' ')
  jq -s --argjson deps "$WITH_DEPS" '{with_deps: $deps, jobs: .}' "$WORK/jobs.jsonl" > "$IN_DIR/jobs.json" \
    || die "展開の指示を組み立てられません。"
  ui_status "展開して絞り込んでいます（${n} 件）…"
  out=$(docker run --rm --network none --read-only --tmpfs /tmp:size=1g \
          -u "$(id -u):$(id -g)" \
          -v "$IN_DIR:/in:ro" -v "$CODE_DIR:/out" \
          -v "$EXTRACT:/x/extract.py:ro" -v "$GATEWAY:/x/gateway.py:ro" \
          "$IMAGE" python3 -B /x/extract.py /in /out 2>&1) || rc=$?
  ui_status_done
  rm -rf "$IN_DIR" "$WORK/jobs.jsonl"
  new_batch
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    if ! fields=$(printf '%s' "$line" | jq -er '[.status, .dest, (.error // ""),
                    "置いた \(.files // 0) 件・名前で除外 \(.skipped // 0) 件・依存として除外 \(.excluded_dependencies // 0) 件・バイナリ \(.binaries // 0) 件" +
                    (if (.restored_sources // 0) > 0 then "・ソースマップから戻した \(.restored_sources) 件" else "" end)] | @tsv' 2>/dev/null); then
      ui_raw "$line"
      continue
    fi
    IFS=$'\t' read -r status dest error summary <<< "$fields"
    if [ "$status" = ok ]; then
      ok=$((ok + 1)); PULLED=$((PULLED + 1))
      ui_ok "$dest  ${C_DIM}${summary}${C_RESET}"
    else
      FAILED=$((FAILED + 1))
      ui_err "$dest  展開できませんでした: $error"
    fi
  done <<< "$out"
  if [ "$ok" -lt "$n" ] && [ "$rc" -ne 0 ] && ! printf '%s' "$out" | grep -q '"status"'; then
    FAILED=$((FAILED + n))
    ui_err "展開のコンテナが失敗しました（終了コード ${rc}）"
  fi
}

pull_function() {
  local fn="$1" dest="lambda/$PULL_REGION/$1" gf gfv sha shav aliases versions v code keep="" alias_meta d
  if ! gf=$(get_function "$fn"); then api_failed "$fn" "$gf"; return 1; fi
  sha=$(printf '%s' "$gf" | jq -r '.c.CodeSha256 // empty')
  LAYERS="$LAYERS"$'\n'"$(printf '%s' "$gf" | jq -r '.c.Layers // [] | .[]')"

  # エイリアスが指す版。$LATEST と CodeSha256 が違う版だけ <関数名>/<版>/ に取り、同じなら控えに書くだけ
  if ! aliases=$(pull_aws lambda list-aliases --function-name "$fn" --query 'Aliases[].{name:Name,version:FunctionVersion}' --output json 2>&1); then
    ui_warn "$fn  エイリアスを読めません（\$LATEST だけ取ります）"
    ui_raw "$(printf '%s\n' "$aliases" | tail -3)"
    aliases='[]'
  fi
  alias_meta=$(jq -c '[.[] | select(.version == "$LATEST") | {name, version, code: "$LATEST"}]' <<< "$aliases")
  versions=$(jq -r '[.[].version | select(. != "$LATEST")] | unique | .[]' <<< "$aliases")
  for v in $versions; do
    [[ "$v" =~ ^[0-9]+$ ]] || continue
    if ! gfv=$(get_function "$fn" "$v"); then api_failed "$fn:$v" "$gfv"; [ "$EXPIRED" -eq 0 ] || return 1; continue; fi
    shav=$(printf '%s' "$gfv" | jq -r '.c.CodeSha256 // empty')
    if [ "$shav" = "$sha" ]; then
      code='$LATEST'
    else
      code="$v/"; keep="$keep $v"
      LAYERS="$LAYERS"$'\n'"$(printf '%s' "$gfv" | jq -r '.c.Layers // [] | .[]')"
      pull_code "$fn:$v" "$dest/$v" "$gfv" \
        "$(jq -c --arg v "$v" '{Aliases: [.[] | select(.version == $v) | .name]}' <<< "$aliases")" || true
    fi
    alias_meta=$(jq -c --argjson a "$aliases" --arg v "$v" --arg code "$code" \
                   '. + [$a[] | select(.version == $v) | {name, version, code: $code}]' <<< "$alias_meta")
  done
  pull_code "$fn" "$dest" "$gf" "$(jq -cn --argjson a "$alias_meta" '{Aliases: $a}')" || return 1
  # どのエイリアスも指さなくなった古い版は消す（古い版は上書きの方針。取り直せば戻る）
  for d in "$CODE_DIR/$dest"/*/; do
    [ -d "$d" ] || continue
    v=$(basename "$d")
    [[ "$v" =~ ^[0-9]+$ ]] || continue
    case " $keep " in *" $v "*) ;; *) rm -rf "$d" ;; esac
  done
}

pull_layer() {
  local arn="$1" region name version gl
  IFS=: read -r _ _ _ region _ _ name version <<< "$arn"
  if ! valid_region "$region" || ! valid_layer "$name" || ! [[ "$version" =~ ^[0-9]+$ ]]; then
    ui_warn "レイヤーの ARN を読めません: $arn"
    return 0
  fi
  if ! gl=$(pull_aws lambda get-layer-version-by-arn --arn "$arn" --query "$GL_QUERY" --output json 2>&1); then
    api_failed "レイヤー $name:$version" "$gl"
    return 1
  fi
  pull_code "レイヤー $name:$version" "lambda-layers/$region/$name/$version" "$gl" '{}'
}

cmd_pull() {
  parse_args pull "$@"
  local t fn functions out arn
  for t in aws curl docker; do
    command -v "$t" >/dev/null || die "$t が見つかりません（$AWS_SURVEY_CMD doctor で確かめられます）。"
  done
  [ -f "$EXTRACT" ] && [ -f "$GATEWAY" ] || die "抽出器が見つかりません: $EXTRACT"
  docker_awsarch

  ui_title "aws-survey lambda pull"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "アカウント" "$ACCOUNT_ID"
  ui_kv "リージョン" "$PULL_REGION"
  if [ "$ALL" -eq 1 ]; then ui_kv "関数" "このリージョンの全部"; else ui_kv "関数" "${NAMES[*]}"; fi
  if [ "$WITH_DEPS" = true ]; then ui_kv "依存ライブラリ" "含める"; else ui_kv "依存ライブラリ" "除外して一覧だけ残す（--with-deps で含める）"; fi
  ui_kv "取り出し先" "$CODE_DIR"
  ui_text "取り出すのはデプロイされたコードだけです。関数は起動せず、AWS には何も置きません。"
  echo ""

  mkdir -p "$CODE_DIR" || die "取り出し先を作れません: $CODE_DIR"
  WORK=$(mktemp -d) || die "一時ディレクトリを作れません。"
  # 生の zip は途中で止まっても残さない
  trap 'rm -rf "$WORK"' EXIT
  new_batch
  : > "$WORK/jobs.jsonl"
  # 展開のコンテナにマウントする場所（本体の抽出器、取り出し先、一時ディレクトリ）が Docker Desktop から見えるか
  docker_check_shared "$AWS_SURVEY_HOME" "$CODE_DIR" "$WORK" || exit 1

  ui_head "1/5 元プロファイル $PROFILE_SRC でログインできているか"
  check_source_profile
  echo ""

  ui_head "2/5 展開に使うイメージを用意する（$IMAGE, ${awsarch}）"
  build_image
  echo ""

  ui_head "3/5 取り出し専用の一時キーを発行する（Lambda の読み取りだけ・$((PULL_DURATION / 60)) 分）"
  issue_pull_key
  self_check
  echo ""

  ui_head "4/5 関数のコードを取り出す"
  if [ "$ALL" -eq 1 ]; then
    out=$(pull_aws lambda list-functions --query 'Functions[].FunctionName' --output json 2>&1) \
      || { ui_raw "$out"; die "関数の一覧を読めません。"; }
    functions=$(jq -r '.[]' <<< "$out")
    [ -n "$functions" ] || ui_skip "このリージョンに関数はありません"
  else
    functions=$(printf '%s\n' "${NAMES[@]}")
  fi
  for fn in $functions; do
    valid_function "$fn" || continue
    pull_function "$fn" || true
    [ "$EXPIRED" -eq 0 ] || break
  done
  flush_jobs
  echo ""

  ui_head "5/5 参照しているレイヤーを取り出す"
  LAYERS=$(printf '%s\n' "$LAYERS" | grep -v '^$' | sort -u)
  if [ "$EXPIRED" -eq 1 ]; then
    ui_skip "一時キーが切れたので飛ばしました"
  elif [ -z "$LAYERS" ]; then
    ui_skip "レイヤーはありません"
  else
    for arn in $LAYERS; do
      pull_layer "$arn" || true
      [ "$EXPIRED" -eq 0 ] || break
    done
    flush_jobs
  fi
  echo ""

  ui_head "結果"
  ui_kv "取り出した" "$PULLED 件"
  ui_kv "変わっていない" "$UNCHANGED 件"
  ui_kv "失敗" "$FAILED 件"
  ui_kv "取り出し先" "$CODE_DIR"
  echo ""
  if [ "$EXPIRED" -eq 1 ]; then
    ui_warn "取り出し専用の一時キー（$((PULL_DURATION / 60)) 分）が切れたので、途中で止めました。"
    next_cmd "$AWS_SURVEY_CMD lambda pull $ARGS_TEXT" "もう一度打てば、取り出し済みの関数は飛ばして残りから進みます"
    exit 1
  fi
  if [ "$FAILED" -gt 0 ]; then
    ui_err "取り出せなかったものがあります（上の ✗ を見てください）"
  else
    ui_ok "取り出しました。生の zip は残していません"
  fi
  next_cmd "$AWS_SURVEY_CMD lambda list" "取り出してある関数とレイヤーを確かめます"
  also_cmd "$AWS_SURVEY_CMD run" "調査コンテナを起動します。取り出したコードは code/ として読み取り専用で渡ります"
  [ "$FAILED" -eq 0 ]
}

# ---- list ----
cmd_list() {
  [ $# -eq 0 ] || die "list に引数はありません。"
  ui_title "aws-survey lambda list"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "取り出し先" "$CODE_DIR"
  local manifests="" m rel n=0 fields name version region sha at files skipped deps pkg status
  if [ -d "$CODE_DIR" ]; then
    # 取り出し先の控えだけを見る（src/ の中の同名のファイルは対象のもの）
    manifests=$(cd "$CODE_DIR" && find lambda lambda-layers -name _manifest.json 2>/dev/null \
                  | grep -E '^(lambda/[^/]+/[^/]+(/[0-9]+)?|lambda-layers/[^/]+/[^/]+/[0-9]+)/_manifest\.json$' | sort)
  fi
  if [ -z "$manifests" ]; then
    echo ""
    ui_skip "取り出してあるコードはありません"
    next_cmd "$AWS_SURVEY_CMD lambda pull <関数名> | --all" "関数のコードを取り出します"
    return 0
  fi
  echo ""
  ui_head "取り出してあるもの（$(printf '%s\n' "$manifests" | wc -l | tr -d ' ')）"
  for rel in $manifests; do
    m="$CODE_DIR/$rel"
    fields=$(jq -r '[(.FunctionName // (.LayerVersionArn // "?" | split(":") | .[6])), (.Version // "?"), (.Region // "?"),
                     (.CodeSha256 // "" | .[0:12]), (.pulled_at // "?"), (.contents.files // 0 | tostring),
                     (.contents.skipped_count // 0 | tostring), (.contents.excluded_dependencies.files // 0 | tostring),
                     (.PackageType // "Zip"), (.contents.status // "?")] | @tsv' "$m" 2>/dev/null) \
      || { ui_warn "${rel%/_manifest.json}  控えを読めません"; continue; }
    IFS=$'\t' read -r name version region sha at files skipped deps pkg status <<< "$fields"
    ui_ok "${rel%/_manifest.json}  ${C_DIM}CodeSha256 ${sha}…  取り出し ${at}${C_RESET}"
    if [ "$pkg" = Image ]; then
      ui_text "コンテナイメージ形式（中身は取り出していません。イメージの場所は _manifest.json）"
    else
      ui_text "置いた $files 件 / 名前で除外 $skipped 件 / 依存として除外 $deps 件"
    fi
  done
  echo ""
  also_cmd "$AWS_SURVEY_CMD lambda pull <関数名>" "取り直します（コードが変わっていなければ落としません）"
  also_cmd "$AWS_SURVEY_CMD lambda remove <関数名> | --all" "取り出したコードを消します"
}

# ---- remove ----
cmd_remove() {
  parse_args remove "$@"
  ui_title "aws-survey lambda remove"
  ui_kv "取り出し先" "$CODE_DIR"
  ui_text "消すのはこのホストに取り出したコードだけです。AWS には何も置いていないので、AWS 側の片付けはありません。"
  echo ""
  local fn d
  if [ "$ALL" -eq 1 ]; then
    if [ -d "$CODE_DIR/lambda" ] || [ -d "$CODE_DIR/lambda-layers" ]; then
      rm -rf "$CODE_DIR/lambda" "$CODE_DIR/lambda-layers" || die "消せませんでした: $CODE_DIR"
      rmdir "$CODE_DIR" 2>/dev/null || true
      ui_ok "取り出したコードを全部消しました（関数とレイヤー）"
    else
      ui_skip "取り出してあるコードはありません"
    fi
    return 0
  fi
  for fn in "${NAMES[@]}"; do
    d="$CODE_DIR/lambda/$PULL_REGION/$fn"
    if [ -d "$d" ]; then
      rm -rf "$d" || die "消せませんでした: $d"
      ui_ok "消しました: lambda/$PULL_REGION/$fn"
    else
      ui_skip "ありません: lambda/$PULL_REGION/$fn"
    fi
  done
  ui_text "レイヤーは他の関数と共有するので残します（全部消すなら --all）。"
}

# ---- 引数なし: 一覧から選ぶ ----
# 一覧は調査コンテナの中で読み取り専用の一時キーが読む（container_inventory）。取り出しと削除はここのホストの関数を呼ぶ。
# choose_menu は EXIT トラップを潰すので、cmd_pull（一時ディレクトリの trap）より前に済ませる。
cmd_interactive() {
  [ $# -eq 0 ] || { usage >&2; die "不明なサブコマンド: ${1}（pull / list / remove。引数なしなら一覧から選びます）"; }
  ui_title "aws-survey lambda"
  ui_kv "対象フォルダ" "$AWS_SURVEY_DIR"
  ui_kv "アカウント" "$ACCOUNT_ID"
  ui_kv "リージョン" "$REGION"
  ui_kv "取り出し先" "$CODE_DIR"
  echo ""
  container_require_key || exit 1
  container_prepare || exit 1
  ui_text "調査コンテナの中から、読み取り専用の一時キーで読みます（画面に出すだけで、何も書きません）。"
  local inv st
  inv=$(container_inventory lambda)
  [ -n "$inv" ] || { echo ""; ui_err "列挙できませんでした（上の出力を確認してください）"; exit 1; }
  st=$(jq -r 'select(.kind == "service" and .service == "lambda") | .status' <<< "$inv" | head -n 1)
  if [ "$st" != ok ]; then
    echo ""
    ui_err "Lambda 関数の一覧を読めませんでした"
    ui_raw "$(jq -r 'select(.kind == "service" and .service == "lambda") | .error // ""' <<< "$inv" | head -n 1)"
    exit 1
  fi
  local -a names=() labels=() pulled=()
  local id state modified m at
  while IFS=$'\t' read -r id state modified; do
    [ -n "$id" ] || continue
    m="$CODE_DIR/lambda/$REGION/$id/_manifest.json"
    at=""
    if [ -f "$m" ]; then
      at=$(jq -r '.pulled_at // empty' "$m" 2>/dev/null); at="取り出し済み${at:+ $at}"
    fi
    names+=("$id"); pulled+=("$at")
    labels+=("${id}  ${state}  更新 ${modified%%.*}${at:+  $at}")
  done < <(jq -r 'select(.kind == "item") | [.id, .state, (.extra.modified // "")] | @tsv' <<< "$inv")
  echo ""
  ui_head "Lambda 関数（${#names[@]}）"
  if [ ${#names[@]} -eq 0 ]; then ui_skip "なし"; return 0; fi

  if ! menu_available; then
    local i
    for i in "${!names[@]}"; do
      if [ -n "${pulled[i]}" ]; then ui_ok "${labels[i]}"; else ui_skip "${labels[i]}"; fi
    done
    echo ""
    next_cmd "$AWS_SURVEY_CMD lambda pull <関数名>" "デプロイされたコードを取り出して code/ に置きます（そのリージョンの全関数なら --all）"
    also_cmd "$AWS_SURVEY_CMD lambda list" "取り出してある関数とレイヤー"
    also_cmd "$AWS_SURVEY_CMD lambda remove <関数名>" "取り出したコードを消します"
    return 0
  fi

  local picked sel="" act
  choose_menu picked "コードを読みたい関数を選んでください" 0 "${labels[@]}"
  for i in "${!labels[@]}"; do [ "${labels[i]}" != "$picked" ] || sel=$i; done
  [ -n "$sel" ] || die "選択を読めませんでした。"
  ui_ok "$picked"
  echo ""
  if [ -z "${pulled[sel]}" ]; then
    choose_menu act "${names[sel]} に何をしますか？" 0 "デプロイされたコードを取り出す" "やめる"
  else
    choose_menu act "${names[sel]} に何をしますか？" 0 "取り直す（コードが変わっていなければ落とさない）" "取り出したコードを消す" "やめる"
  fi
  ui_ok "$act"
  echo ""
  case "$act" in
    "デプロイされたコードを取り出す"|"取り直す"*) cmd_pull "${names[sel]}" ;;
    "取り出したコードを消す") cmd_remove "${names[sel]}" ;;
    *) ui_text "ここで止めます。" ;;
  esac
}

case "$SUB" in
  "")     cmd_interactive "$@" ;;
  pull)   cmd_pull "$@" ;;
  list)   cmd_list "$@" ;;
  remove) cmd_remove "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; die "不明なサブコマンド: ${SUB}（pull / list / remove）" ;;
esac
