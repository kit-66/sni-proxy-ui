FROM alpine:3.19

# Установка зависимостей
RUN apk add --no-cache \
    nginx \
    python3 \
    py3-flask \
    curl \
    socat \
    openssl \
    tzdata

# Установка acme.sh (без автообновления — управляем сами)
RUN set -e; \
    curl -s https://get.acme.sh | sh -s -- \
      --install --home /acme.sh --no-autoupdate && \
    ln -sf /acme.sh/acme.sh /usr/local/bin/acme.sh

WORKDIR /app

# Копируем конфиги, скрипты и статику UI
COPY nginx.conf      /etc/nginx/nginx.conf
COPY start.sh        /app/start.sh
COPY sni-ui.py       /app/sni-ui.py
COPY static/         /app/static/

# Подготовка директорий и прав
RUN chmod +x /app/start.sh && \
    mkdir -p /etc/nginx/certs /var/www/certbot /var/www/site /var/log/nginx /data

# Пробрасываем порты, которые реально нужны внутри контейнера
EXPOSE 80 443 8085

ENTRYPOINT ["/app/start.sh"]