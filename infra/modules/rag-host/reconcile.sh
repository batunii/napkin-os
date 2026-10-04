#!/bin/bash
# Bring the RAG host in line with its store list (an SSM parameter Terraform
# writes): one Qdrant container per store, each on its own EBS volume, port
# and API key. Runs at every boot and whenever Terraform changes the list.
#
# Safe to re-run: a formatted volume is never reformatted, and a container is
# recreated only when its image, port, memory or key changed. A store marked
# "retired" is stopped and its disk unmounted, so Terraform can detach it
# cleanly. Data is never deleted here.
set -euo pipefail
. /etc/napkin-rag.conf        # REGION, PARAM

cfg=$(aws ssm get-parameter --region "$REGION" --name "$PARAM" \
  --query Parameter.Value --output text)
version=$(jq -r .qdrant_version <<<"$cfg")
failed=0

secret() {
  aws secretsmanager get-secret-value --region "$REGION" --secret-id "$1" \
    --query SecretString --output text
}

for name in $(jq -r '.stores | keys[]' <<<"$cfg"); do
  s=$(jq -c --arg n "$name" '.stores[$n]' <<<"$cfg")
  state=$(jq -r .state <<<"$s")
  port=$(jq -r .port <<<"$s")
  mem=$(jq -r .memory_mb <<<"$s")
  vol=$(jq -r .volume_id <<<"$s")
  key_arn=$(jq -r .api_key_secret_arn <<<"$s")
  ro_arn=$(jq -r '.read_only_key_secret_arn // empty' <<<"$s")
  mnt="/var/lib/qdrant/$name"
  ctr="qdrant-$name"

  if [ "$state" = "retired" ]; then
    docker rm -f "$ctr" >/dev/null 2>&1 || true
    if mountpoint -q "$mnt"; then umount "$mnt"; fi
    sed -i "\#[[:space:]]$mnt[[:space:]]#d" /etc/fstab
    echo "$name: retired (stopped, unmounted)"
    continue
  fi

  # The volume shows up as an NVMe device named after its id; a new attachment
  # can take a moment to appear.
  dev="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_$vol"
  for _ in $(seq 1 24); do [ -e "$dev" ] && break; sleep 5; done
  if [ ! -e "$dev" ]; then
    echo "$name: volume $vol not attached yet" >&2
    failed=1
    continue
  fi

  if ! blkid "$dev" >/dev/null 2>&1; then
    mkfs.xfs "$dev"
  fi
  mkdir -p "$mnt"
  uuid=$(blkid -s UUID -o value "$dev")
  grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $mnt xfs defaults,nofail 0 2" >> /etc/fstab
  mountpoint -q "$mnt" || mount "$mnt"
  mkdir -p "$mnt/storage" "$mnt/snapshots"

  env="/etc/qdrant/$name.env"
  mkdir -p /etc/qdrant
  (
    umask 077
    {
      echo "QDRANT__SERVICE__API_KEY=$(secret "$key_arn")"
      # The house store: retrieval reads with this key and cannot write.
      [ -n "$ro_arn" ] && echo "QDRANT__SERVICE__READ_ONLY_API_KEY=$(secret "$ro_arn")"
      echo "QDRANT__TELEMETRY_DISABLED=true"
    } > "$env.new"
    mv "$env.new" "$env"
  )

  image="qdrant/qdrant:$version"
  want="$image|$port|$mem|$(sha256sum "$env" | cut -d' ' -f1)"
  have=$(docker inspect -f '{{index .Config.Labels "napkin.want"}}' "$ctr" 2>/dev/null || true)
  if [ "$have" != "$want" ]; then
    docker rm -f "$ctr" >/dev/null 2>&1 || true
    docker run -d --name "$ctr" --label "napkin.want=$want" \
      --restart unless-stopped --memory "${mem}m" \
      --env-file "$env" -p "$port:6333" \
      -v "$mnt/storage:/qdrant/storage" -v "$mnt/snapshots:/qdrant/snapshots" \
      "$image" >/dev/null
    echo "$name: started $image on :$port"
  else
    echo "$name: up to date"
  fi
done

# A container whose store left the list is stopped; its disk is Terraform's.
for c in $(docker ps -a --format '{{.Names}}' | grep '^qdrant-' || true); do
  n=${c#qdrant-}
  if ! jq -e --arg n "$n" '.stores | has($n)' <<<"$cfg" >/dev/null; then
    docker rm -f "$c" >/dev/null
    echo "$n: no longer listed, container removed"
  fi
done

exit $failed
