#!/usr/bin/env python3
"""
Скачивает файлы из Seafile (Web API), атомарно заменяет локальные копии
и удаляет скачанные файлы в Seafile. Запускать из cron раз в 5 минут.

Используется только стандартная библиотека Python и curl (через subprocess).

Список файлов задаётся (по приоритету):
  1. аргументами командной строки:  seafile_fetch.py /subfolder/a.csv /other/b.csv
  2. переменной SEAFILE_PATHS — пути через перевод строки или точку с запятой
  3. переменной SEAFILE_PATH — один путь (по умолчанию /subfolder/myfile.csv)

Переменные окружения:
  SEAFILE_URL       только https, например https://seafile.company.com
  SEAFILE_TOKEN     API-токен Seafile (рекомендуется: пароль не нужен)
  SEAFILE_USER      логин   } запасной вариант, если SEAFILE_TOKEN не задан:
  SEAFILE_PASSWORD  пароль  } токен запрашивается через /api2/auth-token/
  SEAFILE_REPO      repo-id
  SEAFILE_PATHS     список путей (см. выше)
  SEAFILE_PATH      один путь (если список не задан)
  DEST_DIR          куда класть файлы, по умолчанию текущая папка

Меры безопасности:
  * curl запускается с -q (~/.curlrc игнорируется) и --proto =https;
  * любой запрос curl разрешён только на https и только на хост из SEAFILE_URL;
    токен и пароль не уходят никуда больше (в том числе по ссылке из ответа API);
  * секреты передаются curl через stdin, а не через аргументы или окружение;
    curl получает урезанное окружение без SEAFILE_*;
  * значения с управляющими символами отвергаются (инъекция в конфиг curl).

Каждый файл обрабатывается независимо: ошибка с одним файлом не мешает остальным.
Код выхода: 0 — всё в порядке (отсутствующие файлы — не ошибка), 1 — была ошибка,
2 — неверная конфигурация.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlparse


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def has_control_chars(value):
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def check_value(value, what):
    """Отвергает значения с управляющими символами (перевод строки и т.п.),
    которыми можно было бы дописать лишние директивы в конфиг curl."""
    if has_control_chars(value):
        raise ValueError(f"{what}: недопустимый управляющий символ")
    return value


def fail_config(msg):
    log(f"ОШИБКА: {msg}")
    sys.exit(2)


def env(name, default=None, required=True):
    value = os.environ.get(name, default)
    if not value:
        if required:
            fail_config(f"переменная {name} не задана")
        return None
    try:
        return check_value(value, name)
    except ValueError as e:
        fail_config(str(e))


def parse_base_url(raw):
    u = urlparse(raw)
    if (u.scheme != "https" or not u.hostname or u.username is not None
            or u.password is not None or u.query or u.fragment):
        fail_config("SEAFILE_URL должен быть вида https://host[:port][/prefix] "
                    "(только https, без логина, query и fragment)")
    return u


SEAFILE_URL = env("SEAFILE_URL").rstrip("/")
SEAFILE_HOST = parse_base_url(SEAFILE_URL).netloc.lower()
TOKEN = env("SEAFILE_TOKEN", required=False)
USER = env("SEAFILE_USER", required=False)
PASSWORD = env("SEAFILE_PASSWORD", required=False)
REPO = env("SEAFILE_REPO")
DEST_DIR = os.path.abspath(env("DEST_DIR", "."))
if not re.fullmatch(r"[A-Za-z0-9_-]+", REPO):
    fail_config("SEAFILE_REPO содержит недопустимые символы")
if not TOKEN and not (USER and PASSWORD):
    fail_config("нужен SEAFILE_TOKEN либо пара SEAFILE_USER и SEAFILE_PASSWORD")
API = f"{SEAFILE_URL}/api2/repos/{REPO}"

# Секреты прочитаны — убираем их из окружения процесса, чтобы они не попали
# ни в какой дочерний процесс.
for _name in ("SEAFILE_TOKEN", "SEAFILE_PASSWORD"):
    os.environ.pop(_name, None)

# curl получает только то, что нужно для работы (PATH, прокси, корпоративный CA).
_CURL_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL",
    "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY",
    "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
)
CURL_ENV = {k: os.environ[k] for k in _CURL_ENV_KEYS if k in os.environ}


def get_paths():
    """Список удалённых путей: аргументы > SEAFILE_PATHS > SEAFILE_PATH."""
    if len(sys.argv) > 1:
        raw = sys.argv[1:]
    elif os.environ.get("SEAFILE_PATHS"):
        raw = re.split(r"[\n;]", os.environ["SEAFILE_PATHS"])
    else:
        raw = [os.environ.get("SEAFILE_PATH") or "/subfolder/myfile.csv"]

    paths, seen = [], set()
    for p in raw:
        p = p.strip()
        if not p:
            continue
        if not p.startswith("/"):
            p = "/" + p
        if p not in seen:  # убираем точные дубликаты
            seen.add(p)
            paths.append(p)
    if not paths:
        fail_config("список файлов пуст")

    # Все файлы кладутся в одну папку под своим именем — одинаковые имена
    # из разных папок Seafile перезаписывали бы друг друга.
    by_name = {}
    for p in paths:
        name = os.path.basename(p)
        if name in ("", ".", ".."):
            fail_config(f"путь без имени файла: {p!r}")
        by_name.setdefault(name, []).append(p)
    dups = {n: ps for n, ps in by_name.items() if len(ps) > 1}
    if dups:
        for n, ps in dups.items():
            log(f"ОШИБКА: одинаковое имя файла '{n}' у путей: {', '.join(ps)}")
        sys.exit(2)
    return paths


def is_trusted_url(url):
    """True, только если url — https на тот же хост (и порт), что и SEAFILE_URL."""
    if not isinstance(url, str) or has_control_chars(url) or " " in url:
        return False
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and u.netloc.lower() == SEAFILE_HOST


def cfg_quote(value):
    """Экранирование значения для конфигурации curl (-K)."""
    check_value(value, "значение для curl")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def curl(url, method="GET", output=None, connect_timeout=10, max_time=300,
         auth=True, form=None):
    """Вызывает curl. Токен, логин и пароль передаются через stdin (-K -),
    чтобы не светиться ни в ps, ни в окружении. Возвращает (http_code, body_bytes).
    При output тело пишется в файл, body == b''.
    form — dict полей x-www-form-urlencoded для POST.
    Любой запрос разрешён только на https-хост из SEAFILE_URL."""
    if not is_trusted_url(url):
        raise ValueError("запрос на непроверенный адрес заблокирован")
    config = ""
    if auth:
        config += f"header = {cfg_quote('Authorization: Token ' + TOKEN)}\n"
    if form:
        for k, v in form.items():
            config += f"data-urlencode = {cfg_quote(f'{k}={v}')}\n"
    cmd = [
        "curl", "-q",                      # -q строго первым: не читать ~/.curlrc
        "-sS",
        "--proto", "=https",               # никаких http и прочих протоколов
        "-K", "-",
        "--connect-timeout", str(connect_timeout), "--max-time", str(max_time),
        "-X", method,
        "-w", "%{http_code}",
        "-o", output if output else "-",
        "--url", url,                      # --url: адрес не может быть принят за опцию
    ]
    res = subprocess.run(cmd, input=config.encode(), capture_output=True, env=CURL_ENV)
    if res.returncode != 0:
        raise RuntimeError(f"curl завершился с кодом {res.returncode}: "
                           f"{res.stderr.decode(errors='replace').strip()}")
    if output:
        return int(res.stdout.decode().strip()), b""
    # при выводе в stdout http-код добавлен в конец тела
    body, code = res.stdout[:-3], res.stdout[-3:]
    return int(code), body


def check_seafile_available(attempts=3, delay=3):
    """Проверяет, что SEAFILE_URL доступен: /api2/ping/ (без авторизации) отвечает "pong"."""
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            code, body = curl(f"{SEAFILE_URL}/api2/ping/", auth=False,
                              connect_timeout=5, max_time=15)
            if code == 200 and b"pong" in body:
                return True
            last_error = f"/api2/ping/ вернул HTTP {code}"
        except RuntimeError as e:
            last_error = str(e)
        if attempt < attempts:
            time.sleep(delay)
    log(f"ОШИБКА: {SEAFILE_URL} недоступен после {attempts} попыток ({last_error})")
    return False


def check_token():
    """Проверяет, что заданный SEAFILE_TOKEN принят сервером (/api2/auth/ping/)."""
    try:
        code, body = curl(f"{SEAFILE_URL}/api2/auth/ping/", connect_timeout=5, max_time=15)
    except (RuntimeError, ValueError) as e:
        log(f"ОШИБКА: не удалось проверить токен ({e})")
        return False
    if code in (401, 403):
        log(f"ОШИБКА: токен отклонён сервером (HTTP {code})")
        return False
    if code != 200 or b"pong" not in body:
        log(f"ОШИБКА: /api2/auth/ping/ вернул HTTP {code}")
        return False
    return True


def get_token():
    """Запасной вариант: получает API-токен по логину и паролю (POST /api2/auth-token/)."""
    global TOKEN, PASSWORD
    try:
        code, body = curl(f"{SEAFILE_URL}/api2/auth-token/", method="POST", auth=False,
                          form={"username": USER, "password": PASSWORD},
                          connect_timeout=5, max_time=30)
    except (RuntimeError, ValueError) as e:
        log(f"ОШИБКА: не удалось получить токен ({e})")
        return False
    finally:
        PASSWORD = None  # пароль больше не нужен
    if code == 400:
        log("ОШИБКА: сервер отклонил логин/пароль (HTTP 400)")
        return False
    if code != 200:
        log(f"ОШИБКА: получение токена вернуло HTTP {code}")
        return False
    try:
        token = json.loads(body)["token"]
        if not isinstance(token, str) or not token:
            raise ValueError
        TOKEN = check_value(token, "токен из ответа сервера")
    except (ValueError, KeyError, TypeError):
        log("ОШИБКА: в ответе сервера нет корректного токена")
        return False
    return True


def process_file(remote_path):
    """Обрабатывает один файл. Возвращает 'downloaded', 'absent' или 'error'."""
    file_name = os.path.basename(remote_path)
    dest = os.path.join(DEST_DIR, file_name)
    enc_path = quote(remote_path, safe="/")
    tag = f"[{remote_path}]"

    # 1. Проверка наличия файла
    code, body = curl(f"{API}/file/detail/?p={enc_path}")
    if code == 404:
        return "absent"  # файла нет — штатная ситуация
    if code != 200:
        log(f"{tag} ОШИБКА: проверка файла вернула HTTP {code}")
        return "error"

    # Размер обязателен: без него нельзя убедиться, что файл скачан целиком.
    # Не удалось разобрать — это ошибка, а не повод пропустить проверку.
    try:
        remote_size = json.loads(body)["size"]
        if not isinstance(remote_size, int) or isinstance(remote_size, bool) or remote_size < 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        log(f"{tag} ОШИБКА: не удалось получить размер файла из ответа Seafile")
        return "error"

    # 2. Ссылка на скачивание: JSON-строка, только https на хост Seafile
    code, body = curl(f"{API}/file/?p={enc_path}&reuse=1")
    try:
        dl_url = json.loads(body) if code == 200 else None
    except ValueError:
        dl_url = None
    if not is_trusted_url(dl_url):
        log(f"{tag} ОШИБКА: ссылка на скачивание отсутствует или указывает "
            f"не на https://{SEAFILE_HOST}")
        return "error"

    # 3. Скачивание во временный файл в той же папке (для атомарного rename).
    # Ссылка одноразовая, токен в запросе не нужен и не отправляется.
    fd, tmp_path = tempfile.mkstemp(dir=DEST_DIR, prefix=f".{file_name}.", suffix=".part")
    os.close(fd)
    try:
        code, _ = curl(dl_url, output=tmp_path, auth=False)
        if code != 200:
            log(f"{tag} ОШИБКА: скачивание вернуло HTTP {code}")
            return "error"

        # 4. Проверка целостности: размер должен совпасть с серверным
        local_size = os.path.getsize(tmp_path)
        if local_size != remote_size:
            log(f"{tag} ОШИБКА: размер не совпал (локально {local_size}, в Seafile {remote_size})")
            return "error"

        # 5. Атомарная замена. mkstemp создаёт файл с правами 600 — выставляем
        # права по umask процесса (как у обычного нового файла), чтобы файл
        # могли читать другие сервисы.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp_path, 0o666 & ~umask)
        os.replace(tmp_path, dest)
        log(f"{tag} Файл скачан: {dest} ({local_size} байт)")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # 6. Удаление в Seafile — только после успешной замены
    code, _ = curl(f"{API}/file/?p={enc_path}", method="DELETE")
    if code != 200:
        log(f"{tag} ОШИБКА: удаление вернуло HTTP {code} (файл скачан, но остался в Seafile)")
        return "error"
    log(f"{tag} Файл удалён в Seafile")
    return "downloaded"


def main():
    paths = get_paths()

    # Защита от параллельных запусков. O_NOFOLLOW: не следовать симлинку,
    # подставленному на место lock-файла в общей папке /tmp.
    lock_path = os.path.join(tempfile.gettempdir(), f"seafile_fetch_{REPO}.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Предыдущий запуск ещё работает, выходим")
        return 0

    # 0. Проверка доступности Seafile и аутентификация (один раз на все файлы)
    if not check_seafile_available():
        return 1
    if TOKEN:
        if not check_token():
            return 1
    elif not get_token():
        return 1

    results = {}
    for p in paths:
        try:
            results[p] = process_file(p)
        except Exception as e:  # сбой с одним файлом не должен останавливать остальные
            log(f"[{p}] ОШИБКА: {e}")
            results[p] = "error"

    failed = [p for p, r in results.items() if r == "error"]
    if len(paths) > 1:
        done = sum(1 for r in results.values() if r == "downloaded")
        absent = sum(1 for r in results.values() if r == "absent")
        if done or failed:
            log(f"Итого: скачано {done}, нет в Seafile {absent}, с ошибкой {len(failed)} из {len(paths)}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ОШИБКА: {e}")
        sys.exit(1)
