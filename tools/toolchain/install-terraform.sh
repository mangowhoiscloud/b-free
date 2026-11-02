#!/usr/bin/env bash
# HashiCorp 공식 릴리스에서 Terraform을 받아 SHA256을 확인한 뒤 설치합니다.
# Homebrew(hashicorp/tap)가 막힌 macOS(Command Line Tools 판 문제)에서 씁니다.
set -euo pipefail
VERSION="${TERRAFORM_VERSION:-1.17.1}"
ARCH="${TERRAFORM_ARCH:-darwin_arm64}"
DEST="${TERRAFORM_DEST:-$HOME/.local/bin}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
base="https://releases.hashicorp.com/terraform/${VERSION}"
curl -fsSLO "${base}/terraform_${VERSION}_${ARCH}.zip"
curl -fsSLO "${base}/terraform_${VERSION}_SHA256SUMS"
grep "terraform_${VERSION}_${ARCH}.zip" "terraform_${VERSION}_SHA256SUMS" | shasum -a 256 -c -
unzip -oq "terraform_${VERSION}_${ARCH}.zip" terraform
mkdir -p "$DEST"; install -m 0755 terraform "$DEST/terraform"
"$DEST/terraform" version | head -1
