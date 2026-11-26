import os
import json
import re
import subprocess
import secrets
import shutil
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="")

# --- Конфигурация ---
UI_PASSWORD = os.environ.get("SNI_UI_PASSWORD", "")
if not UI_PASSWORD:
    raise RuntimeError("SNI_UI_PASSWORD is not set — UI cannot start")

DOMAINS_FILE = "/data/domains.json"          # bind-mount ./data/
CONF_DIR = "/etc/nginx/conf.d"
CERTS_BASE = "/etc/nginx/certs"
ACME_HOME = "/acme.sh"
NGINX_CONF = "/etc/nginx/nginx.conf"

# --- Пресеты портов панели 3x-ui (настраиваются через .env) ---
def _parse_xui_ports(raw):
    ports = []
    for p in (raw or "").split(","):
        p = p.strip()
        if p.isdigit() and 1 <= int(p) <= 65535:
            ports.append(int(p))
    return ports or [2083, 2087, 2096, 2053, 8443]

XUI_HOST = os.environ.get("XUI_HOST", "3xui_app")   # хост бэкенда по умолчанию для пресетов
XUI_PORTS = _parse_xui_ports(os.environ.get("XUI_PORTS", ""))

# --- Утилиты ---
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)

def validate_domain(domain: str) -> bool:
    return bool(DOMAIN_RE.match(domain))

def load_domains():
    if not os.path.exists(DOMAINS_FILE):
        return []
    try:
        with open(DOMAINS_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def save_domains(domains):
    os.makedirs(os.path.dirname(DOMAINS_FILE), exist_ok=True)
    with open(DOMAINS_FILE, "w") as f:
        json.dump(domains, f, indent=2)

def find_domain(domains, domain):
    for i, d in enumerate(domains):
        if d["domain"] == domain:
            return i, d
    return -1, None

# --- Токен авторизации ---
auth_token = secrets.token_urlsafe(32)
print(f"[sni-ui] Auth token (use as Bearer): {auth_token}")

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != auth_token:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

# --- Пути сертификатов ---
def cert_source_dir(domain):
    for suffix in ("", "_ecc"):
        p = os.path.join(ACME_HOME, domain + suffix)
        if os.path.isdir(p):
            return p
    return os.path.join(ACME_HOME, domain)

def ensure_cert_symlinks(domain):
    src_dir = cert_source_dir(domain)
    dst_dir = os.path.join(CERTS_BASE, domain)
    os.makedirs(dst_dir, exist_ok=True)

    src_fullchain = os.path.join(src_dir, "fullchain.cer")
    src_privkey   = os.path.join(src_dir, "privkey.pem")
    dst_fullchain = os.path.join(dst_dir, "fullchain.pem")
    dst_privkey   = os.path.join(dst_dir, "privkey.pem")

    for p in (dst_fullchain, dst_privkey):
        if os.path.islink(p) or os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass

    if os.path.exists(src_fullchain):
        os.symlink(src_fullchain, dst_fullchain)
    if os.path.exists(src_privkey):
        os.symlink(src_privkey, dst_privkey)

def remove_cert_symlinks(domain):
    dst_dir = os.path.join(CERTS_BASE, domain)
    for name in ("fullchain.pem", "privkey.pem"):
        p = os.path.join(dst_dir, name)
        if os.path.islink(p) or os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass
    try:
        os.rmdir(dst_dir)
    except OSError:
        pass

# --- Nginx конфиг ---
def build_location_block(root, proxy_host, proxy_port, extra_locations):
    """Строит location / { ... } + extra_locations."""
    if proxy_port:
        # Проксирование на бэкенд
        host = proxy_host or "localhost"
        return f"""    location / {{
        proxy_pass http://{host}:{proxy_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
{extra_locations}"""
    else:
        # Статика
        return f"""    location / {{
        root {root};
        index index.html;
    }}
{extra_locations}"""

def generate_nginx_server_block(domain, root="/var/www/site", proxy_host=None, proxy_port=None, extra_locations=""):
    location_block = build_location_block(root, proxy_host, proxy_port, extra_locations)
    return f"""server {{
    listen 127.0.0.1:8443 ssl;
    server_name {domain};

    ssl_certificate     /etc/nginx/certs/{domain}/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/{domain}/privkey.pem;

    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

{location_block}
}}
"""

def update_nginx_map(domains):
    if not os.path.exists(NGINX_CONF):
        return False
    with open(NGINX_CONF, "r") as f:
        content = f.read()

    lines = ["    # Маппинг SNI -> бэкенд.",
             "    map $ssl_preread_server_name $backend_by_sni {"]
    lines.append("        www.microsoft.com          reality_passthrough;")
    for d in domains:
        lines.append(f"        {d['domain']}           tls_terminate;")
    lines.append("        default                    reality_passthrough;")
    lines.append("    }")
    new_map = "\n".join(lines)

    import re
    pattern = re.compile(r"(\s*)map \$ssl_preread_server_name \$backend_by_sni\s*\{.*?\n\1\}", re.DOTALL)
    if pattern.search(content):
        content = pattern.sub(lambda m: m.group(1) + new_map, content)
    else:
        return False

    with open(NGINX_CONF, "w") as f:
        f.write(content)
    return True

def reload_nginx():
    try:
        subprocess.run(["nginx", "-s", "reload"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False

# --- Роуты ---
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    password = data.get("password")
    if password == UI_PASSWORD:
        return jsonify({"ok": True, "token": auth_token})
    return jsonify({"ok": False, "error": "Invalid password"}), 401

@app.route("/api/domains", methods=["GET"])
@require_auth
def get_domains():
    return jsonify(load_domains())

@app.route("/api/presets", methods=["GET"])
@require_auth
def get_presets():
    """Кнопки-пресеты портов панели 3x-ui (из .env: XUI_HOST, XUI_PORTS)."""
    return jsonify({"host": XUI_HOST, "ports": XUI_PORTS})

@app.route("/api/domains", methods=["POST"])
@require_auth
def add_domain():
    data = request.get_json() or {}
    domain = (data.get("domain") or "").strip().lower()
    if not domain:
        return jsonify({"ok": False, "error": "Domain is required"}), 400
    if not validate_domain(domain):
        return jsonify({"ok": False, "error": "Invalid domain format"}), 400

    domains = load_domains()
    if any(d["domain"] == domain for d in domains):
        return jsonify({"ok": False, "error": "Domain already exists"}), 409

    # Опциональные поля
    root = data.get("root", "/var/www/site")
    proxy_host = data.get("proxy_host")
    proxy_port = data.get("proxy_port")
    extra_locations = data.get("extra_locations", "")
    staging = bool(data.get("staging", False))

    # Валидация порта
    if proxy_port is not None:
        try:
            proxy_port = int(proxy_port)
            if not (1 <= proxy_port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "proxy_port must be 1-65535"}), 400

    domains.append({
        "domain": domain,
        "status": "pending",
        "root": root,
        "proxy_host": proxy_host,
        "proxy_port": proxy_port,
        "extra_locations": extra_locations,
        "staging": staging
    })
    save_domains(domains)

    conf_path = os.path.join(CONF_DIR, f"{domain}.conf")
    with open(conf_path, "w") as f:
        f.write(generate_nginx_server_block(domain, root, proxy_host, proxy_port, extra_locations))

    update_nginx_map(domains)
    ok = reload_nginx()
    return jsonify({"ok": True, "domain": domain, "warning": "" if ok else "Nginx reload failed — check logs"})

@app.route("/api/domains/<domain>", methods=["DELETE"])
@require_auth
def delete_domain(domain):
    domain = domain.strip().lower()
    if not validate_domain(domain):
        return jsonify({"ok": False, "error": "Invalid domain format"}), 400

    domains = load_domains()
    idx, d = find_domain(domains, domain)
    if idx == -1:
        return jsonify({"ok": False, "error": "Domain not found"}), 404

    conf_path = os.path.join(CONF_DIR, f"{domain}.conf")
    if os.path.exists(conf_path):
        try:
            os.unlink(conf_path)
        except OSError:
            pass

    remove_cert_symlinks(domain)

    domains.pop(idx)
    save_domains(domains)

    update_nginx_map(domains)
    ok = reload_nginx()

    return jsonify({"ok": True, "domain": domain, "warning": "" if ok else "Nginx reload failed — check logs"})

@app.route("/api/domains/<domain>", methods=["PUT"])
@require_auth
def update_domain(domain):
    domain = domain.strip().lower()
    if not validate_domain(domain):
        return jsonify({"ok": False, "error": "Invalid domain format"}), 400

    data = request.get_json() or {}
    domains = load_domains()
    idx, d = find_domain(domains, domain)
    if idx == -1:
        return jsonify({"ok": False, "error": "Domain not found"}), 404

    new_domain = (data.get("domain") or "").strip().lower()
    new_root = data.get("root")
    new_proxy_host = data.get("proxy_host")
    new_proxy_port = data.get("proxy_port")
    new_extra = data.get("extra_locations")
    new_status = data.get("status")

    # staging: принимаем и true/false (bool или строка)
    if "staging" in data:
        new_staging = data["staging"]
        if isinstance(new_staging, str):
            new_staging = new_staging.lower() in ("1", "true", "yes", "on")
        new_staging = bool(new_staging)
    else:
        new_staging = None

    # Валидация proxy_port если передан
    if new_proxy_port is not None:
        try:
            new_proxy_port = int(new_proxy_port)
            if not (1 <= new_proxy_port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "proxy_port must be 1-65535"}), 400

    # Переименование домена
    if new_domain and new_domain != domain:
        if not validate_domain(new_domain):
            return jsonify({"ok": False, "error": "Invalid new domain format"}), 400
        if any(x["domain"] == new_domain for x in domains):
            return jsonify({"ok": False, "error": "New domain already exists"}), 409

        old_conf = os.path.join(CONF_DIR, f"{domain}.conf")
        new_conf = os.path.join(CONF_DIR, f"{new_domain}.conf")
        if os.path.exists(old_conf):
            try:
                os.rename(old_conf, new_conf)
            except OSError:
                pass

        old_cert_dir = os.path.join(CERTS_BASE, domain)
        new_cert_dir = os.path.join(CERTS_BASE, new_domain)
        if os.path.isdir(old_cert_dir):
            try:
                os.rename(old_cert_dir, new_cert_dir)
            except OSError:
                pass

        domain = new_domain
        d["domain"] = new_domain

    # Обновить поля
    if new_root is not None:
        d["root"] = new_root
    if new_proxy_host is not None:
        d["proxy_host"] = new_proxy_host
    if new_proxy_port is not None:
        d["proxy_port"] = new_proxy_port
    if new_extra is not None:
        d["extra_locations"] = new_extra
    if new_status is not None:
        d["status"] = new_status
    if new_staging is not None:
        d["staging"] = new_staging

    # Перегенерировать конфиг
    conf_path = os.path.join(CONF_DIR, f"{domain}.conf")
    with open(conf_path, "w") as f:
        f.write(generate_nginx_server_block(
            domain,
            d.get("root", "/var/www/site"),
            d.get("proxy_host"),
            d.get("proxy_port"),
            d.get("extra_locations", "")
        ))

    save_domains(domains)
    update_nginx_map(domains)
    ok = reload_nginx()

    return jsonify({"ok": True, "domain": domain, "warning": "" if ok else "Nginx reload failed — check logs"})

@app.route("/api/certificates", methods=["POST"])
@require_auth
def request_cert():
    data = request.get_json() or {}
    domain = (data.get("domain") or "").strip().lower()
    if not domain:
        return jsonify({"ok": False, "error": "Domain is required"}), 400
    if not validate_domain(domain):
        return jsonify({"ok": False, "error": "Invalid domain format"}), 400

    cmd = [
        "/usr/local/bin/acme.sh",
        "--issue",
        "-d", domain,
        "--webroot", "/var/www/certbot",
        "--home", ACME_HOME,
        "--force"
    ]

    # Тестовые сертификаты (staging CA) — не тратим лимит production
    staging = False
    for d in load_domains():
        if d["domain"] == domain:
            staging = bool(d.get("staging", False))
            break
    if staging:
        cmd.append("--staging")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            ensure_cert_symlinks(domain)
            domains = load_domains()
            for d in domains:
                if d["domain"] == domain:
                    d["status"] = "issued"
            save_domains(domains)
            return jsonify({"ok": True, "domain": domain,
                            "staging": staging,
                            "message": "Certificate issued (staging)" if staging else "Certificate issued and symlinks created"})
        else:
            return jsonify({"ok": False, "error": "ACME issue failed",
                            "stderr": result.stderr[-2000:]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Certificate issuance timed out"}), 504
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "acme.sh not found"}), 500

# --- Мини-UI (статика) ---
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)

if __name__ == "__main__":
    host = "127.0.0.1"
    port = 5000
    print(f"[sni-ui] Starting on {host}:{port}")
    app.run(host=host, port=port)