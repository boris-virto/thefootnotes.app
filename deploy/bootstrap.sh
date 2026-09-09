#!/usr/bin/env bash
# Run once as root from a reviewed checkout; does not fetch or update application code.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root'; exit 1; }
root=/opt/thefootnotes
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
[[ -f "$root/.env" && -d "$root/app" && -x "$root/.venv/bin/python" ]]
[[ ! -e "$root/current" && ! -L "$root/current" ]] || { echo 'Already bootstrapped; refusing to overwrite current'; exit 1; }
[[ $(python3 -c 'import sys; print("%s.%s" % sys.version_info[:2])') == 3.13 ]]
install -d -m 755 -o thefootnotes -g thefootnotes "$root/releases" "$root/incoming"
install -d -m 700 -o thefootnotes -g thefootnotes "$root/backups"
legacy="$root/releases/legacy-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 755 -o thefootnotes -g thefootnotes "$legacy"
cp -a "$root/app" "$legacy/app"
ln -s "$root/.venv" "$legacy/.venv"
basename "$legacy" > "$legacy/RELEASE"
chown -R thefootnotes:thefootnotes "$legacy"
ln -s "$legacy" "$root/current"
chown -h thefootnotes:thefootnotes "$root/current"
cp -a /etc/systemd/system/thefootnotes.service "$root/backups/service-before-releases"
if [[ -f /etc/sudoers.d/thefootnotes ]]; then
    cp -a /etc/sudoers.d/thefootnotes "$root/backups/sudoers-before-releases"
fi
printf '%s\n' 'thefootnotes ALL=(root) NOPASSWD: /usr/bin/systemctl start thefootnotes, /usr/bin/systemctl stop thefootnotes, /usr/bin/systemctl restart thefootnotes' > "$root/backups/sudoers-candidate"
visudo -cf "$root/backups/sudoers-candidate"
install -m 440 "$root/backups/sudoers-candidate" /etc/sudoers.d/thefootnotes
install -m 644 "$source_dir/thefootnotes.service" /etc/systemd/system/thefootnotes.service
systemctl daemon-reload
if systemctl restart thefootnotes && python3 "$source_dir/check_legacy.py"; then
    echo 'Bootstrap complete. Existing application is running via current; CI can now deploy.'
else
    cp -a "$root/backups/service-before-releases" /etc/systemd/system/thefootnotes.service
    systemctl daemon-reload
    systemctl restart thefootnotes
    echo 'Bootstrap failed; old systemd unit restored. Inspect logs before retrying.' >&2
    exit 1
fi
