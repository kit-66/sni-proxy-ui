#!/bin/sh
set -euo pipefail

AUTH_FILE="/data/ui-auth"
DOMAINS_FILE="/data/domains.json"

# --- Логика авторизации ---
print_creds() {
  cat <<EOF
============================================
  UI LOGIN:  ${USER}
  UI PASS:   ${PASS}
  (see file: ${AUTH_FILE})
============================================
EOF
}

if [ -n "${UI_USER:-}" ] && [ -n "${SNI_UI_PASSWORD:-}" ]; then
  # Если заданы в ENV — используем их, файл не трогаем
  echo "[start] UI credentials from ENV (user=${UI_USER}) — см. ваш .env, файл не используется"
  export SNI_UI_PASSWORD="${SNI_UI_PASSWORD}"
else
  # Иначе работаем с файлом
  if [ ! -f "${AUTH_FILE}" ]; then
    echo "[start] Generating new UI credentials..."
    PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
    USER="admin"

    printf "%s:%s" "${USER}" "${PASS}" > "${AUTH_FILE}"
    chmod 600 "${AUTH_FILE}"

    print_creds
    export SNI_UI_PASSWORD="${PASS}"
  else
    # Читаем существующий файл — пароль стабилен между рестартами
    IFS=':' read -r USER PASS < "${AUTH_FILE}" || {
      echo "[error] Corrupted auth file: ${AUTH_FILE}" >&2
      exit 1
    }
    echo "[start] Using existing UI credentials from ${AUTH_FILE}"
    print_creds   # печатаем снова — актуальные креды всегда видны в свежем логе
    export SNI_UI_PASSWORD="${PASS}"
  fi
fi

# --- Инициализация domains.json в bind-mount ---
if [ ! -f "${DOMAINS_FILE}" ]; then
  echo "[start] Initializing domains.json..."
  printf "[]" > "${DOMAINS_FILE}"
fi

# --- Подготовка окружения ---
mkdir -p /var/log/nginx /etc/nginx/certs

# Если нет дефолтного сертификата — создадим заглушку, чтобы nginx стартовал
if [ ! -f /etc/nginx/certs/default/fullchain.pem ] || [ ! -f /etc/nginx/certs/default/privkey.pem ]; then
  echo "[start] Creating default self-signed certificate for nginx startup..."
  mkdir -p /etc/nginx/certs/default
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout /etc/nginx/certs/default/privkey.pem \
    -out /etc/nginx/certs/default/fullchain.pem \
    -subj "/CN=default" \
    > /dev/null 2>&1
fi

echo "[start] Starting nginx..."
nginx

# --- Фоновый процесс: автообновление сертификатов (acme.sh --cron) ---
(
  while true; do
    sleep 12h
    acme.sh --cron --home /acme.sh >/dev/null 2>&1 || true
  done
) &

echo "[start] Launching SNI-UI (Flask)..."
exec python3 /app/sni-ui.py