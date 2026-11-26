# SNI Proxy + UI (Nginx + acme.sh + Flask)

Минималистичный стек для SNI-маршрутизации, выдачи Let's Encrypt-сертификатов и простого UI. Работает на Podman + Alpine, без лишних зависимостей.

## Структура проекта

```
sni-proxy/
├── docker-compose.yml      # оркестрация
├── .env                    # переменные окружения (пароля НЕТ — он генерируется)
├── .env.example            # шаблон .env
├── Dockerfile              # сборка образа
├── nginx.conf              # конфиг Nginx (stream + http)
├── start.sh                # запуск, пароли, заглушка сертификата, cron
├── sni-ui.py               # Flask-UI: домены, сертификаты, симлинки
├── static/                 # мини-UI (HTML/JS)
├── data/                   # volume: файл пароля ui-auth + domains.json
├── certs/                  # volume: сертификаты (симлинки для Nginx)
├── acme_data/              # volume: рабочая директория acme.sh
├── nginx/conf.d/           # volume: сгенерированные server-блоки
├── www/                    # volume: статика сайтов
└── README.md
```

---

## Быстрый старт

1. **Создай `.env` из шаблона** (пароль НЕ задаём — он сгенерируется при старте):

```bash
cp .env.example .env
```

Открой `.env` и укажи свою почту для Let's Encrypt:

```env
ACME_EMAIL=your@email.com
UI_PORT_BIND=127.0.0.1
TZ=Europe/Yekaterinburg
```

2. **Собери и запусти стек:**

```bash
podman compose up -d --build
```

3. **Найди пароль в логах** (печатается при каждом старте — всегда актуален):

```bash
podman logs sni_proxy | grep -A 5 "UI PASS"
```

В логе увидишь блок:

```text
============================================
  UI LOGIN:  admin
  UI PASS:   a1B2c3D4e5F6g7H8i9J0k1L2m3N4
  (see file: /data/ui-auth)
============================================
```

4. **Открой UI:** `http://localhost:8085`
   - Логин: `admin`
   - Пароль: тот, что вывел лог (он же лежит в `./data/ui-auth`).

> **Логика пароля:**
> - **Первый старт:** пароль генерируется (`python secrets`) и сохраняется в `./data/ui-auth` (права 600).
> - **Последующие старты:** пароль читается из файла — **стабилен** между рестартами и пересозданиями контейнера, и печатается в лог снова.
> - **Переопределение через `.env`:** задал `UI_USER` + `SNI_UI_PASSWORD` — env главнее, файл не используется (в логе только подсказка).
> - **Смена пароля:** автогенерированный — удали `./data/ui-auth` → рестарт → новый в логе; ручной — правь `.env` → `podman compose up -d`.

---

## Как добавить домен и выпустить сертификат

### Через UI (браузер)

1. Открой `http://localhost:8085`, введи пароль.
2. Получишь **Bearer token** — он показан в интерфейсе и нужен для curl.
3. Нажми «Добавить» → введи домен, при необходимости `root` путь и дополнительные `location` → UI создаст server-блок в `nginx/conf.d/`, обновит `map` в `nginx.conf` и сделает reload Nginx.
4. Нажми «Правка» → измени `root` или добавь `location` (например, прокси на бэкенд).
5. Нажми «Выпустить сертификат» — после успеха автоматически создадутся симлинки в `certs/{domain}/`.
6. «Удалить» — полностью удаляет домен: конфиг, симлинки, запись из SNI map, перезагружает nginx.

### Через curl (API)

```bash
# 1. Авторизация — получаем Bearer token
curl -X POST http://127.0.0.1:8085/api/login \
  -H "Content-Type: application/json" \
  -d '{"password":"твой_пароль"}'
# Ответ: {"ok":true,"token":"<BEARER_TOKEN>"}
```

```bash
# 2. Добавить домен — статика (нужен Bearer token)
curl -X POST http://127.0.0.1:8085/api/domains \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BEARER_TOKEN>" \
  -d '{"domain":"example.com", "root":"/var/www/site", "extra_locations":""}'
```

```bash
# 2b. Добавить домен — прокси на бэкенд (proxy_port включает режим прокси)
curl -X POST http://127.0.0.1:8085/api/domains \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BEARER_TOKEN>" \
  -d '{"domain":"api.example.com", "proxy_host":"backend", "proxy_port":8080}'
```

```bash
# 2c. Добавить домен с ТЕСТОВЫМ сертификатом (staging — не тратит лимит 5/нед)
curl -X POST http://127.0.0.1:8085/api/domains \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BEARER_TOKEN>" \
  -d '{"domain":"test.example.com", "staging":true}'
```

```bash
# 3. Список доменов
curl http://127.0.0.1:8085/api/domains \
  -H "Authorization: Bearer <BEARER_TOKEN>"
```

```bash
# 4. Выпустить сертификат
curl -X POST http://127.0.0.1:8085/api/certificates \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BEARER_TOKEN>" \
  -d '{"domain":"example.com"}'
```

```bash
# 5. Правка домена — смена root / extra_locations
curl -X PUT http://127.0.0.1:8085/api/domains/example.com \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BEARER_TOKEN>" \
  -d '{"root":"/var/www/mysite", "extra_locations":"location /api { proxy_pass http://backend:8080; }"}'
```

```bash
# 5b. Переключить домен в режим прокси (или сменить порт/хост)
curl -X PUT http://127.0.0.1:8085/api/domains/example.com \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BEARER_TOKEN>" \
  -d '{"proxy_host":"backend", "proxy_port":3000}'
```

```bash
# 5c. Вернуть домен в режим статики (убрать proxy_port)
curl -X PUT http://127.0.0.1:8085/api/domains/example.com \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BEARER_TOKEN>" \
  -d '{"proxy_port": null, "root":"/var/www/site"}'
```

```bash
# 6. Переименование домена
curl -X PUT http://127.0.0.1:8085/api/domains/old.example.com \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BEARER_TOKEN>" \
  -d '{"domain":"new.example.com"}'
```

```bash
# 7. Удаление домена
curl -X DELETE http://127.0.0.1:8085/api/domains/example.com \
  -H "Authorization: Bearer <BEARER_TOKEN>"
```

> Все эндпоинты кроме `/api/login` требуют заголовок `Authorization: Bearer <token>`. Токен генерируется при каждом старте контейнера и выводится в логах `[sni-ui] Auth token (use as Bearer): ...`.

### Поля домена (domains.json)

| Поле | Описание | По умолчанию |
|------|----------|--------------|
| `domain` | Имя домена (обязательно) | — |
| `status` | `pending` / `issued` / `error` | `pending` |
| `root` | Корневая папка для статики (если не задан `proxy_port`) | `/var/www/site` |
| `proxy_host` | Хост бэкенда для проксирования | `localhost` |
| `proxy_port` | Порт бэкенда (если задан — включается режим прокси, `root` игнорируется для `/`) | — |
| `extra_locations` | Дополнительные nginx `location` блоки (сырой конфиг) | `""` |
| `staging` | `true` — тестовый сертификат (Let's Encrypt staging, не тратит production-лимит 5/нед) | `false` |

**Режимы работы:**
- **Статика** — `proxy_port` не задан: `location / { root /var/www/site; }`
- **Прокси** — `proxy_port` задан: `location / { proxy_pass http://<proxy_host>:<proxy_port>; ... }`

---

## Где что лежит

| Назначение | Внутри контейнера | На хосте (bind-mount) |
| --- | --- | --- |
| Пароль UI | `/data/ui-auth` | `./data/ui-auth` |
| Список доменов | `/data/domains.json` | `./data/domains.json` |
| Сертификаты (acme.sh) | `/acme.sh/{domain}/` | `./acme_data/` |
| Симлинки для Nginx | `/etc/nginx/certs/{domain}/` | `./certs/{domain}/` |
| Server-блоки Nginx | `/etc/nginx/conf.d/{domain}.conf` | `./nginx/conf.d/` |
| Статика сайтов | `/var/www/site` | `./www/` |
| Мини-UI | `/app/static/` | `./static/` |

### Кнопки-пресеты портов панели 3x-ui

В модалке добавления/правки домена (режим «Прокси») есть ряд кнопок-чипов с портами панели 3x-ui.
По клику подставляются `proxy_host` (из `.env`) и выбранный порт — сразу готовый проксированный домен.

Настройка через `.env`:

```env
# Хост бэкенда (имя контейнера панели в сети proxy-net)
XUI_HOST=3xui_app

# Список портов через запятую. Пусто = встроенный набор:
XUI_PORTS=2083,2087,2096,2053,8443
```

> Порты можно менять без правки кода — только `.env` + `podman compose up -d`.

> **Важно:** `domains.json` теперь лежит на bind-mount (`./data/`), поэтому список доменов переживает пересборку контейнера.

---

## Как это работает

- **Порт 80** — отдаёт http-01 челленджи для Let's Encrypt, остальное редиректит на HTTPS.
- **Порт 443** — маршрутизация по SNI (`ssl_preread`):
  - домены из `map` с терминацией → локальный listener `127.0.0.1:8443` (обычные сайты с сертификатами);
  - маскировочный домен (`www.microsoft.com`) и всё остальное → passthrough на `3xui_app:443`.
- **Порт 8085** — UI (Flask), по умолчанию привязан к `127.0.0.1` и наружу не торчит.
- **Добавление домена** делает три вещи:
  1. Создаёт `/etc/nginx/conf.d/{domain}.conf` с `listen 127.0.0.1:8443 ssl`.
  2. Дописывает запись в `map $ssl_preread_server_name $backend_by_sni` в `nginx.conf`.
  3. Делает `nginx -s reload`.
- **Правка домена** (PUT) перегенерарует конфиг с новыми `root` / `extra_locations`, при смене имени — переименовывает файлы конфига и симлинки сертификатов.
- **Удаление домена** (DELETE) удаляет конфиг, симлинки, чистит `map`, reload nginx.
- **Симлинки** создаются автоматически после успешного выпуска сертификата: ищутся в `/acme.sh/{domain}/` или `/acme.sh/{domain}_ecc/`, кладутся в `certs/{domain}/`.
- **Тестовые сертификаты (`staging: true`)** — при выпуске к `acme.sh --issue` добавляется `--staging`: используется staging CA Let's Encrypt (большой лимит, не тратит production-лимит 5/нед). Такие сертификаты не доверяются браузерами — нужны только для проверки настроек. Когда всё заработает — сними галочку и выпусти боевой.
- **Автообновление сертификатов** — фоновый цикл `acme.sh --cron` каждые 12 часов.

---

## Полезные команды для отладки

```bash
# Статус контейнера
podman compose ps
```

```bash
# Логи контейнера (nginx + UI + start.sh)
podman logs -f sni_proxy
```

```bash
# Проверка синтаксиса Nginx
podman exec sni_proxy nginx -t
```

```bash
# Graceful reload Nginx
podman exec sni_proxy nginx -s reload
```

```bash
# Ручной выпуск сертификата с отладкой
podman exec sni_proxy acme.sh --issue -d example.com --webroot /var/www/certbot --home /acme.sh --force --debug
```

```bash
# Посмотреть пароль ещё раз
cat ./data/ui-auth
```

```bash
# Посмотреть Bearer token (выводится при старте UI в логах)
podman logs sni_proxy | grep "Auth token"
```

---

## Пересборка и обновления

```bash
podman compose down
podman compose build
podman compose up -d
```

Пароль UI, сертификаты и `domains.json` не потеряются: они лежат на bind-mount'ах (`./data`, `./certs`, `./acme_data`).

---

## Автозапуск через systemd

Юнит: `/etc/systemd/system/sni-proxy.service` (в комплекте).

```bash
# Замени путь к проекту в WorkingDirectory юнита, затем:
systemctl daemon-reload
systemctl enable sni-proxy
systemctl start sni-proxy
systemctl status sni-proxy
```

Логи сервиса:

```bash
journalctl -u sni-proxy -f
```

После пересборки образа:

```bash
systemctl stop sni-proxy
podman compose build
systemctl start sni-proxy
```

> Юнит ссылается на `podman compose`, а не на ID контейнера, поэтому пересборка не ломает автозапуск.

---

## Безопасность

- **UI на 8085** — держи привязку `UI_PORT_BIND=127.0.0.1`; доступ снаружи только через SSH-туннель:

```bash
ssh -L 8085:127.0.0.1:8085 user@server
```

- **Файл пароля** — права `600`, в git не коммитить (`data/` должен быть в `.gitignore`).
- **Ручной пароль** — если хочешь задать свой, добавь в `.env`:

```env
UI_USER=admin
SNI_UI_PASSWORD=твой_сложный_пароль
```

  В этом случае файл `ui-auth` игнорируется и новый пароль не генерируется.
- **Bearer token** — генерируется при каждом старте, выводится в логах. Не коммить логи.
- **Никогда не коммить `.env`** с реальным паролем.

---

## Частые проблемы и решения

| Проблема | Что проверить |
| --- | --- |
| UI не пускает | Пароль: `cat ./data/ui-auth` или логи `podman logs sni_proxy` |
| Сертификат не выпускается | Доступность порта 80 из интернета; DNS A-запись; логи acme.sh (`--debug`) |
| Nginx не видит сертификат | Симлинки в `certs/{domain}/`; пути `fullchain.pem` / `privkey.pem` |
| Домен не открывается | Конфиг `nginx/conf.d/{domain}.conf`; reload Nginx; запись в `map` в `nginx.conf` |
| SNI-маршрут не работает | `3xui_app` запущен в сети `proxy-net`; запись в `map` в `nginx.conf` |
| Контейнер падает со сдвигом | Логи `podman logs sni_proxy`; чаще всего отсутствует правка в `.env` |
| API возвращает 401 | Нужен заголовок `Authorization: Bearer <token>`; токен берётся из логов UI |

---

## Схема потока трафика

```
Клиент → :443 (nginx stream, SNI-розыск)
  ├─ www.microsoft.com и прочее → passthrough → 3xui_app:443 (Reality)
  └─ site.example.com → tls_terminate → 127.0.0.1:8443 (nginx http, свои сертификаты)

Клиент → :80 → /.well-known/acme-challenge/ → acme.sh
        → прочее → 301 на https

Админ → :8085 (только 127.0.0.1) → Flask UI → API доменов/сертификатов
```