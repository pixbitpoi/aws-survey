#!/usr/bin/env bash
# アカウントで使っているリソースを読み取り専用で列挙し、1 行 1 JSON で出す。
# 調査コンテナと同じイメージの中で、読み取り専用の一時キーで走る（ホストが /x/ に貸して bash で叩く）。
# 読むだけで、ファイルには何も書かない。
#
#   inventory.sh --region <r> [サービス名...]      無指定なら SERVICES の全部を固定順で。cost（Cost Explorer）は名指ししたときだけ
#
# 出力（順序は決定的。サービスは固定順、項目は id 順）:
#   {"kind":"service","service":"ec2","region":"ap-northeast-1","status":"ok","count":3}
#   {"kind":"item","service":"ec2","region":"ap-northeast-1","id":"i-0123","name":"web1","state":"running","extra":{"type":"t3.small","ssm":"Online"}}
#   {"kind":"service","service":"rds","region":"ap-northeast-1","status":"denied","error":"An error occurred (AccessDeniedException) ..."}
# service 行は ok / denied / error のどれでも必ず 1 行出す（権限が無いサービスも行を落とさない）。
# 項目の共通キーは id / name / state、サービス固有の値は extra に入れる（読む側が知らないキーが増えても壊れない）。
# ec2 の extra.ssm は SSM の管理下か（Online / なし / ?）。SSM が読めないときは ssm の denied 行を別に出し、? にする。
# cost は先月に課金のあったサービス（Cost Explorer。us-east-1 だけ）。id はサービス名、extra.amount は金額。
# 無指定の一覧には入れない（ls はリソースの一覧で、scan が調査の量を見積もるときに名指しで読む）。
# 終了コードは 0 = 全部 ok、1 = 読めないサービスがあった。
set -u

SERVICES=(ec2 lambda vpc s3 rds ecs elb cloudfront)
EXTRA_SERVICES=(cost)
INV_REGION="${AWS_DEFAULT_REGION:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --region)   INV_REGION="$2"; shift 2 ;;
    --region=*) INV_REGION="${1#--region=}"; shift ;;
    -*) echo "不明なオプション: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ -n "$INV_REGION" ] || { echo "--region が要ります" >&2; exit 2; }

# service 行は fd 3（= 元の標準出力）に書く。call は $(...) で受けるので、標準出力に混ぜると項目に紛れる
exec 3>&1
emit_service() {  # <service> <status> [error] [count] [region]
  jq -cn --arg s "$1" --arg st "$2" --arg e "${3:-}" --argjson n "${4:-0}" --arg r "${5:-$INV_REGION}" \
    '{kind:"service",service:$s,region:$r,status:$st} + (if $st == "ok" then {count:$n} else {error:$e} end)' >&3
}
# aws を叩いて JSON を標準出力に返す。失敗したら標準エラーの 1 行目を分類して service 行を出し、1 を返す
call() {  # <service> <aws の引数>...
  local svc="$1" out status; shift
  if out=$(aws --output json "$@" 2>&1); then printf '%s' "${out:-null}"; return 0; fi
  case "$out" in
    *AccessDenied*|*UnauthorizedOperation*|*"not authorized"*) status=denied ;;
    *) status=error ;;
  esac
  emit_service "$svc" "$status" "$(printf '%s\n' "$out" | grep -v '^$' | head -n 1)"
  return 1
}
regional() { call "$1" "$2" "$3" --region "$INV_REGION" "${@:4}"; }
# ok の service 行と、項目を id 順に出す。<service> <JSON 配列> [region]
publish() {
  local items n
  items=$(jq -c '. // []' <<< "$2")
  n=$(jq 'length' <<< "$items")
  emit_service "$1" ok "" "$n" "${3:-}"
  jq -c --arg s "$1" --arg r "${3:-$INV_REGION}" \
    'sort_by(.id)[] | {kind:"item",service:$s,region:$r,id:.id,name:(.name // ""),state:(.state // ""),extra:(.extra // {})}' <<< "$items"
}

inv_ec2() {
  local items ssm
  items=$(regional ec2 ec2 describe-instances \
    --query 'Reservations[].Instances[].{id:InstanceId,name:Tags[?Key==`Name`]|[0].Value,state:State.Name,extra:{type:InstanceType,az:Placement.AvailabilityZone,launched:LaunchTime}}') || return 1
  if ssm=$(regional ssm ssm describe-instance-information --query 'InstanceInformationList[].{id:InstanceId,ping:PingStatus}'); then
    items=$(jq --argjson ssm "${ssm:-[]}" 'map(. as $i | .extra.ssm = (([($ssm // [])[] | select(.id == $i.id) | .ping][0]) // "なし"))' <<< "$items")
  else
    items=$(jq 'map(.extra.ssm = "?")' <<< "$items")
  fi
  publish ec2 "$items"
}

inv_lambda() {
  local items
  items=$(regional lambda lambda list-functions \
    --query 'Functions[].{id:FunctionName,name:FunctionName,state:(Runtime || PackageType),extra:{handler:Handler,memory:MemorySize,modified:LastModified,size:CodeSize}}') || return 1
  publish lambda "$items"
}

inv_vpc() {
  local items
  items=$(regional vpc ec2 describe-vpcs \
    --query 'Vpcs[].{id:VpcId,name:Tags[?Key==`Name`]|[0].Value,state:State,extra:{cidr:CidrBlock,default:IsDefault}}') || return 1
  publish vpc "$items"
}

inv_s3() {
  local items
  items=$(call s3 s3api list-buckets --query 'Buckets[].{id:Name,name:Name,extra:{created:CreationDate}}') || return 1
  publish s3 "$items" global
}

inv_rds() {
  local inst clus
  inst=$(regional rds rds describe-db-instances \
    --query 'DBInstances[].{id:DBInstanceIdentifier,name:DBInstanceIdentifier,state:DBInstanceStatus,extra:{engine:Engine,version:EngineVersion,class:DBInstanceClass,cluster:DBClusterIdentifier}}') || return 1
  clus=$(regional rds rds describe-db-clusters \
    --query 'DBClusters[].{id:DBClusterIdentifier,name:DBClusterIdentifier,state:Status,extra:{engine:Engine,version:EngineVersion}}') || return 1
  publish rds "$(jq -c --argjson c "${clus:-[]}" '(. // []) + (($c // []) | map(.extra.kind = "cluster"))' <<< "$inst")"
}

inv_ecs() {
  local arns items='[]'
  arns=$(regional ecs ecs list-clusters --query 'clusterArns') || return 1
  if [ "$(jq 'length' <<< "${arns:-[]}")" -gt 0 ]; then
    # shellcheck disable=SC2046
    items=$(regional ecs ecs describe-clusters --clusters $(jq -r '.[]' <<< "$arns") \
      --query 'clusters[].{id:clusterName,name:clusterName,state:status,extra:{services:activeServicesCount,tasks:runningTasksCount,instances:registeredContainerInstancesCount}}') || return 1
  fi
  publish ecs "$items"
}

inv_elb() {
  local items
  items=$(regional elb elbv2 describe-load-balancers \
    --query 'LoadBalancers[].{id:LoadBalancerName,name:LoadBalancerName,state:State.Code,extra:{type:Type,scheme:Scheme,dns:DNSName}}') || return 1
  publish elb "$items"
}

inv_cloudfront() {
  local items
  items=$(call cloudfront cloudfront list-distributions \
    --query 'DistributionList.Items[].{id:Id,name:(Aliases.Items[0] || DomainName),state:Status,extra:{domain:DomainName,enabled:Enabled,comment:Comment}}') || return 1
  publish cloudfront "$items" global
}

# 先月の 1 日から今月の 1 日まで（Cost Explorer の End は含まない）。date -d に頼らず月を数える
inv_cost() {
  local y m ly lm items
  y=$(date -u +%Y); m=$((10#$(date -u +%m)))
  if [ "$m" -eq 1 ]; then ly=$((y - 1)); lm=12; else ly=$y; lm=$((m - 1)); fi
  items=$(call cost ce get-cost-and-usage --region us-east-1 \
    --time-period "Start=$(printf '%04d-%02d-01' "$ly" "$lm"),End=$(printf '%04d-%02d-01' "$y" "$m")" \
    --granularity MONTHLY --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE \
    --query 'ResultsByTime[0].Groups[].{id:Keys[0],name:Keys[0],extra:{amount:Metrics.UnblendedCost.Amount,unit:Metrics.UnblendedCost.Unit}}') || return 1
  # 金額が 0 のサービスは落とす（無料枠・過去の名残で行だけ残る）
  publish cost "$(jq -c '(. // []) | map(select((.extra.amount | tonumber? // 0) > 0))' <<< "$items")" global
}

[ $# -gt 0 ] || set -- "${SERVICES[@]}"
for s in "$@"; do
  case " ${SERVICES[*]} ${EXTRA_SERVICES[*]} " in
    *" $s "*) ;;
    *) echo "知らないサービスです: ${s}（${SERVICES[*]} ${EXTRA_SERVICES[*]}）" >&2; exit 2 ;;
  esac
done
failed=0
for s in "$@"; do "inv_$s" || failed=1; done
exit "$failed"
