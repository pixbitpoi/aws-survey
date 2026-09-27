#!/bin/bash
# launch.sh で作ったものを片付ける（state/<名前>.env に記録したものだけを消す）
#
# 使い方: ./cleanup.sh [-y] [名前]     名前の既定は web2。-y で確認を省く
#
# aws-survey の診断ゲートウェイを入れていたら、先に対象フォルダで aws-survey ssh remove <名前> を実行する
# （入れたままインスタンスだけ消すと、aws-survey 側の登録が残る）。
# 途中で失敗しても、もう一度実行すれば残りを片付ける。
set -euo pipefail
cd "$(dirname "$0")"

yes=0
if [[ ${1:-} == -y ]]; then yes=1; shift; fi
name=${1:-web2}
state=state/$name.env
[[ -f $state ]] || { echo "$state がありません（片付けるものの記録が無い）" >&2; exit 1; }
INSTANCE_ID='' SG_ID='' CREATED_ROLE='' CREATED_PROFILE=''
# shellcheck disable=SC1090
source "$state"
export AWS_PROFILE AWS_REGION
ssm_policy=arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

step() { printf '\n== %s\n' "$*"; }

echo "消すもの（$state の記録。$AWS_PROFILE / ${AWS_REGION}）:"
if [[ -n $INSTANCE_ID ]]; then echo "  インスタンス             ${INSTANCE_ID}（ルートボリュームも一緒に消える）"; fi
if [[ -n $SG_ID ]]; then echo "  セキュリティグループ     $SG_ID"; fi
if [[ -n $CREATED_PROFILE ]]; then echo "  インスタンスプロファイル ${CREATED_PROFILE}（ほかのインスタンスが使っていれば残す）"; fi
if [[ -n $CREATED_ROLE ]]; then echo "  IAM ロール               ${CREATED_ROLE}（同上）"; fi

if [[ -n $INSTANCE_ID ]]; then
  diag=$(aws ec2 describe-tags --filters "Name=resource-id,Values=$INSTANCE_ID" Name=key,Values=diag:ssh \
    --query 'Tags[0].Value' --output text)
  if [[ $diag != None ]]; then
    echo
    echo "注意: diag:ssh=$diag のタグがあります（aws-survey の診断ゲートウェイが入っている）。"
    echo "      先に対象フォルダで aws-survey ssh remove $name を実行してください。"
  fi
fi

if (( ! yes )); then
  echo
  read -r -p "消してよければ yes と入力: " ans
  [[ $ans == yes ]] || { echo "中止しました"; exit 1; }
fi

if [[ -n $INSTANCE_ID ]]; then
  step "インスタンスを終了"
  st=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo gone)
  if [[ $st == gone ]]; then
    echo "既にありません"
  else
    if [[ $st != terminated ]]; then
      aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" \
        --query 'TerminatingInstances[0].CurrentState.Name' --output text
    fi
    aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
    echo "terminated"
  fi
fi

if [[ -n $SG_ID ]]; then
  step "セキュリティグループを削除"
  for try in 1 2 3 4 5 6 7 8 9 10; do
    if out=$(aws ec2 delete-security-group --group-id "$SG_ID" 2>&1); then
      echo "$SG_ID を削除しました"
      break
    fi
    case $out in
      *InvalidGroup.NotFound*) echo "既にありません"; break ;;
      *DependencyViolation*)
        (( try < 10 )) || { echo "$out" >&2; exit 1; }
        echo "ネットワークインターフェースの解放待ち（$try/10）"
        sleep 10
        ;;
      *) echo "$out" >&2; exit 1 ;;
    esac
  done
fi

if [[ -n $CREATED_PROFILE$CREATED_ROLE ]]; then
  step "インスタンスプロファイルと IAM ロール（launch.sh が作ったもの）"
  arn=''
  if [[ -n $CREATED_PROFILE ]]; then
    arn=$(aws iam get-instance-profile --instance-profile-name "$CREATED_PROFILE" \
      --query InstanceProfile.Arn --output text 2>/dev/null || true)
  fi
  users=''
  if [[ -n $arn ]]; then
    users=$(aws ec2 describe-instances --filters "Name=iam-instance-profile.arn,Values=$arn" \
      Name=instance-state-name,Values=pending,running,stopping,stopped \
      --query 'Reservations[].Instances[].InstanceId' --output text)
  fi
  if [[ -n $users ]]; then
    echo "ほかのインスタンスが使っているので残します: ${users}（不要になったら手で消してください）"
  else
    if [[ -n $arn ]]; then
      aws iam remove-role-from-instance-profile --instance-profile-name "$CREATED_PROFILE" \
        --role-name "${CREATED_ROLE:-$CREATED_PROFILE}" 2>/dev/null || true
      aws iam delete-instance-profile --instance-profile-name "$CREATED_PROFILE"
      echo "インスタンスプロファイル $CREATED_PROFILE を削除しました"
    fi
    if [[ -n $CREATED_ROLE ]] && aws iam get-role --role-name "$CREATED_ROLE" >/dev/null 2>&1; then
      aws iam detach-role-policy --role-name "$CREATED_ROLE" --policy-arn "$ssm_policy" 2>/dev/null || true
      aws iam delete-role --role-name "$CREATED_ROLE"
      echo "IAM ロール $CREATED_ROLE を削除しました"
    fi
  fi
fi

rm -f "$state"
echo
echo "片付けました（$state を消しました）"
