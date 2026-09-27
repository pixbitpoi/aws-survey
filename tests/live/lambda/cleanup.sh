#!/bin/bash
# launch.sh で作ったものを片付ける（state/<名前>.env に記録したものだけを消す）
#
# 使い方: ./cleanup.sh [-y] [名前]     名前の既定は shop。-y で確認を省く
#
# 対象フォルダに取り出したコード（code/）は AWS には無いので、ここでは消さない。対象フォルダで aws-survey lambda remove --all。
# 途中で失敗しても、もう一度実行すれば残りを片付ける。
set -euo pipefail
cd "$(dirname "$0")"

yes=0
if [[ ${1:-} == -y ]]; then yes=1; shift; fi
name=${1:-shop}
state=state/$name.env
[[ -f $state ]] || { echo "$state がありません（片付けるものの記録が無い）" >&2; exit 1; }
PY_FUNCTION='' NODE_FUNCTION='' LAYER_NAME='' LAYER_VERSION='' CREATED_ROLE=''
# shellcheck disable=SC1090
source "$state"
export AWS_PROFILE AWS_REGION

step() { printf '\n== %s\n' "$*"; }

echo "消すもの（$state の記録。$AWS_PROFILE / ${AWS_REGION}）:"
if [[ -n $PY_FUNCTION ]]; then echo "  関数           ${PY_FUNCTION}（版とエイリアスも一緒に消える）"; fi
if [[ -n $NODE_FUNCTION ]]; then echo "  関数           $NODE_FUNCTION"; fi
if [[ -n $LAYER_NAME ]]; then echo "  レイヤーの版   $LAYER_NAME:$LAYER_VERSION"; fi
if [[ -n $CREATED_ROLE ]]; then echo "  IAM ロール     $CREATED_ROLE"; fi

if (( ! yes )); then
  echo
  read -r -p "消してよければ yes と入力: " ans
  [[ $ans == yes ]] || { echo "中止しました"; exit 1; }
fi

for fn in $PY_FUNCTION $NODE_FUNCTION; do
  step "関数 $fn を削除"
  if out=$(aws lambda delete-function --function-name "$fn" 2>&1); then
    echo "削除しました"
  else
    case $out in
      *ResourceNotFoundException*) echo "既にありません" ;;
      *) echo "$out" >&2; exit 1 ;;
    esac
  fi
done

if [[ -n $LAYER_NAME ]]; then
  step "レイヤーの版 $LAYER_NAME:$LAYER_VERSION を削除"
  aws lambda delete-layer-version --layer-name "$LAYER_NAME" --version-number "$LAYER_VERSION"
  echo "削除しました（無かった場合もここに来ます）"
fi

if [[ -n $CREATED_ROLE ]]; then
  step "IAM ロール $CREATED_ROLE を削除"
  if out=$(aws iam delete-role --role-name "$CREATED_ROLE" 2>&1); then
    echo "削除しました"
  else
    case $out in
      *NoSuchEntity*) echo "既にありません" ;;
      *) echo "$out" >&2; exit 1 ;;
    esac
  fi
fi

rm -f "$state"
echo
echo "片付けました（$state を消しました）"
echo "対象フォルダの code/ は、そこで aws-survey lambda remove --all で消します"
