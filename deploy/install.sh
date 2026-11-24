#!/usr/bin/env bash
# Install skynet-live-clipper on a small Ubuntu/Debian VPS (4 vCPU / 8 GB, no GPU).
# NOT run by the author against any server. Read before running. Usage: sudo bash deploy/install.sh
set -euo pipefail
APP=/opt/skynet-live-clipper
DATA=/var/lib/clipper-data
apt-get update
apt-get install -y python3 python3-venv ffmpeg fonts-dejavu-core
id clipper >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin clipper
mkdir -p "$APP" "$DATA"
rsync -a --exclude .git --exclude __pycache__ "$(dirname "$0")/../" "$APP/"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt" yt-dlp
# VPS config: Linux paths, light profile, libx264, small whisper on CPU
sed -e 's#"data/#"'"$DATA"'/#g' -e 's#data_root: "data"#data_root: "'"$DATA"'"#' \
    -e 's#encoder: "h264_nvenc"#encoder: "libx264"#' \
    -e 's#device: "cuda"#device: "cpu"#' -e 's#compute_type: "float16"#compute_type: "int8"#' \
    -e 's#mode: file#mode: live#' \
    "$APP/config.yaml" > "$APP/config.vps.yaml"
# the claude CLI judge is optional on the VPS; without it the deterministic fallback scores clips
chown -R clipper:clipper "$APP" "$DATA"
cp "$APP/deploy/skynet-live-clipper.service" /etc/systemd/system/
systemctl daemon-reload
echo "Installed. Edit $APP/config.vps.yaml (source.url), then: systemctl enable --now skynet-live-clipper"
echo "Stop cleanly: touch $DATA/STOP   |  logs: journalctl -u skynet-live-clipper -f"
