#!/bin/bash
# 動作テスト用の EC2 を 1 台立ち上げる（Amazon Linux 2023、受信なしの SG、SSM で入る）
#
# 使い方:
#   AWS_PROFILE=<プロファイル> ./launch.sh [--bare] [名前]     名前の既定は web2
#     --bare  中身を構成せずに起動する（あとで ./configure.sh --send <名前>）
#
# 環境変数:
#   AWS_PROFILE    必須。EC2・IAM を作れる権限（管理者）
#   AWS_REGION     既定 ap-northeast-1
#   INSTANCE_TYPE  既定 t3.micro。無料枠の対象でなければ止まる（承知の上なら FORCE=1）
#   VOLUME_GB      ルートボリュームの大きさ。既定 20（EBS の無料枠は全体で 30GB）
#
# 作ったものは state/<名前>.env に書き、cleanup.sh はそこに書かれたものだけを消す。
# user-data には configure.sh --payload の出力（EC2 側の部分だけ。コメント行なし）を渡す。
set -euo pipefail
cd "$(dirname "$0")"

bare=0
if [[ ${1:-} == --bare ]]; then bare=1; shift; fi
name=${1:-web2}
: "${AWS_PROFILE:?AWS_PROFILE を指定してください（例: AWS_PROFILE=my-admin ./launch.sh）}"
export AWS_PROFILE AWS_REGION=${AWS_REGION:-ap-northeast-1}
type=${INSTANCE_TYPE:-t3.micro}
volume_gb=${VOLUME_GB:-20}
profile_name=ec2-ssm-managed
state=state/$name.env

step() { printf '\n== %s\n' "$*"; }
save() { echo "$1=$2" >> "$state"; }
die() { echo "$*" >&2; exit 1; }

[[ ! -e $state ]] || die "$state が残っています。先に ./cleanup.sh $name を実行してください"

step "認証"
aws sts get-caller-identity --query Arn --output text

step "インスタンスタイプ"
eligible=$(aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true \
  --query 'InstanceTypes[].InstanceType' --output text)
echo "無料枠の対象: $eligible"
if ! grep -qwF -- "$type" <<<"$eligible"; then
  [[ ${FORCE:-} == 1 ]] || die "$type は無料枠の対象ではありません（INSTANCE_TYPE で選び直すか、承知の上なら FORCE=1）"
  echo "注意: $type は無料枠の対象外です（FORCE=1）"
fi
arch=$(aws ec2 describe-instance-types --instance-types "$type" \
  --query 'InstanceTypes[0].ProcessorInfo.SupportedArchitectures[0]' --output text)
echo "$type: $arch"

step "名前の重複"
dup=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=$name" \
  Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId' --output text)
[[ -z $dup ]] || die "Name=$name のインスタンスが既にあります: $dup"
echo "なし"

step "AMI・VPC・サブネット"
ami=$(aws ssm get-parameter --name "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-$arch" \
  --query Parameter.Value --output text)
vpc=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
[[ $vpc != None ]] || die "既定の VPC がありません"
subnet=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$vpc" Name=default-for-az,Values=true \
  --query 'Subnets[0].SubnetId' --output text)
echo "ami=$ami vpc=$vpc subnet=$subnet"

mkdir -p state
save AWS_PROFILE "$AWS_PROFILE"
save AWS_REGION "$AWS_REGION"

step "セキュリティグループ（受信なし。送信は既定で全許可）"
sg=$(aws ec2 create-security-group --group-name "$name-sg" --description "$name: no inbound, SSM only" \
  --vpc-id "$vpc" \
  --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=$name-sg},{Key=purpose,Value=test-ec2}]" \
  --query GroupId --output text)
save SG_ID "$sg"
echo "$sg"

step "インスタンスプロファイル $profile_name"
if aws iam get-instance-profile --instance-profile-name "$profile_name" >/dev/null 2>&1; then
  echo "既存のものを使います"
else
  aws iam create-role --role-name "$profile_name" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  save CREATED_ROLE "$profile_name"
  aws iam attach-role-policy --role-name "$profile_name" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  aws iam create-instance-profile --instance-profile-name "$profile_name" >/dev/null
  save CREATED_PROFILE "$profile_name"
  aws iam add-role-to-instance-profile --instance-profile-name "$profile_name" --role-name "$profile_name"
  echo "作りました"
fi

step "起動（IMDSv2 必須・CPU クレジット standard・gp3 ${volume_gb}GB）"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
user_data=()
if (( ! bare )); then
  ./configure.sh --payload > "$work/user-data.sh"
  bash -n "$work/user-data.sh"
  (( $(wc -c < "$work/user-data.sh") < 16384 )) || die "user-data が上限（16KB）を超えています"
  user_data=(--user-data "file://$work/user-data.sh")
fi
err=$work/err
for try in 1 2 3 4 5 6; do
  if iid=$(aws ec2 run-instances \
      --image-id "$ami" --instance-type "$type" \
      --network-interfaces "DeviceIndex=0,SubnetId=$subnet,Groups=$sg,AssociatePublicIpAddress=true" \
      --iam-instance-profile "Name=$profile_name" \
      --metadata-options HttpTokens=required,HttpEndpoint=enabled \
      --credit-specification CpuCredits=standard \
      --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=$volume_gb,VolumeType=gp3,DeleteOnTermination=true}" \
      ${user_data[@]+"${user_data[@]}"} \
      --tag-specifications \
        "ResourceType=instance,Tags=[{Key=Name,Value=$name},{Key=purpose,Value=test-ec2}]" \
        "ResourceType=volume,Tags=[{Key=Name,Value=$name},{Key=purpose,Value=test-ec2}]" \
      --query 'Instances[0].InstanceId' --output text 2>"$err"); then
    break
  fi
  grep -q 'Invalid IAM Instance Profile' "$err" && (( try < 6 )) || { cat "$err" >&2; exit 1; }
  echo "インスタンスプロファイルの反映待ち（$try/6）"
  sleep 10
done
save INSTANCE_ID "$iid"
echo "$iid"

step "起動を待つ（2〜3 分）"
aws ec2 wait instance-status-ok --instance-ids "$iid"
echo "status ok"

step "SSM の管理下に入るのを待つ"
ping=None
for _ in $(seq 30); do
  ping=$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$iid" \
    --query 'InstanceInformationList[0].PingStatus' --output text)
  [[ $ping == Online ]] && break
  sleep 10
done
[[ $ping == Online ]] || die "SSM で Online になりません（インスタンスプロファイルと送信経路を確かめてください）"
echo "Online"

cat <<EOF

$name を起動しました: ${iid}（$type, ${arch}）
記録: $(pwd)/$state

次の手順:
EOF
if (( bare )); then
  echo "  ./configure.sh --send $name     # 中身を構成する"
else
  echo "  ./configure.sh --check $name    # 起動時の構成（5 分ほど）の完了を待って結果を見る"
fi
cat <<EOF
  aws ssm start-session --target $iid --profile $AWS_PROFILE --region $AWS_REGION   # 中に入る（session-manager-plugin が要る）
  ./cleanup.sh $name              # 片付け
EOF
