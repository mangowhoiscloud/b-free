#!/bin/sh
# Harden a Debian (trixie, usr-merged) based agent sandbox image at build time. Runs as root.
# Used via: RUN --mount=type=bind,source=images/common/harden-debian.sh,target=/tmp/harden-debian.sh sh /tmp/harden-debian.sh
# Keeps /var/lib/dpkg/status so SBOM and vulnerability scanners can still identify OS packages.
set -eu

PATH_DIRS="/usr/bin /usr/sbin /usr/local/bin /usr/local/sbin"

# 1. Package managers: apt, dpkg, pip, ensurepip (venv's pip bootstrap).
rm -f /usr/bin/apt /usr/bin/apt-* /usr/bin/dpkg /usr/bin/dpkg-* /usr/sbin/dpkg-* \
      /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*
rm -rf /usr/lib/apt /usr/lib/dpkg /etc/apt /var/lib/apt /var/cache/apt /var/log/apt
rm -f /usr/bin/update-alternatives /usr/sbin/start-stop-daemon
rm -f /etc/cron.daily/apt-compat /etc/cron.daily/dpkg /etc/logrotate.d/apt /etc/logrotate.d/dpkg
for sp in /usr/local/lib/python3*/site-packages /usr/lib/python3/dist-packages; do
  [ -d "$sp" ] || continue
  rm -rf "$sp"/pip "$sp"/pip-*.dist-info
done
rm -rf /usr/local/lib/python3*/ensurepip

# 2. Interpreters and shells we do not ship: perl, bash/rbash (dash stays as the only /bin/sh).
rm -f /usr/bin/perl /usr/bin/perl5* /usr/bin/perldoc /usr/bin/perlivp /usr/bin/bash /usr/bin/rbash /usr/bin/bashbug
rm -rf /usr/lib/*-linux-gnu/perl-base /usr/lib/*-linux-gnu/perl /usr/share/perl /usr/share/perl5

# 3. Account, privilege and mount tools.
for b in su runuser passwd chsh chfn newgrp sg gpasswd login mount umount sudo; do
  rm -f "/usr/bin/$b" "/usr/sbin/$b"
done

# 4. Network clients (none expected in these bases; removed defensively).
for b in curl wget nc nc.openbsd nc.traditional netcat ncat ssh scp sftp telnet ftp; do
  rm -f "/usr/bin/$b" "/usr/local/bin/$b"
done

# 5. Scripts left without an interpreter (perl/bash shebang), then dangling symlinks.
for d in $PATH_DIRS; do
  for f in "$d"/*; do
    [ -f "$f" ] || continue
    first=$(head -c 64 "$f" 2>/dev/null | head -n 1 | tr -d '\0') || first=""
    case "$first" in
      '#!'*perl*|'#!'*bash*) rm -f "$f" ;;
    esac
  done
done
for d in $PATH_DIRS; do
  for f in "$d"/*; do
    if [ -L "$f" ] && [ ! -e "$f" ]; then rm -f "$f"; fi
  done
done

# 6. No setuid/setgid binaries.
find / -xdev -perm /6000 -type f -exec chmod a-s {} +

# 7. Exactly one POSIX shell: make /usr/bin/sh the dash binary itself and drop the `dash` name.
if [ -f /usr/bin/dash ]; then
  cp -p /usr/bin/dash /usr/bin/sh.new
  mv -f /usr/bin/sh.new /usr/bin/sh
  rm -f /usr/bin/dash
fi
