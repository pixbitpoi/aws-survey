#!/bin/bash
# aws-survey の Lambda のコードを読む機能（aws-survey lambda pull）の動作テスト用に、関数 2 つとレイヤー 1 つを作る
#
# 使い方:
#   AWS_PROFILE=<プロファイル> ./launch.sh [名前]     名前の既定は shop。関数・レイヤー・ロールの名前の接頭辞になる
#   ./launch.sh --build-only <フォルダ>              zip を組み立てるだけ（AWS は叩かない。フォルダは新しく作る）
#
# 環境変数:
#   AWS_PROFILE    必須。IAM ロールと Lambda を作れる権限（管理者）
#   AWS_REGION     既定 ap-northeast-1
#
# 作るもの（関数は起動しない。実行ロールは信頼ポリシーだけで、権限を付けない）:
#   <名前>-lambda-exec   実行ロール
#   <名前>-common        Python のレイヤー（dist-info つきの依存だけ）
#   <名前>-order-api     Python 3.12。版 1 を公開してエイリアス live を付け、そのあと $LATEST だけ 1 行変える。
#                        直書きの秘密・os.environ・.env・config/secrets.yml・secrets_client.py・同梱の依存（idna）
#   <名前>-thumbnail     Node.js 20。esbuild 風のバンドル（100 KiB 超）とソースマップ（sourcesContent）、node_modules。
#                        ソースマップの元ソースはバンドルと同じ処理（環境変数 BUCKET_NAME・tiny-dep の resize・apiKey の長さ）
# コードに仕込んだ秘密に見立てた値は LEAKCHECK- で始まり（漏れたときにしか見えない）、環境変数 DB_PASSWORD の値は
# p4ss-env-7f3a9c（調査エージェントも設定から読めるので、テストだと分かる字面にしない）。取り出したあと ./check.sh が、
# どちらも code/ に残っていないことを見る。デプロイするコードには、テストの意図が分かるコメントを書かない（調査エージェントが読む）。
# 作ったものは state/<名前>.env に書き、cleanup.sh はそこに書かれたものだけを消す。
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n== %s\n' "$*"; }
die() { echo "$*" >&2; exit 1; }

# ---- zip を組み立てる（AWS は叩かない） ----
build() {  # build <フォルダ>（無いこと）
  local w=$1 py node layer i
  command -v zip >/dev/null || die "zip が見つかりません"
  mkdir -p "$w"

  step "Python の関数（order-api）"
  py=$w/order-api
  mkdir -p "$py/config" "$py/idna" "$py/idna-3.7.dist-info" "$py/__pycache__"
  cat > "$py/app.py" <<'EOF'
import json
import os
import uuid

import boto3

from db import connect
from secrets_client import get_db_password

API_TOKEN = "LEAKCHECK-hardcoded-token"

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
RECEIPT_BUCKET = os.environ["RECEIPT_BUCKET"]

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")


def lambda_handler(event, context):
    body = json.loads(event.get("body") or "{}")
    order_id = str(uuid.uuid4())
    dynamodb.Table(ORDERS_TABLE).put_item(Item={"id": order_id, "item": body.get("item", "unknown")})
    s3.put_object(Bucket=RECEIPT_BUCKET, Key=f"receipts/{order_id}.json", Body=json.dumps(body))
    with connect(get_db_password()) as conn:
        conn.record(order_id)
    return {"statusCode": 201, "body": json.dumps({"id": order_id})}
EOF
  cat > "$py/db.py" <<'EOF'
DSN = "postgresql://shop:LEAKCHECK-dsn-password@db.shop.internal:5432/shop"


class _Connection:
    def __init__(self, password):
        self.password = password

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record(self, order_id):
        print(f"recorded {order_id}")


def connect(password):
    return _Connection(password)
EOF
  cat > "$py/secrets_client.py" <<'EOF'
import os

import boto3


def get_db_password():
    client = boto3.client("secretsmanager")
    return client.get_secret_value(SecretId=os.environ["DB_SECRET_ID"])["SecretString"]
EOF
  printf 'DB_PASSWORD=LEAKCHECK-dotenv\n' > "$py/.env"
  printf 'password: LEAKCHECK-yml\n' > "$py/config/secrets.yml"
  printf '{"apiKey": "LEAKCHECK-json-key", "timeoutSeconds": 10}\n' > "$py/config/settings.json"
  printf 'idna==3.7\n' > "$py/requirements.txt"
  printf '__version__ = "3.7"\n' > "$py/idna/__init__.py"
  printf 'Metadata-Version: 2.1\nName: idna\nVersion: 3.7\n' > "$py/idna-3.7.dist-info/METADATA"
  printf 'idna/__init__.py,,\nidna-3.7.dist-info/METADATA,,\nidna-3.7.dist-info/RECORD,,\n' > "$py/idna-3.7.dist-info/RECORD"
  printf '\x00\x01\x02\x03pyc' > "$py/__pycache__/app.cpython-312.pyc"
  (cd "$py" && zip -qr ../order-api-v1.zip .)
  printf '# v2: 注文番号の形式を変えた\n' >> "$py/app.py"
  (cd "$py" && zip -qr ../order-api-v2.zip .)
  echo "order-api-v1.zip / order-api-v2.zip"

  step "Node.js の関数（thumbnail）"
  node=$w/thumbnail
  mkdir -p "$node/node_modules/tiny-dep"
  {
    printf '"use strict";\n'
    printf 'var __commonJS = (cb, mod) => function __require() { return mod || (0, cb[Object.keys(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports; };\n'
    printf 'var require_tiny_dep = __commonJS({ "node_modules/tiny-dep/index.js"(exports, module) { module.exports = { resize: (x) => x }; } });\n'
    # バンドルらしい大きさ（100 KiB 超）にする
    for i in $(seq 1 4000); do printf 'var helper_%d = (x) => x + %d;\n' "$i" "$i"; done
    printf 'var apiKey = "LEAKCHECK-bundle-key";\n'
    printf 'exports.handler = async (event) => { const dep = require_tiny_dep(); return { bucket: process.env.BUCKET_NAME, size: dep.resize(event.size || 0), key: apiKey.length }; };\n'
    printf '//# sourceMappingURL=index.js.map\n'
  } > "$node/index.js"
  # sourcesContent の元ソースは、バンドルと同じ処理にする（食い違いがあると、調査エージェントがそれを読み取りの結論にしてしまう）
  cat > "$node/index.js.map" <<'EOF'
{"version":3,"sources":["../src/handler.ts","../node_modules/tiny-dep/index.js"],"sourcesContent":["import { resize } from \"tiny-dep\";\n\nconst apiKey = \"LEAKCHECK-source-key\";\n\nexport const handler = async (event: { size?: number }) => {\n  return { bucket: process.env.BUCKET_NAME, size: resize(event.size ?? 0), key: apiKey.length };\n};\n","module.exports = { resize: (x) => x };\n"],"mappings":""}
EOF
  printf '{"name": "thumbnail", "version": "1.0.0", "dependencies": {"tiny-dep": "2.1.0"}}\n' > "$node/package.json"
  printf '{"name": "tiny-dep", "version": "2.1.0"}\n' > "$node/node_modules/tiny-dep/package.json"
  printf 'module.exports = { resize: (x) => x };\n' > "$node/node_modules/tiny-dep/index.js"
  (cd "$node" && zip -qr ../thumbnail.zip .)
  echo "thumbnail.zip（index.js $(wc -c < "$node/index.js" | tr -d ' ') バイト）"

  step "レイヤー（common）"
  layer=$w/common
  mkdir -p "$layer/python/shopcommon" "$layer/python/shopcommon-1.4.0.dist-info"
  printf 'def money(x):\n    return round(x, 2)\n' > "$layer/python/shopcommon/__init__.py"
  printf 'Metadata-Version: 2.1\nName: shopcommon\nVersion: 1.4.0\n' > "$layer/python/shopcommon-1.4.0.dist-info/METADATA"
  printf 'shopcommon/__init__.py,,\nshopcommon-1.4.0.dist-info/METADATA,,\nshopcommon-1.4.0.dist-info/RECORD,,\n' \
    > "$layer/python/shopcommon-1.4.0.dist-info/RECORD"
  (cd "$layer" && zip -qr ../common.zip python)
  echo "common.zip"
}

if [[ ${1:-} == --build-only ]]; then
  dir=${2:?組み立て先のフォルダを指定してください}
  [[ ! -e $dir ]] || die "$dir が既にあります（新しいフォルダを指定してください）"
  build "$dir"
  exit 0
fi

name=${1:-shop}
[[ $name =~ ^[a-z0-9-]{1,20}$ ]] || die "名前は英小文字・数字・- で 20 字まで: $name"
: "${AWS_PROFILE:?AWS_PROFILE を指定してください（例: AWS_PROFILE=my-admin ./launch.sh）}"
export AWS_PROFILE AWS_REGION=${AWS_REGION:-ap-northeast-1}
state=state/$name.env
role=$name-lambda-exec
layer=$name-common
py=$name-order-api
node=$name-thumbnail

save() { echo "$1=$2" >> "$state"; }

[[ ! -e $state ]] || die "$state が残っています。先に ./cleanup.sh $name を実行してください"

step "認証"
aws sts get-caller-identity --query Arn --output text
account=$(aws sts get-caller-identity --query Account --output text)

step "名前の重複"
for fn in "$py" "$node"; do
  if aws lambda get-function-configuration --function-name "$fn" >/dev/null 2>&1; then
    die "関数 $fn が既にあります"
  fi
done
if aws iam get-role --role-name "$role" >/dev/null 2>&1; then die "IAM ロール $role が既にあります"; fi
echo "なし"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
build "$work/zips"
zips=$work/zips

mkdir -p state
save AWS_PROFILE "$AWS_PROFILE"
save AWS_REGION "$AWS_REGION"

step "実行ロール ${role}（信頼ポリシーだけ。関数は起動しないので権限は付けない）"
aws iam create-role --role-name "$role" --tags Key=purpose,Value=test-lambda --assume-role-policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  --query Role.Arn --output text
save CREATED_ROLE "$role"
role_arn=arn:aws:iam::$account:role/$role

step "レイヤー $layer"
layer_arn=$(aws lambda publish-layer-version --layer-name "$layer" --zip-file "fileb://$zips/common.zip" \
  --compatible-runtimes python3.12 --description "test-lambda" --query LayerVersionArn --output text)
save LAYER_NAME "$layer"
save LAYER_VERSION "${layer_arn##*:}"
echo "$layer_arn"

# 作った直後の実行ロールは Lambda から見えないことがある。反映を待って打ち直す
create_function() {
  local err
  err=$(mktemp)
  for try in $(seq 1 12); do
    if aws lambda create-function "$@" --tags purpose=test-lambda --query FunctionArn --output text 2>"$err"; then
      rm -f "$err"; return 0
    fi
    grep -q 'cannot be assumed' "$err" && (( try < 12 )) || { cat "$err" >&2; rm -f "$err"; return 1; }
    echo "実行ロールの反映待ち（$try/12）"
    sleep 5
  done
}

step "Python の関数 ${py}（版 1 を公開してエイリアス live、そのあと \$LATEST だけ変える）"
create_function --function-name "$py" --runtime python3.12 --handler app.lambda_handler --role "$role_arn" \
  --zip-file "fileb://$zips/order-api-v1.zip" --layers "$layer_arn" \
  --environment "Variables={ORDERS_TABLE=$name-orders,RECEIPT_BUCKET=$name-receipts,DB_SECRET_ID=$name/db,DB_PASSWORD=p4ss-env-7f3a9c}"
save PY_FUNCTION "$py"
aws lambda wait function-active-v2 --function-name "$py"
version=$(aws lambda publish-version --function-name "$py" --description "live" --query Version --output text)
save PY_VERSION "$version"
aws lambda create-alias --function-name "$py" --name live --function-version "$version" --query AliasArn --output text
aws lambda update-function-code --function-name "$py" --zip-file "fileb://$zips/order-api-v2.zip" --query CodeSha256 --output text
aws lambda wait function-updated-v2 --function-name "$py"

step "Node.js の関数 $node"
create_function --function-name "$node" --runtime nodejs20.x --handler index.handler --role "$role_arn" \
  --zip-file "fileb://$zips/thumbnail.zip" --environment "Variables={BUCKET_NAME=$name-thumbnails}"
save NODE_FUNCTION "$node"
aws lambda wait function-active-v2 --function-name "$node"

cat <<EOF

作りました（$AWS_PROFILE / ${AWS_REGION}）
  $py   \$LATEST と、エイリアス live → 版 ${version}（コードが違う）
  $node
  $layer:${layer_arn##*:}（$py が参照）
記録: $(pwd)/$state

次の手順（対象フォルダで。リージョンが environment.json と違うなら --region ${AWS_REGION}）:
  aws-survey lambda pull $py $node
  $(pwd)/check.sh <対象フォルダ> $name     # 取り出したものが期待どおりか（AWS は叩かない）
  aws-survey run                            # 中で method/07 に沿って読ませる
  ./cleanup.sh $name                        # 片付け（対象フォルダの code/ は aws-survey lambda remove --all）
EOF
