#!/bin/bash
# QED (Query Equivalence Decider) Installation Script
# This script installs Nix package manager and QED tools for SQL query equivalence verification

apt-get update
apt-get install -y curl git

groupadd -r nixbld 2>/dev/null || true
for i in $(seq 1 32); do 
    id "nixbld$i" &>/dev/null || \
    useradd -r -g nixbld -G nixbld -d /var/empty -s /sbin/nologin -c "Nix build user $i" "nixbld$i" 2>/dev/null || true
done

sh <(curl -L https://nixos.org/nix/install) --no-daemon

source ~/.nix-profile/etc/profile.d/nix.sh

mkdir -p ~/.config/nix
cat > ~/.config/nix/nix.conf << 'EOF'
experimental-features = nix-command flakes
substituters = https://mirrors.tuna.tsinghua.edu.cn/nix-channels/store https://cache.nixos.org/
trusted-substituters = https://mirrors.tuna.tsinghua.edu.cn/nix-channels/store
EOF

export PATH="$HOME/.nix-profile/bin:$PATH"
nix --version

# (Optional) Clone Z3 repository to speed up builds (if SSH access to GitHub is needed)
git clone git@github.com:Z3Prover/z3.git /tmp/z3 2>/dev/null || true
export GIT_CACHE_PATH=/tmp/z3

nix shell github:qed-solver/parser github:qed-solver/prover
