#!/bin/bash
# setup.sh — deploy and configure homecagev3 web app on cam2
# Run this on cam2: bash setup.sh
set -e

APPDIR="/home/ab-ivnc/homecagev3"
VENV="$APPDIR/venv"

echo "==> Creating app directory"
mkdir -p "$APPDIR"

echo "==> Creating Python virtualenv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$APPDIR/requirements.txt" -q
echo "    Dependencies installed."

echo ""
echo "==> Setting admin password"
read -rsp "Enter admin password: " PASSWORD
echo
HASH=$("$VENV/bin/python3" -c \
  "from passlib.context import CryptContext; print(CryptContext(schemes=['bcrypt']).hash('$PASSWORD'))")

echo "==> Writing .env"
cat > "$APPDIR/.env" <<EOF
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=$HASH
EOF
chmod 600 "$APPDIR/.env"
echo "    .env written."

echo "==> Installing NGINX config"
sudo cp "$APPDIR/nginx/homecagev3.conf" /etc/nginx/sites-available/homecagev3
sudo ln -sf /etc/nginx/sites-available/homecagev3 /etc/nginx/sites-enabled/homecagev3
sudo nginx -t && sudo systemctl reload nginx
echo "    NGINX configured."

echo "==> Installing systemd service"
sudo cp "$APPDIR/homecagev3.service" /etc/systemd/system/homecagev3.service
sudo systemctl daemon-reload
sudo systemctl enable homecagev3
sudo systemctl restart homecagev3
echo "    Service started."

echo ""
echo "==> Done. App available at http://100.70.127.43"
echo "    Logs: sudo journalctl -u homecagev3 -f"
