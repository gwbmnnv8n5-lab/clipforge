#!/usr/bin/env bash
# 拼影工坊 一键安装 / 升级脚本
# 用法：sudo bash install.sh            （首次安装会让你设置登录密码）
#       sudo bash install.sh            （再次运行 = 升级代码，数据和配置保留）
set -e
APP_DIR=/opt/clipforge
ENV_FILE=/etc/clipforge.env
PORT_DEFAULT=8800
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" = "0" ] || { echo "请用 root 运行：sudo bash install.sh"; exit 1; }
echo "==> 安装依赖（python3、ffmpeg）"
if command -v apt-get >/dev/null; then
  apt-get update -y >/dev/null && apt-get install -y python3 ffmpeg openssl >/dev/null || true
elif command -v dnf >/dev/null; then
  dnf install -y python3 openssl >/dev/null || true; dnf install -y ffmpeg >/dev/null 2>&1 || true
elif command -v yum >/dev/null; then
  yum install -y python3 openssl >/dev/null || true; yum install -y ffmpeg >/dev/null 2>&1 || true
fi
command -v python3 >/dev/null || { echo "没装上 python3，请手动安装后重试"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' || { echo "需要 Python 3.8 以上"; exit 1; }

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "==> 系统源里没有 ffmpeg，下载静态版"
  ARCH=$(uname -m); case "$ARCH" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; *) A="";; esac
  if [ -n "$A" ]; then
    TMP=$(mktemp -d)
    if curl -fsSL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${A}-static.tar.xz" -o "$TMP/f.tar.xz"; then
      tar -xJf "$TMP/f.tar.xz" -C "$TMP" && cp "$TMP"/ffmpeg-*-static/ffmpeg "$TMP"/ffmpeg-*-static/ffprobe /usr/local/bin/ && chmod +x /usr/local/bin/ff*
    fi
    rm -rf "$TMP"
  fi
  command -v ffmpeg >/dev/null && echo "    ffmpeg 已安装" || echo "    ⚠️ ffmpeg 安装失败：可以生成视频，但「合成成片」不可用，请手动安装 ffmpeg"
fi

echo "==> 复制程序到 $APP_DIR"
mkdir -p "$APP_DIR/static"
cp "$SRC"/app.py "$SRC"/providers.py "$SRC"/render.py "$SRC"/router.py "$APP_DIR/"
cp "$SRC"/static/* "$APP_DIR/static/"
mkdir -p "$APP_DIR/data"

if [ ! -f "$ENV_FILE" ]; then
  echo "==> 首次安装，设置登录密码"
  while true; do
    read -r -s -p "请设置登录密码（至少 8 位）: " PW; echo
    [ ${#PW} -ge 8 ] && break; echo "太短了，再来一次"
  done
  read -r -p "端口 [${PORT_DEFAULT}]: " PORT; PORT=${PORT:-$PORT_DEFAULT}
  umask 077
  cat > "$ENV_FILE" <<EOF
CF_BIND=0.0.0.0
CF_PORT=$PORT
CF_PASSWORD=$PW
CF_DATA=$APP_DIR/data
EOF
fi
PORT=$(grep '^CF_PORT=' "$ENV_FILE" | cut -d= -f2)

cat > /etc/systemd/system/clipforge.service <<EOF
[Unit]
Description=ClipForge AI video studio
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$(command -v python3) $APP_DIR/app.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable clipforge >/dev/null 2>&1
systemctl restart clipforge

if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow "$PORT"/tcp >/dev/null; fi
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then firewall-cmd --add-port="$PORT"/tcp --permanent >/dev/null && firewall-cmd --reload >/dev/null; fi

sleep 1.5
if systemctl is-active --quiet clipforge; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo
  echo "✅ 拼影工坊已启动"
  echo "   访问地址：http://<你的服务器公网IP>:$PORT   （内网 IP：$IP）"
  echo "   云服务器记得在控制台「安全组」放行 TCP $PORT 端口"
  echo "   查看日志：journalctl -u clipforge -f"
  echo "   修改密码：编辑 $ENV_FILE 后执行 systemctl restart clipforge"
else
  echo "❌ 启动失败，查看原因：journalctl -u clipforge -n 50"
fi
