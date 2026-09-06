#!/usr/bin/env bash
set -Eeuo pipefail

apt update -y
apt install -y apt-transport-https ca-certificates curl gnupg

mkdir -p /usr/share/keyrings
rm -f \
  /usr/share/keyrings/nodesource.gpg \
  /etc/apt/sources.list.d/nodesource.list \
  /etc/apt/sources.list.d/nodesource.sources

curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg
chmod 644 /usr/share/keyrings/nodesource.gpg

arch="$(dpkg --print-architecture)"
cat > /etc/apt/sources.list.d/nodesource.sources <<EOF
Types: deb
URIs: https://deb.nodesource.com/node_24.x
Suites: nodistro
Components: main
Architectures: $arch
Signed-By: /usr/share/keyrings/nodesource.gpg
EOF

cat > /etc/apt/preferences.d/nodejs <<'EOF'
Package: nodejs
Pin: origin deb.nodesource.com
Pin-Priority: 600
EOF

apt update -y
apt install -y nodejs
npm install --global yarn@1.22.22

node -v
npm -v
yarn -v
