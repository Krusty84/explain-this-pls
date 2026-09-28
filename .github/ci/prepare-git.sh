#!/usr/bin/env bash
# Installation is the only network-enabled phase. Analysis runs use unshare --net.
set -euo pipefail
build=$1
exec > >(tee git-build.log) 2>&1
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl xz-utils util-linux

case "$build" in
  upstream-2.34.1)
    apt-get install -y --no-install-recommends build-essential libcurl4-openssl-dev libexpat1-dev zlib1g-dev gettext
    curl -fL --retry 3 https://www.kernel.org/pub/software/scm/git/git-2.34.1.tar.xz -o /tmp/git-2.34.1.tar.xz
    echo '3a0755dd1cfab71a24dd96df3498c29cd0acd13b04f3d08bf933e81286db802c  /tmp/git-2.34.1.tar.xz' | sha256sum -c -
    tar -xJf /tmp/git-2.34.1.tar.xz -C /tmp
    make -C /tmp/git-2.34.1 -j2 prefix=/opt/git-2.34.1 NO_TCLTK=YesPlease all install
    export PATH="/opt/git-2.34.1/bin:$PATH"
    echo /opt/git-2.34.1/bin >> "$GITHUB_PATH"
    test "$(git --version)" = 'git version 2.34.1'
    ;;
  jammy-updates)
    apt-get install -y --no-install-recommends git
    dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' git git-man
    test "$(git --version)" = 'git version 2.34.1'
    ;;
  jammy-command-scope-broken)
    # Security-patched Ubuntu build before USN-5376-4 fixed command-scope trust.
    # Freeze both the archive date and package revision; never silently use latest.
    cat > /tmp/git-snapshot.list <<'EOF'
deb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/20260201T000000Z jammy main universe
deb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/20260201T000000Z jammy-updates main universe
deb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/20260201T000000Z jammy-security main universe
EOF
    snapshot=(-o Dir::Etc::sourcelist=/tmp/git-snapshot.list -o Dir::Etc::sourceparts=-)
    apt-get "${snapshot[@]}" update
    apt-get "${snapshot[@]}" install -y --no-install-recommends --allow-downgrades \
      git=1:2.34.1-1ubuntu1.15 git-man=1:2.34.1-1ubuntu1.15
    dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' git git-man
    test "$(dpkg-query -W -f='${Version}' git)" = '1:2.34.1-1ubuntu1.15'
    test "$(git --version)" = 'git version 2.34.1'
    ;;
  *) echo "Unknown build: $build" >&2; exit 1 ;;
esac
command -v git
readlink -f "$(command -v git)"
git --version
