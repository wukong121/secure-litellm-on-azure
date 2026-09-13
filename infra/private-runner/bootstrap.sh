#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

download() {
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --tlsv1.2 --retry 3 --connect-timeout 20 --max-time 900 --output "$2" "$1"
}

verified_download() {
  download "$1" "$2"
  printf '%s  %s\n' "$3" "$2" | sha256sum --check --status
}

prepare_work_disk() {
  local disk_path filesystem disk_uuid existing_source
  udevadm settle --timeout=60
  disk_path="$(readlink -e /dev/disk/azure/scsi1/lun0)"
  [[ -b "$disk_path" && "$(lsblk -nr -o TYPE "$disk_path")" == disk ]] || {
    printf '%s\n' 'Expected an unpartitioned Azure SCSI LUN 0 data disk.' >&2
    return 1
  }
  filesystem="$(lsblk -dn -o FSTYPE "$disk_path")"
  if [[ -z "$filesystem" ]]; then
    [[ -z "$(wipefs --no-act --noheadings --output TYPE "$disk_path")" ]] || return 1
    mkfs.ext4 -L llmgw-runner "$disk_path"
  else
    [[ "$filesystem" == ext4 && "$(e2label "$disk_path")" == llmgw-runner ]] || {
      printf '%s\n' 'Refusing to format or adopt an existing unrelated filesystem.' >&2
      return 1
    }
  fi
  disk_uuid="$(blkid -s UUID -o value "$disk_path")"
  [[ -n "$disk_uuid" ]] || return 1
  existing_source="$(awk '$2 == "/srv/runner" {print $1}' /etc/fstab)"
  [[ -z "$existing_source" || "$existing_source" == "UUID=$disk_uuid" ]] || return 1
  install -d -m 0750 /srv/runner
  if [[ -z "$existing_source" ]]; then
    printf 'UUID=%s /srv/runner ext4 defaults,nodev,nosuid 0 2\n' "$disk_uuid" >> /etc/fstab
  fi
  if ! mountpoint -q /srv/runner; then
    mount /srv/runner
  fi
  [[ "$(findmnt -n -o UUID --target /srv/runner)" == "$disk_uuid" ]] || return 1
}

add_apt_repository() {
  local repository_name="$1" key_url="$2" source_line="$3"
  download "$key_url" "$scratch/$repository_name.key"
  gpg --batch --yes --dearmor --output "/etc/apt/keyrings/$repository_name.gpg" "$scratch/$repository_name.key"
  chmod 0644 "/etc/apt/keyrings/$repository_name.gpg"
  printf '%s\n' "$source_line" > "/etc/apt/sources.list.d/$repository_name.list"
  chmod 0644 "/etc/apt/sources.list.d/$repository_name.list"
}

main() {
  [[ "$EUID" == 0 ]] || { printf '%s\n' 'Run only as root on the newly provisioned runner VM.' >&2; return 1; }
  [[ "$(uname -m)" == x86_64 ]] || return 1
  source /etc/os-release
  [[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || return 1
  if [[ -f /var/lib/llmgw-runner/bootstrap-complete ]]; then
    printf '%s\n' 'Bootstrap already completed; no installation or registration changes made.'
    return 0
  fi
  install -d -m 0700 /var/lib/llmgw-runner
  install -m 0600 /dev/null /var/log/llmgw-runner-bootstrap.log
  exec > >(tee -a /var/log/llmgw-runner-bootstrap.log) 2>&1
  trap 'printf "Bootstrap failed at line %s; inspect the private bootstrap log.\n" "$LINENO" >&2' ERR
  if [[ "$(readlink -f "${BASH_SOURCE[0]}")" != /usr/local/sbin/llmgw-runner-bootstrap ]]; then
    install -m 0700 "${BASH_SOURCE[0]}" /usr/local/sbin/llmgw-runner-bootstrap
  fi
  prepare_work_disk
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  apt-get update
  apt-get install -y ca-certificates curl gnupg jq git unzip tar openssl skopeo \
    python3 python3-venv postgresql-client-16
  scratch="$(mktemp -d /srv/runner/bootstrap.XXXXXX)"
  trap 'rm -rf -- "$scratch"' EXIT
  install -d -m 0755 /etc/apt/keyrings
  add_apt_repository docker https://download.docker.com/linux/ubuntu/gpg \
    'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable'
  add_apt_repository azure-cli https://packages.microsoft.com/keys/microsoft.asc \
    'deb [arch=amd64 signed-by=/etc/apt/keyrings/azure-cli.gpg] https://packages.microsoft.com/repos/azure-cli/ noble main'
  add_apt_repository nodejs https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    'deb [arch=amd64 signed-by=/etc/apt/keyrings/nodejs.gpg] https://deb.nodesource.com/node_24.x nodistro main'
  add_apt_repository kubernetes https://pkgs.k8s.io/core:/stable:/v1.35/deb/Release.key \
    'deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/v1.35/deb/ /'
  download https://cli.github.com/packages/githubcli-archive-keyring.gpg /etc/apt/keyrings/github-cli.gpg
  chmod 0644 /etc/apt/keyrings/github-cli.gpg
  printf '%s\n' 'deb [arch=amd64 signed-by=/etc/apt/keyrings/github-cli.gpg] https://cli.github.com/packages stable main' > /etc/apt/sources.list.d/github-cli.list
  chmod 0644 /etc/apt/sources.list.d/github-cli.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin azure-cli nodejs kubectl gh
  apt-mark hold kubectl
  systemctl stop docker.service docker.socket
  getent passwd actions-runner >/dev/null || useradd --system --create-home --shell /bin/bash actions-runner
  usermod -aG docker actions-runner
  chown root:actions-runner /srv/runner
  chmod 0750 /srv/runner
  install -d -m 0750 -o actions-runner -g actions-runner /srv/runner/agent /srv/runner/work /srv/runner/toolcache
  install -d -m 0755 /etc/docker /etc/systemd/system/docker.service.d
  if [[ -f /etc/docker/daemon.json ]]; then
    jq -e '."data-root" == "/srv/runner/docker"' /etc/docker/daemon.json >/dev/null
  else
    printf '%s\n' '{"data-root":"/srv/runner/docker","log-driver":"local"}' > /etc/docker/daemon.json
  fi
  printf '%s\n' '[Unit]' 'RequiresMountsFor=/srv/runner' '[Service]' 'ExecStartPre=/usr/bin/mountpoint -q /srv/runner' \
    > /etc/systemd/system/docker.service.d/runner-storage.conf
  systemctl daemon-reload
  systemctl enable --now docker.service
  verified_download https://github.com/Azure/kubelogin/releases/download/v0.2.19/kubelogin-linux-amd64.zip \
    "$scratch/kubelogin.zip" ebaeff02aa899c5cae6a2b954b64fc02738185319df2570f7dc053451efa4b2f
  unzip -q "$scratch/kubelogin.zip" -d "$scratch/kubelogin"
  install -m 0755 "$scratch/kubelogin/bin/linux_amd64/kubelogin" /usr/local/bin/kubelogin
  verified_download https://github.com/anchore/syft/releases/download/v1.51.1/syft_1.51.1_linux_amd64.tar.gz \
    "$scratch/syft.tar.gz" 8fcb33017a0dc1058298c923c436d19dfa68ae93968e0b423248542e3afb9fc3
  tar -xzf "$scratch/syft.tar.gz" -C "$scratch" syft
  install -m 0755 "$scratch/syft" /usr/local/bin/syft
  verified_download https://github.com/aquasecurity/trivy/releases/download/v0.74.0/trivy_0.74.0_Linux-64bit.tar.gz \
    "$scratch/trivy.tar.gz" 2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a
  tar -xzf "$scratch/trivy.tar.gz" -C "$scratch" trivy
  install -m 0755 "$scratch/trivy" /usr/local/bin/trivy
  verified_download https://github.com/sigstore/cosign/releases/download/v3.1.3/cosign-linux-amd64 \
    "$scratch/cosign" 4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71
  install -m 0755 "$scratch/cosign" /usr/local/bin/cosign
  verified_download https://github.com/Azure/bicep/releases/download/v0.46.1/bicep-linux-x64 \
    "$scratch/bicep" 3e011d629ea4311b7a7dd8f0040ab2b1a072ea4ff5d02cb75e0e55a9a6703fb9
  install -m 0755 "$scratch/bicep" /usr/local/bin/bicep
  verified_download https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz \
    "$scratch/runner.tar.gz" 70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613
  [[ ! -f /srv/runner/agent/.runner ]] || { printf '%s\n' 'Refusing to overwrite a registered runner.' >&2; return 1; }
  tar -xzf "$scratch/runner.tar.gz" -C /srv/runner/agent
  bash /srv/runner/agent/bin/installdependencies.sh
  printf '%s\n' 'AGENT_TOOLSDIRECTORY=/srv/runner/toolcache' 'RUNNER_TOOL_CACHE=/srv/runner/toolcache' > /srv/runner/agent/.env
  chown -R actions-runner:actions-runner /srv/runner/agent
  cat > /usr/local/sbin/llmgw-runner-register <<'REGISTER'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$EUID" == 0 ]] || { printf '%s\n' 'Run this registration helper using sudo.' >&2; exit 1; }
test -f /var/lib/llmgw-runner/bootstrap-complete
cd /srv/runner/agent
if [[ ! -f .runner ]]; then
  runuser --user actions-runner -- ./config.sh --name "$(hostname -s)" \
    --labels 'llmgw-__LLMGW_ENVIRONMENT__-private' --work /srv/runner/work
fi
if [[ ! -f .service ]]; then
  ./svc.sh install actions-runner
fi
./svc.sh start
REGISTER
  chmod 0700 /usr/local/sbin/llmgw-runner-register
  install -d -m 0755 /usr/local/share/llmgw-runner
  {
    date -u +%FT%TZ
    dpkg-query -W ca-certificates curl git jq python3 postgresql-client-16 docker-ce azure-cli nodejs kubectl gh
    node --version
    npm --version
    kubelogin --version
    bicep --version
    syft version
    trivy --version
    cosign version
    runuser --user actions-runner -- docker info --format '{{.DockerRootDir}}'
    runuser --user actions-runner -- /srv/runner/agent/bin/Runner.Listener --version
  } > /usr/local/share/llmgw-runner/tool-versions.txt
  touch /var/lib/llmgw-runner/bootstrap-complete
  printf '%s\n' 'Tools installed. GitHub registration, OIDC permissions and private connectivity still require verification.'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi