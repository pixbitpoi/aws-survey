#!/bin/bash
# aws-survey lambda pull で取り出したものが期待どおりかを見る（AWS は叩かない。対象フォルダの code/ を読むだけ）
#
# 使い方: ./check.sh <対象フォルダ> [名前]      名前の既定は shop（launch.sh と同じ）
#
# リージョン・レイヤーの版・エイリアスの版は state/<名前>.env から読む。無ければ AWS_REGION・LAYER_VERSION・PY_VERSION
# （既定 ap-northeast-1・1・1）。見るのは launch.sh が仕込んだものだけ:
#   マスク（直書き・URL のユーザー情報・引用符つきのキー）、名前での除外（.env・secrets.yml）とコードは除外しないこと、
#   依存の除外と一覧、ソースマップからの復元、エイリアスの版のディレクトリ、レイヤーの判定、LEAKCHECK- と署名付き URL が残っていないこと
set -uo pipefail
cd "$(dirname "$0")"

target=${1:?対象フォルダを指定してください（例: ./check.sh ~/surveys/trial）}
name=${2:-shop}
AWS_REGION=${AWS_REGION:-} LAYER_VERSION=${LAYER_VERSION:-} PY_VERSION=${PY_VERSION:-}
if [[ -f state/$name.env ]]; then
  # shellcheck disable=SC1090
  source "state/$name.env"
fi
region=${AWS_REGION:-ap-northeast-1}
code=$target/code
py=$code/lambda/$region/$name-order-api
node=$code/lambda/$region/$name-thumbnail
layer=$code/lambda-layers/$region/$name-common/${LAYER_VERSION:-1}
v=${PY_VERSION:-1}

pass=0; fail=0
ok() { printf '  ✔ %s\n' "$1"; pass=$((pass + 1)); }
ng() { printf '  ✗ %s\n' "$1"; fail=$((fail + 1)); }
check() { local label=$1; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else ng "$label"; fi; }
has()    { grep -qF -- "$2" "$1"; }
hasnt()  { [[ -f $1 ]] && ! grep -qF -- "$2" "$1"; }
absent() { [[ ! -e $1 ]]; }
jqt()    { jq -e "$2" "$1"; }
step() { printf '\n== %s\n' "$*"; }

[[ -d $code ]] || { echo "$code がありません（先に aws-survey lambda pull）" >&2; exit 1; }
echo "対象: ${code}（リージョン ${region}、名前 ${name}）"

step "$name-order-api（Python）"
check "_manifest.json がある" test -f "$py/_manifest.json"
check "環境変数は名前だけ（DB_PASSWORD の名前はあり、値の項目は無い）" \
  jqt "$py/_manifest.json" '(.EnvironmentVariableNames | index("DB_PASSWORD")) != null and (has("Environment") | not)'
check "直書きの API_TOKEN が *** になっている" has "$py/src/app.py" 'API_TOKEN = "***"'
check "os.environ からの読み出しはそのまま" has "$py/src/app.py" 'os.environ["ORDERS_TABLE"]'
check "URL のユーザー情報が *** になっている（db.py）" has "$py/src/db.py" 'postgresql://***@db.shop.internal'
check "引用符つきのキーが *** になっている（config/settings.json）" has "$py/src/config/settings.json" '"apiKey": "***"'
check ".env は置かれていない" absent "$py/src/.env"
check "config/secrets.yml は置かれていない" absent "$py/src/config/secrets.yml"
check "除外の理由が控えにある（.env と secrets.yml）" \
  jqt "$py/_manifest.json" '[.contents.skipped[].path] | (index(".env") != null) and (index("config/secrets.yml") != null)'
check "secrets_client.py（コード）は置かれ、masked_only に記録" \
  jqt "$py/_manifest.json" '[.contents.masked_only[].path] | index("secrets_client.py") != null'
check "依存（idna）は置かれず、一覧にある" absent "$py/src/idna"
check "依存の一覧に idna 3.7" jqt "$py/_manifest.json" '.contents.dependencies.python | index({"name": "idna", "version": "3.7"}) != null'
check "__pycache__ は置かれていない" absent "$py/src/__pycache__"
check "\$LATEST は v2（# v2 の行がある）" has "$py/src/app.py" '# v2'
check "エイリアス live → 版 ${v}（別のディレクトリ）" jqt "$py/_manifest.json" ".Aliases | index({\"name\": \"live\", \"version\": \"$v\", \"code\": \"$v/\"}) != null"
check "版 $v のディレクトリがあり、v2 の行は無い" hasnt "$py/$v/src/app.py" '# v2'
check "レイヤーの版を参照している" jqt "$py/_manifest.json" '.Layers | length == 1'

step "$name-thumbnail（Node.js）"
check "_manifest.json がある" test -f "$node/_manifest.json"
check "バンドルと判定され、ソースマップから戻した" jqt "$node/_manifest.json" '.contents.bundles | length == 1 and .[0].map == "restored"'
check "元のソースが _sources/src/handler.ts にある" test -f "$node/src/_sources/src/handler.ts"
check "元のソースの apiKey が *** になっている" has "$node/src/_sources/src/handler.ts" 'const apiKey = "***"'
check "元のソースの環境変数（BUCKET_NAME）はそのまま" has "$node/src/_sources/src/handler.ts" 'process.env.BUCKET_NAME'
check "バンドルの apiKey も *** になっている" has "$node/src/index.js" 'var apiKey = "***"'
check "node_modules 由来のソースは戻していない" absent "$node/src/_sources/node_modules"
check "node_modules は置かれていない" absent "$node/src/node_modules"
check "ソースマップ（.map）は置かれていない" absent "$node/src/index.js.map"
check "依存の一覧に tiny-dep 2.1.0" jqt "$node/_manifest.json" '.contents.dependencies.node | index({"name": "tiny-dep", "version": "2.1.0"}) != null'

step "$name-common（レイヤー）"
check "_manifest.json がある（版 ${LAYER_VERSION:-1}）" test -f "$layer/_manifest.json"
check "関数自身のコードは含まない（依存だけ）" jqt "$layer/_manifest.json" '.contents.contains_own_code == false'
check "依存の一覧に shopcommon 1.4.0" jqt "$layer/_manifest.json" '.contents.dependencies.python | index({"name": "shopcommon", "version": "1.4.0"}) != null'

step "漏れていないこと（code/ 全体）"
leaks=$(grep -rlF -e LEAKCHECK -e p4ss-env-7f3a9c "$code" 2>/dev/null)
if [[ -z $leaks ]]; then ok "仕込んだ値（LEAKCHECK- と環境変数の値）がどこにも無い"; else ng "仕込んだ値が残っている: $leaks"; fi
urls=$(grep -rlE 'X-Amz-(Signature|Credential)|amazonaws\.com/[^" ]*\?' "$code" 2>/dev/null)
if [[ -z $urls ]]; then ok "署名付き URL がどこにも無い"; else ng "署名付き URL らしきものがある: $urls"; fi

printf '\n確かめた %d 件 / 問題あり %d 件\n' "$pass" "$fail"
(( fail == 0 ))
