#!/usr/bin/env bash
# Borra las VPCs que Databricks deja al destruir un workspace (etiqueta databricks-WorkerEnvId(...)).
# Si se acumulan, el próximo `terraform apply` falla: AWS permite 5 VPCs por región.
#
# Uso (después del destroy, perfil smartestate):  bash scripts/delete_orphan_vpcs.sh
#
# Seguro de repetir: solo borra lo que queda. Salta toda VPC con interfaces de red (un workspace o un
# NAT vivo). Los endpoints se borran en segundo plano: si da DependencyViolation, esperar 2 min y repetir.
set -u
export AWS_PAGER=""
R=(--region us-east-1 --profile smartestate)

vpcs=$(aws ec2 describe-vpcs "${R[@]}" --filters "Name=tag:Name,Values=databricks-WorkerEnvId*" \
  --query 'Vpcs[].VpcId' --output text)
[[ -z "$vpcs" ]] && echo "No hay VPCs de Databricks."

for v in $vpcs; do
  echo "== $v"
  enis=$(aws ec2 describe-network-interfaces "${R[@]}" --filters Name=vpc-id,Values="$v" \
    --query 'length(NetworkInterfaces)')
  if [[ "$enis" != "0" ]]; then
    echo "   SALTADA: tiene $enis interfaces de red (¿workspace o NAT vivo?)"
    continue
  fi

  for e in $(aws ec2 describe-vpc-endpoints "${R[@]}" --filters Name=vpc-id,Values="$v" --query 'VpcEndpoints[].VpcEndpointId' --output text); do
    echo "   endpoint $e"; aws ec2 delete-vpc-endpoints "${R[@]}" --vpc-endpoint-ids "$e" >/dev/null
  done
  for g in $(aws ec2 describe-internet-gateways "${R[@]}" --filters Name=attachment.vpc-id,Values="$v" --query 'InternetGateways[].InternetGatewayId' --output text); do
    echo "   internet gateway $g"
    aws ec2 detach-internet-gateway "${R[@]}" --internet-gateway-id "$g" --vpc-id "$v"
    aws ec2 delete-internet-gateway "${R[@]}" --internet-gateway-id "$g"
  done
  for s in $(aws ec2 describe-subnets "${R[@]}" --filters Name=vpc-id,Values="$v" --query 'Subnets[].SubnetId' --output text); do
    echo "   subnet $s"; aws ec2 delete-subnet "${R[@]}" --subnet-id "$s"
  done
  # La tabla principal cae con la VPC; las secundarias hay que borrarlas.
  for t in $(aws ec2 describe-route-tables "${R[@]}" --filters Name=vpc-id,Values="$v" --query 'RouteTables[?Associations[0].Main != `true`].RouteTableId' --output text); do
    echo "   route table $t"; aws ec2 delete-route-table "${R[@]}" --route-table-id "$t"
  done
  for sg in $(aws ec2 describe-security-groups "${R[@]}" --filters Name=vpc-id,Values="$v" --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text); do
    echo "   security group $sg"; aws ec2 delete-security-group "${R[@]}" --group-id "$sg" >/dev/null
  done
  aws ec2 delete-vpc "${R[@]}" --vpc-id "$v" && echo "   BORRADA"
done

echo; echo "VPCs en us-east-1:"
aws ec2 describe-vpcs "${R[@]}" --query 'Vpcs[].[VpcId,IsDefault]' --output text
echo "IPs elásticas sueltas (vacío = ninguna):"
aws ec2 describe-addresses "${R[@]}" --query 'Addresses[?AssociationId==null].AllocationId' --output text
