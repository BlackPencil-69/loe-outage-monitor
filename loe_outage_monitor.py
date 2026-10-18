# -*- coding: utf-8 -*-
"""
LOE Outage Monitor — моніторинг графіків відключень електроенергії «Львівобленерго».

Працює у фоні в системному треї Windows, відстежує зміни графіків (diff по групах
і годинах) та надсилає сповіщення в Telegram.

Встановлення, налаштування, збірка в .exe та адмін-панель — див. README.md.
Мінімум для запуску: BOT_TOKEN та ADMIN_ID у config.json (або у змінних середовища
LOE_BOT_TOKEN та LOE_ADMIN_ID).
"""
from __future__ import annotations

import ctypes
import hashlib
import html
import io
import json
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# У режимі --noconsole stdout/stderr дорівнюють None — захищаємося від падінь.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# ──────────────────────────────────────────────────────────────────────────────
# Константи
# ──────────────────────────────────────────────────────────────────────────────
APP_NAME = "LOE Outage Monitor"
# Усе нижче з префіксом DEFAULT_ можна перевизначити в config.json (див. load_config).
ENV_PREFIX = "LOE_"           # змінні середовища: LOE_BOT_TOKEN, LOE_ADMIN_ID, …
DEFAULT_API_URL = "https://api.loe.lviv.ua/api/menus?page=1&type=photo-grafic"
DEFAULT_API_BASE = "https://api.loe.lviv.ua"
DEFAULT_SITE_URL = "https://poweron.loe.lviv.ua/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
CAPTION_LIMIT = 1024          # ліміт підпису до фото в Telegram
MESSAGE_LIMIT = 4000          # безпечний ліміт для sendMessage (максимум 4096)
FALLBACK_TEXT_LIMIT = 3000      # макс. довжина тексту rawHtml у сповіщенні без груп
MIN_INTERVAL_SECONDS = 30
DEFAULT_INTERVAL_SECONDS = 60
LEGACY_DEFAULT_INTERVAL = 600  # старе значення за замовчуванням (до CONFIG_VERSION 2)
CONFIG_VERSION = 2
DEFAULT_STATE_RETENTION_DAYS = 7
POLL_TIMEOUT = 25              # long polling getUpdates, с
MAX_RECIPIENT_ATTEMPTS = 5     # скільки циклів повторювати доставку одному одержувачу
BROADCAST_WORKERS = 5          # паралельні відправки (після першого одержувача)

log = logging.getLogger("loe_monitor")


# ──────────────────────────────────────────────────────────────────────────────
# Шляхи та ресурси
# ──────────────────────────────────────────────────────────────────────────────
def app_dir() -> str:
    """Каталог з .exe (або зі скриптом при запуску з вихідного коду)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(relative_path: str) -> str:
    """Шлях до ресурсу, вбудованого в PyInstaller (--onefile) або лежачого поруч."""
    base = getattr(sys, "_MEIPASS", None) or app_dir()
    return os.path.join(base, relative_path)


def data_dir() -> str:
    """Де лежать config.json, state.json, access.json і bot.log: каталог із
    LOE_MONITOR_DIR (якщо задано й доступний), інакше — поруч із програмою."""
    custom = os.environ.get(ENV_PREFIX + "MONITOR_DIR", "").strip()
    if custom:
        path = os.path.abspath(os.path.expanduser(custom))
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            pass
    return app_dir()


APP_DIR = app_dir()                  # ресурси (icon.ico) шукаємо поруч із програмою
DATA_DIR = data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH = os.path.join(DATA_DIR, "bot.log")
ACCESS_PATH = os.path.join(DATA_DIR, "access.json")


# ──────────────────────────────────────────────────────────────────────────────
# Часовий пояс Europe/Kyiv (із запасним варіантом без tzdata)
# ──────────────────────────────────────────────────────────────────────────────
def _last_sunday(year: int, month: int) -> date:
    d = date(year, month, 31)  # березень і жовтень мають 31 день
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _eu_transition(year: int, month: int, hour: int) -> datetime:
    """Наївний datetime переходу: остання неділя місяця о `hour` (год)."""
    d = _last_sunday(year, month)
    return datetime(d.year, d.month, d.day, hour)


class _FallbackKyiv(tzinfo):
    """Europe/Kyiv за правилами ЄС: UTC+2 взимку, UTC+3 влітку.

    Переходи відбуваються в останню неділю березня та жовтня о 01:00 UTC:
      • березень: локальний час 03:00 → 04:00 (години 03:xx не існує);
      • жовтень:  локальний час 04:00 → 03:00 (години 03:xx дві: fold=0 — літня
        (+03:00), fold=1 — зимова (+02:00)).
    Поведінка fold відповідає PEP 495 та zoneinfo."""

    _WINTER = timedelta(hours=2)
    _SUMMER = timedelta(hours=3)
    _HOUR = timedelta(hours=1)

    def _offset(self, dt: datetime) -> timedelta:
        """UTC-зсув для локального (wall-clock) часу `dt` з урахуванням dt.fold."""
        naive = dt.replace(tzinfo=None)
        spring = _eu_transition(naive.year, 3, 3)    # 03:00 EET — початок «діри»
        autumn = _eu_transition(naive.year, 10, 3)   # 03:00 EET — початок повтору
        if naive < spring:
            return self._WINTER
        if naive < spring + self._HOUR:              # неіснуючий час: fold=1 → вже літній
            return self._SUMMER if dt.fold else self._WINTER
        if naive < autumn:
            return self._SUMMER
        if naive < autumn + self._HOUR:              # повтор години: fold=1 → вже зимовий
            return self._WINTER if dt.fold else self._SUMMER
        return self._WINTER

    def utcoffset(self, dt):
        if dt is None:
            return None
        return self._offset(dt)

    def dst(self, dt):
        if dt is None:
            return None
        return self._offset(dt) - self._WINTER

    def tzname(self, dt):
        if dt is None:
            return None
        return "EEST" if self.dst(dt) else "EET"

    def fromutc(self, dt):
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        naive = dt.replace(tzinfo=None)
        start = _eu_transition(naive.year, 3, 1)     # 01:00 UTC
        end = _eu_transition(naive.year, 10, 1)      # 01:00 UTC
        if start <= naive < end:
            return (naive + self._SUMMER).replace(tzinfo=self, fold=0)
        # Перша година після осіннього переходу: локальний час 03:xx вдруге → fold=1.
        fold = 1 if end <= naive < end + self._HOUR else 0
        return (naive + self._WINTER).replace(tzinfo=self, fold=fold)


def _load_kyiv_tz() -> tzinfo:
    if ZoneInfo is not None:
        for name in ("Europe/Kyiv", "Europe/Kiev"):
            try:
                return ZoneInfo(name)
            except Exception:
                continue
    return _FallbackKyiv()


KYIV_TZ = _load_kyiv_tz()


def kyiv_now() -> datetime:
    return datetime.now(KYIV_TZ)


# ──────────────────────────────────────────────────────────────────────────────
# Логування (з приховуванням токена бота)
# ──────────────────────────────────────────────────────────────────────────────
class RedactingFormatter(logging.Formatter):
    """Замінює секрети (токен бота) у будь-яких записах, зокрема в traceback."""

    secrets: list[str] = []

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "<BOT_TOKEN>")
        return text


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    try:
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            RedactingFormatter("%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s")
        )
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────────────────────
# Повідомлення користувачу (Windows MessageBox)
# ──────────────────────────────────────────────────────────────────────────────
def show_message(title: str, text: str, error: bool = False) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x40)
            return
        except Exception:
            log.exception("Не вдалося показати вікно повідомлення")
    log.info("%s: %s", title, text)


_mutex_handle = None


def acquire_single_instance() -> bool:
    """Не дає запустити дві копії програми одночасно (іменований mutex Windows)."""
    global _mutex_handle
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\LoeOutageMonitor_SingleInstance")
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


# ──────────────────────────────────────────────────────────────────────────────
# Конфігурація
# ──────────────────────────────────────────────────────────────────────────────
class ConfigError(Exception):
    pass


def _write_json_atomic(path: str, data, sort_keys: bool = True) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=sort_keys)
    os.replace(tmp, path)


def url_origin(url: str) -> str:
    """`https://host[:port]` з повного URL; для некоректного URL — DEFAULT_API_BASE."""
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return DEFAULT_API_BASE


@dataclass
class Config:
    bot_token: str
    admin_id: int
    chat_id: str = ""            # «основний» чат/канал; може бути порожнім
    interval: int = DEFAULT_INTERVAL_SECONDS
    api_url: str = DEFAULT_API_URL
    site_url: str = DEFAULT_SITE_URL
    user_agent: str = DEFAULT_USER_AGENT
    state_retention_days: int = DEFAULT_STATE_RETENTION_DAYS

    @property
    def media_base(self) -> str:
        """Корінь API — від нього будуються абсолютні посилання на зображення графіків."""
        return url_origin(self.api_url)


CONFIG_TEMPLATE = {
    "BOT_TOKEN": "PUT_YOUR_BOT_TOKEN_HERE",
    "ADMIN_ID": "PUT_YOUR_TELEGRAM_ID_HERE",
    "CHAT_ID": "",
    "CHECK_INTERVAL_SECONDS": DEFAULT_INTERVAL_SECONDS,
    "CONFIG_VERSION": CONFIG_VERSION,
}


def _env(name: str) -> str:
    return os.environ.get(ENV_PREFIX + name, "").strip()


def _to_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _http_url(raw: dict, key: str, default: str) -> str:
    value = _env(key) or str(raw.get(key) or "").strip() or default
    if not value.lower().startswith(("https://", "http://")):
        raise ConfigError(f"{key} у config.json має починатися з https:// (отримано: {value!r}).")
    return value


def load_config(path: str = CONFIG_PATH) -> Config:
    """Читає налаштування з config.json; змінні середовища LOE_* мають пріоритет
    (так токен можна не зберігати у файлі). Обов'язкові: BOT_TOKEN і ADMIN_ID."""
    raw: dict = {}
    from_file = os.path.isfile(path)
    if from_file:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("корінь JSON має бути об'єктом")
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Не вдалося прочитати config.json:\n{path}\n\n{exc}") from exc
    elif not (_env("BOT_TOKEN") and _env("ADMIN_ID")):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(CONFIG_TEMPLATE, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            raise ConfigError(f"Не вдалося створити config.json: {exc}") from exc
        raise ConfigError(
            "Створено шаблон config.json:\n"
            f"{path}\n\n"
            "Заповніть BOT_TOKEN та ADMIN_ID, збережіть файл і запустіть програму знову."
        )

    token = _env("BOT_TOKEN") or str(raw.get("BOT_TOKEN", "")).strip()
    if not token or ":" not in token or "PUT_YOUR" in token:
        raise ConfigError(
            "Не заповнено BOT_TOKEN (токен від @BotFather).\n"
            f"{path}\n\nЗаповніть його і запустіть програму знову."
        )

    try:
        admin_id = int(str(_env("ADMIN_ID") or raw.get("ADMIN_ID", "")).strip())
    except ValueError:
        admin_id = 0
    if admin_id <= 0:
        raise ConfigError(
            "Не заповнено ADMIN_ID — числовий Telegram-ID адміністратора "
            "(його можна дізнатися, наприклад, у бота @userinfobot).\n"
            f"{path}\n\nЗаповніть його і запустіть програму знову."
        )

    chat_id = _env("CHAT_ID") or str(raw.get("CHAT_ID", "") or "").strip()
    if "PUT_YOUR" in chat_id:
        chat_id = ""

    interval = _to_int(raw.get("CHECK_INTERVAL_SECONDS"), DEFAULT_INTERVAL_SECONDS)

    # Одноразова міграція зі старого формату: до CONFIG_VERSION 2 шаблон писав інтервал
    # 600 с. Щоб сповіщення прискорилися без ручного редагування, 600 → 60; далі
    # (вже з CONFIG_VERSION) будь-яке значення користувача поважається.
    if from_file and _to_int(raw.get("CONFIG_VERSION"), 0) < CONFIG_VERSION:
        if interval == LEGACY_DEFAULT_INTERVAL:
            interval = DEFAULT_INTERVAL_SECONDS
            raw["CHECK_INTERVAL_SECONDS"] = interval
            log.info("config.json: інтервал 600 с замінено на %s с", interval)
        raw["CONFIG_VERSION"] = CONFIG_VERSION
        try:
            _write_json_atomic(path, raw, sort_keys=False)
        except OSError as exc:
            log.warning("Не вдалося оновити config.json: %s", exc)

    if _env("CHECK_INTERVAL_SECONDS"):
        interval = _to_int(_env("CHECK_INTERVAL_SECONDS"), interval)
    interval = max(MIN_INTERVAL_SECONDS, interval)

    return Config(
        bot_token=token,
        admin_id=admin_id,
        chat_id=chat_id,
        interval=interval,
        api_url=_http_url(raw, "API_URL", DEFAULT_API_URL),
        site_url=_http_url(raw, "SITE_URL", DEFAULT_SITE_URL),
        user_agent=_env("USER_AGENT") or str(raw.get("USER_AGENT") or "").strip() or DEFAULT_USER_AGENT,
        state_retention_days=max(1, _to_int(_env("STATE_RETENTION_DAYS")
                                            or raw.get("STATE_RETENTION_DAYS"),
                                            DEFAULT_STATE_RETENTION_DAYS)),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Інтервали часу (у хвилинах від початку доби)
# ──────────────────────────────────────────────────────────────────────────────
Interval = tuple  # (start_minute, end_minute)


def fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_hhmm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def merge_intervals(items) -> list:
    result: list = []
    for s, e in sorted(items):
        if result and s <= result[-1][1]:
            if e > result[-1][1]:
                result[-1] = (result[-1][0], e)
        else:
            result.append((s, e))
    return result


def subtract_intervals(a: list, b: list) -> list:
    """Частини інтервалів `a`, які не перекриваються інтервалами `b` (обидва впорядковані й злиті)."""
    result = []
    for s, e in a:
        cur = s
        for bs, be in b:
            if be <= cur or bs >= e:
                continue
            if bs > cur:
                result.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            result.append((cur, e))
    return result


def fmt_ranges(ranges) -> str:
    return ", ".join(f"{fmt_minutes(s)} - {fmt_minutes(e)}" for s, e in ranges)


def group_sort_key(group: str):
    try:
        return tuple(int(p) for p in group.split("."))
    except ValueError:
        return (999, group)


# ──────────────────────────────────────────────────────────────────────────────
# Парсинг rawHtml
# ──────────────────────────────────────────────────────────────────────────────
_BLOCK_END_RE = re.compile(r"</(?:p|div|li|tr|h[1-6]|ul|ol|table|section)\s*>|<br\s*/?>", re.I)

_GROUP_TAIL = r"(?:\s*(?:,|;|/|та|і|й)\s*\d\s*[.,]\s*\d(?!\d))*"
_GROUP_KEYWORD_RE = re.compile(
    r"(?:груп|(?:під)?черг)[а-яіїєґ']*\s*№?\s*(\d\s*[.,]\s*\d(?!\d)" + _GROUP_TAIL + ")",
    re.I,
)
_GROUP_BARE_RE = re.compile(r"^\s*(\d\s*[.,]\s*\d(?!\d)" + _GROUP_TAIL + ")")
_GROUP_NUM_RE = re.compile(r"(\d)\s*[.,]\s*(\d)")
# Час: `08:00` (двокрапка, будь-які хвилини) або `08.00` / `12.30` (крапка — лише :00 і :30).
# Обмеження для крапки захищає від дат (`17.10 - 18.10`) і груп (`1.1 - 1.2`): місяць не
# може бути 00 чи 30, а номер підгрупи — двоцифровим. Захисні умови для крапкової форми:
#   (?<![\d.])    — час не є хвостом більшого числа/дати («26.12.00»);
#   (?!\d|\.\d)  — після хвилин не йде ще цифра/«.цифра» («08.00.2026», «12.300»).
_TIME_PAT = (
    r"(?:(?<!\d)(\d{1,2})\s*:\s*(\d{2})"                 # групи: год, хв (двокрапка)
    r"|(?<![\d.])(\d{1,2})\.(00|30)(?!\d|\.\d))"          # групи: год, хв (крапка)
)
_RANGE_RE = re.compile(_TIME_PAT + r"\s*(?:[-–—−]|до|по)\s*" + _TIME_PAT, re.I)


def html_to_text(raw_html: str) -> str:
    """HTML → чистий текст; кожен блочний елемент починається з нового рядка."""
    prepared = _BLOCK_END_RE.sub("\n", raw_html or "")
    text = BeautifulSoup(prepared, "html.parser").get_text(" ")
    text = text.replace("\xa0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def extract_ranges(segment: str) -> list:
    """Усі часові проміжки `HH:MM - HH:MM` / `з HH:MM до HH:MM` / `з HH.00 до HH.30`.

    Особливі випадки: `22:00 - 00:00` → до кінця доби; `00:00 - 24:00` і `00:00 - 00:00`
    → уся доба (0, 1440); перехід через північ (`22:00 - 02:00`) розбивається на два інтервали.
    """
    out = []
    for m in _RANGE_RE.finditer(segment):
        g = m.groups()          # (год, хв, год, хв) — двокрапка; потім те саме для крапки
        h1, m1 = (int(g[0]), int(g[1])) if g[0] is not None else (int(g[2]), int(g[3]))
        h2, m2 = (int(g[4]), int(g[5])) if g[4] is not None else (int(g[6]), int(g[7]))
        if h1 > 24 or h2 > 24 or m1 > 59 or m2 > 59:
            continue
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if start >= 1440 or end > 1440:
            continue
        if start == 0 and end in (0, 1440):   # «00:00 - 24:00» / «00:00 - 00:00» = уся доба
            out.append((0, 1440))
        elif end == 0:                        # «22:00 - 00:00» = до кінця доби
            out.append((start, 1440))
        elif end > start:
            out.append((start, end))
        elif end < start:                     # перехід через північ: 22:00 - 02:00
            out.extend([(start, 1440), (0, end)])
        # end == start (≠ 0) — нульова тривалість, ігноруємо
    return out


def _group_numbers(fragment: str) -> list:
    seen: list = []
    for a, b in _GROUP_NUM_RE.findall(fragment):
        g = f"{a}.{b}"
        if g not in seen:
            seen.append(g)
    return seen


# Явні маркери «відключень немає» у фрагменті після назви групи. Свідомо НЕ збігається з
# «Електроенергії немає» (це якраз відключення).
_NO_OUTAGE_RE = re.compile(
    r"(?:електроенергі[яю]|світло)\s+є(?!\w)"
    r"|відключень\s+(?:немає|нема|не\s+заплановано)"
    r"|не\s+вимика\w+"
    r"|без\s+(?:планових\s+)?відключень",
    re.I,
)
# Службові рядки, години в яких не стосуються жодної групи.
_SERVICE_LINE_RE = re.compile(r"гаряч\w*\s+лін|телефон|call[-\s]?центр|диспетчер|контакт", re.I)


def _is_info_line(line: str) -> bool:
    return bool(_INFO_DATE_HINT_RE.search(line) or _SERVICE_LINE_RE.search(line))


def _parse_lines(lines: list, marker_re) -> dict:
    schedule: dict = {}
    context: list = []  # групи з попереднього рядка (для рядків-продовжень лише з часом)

    def assign(groups, ranges):
        for g in groups:
            schedule.setdefault(g, []).extend(ranges)

    for line in lines:
        matches = list(marker_re.finditer(line))
        if not matches:
            if _is_info_line(line):          # «Станом на 14:00 - 15:00», «Гаряча лінія: 08:00 - 20:00»
                context = []
                continue
            ranges = extract_ranges(line)
            if not ranges:                   # рядок без груп і без часу розриває «продовження»
                context = []
            elif context:
                assign(context, ranges)
            continue
        prefix = line[: matches[0].start()]
        before = [] if _is_info_line(prefix) else extract_ranges(prefix)
        for idx, m in enumerate(matches):
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
            groups = _group_numbers(m.group(1))
            segment = line[m.end():end]
            ranges = extract_ranges(segment)
            no_outage = bool(_NO_OUTAGE_RE.search(segment))
            if idx == 0 and before and not ranges and not no_outage:   # «08:00 - 12:00 — групи 1.1, 2.1»
                ranges = before
            if ranges:
                assign(groups, ranges)
            # «Група 2.1. Електроенергія є.» — відключень немає, наступні рядки з часом
            # (службові години тощо) до цієї групи не належать.
            context = [] if no_outage else groups
    return schedule


def parse_schedule_from_html(raw_html: str) -> dict:
    """
    rawHtml → {"1.1": [["08:00","12:00"], ["18:00","22:00"]], "1.2": [...]}.
    Групи без запланованих відключень у результат не потрапляють.
    """
    text = html_to_text(raw_html)
    lines = text.split("\n")
    schedule = _parse_lines(lines, _GROUP_KEYWORD_RE)
    if not schedule and not any(_GROUP_KEYWORD_RE.search(ln) for ln in lines):
        schedule = _parse_lines(lines, _GROUP_BARE_RE)   # запасний варіант: «1.1: 08:00-12:00»
    result = {}
    for group, ranges in schedule.items():
        merged = merge_intervals(ranges)
        if merged:
            result[group] = [[fmt_minutes(s), fmt_minutes(e)] for s, e in merged]
    return dict(sorted(result.items(), key=lambda kv: group_sort_key(kv[0])))


def schedule_to_minutes(schedule: dict) -> dict:
    out = {}
    for group, ranges in (schedule or {}).items():
        try:
            items = merge_intervals((parse_hhmm(s), parse_hhmm(e)) for s, e in ranges)
        except (ValueError, TypeError):
            continue
        if items:
            out[group] = items
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Визначення дати графіка
# ──────────────────────────────────────────────────────────────────────────────
MONTHS_UA = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
_NUM_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)")
_WORD_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS_UA) + r")(?:\s+(\d{4}))?", re.I
)
_INFO_DATE_HINT_RE = re.compile(r"станом|оновлен|актуальн|опублік|на\s+момент", re.I)
# «на» (+ необов'язковий день тижня/«завтра») безпосередньо перед датою: «графік на неділю, »
_DAY_WORD = (
    r"(?:понеділ\w*|вівтор\w*|серед\w*|четвер\w*|п['’ʼ`]?ятниц\w*|субот\w*|неділ\w*"
    r"|сьогодні|завтра|післязавтра|пн|вт|ср|чт|пт|сб|нд)\.?"
)
_SCHEDULE_ON_TAIL_RE = re.compile(
    r"(?<!\w)на\s*(?:" + _DAY_WORD + r"[\s,(–—-]*)?$", re.I
)
# Межа речення/клаузи: перенос рядка, «;», «!», «?» або крапка перед пробілом (не крапка в даті).
_CLAUSE_BREAK_RE = re.compile(r"[\n;!?]|\.(?=\s|$)")
_URL_DATE_RES = (
    re.compile(r"(?<!\d)(\d{4})[-_.](\d{2})[-_.](\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{2})[-_.](\d{2})[-_.](\d{4})(?!\d)"),
)


def _safe_date(y: int, m: int, d: int):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _nearest_year_date(day: int, month: int, today: date):
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        cand = _safe_date(y, month, day)
        if cand and (best is None or abs((cand - today).days) < abs((best - today).days)):
            best = cand
    return best


def _date_priority(text: str, pos: int) -> int:
    """0 — «на [день тижня,] <дата>» (дата графіка); 1 — нейтрально; 2 — «станом на…/оновлено…».

    Аналізується лише локальний контекст перед датою: вікно обрізається по останній межі
    речення/клаузи (перенос рядка, «;», «.» перед пробілом). Якщо в клаузі є і «станом…»,
    і «на …» прямо перед датою, виграє маркер, що ближче до дати:
      «Станом на 14:00 графік на 18.10.2026» → 0 (між «станом» і «на» є інший текст);
      «Станом на 17.10.2026»                  → 2 («на» належить до «станом на»).
    """
    window = text[max(0, pos - 80):pos]
    breaks = list(_CLAUSE_BREAK_RE.finditer(window))
    if breaks:
        window = window[breaks[-1].end():]
    hints = list(_INFO_DATE_HINT_RE.finditer(window))
    on = _SCHEDULE_ON_TAIL_RE.search(window)
    if on:
        if hints:
            # хвіст слова-маркера («оновлена», «актуальні») не рахуємо проміжним текстом
            between = re.sub(r"^\w*", "", window[hints[-1].end():on.start()]).strip()
            if not between:
                return 2          # «станом на <дата>» / «оновлено на <дата>»
        return 0
    if hints:
        return 2
    return 1


def extract_schedule_date(text: str, today: date):
    """Дата, на яку складено графік (формати `ДД.ММ.РРРР` та «18 жовтня 2026 року»)."""
    t = (text or "").lower()
    found = []
    for m in _NUM_DATE_RE.finditer(t):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        dt = _safe_date(y, mo, d)
        if dt:
            found.append((_date_priority(t, m.start()), m.start(), dt))
    for m in _WORD_DATE_RE.finditer(t):
        d, mo = int(m.group(1)), MONTHS_UA[m.group(2)]
        dt = _safe_date(int(m.group(3)), mo, d) if m.group(3) else _nearest_year_date(d, mo, today)
        if dt:
            found.append((_date_priority(t, m.start()), m.start(), dt))
    if not found:
        return None
    found.sort(key=lambda x: (x[0], x[1]))
    return found[0][2]


def extract_date_from_url(url: str):
    name = url.rsplit("/", 1)[-1]
    m = _URL_DATE_RES[0].search(name)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _URL_DATE_RES[1].search(name)
    if m:
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Diff Engine
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class ScheduleDiff:
    added_hours: dict           # група → нові години відключень
    removed_hours: dict         # група → скасовані години відключень
    added_groups: dict          # група → години (групи, яких раніше не було)
    removed_groups: dict        # група → години (групи, відключення яких скасовано повністю)

    def is_empty(self) -> bool:
        return not (self.added_hours or self.removed_hours
                    or self.added_groups or self.removed_groups)


def diff_schedules(old: dict, new: dict) -> ScheduleDiff:
    old_m, new_m = schedule_to_minutes(old), schedule_to_minutes(new)
    diff = ScheduleDiff({}, {}, {}, {})
    for group in sorted(set(old_m) | set(new_m), key=group_sort_key):
        if group in new_m and group not in old_m:
            diff.added_groups[group] = new_m[group]
        elif group in old_m and group not in new_m:
            diff.removed_groups[group] = old_m[group]
        else:
            added = subtract_intervals(new_m[group], old_m[group])
            removed = subtract_intervals(old_m[group], new_m[group])
            if added:
                diff.added_hours[group] = added
            if removed:
                diff.removed_hours[group] = removed
    return diff


# ──────────────────────────────────────────────────────────────────────────────
# Шаблони сповіщень
# ──────────────────────────────────────────────────────────────────────────────
KIND_NEW = "new"                         # сценарій 1 (сьогодні / загальний новий графік)
KIND_TOMORROW_FIRST = "tomorrow_first"   # сценарій 2
KIND_TOMORROW_UPDATE = "tomorrow_update" # сценарій 3

NO_DIFF_NOTE = "ℹ️ Змін у годинах чи групах не виявлено — оновлено зображення графіка."


def _diff_lines(diff: ScheduleDiff, h_add: str, h_rem: str, h_grp: str) -> list:
    lines: list = []
    if diff.added_hours:
        lines.append(h_add)
        lines += [f"• Група {g}: {fmt_ranges(r)}" for g, r in diff.added_hours.items()]
    if diff.removed_hours:
        lines.append(h_rem)
        lines += [f"• Група {g}: {fmt_ranges(r)}" for g, r in diff.removed_hours.items()]
    if diff.added_groups or diff.removed_groups:
        lines.append(h_grp)
        lines += [f"• Групу {g} додано до графіків ({fmt_ranges(r)})"
                  for g, r in diff.added_groups.items()]
        lines += [f"• Групу {g} прибрано з графіків — відключень не заплановано "
                  f"(раніше: {fmt_ranges(r)})" for g, r in diff.removed_groups.items()]
    return lines


def build_notification(kind: str, schedule_date: date, groups: dict, diff) -> tuple:
    """Повертає (заголовок, деталі). Деталі можуть бути порожніми."""
    if kind == KIND_TOMORROW_FIRST:
        return "<b>ГРАФІК ВІДКЛЮЧЕННЯ СВІТЛА НА ЗАВТРА</b>", ""

    if kind == KIND_TOMORROW_UPDATE:
        title = "<b>ОНОВЛЕНИЙ ГРАФІК НА ЗАВТРА</b>"
        lines = _diff_lines(
            diff,
            "➕ <b>Додано години відключень:</b>",
            "➖ <b>Забрано години (буде світло):</b>",
            "🔄 <b>Зміни в групах:</b>",
        ) if diff else []
        body = lines or [NO_DIFF_NOTE]
        return title, "\n".join(["📊 <b>Що змінилося:</b>"] + body)

    title = f"<b>НОВИЙ ГРАФІК ВІДКЛЮЧЕННЯ СВІТЛА на {schedule_date:%d.%m.%Y}</b>"
    if diff is None:   # перша фіксація — перелік запланованих вимкнень
        if not groups:
            return title, ""
        sched = schedule_to_minutes(groups)
        lines = ["🗓 <b>Заплановані відключення:</b>"]
        lines += [f"• Група {g}: {fmt_ranges(sched[g])}"
                  for g in sorted(sched, key=group_sort_key)]
        return title, "\n".join(lines)
    lines = _diff_lines(
        diff,
        "➕ <b>Додано години:</b>",
        "➖ <b>Скасовано відключення (додано світло):</b>",
        "👥 <b>Зміни по групах:</b>",
    )
    return title, "\n".join(["📊 <b>Зміни в графіку:</b>"] + (lines or [NO_DIFF_NOTE]))


def visible_len(text: str) -> int:
    """Довжина тексту так, як її рахує Telegram (без HTML-тегів)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _cut_position(s: str, limit: int) -> tuple[int, int]:
    """Де розрізати надто довгий рядок `s` (len(s) > limit).

    Повертає (cut, skip): перша частина — s[:cut] (cut <= limit), далі продовжуємо
    з s[cut + skip:]. Перевага — розріз по пробілу (він «з'їдається», skip=1);
    розріз не робиться всередині <тегу> чи &сутності;, щоб не ламати parse_mode=HTML.
    Якщо безпечного місця немає — жорсткий розріз рівно по `limit`."""

    def unsafe(i: int) -> bool:
        head = s[:i]
        if head.rfind("<") > head.rfind(">"):
            return True
        amp = head.rfind("&")
        return amp != -1 and ";" not in head[amp:] and i - amp <= 10

    for i in range(limit, 0, -1):
        if s[i] == " " and not unsafe(i):
            return i, 1
    for i in range(limit, 0, -1):
        if not unsafe(i):
            return i, 0
    return limit, 0


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list:
    """Ділить текст на частини довжиною <= limit (по рядках; надто довгий рядок —
    по пробілах, а якщо їх немає — жорстко)."""
    if limit < 1:
        raise ValueError("limit має бути >= 1")
    chunks, cur = [], ""
    for line in text.split("\n"):
        was_long = len(line) > limit
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            cut, skip = _cut_position(line, limit)
            piece = line[:cut]
            if piece.strip():
                chunks.append(piece)
            line = line[cut + skip:]
        if was_long and not line.strip():
            continue
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


# ──────────────────────────────────────────────────────────────────────────────
# Мережа: API Львівобленерго
# ──────────────────────────────────────────────────────────────────────────────
def absolute_media_url(image_url: str, base: str = DEFAULT_API_BASE) -> str:
    if image_url.startswith(("http://", "https://")):
        return image_url
    return base.rstrip("/") + ("" if image_url.startswith("/") else "/") + image_url


def is_gpv_image(image_url) -> bool:
    """Чи є це зображенням графіка ГПВ (у назві файлу є `_GPV`). None/порожнє → False."""
    return bool(image_url) and "_GPV" in str(image_url).upper()


def extract_menu_items(data) -> list[dict]:
    """Усі елементи ["hydra:member"][0]["menuItems"] (сьогодні + завтра тощо)."""
    if isinstance(data, dict):
        members = data.get("hydra:member") or data.get("member") or []
    elif isinstance(data, list):
        members = data
    else:
        members = []
    if not members:
        raise ValueError("API повернуло порожній список hydra:member")
    items = (members[0] or {}).get("menuItems") or []
    items = [it for it in items if isinstance(it, dict)]
    if not items:
        raise ValueError("У hydra:member[0] відсутній menuItems")
    return items


def extract_menu_item(data) -> dict:
    """Зворотна сумісність: лише перший елемент menuItems."""
    return extract_menu_items(data)[0]


class LoeApiClient:
    def __init__(self, api_url: str = DEFAULT_API_URL, site_url: str = DEFAULT_SITE_URL,
                 user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.api_url = api_url
        self.session = requests.Session()
        retry = Retry(
            total=3, backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": user_agent,
            "Referer": site_url,
            "Origin": url_origin(site_url),
            "Accept": "application/ld+json, application/json;q=0.9, */*;q=0.8",
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
        })

    def fetch_menu_items(self) -> list[dict]:
        resp = self.session.get(self.api_url, timeout=(10, 30))
        resp.raise_for_status()
        return extract_menu_items(resp.json())

    def fetch_menu_item(self) -> dict:
        """Зворотна сумісність: лише перший елемент."""
        return self.fetch_menu_items()[0]

    def download_image(self, url: str) -> bytes:
        resp = self.session.get(
            url, timeout=(10, 60),
            headers={"Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8"},
        )
        resp.raise_for_status()
        if not resp.content:
            raise ValueError("Порожня відповідь при завантаженні зображення")
        return resp.content


# ──────────────────────────────────────────────────────────────────────────────
# Мережа: Telegram Bot API
# ──────────────────────────────────────────────────────────────────────────────
class TelegramError(Exception):
    def __init__(self, method: str, status: int, description: str,
                 migrate_to: int | None = None) -> None:
        super().__init__(f"{method}: HTTP {status}: {description}")
        self.method, self.status, self.description = method, status, description
        self.migrate_to = migrate_to      # група стала супергрупою → новий chat_id


class TelegramClient:
    API = "https://api.telegram.org/bot{token}/{method}"
    FILE_CACHE_SIZE = 16

    def __init__(self, token: str, chat_id: str = "") -> None:
        self.token, self.chat_id = token, str(chat_id or "")
        self.session = requests.Session()
        self.poll_session = requests.Session()     # окремо від відправки: long polling тримає з'єднання
        self._file_ids: dict = {}                  # (тип, sha1 зображення) → file_id у Telegram

    def _call(self, method: str, data: dict, files: dict | None = None, _retry: bool = True,
              timeout=(10, 90), session: requests.Session | None = None) -> dict:
        url = self.API.format(token=self.token, method=method)
        resp = (session or self.session).post(url, data=data, files=files, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code == 200 and payload.get("ok"):
            return payload
        params = payload.get("parameters") or {}
        if resp.status_code == 429 and _retry:
            wait = min(int(params.get("retry_after", 5)), 30) + 1
            log.warning("Telegram просить зачекати %s с (429)", wait)
            time.sleep(wait)
            return self._call(method, data, files, _retry=False, timeout=timeout, session=session)
        raise TelegramError(method, resp.status_code,
                            payload.get("description") or resp.text[:200],
                            migrate_to=params.get("migrate_to_chat_id"))

    def _cid(self, chat_id) -> str:
        return str(chat_id) if chat_id is not None else self.chat_id

    def bind(self, chat_id) -> "BoundTelegram":
        """Той самий інтерфейс відправки, але жорстко прив'язаний до одного чату."""
        return BoundTelegram(self, chat_id)

    # ---- відправка ----
    def send_message(self, text: str, chat_id=None, reply_markup: dict | None = None) -> dict:
        data = {
            "chat_id": self._cid(chat_id), "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return self._call("sendMessage", data)

    @staticmethod
    def _extract_file_id(payload: dict, kind: str):
        try:
            result = payload.get("result") or {}
            if kind == "photo":
                return result["photo"][-1]["file_id"]
            return result["document"]["file_id"]
        except (KeyError, IndexError, TypeError, AttributeError):
            return None

    def _send_media(self, method: str, field: str, kind: str, image: bytes, filename: str,
                    caption: str, chat_id) -> dict:
        """Перший раз завантажує файл, далі для інших чатів шле лише file_id — це
        в рази швидше й не навантажує канал, коли одержувачів багато."""
        base = {"chat_id": self._cid(chat_id), "caption": caption, "parse_mode": "HTML"}
        key = (kind, hashlib.sha1(image).hexdigest())
        file_id = self._file_ids.get(key)
        if file_id:
            try:
                return self._call(method, {**base, field: file_id})
            except TelegramError as exc:
                if exc.status != 400 or "file" not in exc.description.lower():
                    raise
                self._file_ids.pop(key, None)          # file_id недійсний — завантажимо заново
        payload = self._call(method, base, files={field: (filename, image)})
        new_id = self._extract_file_id(payload, kind)
        if new_id:
            if len(self._file_ids) >= self.FILE_CACHE_SIZE:
                self._file_ids.pop(next(iter(self._file_ids)), None)
            self._file_ids[key] = new_id
        return payload

    def send_photo(self, image: bytes, filename: str, caption: str, chat_id=None) -> dict:
        return self._send_media("sendPhoto", "photo", "photo", image, filename, caption, chat_id)

    def send_document(self, image: bytes, filename: str, caption: str, chat_id=None) -> dict:
        return self._send_media("sendDocument", "document", "document", image, filename,
                                caption, chat_id)

    def send_photo_url(self, url: str, caption: str, chat_id=None) -> dict:
        return self._call("sendPhoto", {
            "chat_id": self._cid(chat_id), "photo": url, "caption": caption, "parse_mode": "HTML",
        })

    # ---- керування ботом ----
    def get_updates(self, offset: int | None = None, timeout: int = POLL_TIMEOUT) -> list:
        data = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query", "my_chat_member"]),
        }
        if offset is not None:
            data["offset"] = offset
        payload = self._call("getUpdates", data, timeout=(10, timeout + 20),
                             session=self.poll_session)
        return payload.get("result") or []

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> dict:
        data = {"callback_query_id": callback_id}
        if text:
            data["text"] = text
        if alert:
            data["show_alert"] = "true"
        return self._call("answerCallbackQuery", data)

    def edit_message_text(self, chat_id, message_id: int, text: str,
                          reply_markup: dict | None = None) -> dict:
        data = {
            "chat_id": str(chat_id), "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return self._call("editMessageText", data)

    def leave_chat(self, chat_id) -> dict:
        return self._call("leaveChat", {"chat_id": str(chat_id)})

    def delete_webhook(self) -> dict:
        return self._call("deleteWebhook", {"drop_pending_updates": "false"})


class BoundTelegram:
    """Обгортка TelegramClient для одного чату (інтерфейс, який очікує TelegramNotifier)."""

    def __init__(self, client: TelegramClient, chat_id) -> None:
        self.client, self.chat_id = client, str(chat_id)

    def send_message(self, text: str) -> dict:
        return self.client.send_message(text, chat_id=self.chat_id)

    def send_photo(self, image: bytes, filename: str, caption: str) -> dict:
        return self.client.send_photo(image, filename, caption, chat_id=self.chat_id)

    def send_document(self, image: bytes, filename: str, caption: str) -> dict:
        return self.client.send_document(image, filename, caption, chat_id=self.chat_id)

    def send_photo_url(self, url: str, caption: str) -> dict:
        return self.client.send_photo_url(url, caption, chat_id=self.chat_id)


def photo_fits_telegram(image: bytes) -> bool:
    """sendPhoto: ≤10 МБ, ширина+висота ≤10000, співвідношення сторін ≤20."""
    if len(image) > 10 * 1024 * 1024:
        return False
    try:
        with Image.open(io.BytesIO(image)) as im:
            w, h = im.size
    except Exception:
        return False
    return w + h <= 10000 and max(w, h) / max(1, min(w, h)) <= 20


class TelegramNotifier:
    def __init__(self, tg: TelegramClient, api: LoeApiClient) -> None:
        self.tg, self.api = tg, api

    def send(self, image_url: str | None, title: str, details: str) -> None:
        """Фото з підписом; якщо підпис задовгий — фото із заголовком + деталі окремо.
        Якщо `image_url` порожній або не містить `_GPV` — лише текст через sendMessage."""
        if not is_gpv_image(image_url):
            self._send_text_only(title, details)
            return

        caption = f"{title}\n\n{details}" if details else title
        too_long = visible_len(caption) > CAPTION_LIMIT
        if too_long:
            caption = title

        image = None
        try:
            image = self.api.download_image(image_url)
        except Exception as exc:
            log.warning("Не вдалося завантажити зображення %s: %s", image_url, exc)

        filename = os.path.basename(image_url.split("?")[0]) or "schedule.png"
        link = f'<a href="{html.escape(image_url, quote=True)}">Відкрити зображення графіка</a>'
        attempts = []
        if image:
            if photo_fits_telegram(image):
                attempts.append(("sendPhoto", lambda: self.tg.send_photo(image, filename, caption)))
            attempts.append(("sendDocument", lambda: self.tg.send_document(image, filename, caption)))
        attempts.append(("sendPhoto(URL)", lambda: self.tg.send_photo_url(image_url, caption)))
        attempts.append(("sendMessage", lambda: self.tg.send_message(f"{caption}\n\n{link}")))

        last_error: Exception | None = None
        for name, action in attempts:
            try:
                action()
                log.info("Сповіщення надіслано через %s", name)
                break
            except (TelegramError, requests.RequestException) as exc:
                last_error = exc
                log.warning("Спроба %s не вдалася: %s", name, exc)
        else:
            raise last_error  # type: ignore[misc]

        if too_long and details:
            self._send_chunks(details)

    def _send_text_only(self, title: str, details: str) -> None:
        """Сповіщення без зображення: заголовок + деталі однією або кількома sendMessage."""
        text = f"{title}\n\n{details}" if details else title
        self._send_chunks(text)
        log.info("Сповіщення надіслано текстом (без зображення)")

    def _send_chunks(self, text: str) -> None:
        """Шле текст частинами. Якщо хоч одну не вдалося надіслати — кидає TelegramError,
        щоб OutageMonitor не зберіг новий хеш і повторив спробу в наступному циклі."""
        chunks = split_message(text)
        for i, chunk in enumerate(chunks, 1):
            if not self._send_text_with_retry(chunk):
                raise TelegramError(
                    "sendMessage", 0,
                    f"не вдалося надіслати частину {i}/{len(chunks)} після всіх спроб",
                )

    def _send_text_with_retry(self, text: str, attempts: int = 3) -> bool:
        for n in range(1, attempts + 1):
            try:
                self.tg.send_message(text)
                return True
            except (TelegramError, requests.RequestException) as exc:
                log.warning("sendMessage (спроба %s/%s): %s", n, attempts, exc)
                if n < attempts:
                    time.sleep(2)
        log.error("Не вдалося надіслати деталі змін у Telegram")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Стан (state.json)
# ──────────────────────────────────────────────────────────────────────────────
class StateStore:
    def __init__(self, path: str, retention_days: int = DEFAULT_STATE_RETENTION_DAYS) -> None:
        self.path = path
        self.retention_days = retention_days
        self.data: dict = {}

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as exc:
            log.error("state.json пошкоджено (%s) — починаю з порожнього стану", exc)
            try:
                os.replace(self.path, self.path + ".bak")
            except OSError:
                pass
            self.data = {}

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def prune(self, today: date) -> None:
        limit = today - timedelta(days=self.retention_days)
        for key in list(self.data):
            try:
                if date.fromisoformat(key) < limit:
                    del self.data[key]
            except ValueError:
                del self.data[key]


def compute_hash(image_url, groups: dict, text: str = "") -> str:
    payload = {"image_url": image_url, "groups": groups}
    if text:   # текст враховуємо лише коли груп немає — хеші зі старого state.json лишаються чинними
        payload["text"] = text
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def append_fallback_text(details: str, text: str) -> str:
    """Додає очищений текст rawHtml (екранований під HTML-режим Telegram) до деталей."""
    if not text:
        return details
    if len(text) > FALLBACK_TEXT_LIMIT:
        text = text[:FALLBACK_TEXT_LIMIT].rstrip() + "…"
    body = html.escape(text, quote=False)
    return f"{details}\n\n{body}" if details else body


# ──────────────────────────────────────────────────────────────────────────────
# Доступ: користувачі, групи, одержувачі сповіщень (access.json)
# ──────────────────────────────────────────────────────────────────────────────
PANEL_LIST_LIMIT = 30      # скільки рядків/кнопок показувати в одному екрані панелі


def _now_iso() -> str:
    return kyiv_now().isoformat(timespec="seconds")


def display_name(user: dict) -> str:
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    return (name or user.get("username") or str(user.get("id", "?")))[:64]


def fmt_person(uid, rec: dict) -> str:
    """«Ім'я (@username) — ID» у HTML (усе екрановано)."""
    name = html.escape(str(rec.get("name") or "без імені"))
    uname = f" (@{html.escape(str(rec['username']))})" if rec.get("username") else ""
    return f"{name}{uname} — <code>{uid}</code>"


class AccessRegistry:
    """Хто отримує сповіщення: адмін, основний чат із config.json, схвалені
    користувачі та групи/канали, куди бота додав адмін. Зберігається в access.json."""

    def __init__(self, path: str, admin_id: int, main_chat_id: str = "") -> None:
        self.path = path
        self.admin_id = int(admin_id)
        self.main_chat_id = str(main_chat_id or "")
        self._lock = threading.RLock()
        self.data = self._empty()
        self.load()

    @staticmethod
    def _empty() -> dict:
        return {"users": {}, "pending": {}, "denied": {}, "groups": {}, "admin_notify": True}

    # ---- збереження ----
    def load(self) -> None:
        with self._lock:
            raw: dict = {}
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("корінь JSON має бути об'єктом")
                raw = loaded
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                log.error("access.json пошкоджено (%s) — починаю з порожнього списку", exc)
                try:
                    os.replace(self.path, self.path + ".bak")
                except OSError:
                    pass
            data = self._empty()
            for key in ("users", "pending", "denied", "groups"):
                if isinstance(raw.get(key), dict):
                    data[key] = raw[key]
            data["admin_notify"] = bool(raw.get("admin_notify", True))
            self.data = data

    def _save(self) -> None:           # викликати під self._lock
        try:
            _write_json_atomic(self.path, self.data)
        except OSError as exc:
            log.error("Не вдалося зберегти access.json: %s", exc)

    # ---- перевірки ----
    def is_admin(self, uid) -> bool:
        try:
            return int(uid) == self.admin_id
        except (TypeError, ValueError):
            return False

    def is_allowed(self, uid) -> bool:
        return self.is_admin(uid) or str(uid) in self.data["users"]

    @property
    def admin_notify(self) -> bool:
        return self.data["admin_notify"]

    def set_admin_notify(self, value: bool) -> None:
        with self._lock:
            self.data["admin_notify"] = bool(value)
            self._save()

    # ---- користувачі ----
    def request_access(self, uid, name: str, username: str) -> str:
        """'allowed' | 'denied' | 'pending' (вже очікує) | 'new' (запит створено)."""
        with self._lock:
            key = str(uid)
            if self.is_allowed(uid):
                return "allowed"
            if key in self.data["denied"]:
                return "denied"
            if key in self.data["pending"]:
                return "pending"
            self.data["pending"][key] = {
                "name": name, "username": username, "requested_at": _now_iso(),
            }
            self._save()
            return "new"

    def add_user(self, uid, name: str = "", username: str = "", by=None) -> dict:
        with self._lock:
            key = str(uid)
            prev = self.data["pending"].pop(key, None) or self.data["denied"].pop(key, None) or {}
            rec = self.data["users"].get(key) or {"added_at": _now_iso()}
            rec["name"] = name or rec.get("name") or prev.get("name", "")
            rec["username"] = username or rec.get("username") or prev.get("username", "")
            rec.pop("inactive", None)
            if by is not None:
                rec["added_by"] = int(by)
            self.data["users"][key] = rec
            self._save()
            return rec

    def remove_user(self, uid) -> dict | None:
        with self._lock:
            rec = self.data["users"].pop(str(uid), None)
            if rec is not None:
                self._save()
            return rec

    def deny(self, uid) -> dict | None:
        """Відхиляє запит, що очікує. None — такого запиту вже немає."""
        with self._lock:
            rec = self.data["pending"].pop(str(uid), None)
            if rec is None:
                return None
            self.data["denied"][str(uid)] = {**rec, "denied_at": _now_iso()}
            self._save()
            return rec

    def touch_user(self, uid, name: str = "", username: str = "") -> bool:
        """Оновлює ім'я відомого користувача й знімає позначку «неактивний»."""
        with self._lock:
            rec = self.data["users"].get(str(uid))
            if rec is None:
                return False
            changed = False
            for field, value in (("name", name), ("username", username)):
                if value and rec.get(field) != value:
                    rec[field] = value
                    changed = True
            if rec.pop("inactive", None):
                changed = True
            if changed:
                self._save()
            return True

    def mark_user_inactive(self, uid) -> bool:
        with self._lock:
            rec = self.data["users"].get(str(uid))
            if rec is None or rec.get("inactive"):
                return False
            rec["inactive"] = True
            self._save()
            return True

    def pending_record(self, uid) -> dict | None:
        return self.data["pending"].get(str(uid))

    # ---- групи / канали ----
    def add_group(self, chat_id, title: str, kind: str, by) -> None:
        with self._lock:
            self.data["groups"][str(chat_id)] = {
                "title": title, "type": kind, "added_by": int(by), "added_at": _now_iso(),
            }
            self._save()

    def has_group(self, chat_id) -> bool:
        return str(chat_id) in self.data["groups"]

    def remove_group(self, chat_id) -> dict | None:
        with self._lock:
            rec = self.data["groups"].pop(str(chat_id), None)
            if rec is not None:
                self._save()
            return rec

    def migrate_chat(self, old_id, new_id) -> str:
        """Група стала супергрупою (змінився chat_id). Повертає новий id як рядок."""
        old, new = str(old_id), str(new_id)
        with self._lock:
            rec = self.data["groups"].pop(old, None)
            if rec is not None:
                self.data["groups"][new] = rec
                self._save()
            if self.main_chat_id == old:
                self.main_chat_id = new
                log.warning("Основний чат змінив id: %s → %s (оновіть CHAT_ID у config.json)",
                            old, new)
        return new

    def drop_recipient(self, chat_id) -> str:
        """Чат недоступний (бота вигнали/заблокували): групу прибираємо, користувача
        позначаємо неактивним. Повертає 'group' | 'user' | 'other'."""
        key = str(chat_id)
        with self._lock:
            if key in self.data["groups"]:
                self.remove_group(key)
                return "group"
            if key in self.data["users"]:
                self.mark_user_inactive(key)
                return "user"
        return "other"

    # ---- одержувачі та лічильники ----
    def recipients(self) -> list:
        """Унікальні chat_id у порядку пріоритету: адмін, основний чат, користувачі, групи."""
        with self._lock:
            out: list = []

            def add(chat) -> None:
                chat = str(chat or "")
                if chat and chat not in out:
                    out.append(chat)

            if self.data["admin_notify"]:
                add(self.admin_id)
            add(self.main_chat_id)
            for key, rec in self.data["users"].items():
                if not rec.get("inactive"):
                    add(key)
            for key in self.data["groups"]:
                add(key)
            return out

    def counts(self) -> dict:
        with self._lock:
            return {
                "users": len(self.data["users"]),
                "pending": len(self.data["pending"]),
                "groups": len(self.data["groups"]),
            }


# ──────────────────────────────────────────────────────────────────────────────
# Розсилка сповіщень усім одержувачам
# ──────────────────────────────────────────────────────────────────────────────
def is_chat_gone(exc: TelegramError) -> bool:
    """Помилка означає, що в цей чат бот більше не може писати (повторювати марно)."""
    if exc.status == 403:
        return True
    desc = exc.description.lower()
    return exc.status in (400, 404) and any(s in desc for s in (
        "chat not found", "peer_id_invalid", "user is deactivated", "bot was kicked",
        "not a member", "have no rights", "chat_write_forbidden", "channel_private",
    ))


class _CachingImageApi:
    """Завантажує зображення графіка один раз для всіх одержувачів розсилки."""

    def __init__(self, api) -> None:
        self.api = api
        self._cache: dict = {}
        self._lock = threading.Lock()

    def download_image(self, url: str) -> bytes:
        with self._lock:                  # інші потоки чекають на перше завантаження
            hit = self._cache.get(url)
            if hit is None:
                try:
                    hit = ("ok", self.api.download_image(url))
                except Exception as exc:
                    hit = ("err", exc)
                self._cache[url] = hit
        if hit[0] == "err":
            raise hit[1]
        return hit[1]


class BroadcastNotifier:
    """Надсилає одне сповіщення всім одержувачам з AccessRegistry.

    • Перший одержувач (адмін) — одразу; решта — паралельно (зображення качається один
      раз, у Telegram завантажується один раз, далі йде file_id).
    • Недоступні чати (403 тощо) не блокують решту: групу прибираємо зі списку,
      користувача позначаємо неактивним, адміна повідомляємо.
    • Тимчасова помилка в одного одержувача → виняток (OutageMonitor не збереже стан і
      повторить спробу), але повторно слатимемо лише тим, кому ще не дійшло."""

    def __init__(self, tg: TelegramClient, api, registry: AccessRegistry,
                 max_workers: int = BROADCAST_WORKERS) -> None:
        self.tg, self.api, self.registry, self.max_workers = tg, api, registry, max_workers
        self._lock = threading.Lock()
        self._delivery: dict = {}     # ключ сповіщення → {"done": set, "fails": dict}

    def _send_one(self, chat_id: str, api, image_url, title: str, details: str) -> tuple:
        """→ (статус, виняток, фактичний chat_id); статус: 'ok' | 'gone' | 'error'."""
        for attempt in (1, 2):
            try:
                TelegramNotifier(self.tg.bind(chat_id), api).send(image_url, title, details)
                return "ok", None, chat_id
            except TelegramError as exc:
                if exc.migrate_to and attempt == 1:
                    chat_id = self.registry.migrate_chat(chat_id, exc.migrate_to)
                    continue
                return ("gone" if is_chat_gone(exc) else "error"), exc, chat_id
            except Exception as exc:        # requests.RequestException та решта
                return "error", exc, chat_id
        return "error", RuntimeError("migrate loop"), chat_id  # pragma: no cover

    def _notify_admin(self, text: str) -> None:
        try:
            self.tg.send_message(text, chat_id=self.registry.admin_id)
        except Exception as exc:
            log.warning("Не вдалося повідомити адміна: %s", exc)

    def _handle_gone(self, chat_id: str, exc: TelegramError) -> None:
        kind = self.registry.drop_recipient(chat_id)
        reason = html.escape(exc.description)
        if kind == "group":
            self._notify_admin(f"⚠️ Чат <code>{chat_id}</code> прибрано зі списку розсилки: "
                               f"бот втратив доступ ({reason}).")
        elif kind == "user":
            self._notify_admin(f"⚠️ Користувач <code>{chat_id}</code> недоступний (бота "
                               f"заблоковано?) — позначено неактивним. Сповіщення "
                               f"поновляться після його /start.")
        else:
            log.warning("Чат %s недоступний: %s", chat_id, exc)

    def send(self, image_url, title: str, details: str) -> None:
        targets = self.registry.recipients()
        if not targets:
            log.warning("Немає жодного одержувача сповіщень — нікому надсилати")
            return
        key = hashlib.sha1(json.dumps([image_url, title, details],
                                      ensure_ascii=False).encode("utf-8")).hexdigest()
        with self._lock:
            rec = self._delivery.setdefault(key, {"done": set(), "fails": {}})
            while len(self._delivery) > 20:
                self._delivery.pop(next(iter(self._delivery)))
        pending = [c for c in targets if c not in rec["done"]]
        api = _CachingImageApi(self.api)

        results: dict = {}
        if pending:
            results[pending[0]] = self._send_one(pending[0], api, image_url, title, details)
        rest = pending[1:]
        if rest:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(rest))) as pool:
                futures = {c: pool.submit(self._send_one, c, api, image_url, title, details)
                           for c in rest}
                for chat_id, fut in futures.items():
                    results[chat_id] = fut.result()

        failed: list = []
        for chat_id, (status, exc, effective) in results.items():
            if status == "ok":
                rec["done"].update((chat_id, effective))
            elif status == "gone":
                rec["done"].update((chat_id, effective))
                self._handle_gone(effective, exc)
            else:
                rec["fails"][chat_id] = rec["fails"].get(chat_id, 0) + 1
                if rec["fails"][chat_id] >= MAX_RECIPIENT_ATTEMPTS:
                    log.error("Чат %s: %s невдалих спроб підряд — припиняю повтори: %s",
                              chat_id, MAX_RECIPIENT_ATTEMPTS, exc)
                    rec["done"].update((chat_id, effective))
                else:
                    log.warning("Чат %s: сповіщення не доставлено (%s) — повторю", chat_id, exc)
                    failed.append(exc)
        if failed:
            raise failed[0]               # стан не зберігається → наступний цикл дошле решті
        with self._lock:
            self._delivery.pop(key, None)
        log.info("Розсилку завершено: %s одержувач(ів)", len(targets))


# ──────────────────────────────────────────────────────────────────────────────
# Керування ботом у Telegram: запити доступу, адмін-панель, групи
# ──────────────────────────────────────────────────────────────────────────────
def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _kb(*rows) -> dict:
    return {"inline_keyboard": [list(r) for r in rows]}


class BotController:
    """Long polling getUpdates у власному потоці.

    Приватні чати: запит доступу (/start) для нових, адмін-панель для ADMIN_ID.
    Групи/канали: бота може додати лише адмін — інакше бот одразу виходить із чату."""

    def __init__(self, tg: TelegramClient, registry: AccessRegistry, interval: int,
                 stop_event: threading.Event, check_now=None) -> None:
        self.tg, self.registry, self.interval = tg, registry, interval
        self.stop_event, self.check_now = stop_event, check_now
        self._offset: int | None = None
        self._awaiting_id = False

    # ---- цикл опитування ----
    def run(self) -> None:
        log.info("Прийом команд Telegram запущено (адмін ID %s)", self.registry.admin_id)
        try:
            self.tg.delete_webhook()      # webhook блокує getUpdates (409)
        except Exception as exc:
            log.warning("deleteWebhook: %s", exc)
        delay = 0
        while not self.stop_event.is_set():
            try:
                updates = self.tg.get_updates(self._offset)
                delay = 0
            except TelegramError as exc:
                log.warning("getUpdates: %s", exc)
                delay = 10 if exc.status == 409 else min(max(delay * 2, 5), 60)
                if self.stop_event.wait(delay):
                    break
                continue
            except requests.RequestException as exc:
                log.warning("getUpdates (мережа): %s", exc)
                delay = min(max(delay * 2, 5), 60)
                if self.stop_event.wait(delay):
                    break
                continue
            except Exception:
                log.exception("Неочікувана помилка getUpdates")
                if self.stop_event.wait(10):
                    break
                continue
            for upd in updates:
                self._offset = upd["update_id"] + 1
                try:
                    self.handle_update(upd)
                except Exception:
                    log.exception("Помилка обробки оновлення Telegram")
        log.info("Прийом команд Telegram зупинено")

    def handle_update(self, upd: dict) -> None:
        if upd.get("message"):
            self._on_message(upd["message"])
        elif upd.get("callback_query"):
            self._on_callback(upd["callback_query"])
        elif upd.get("my_chat_member"):
            self._on_my_chat_member(upd["my_chat_member"])

    # ---- допоміжне ----
    def _safe_send(self, chat_id, text: str, markup: dict | None = None) -> bool:
        try:
            self.tg.send_message(text, chat_id=chat_id, reply_markup=markup)
            return True
        except (TelegramError, requests.RequestException) as exc:
            log.warning("Не вдалося надіслати повідомлення в %s: %s", chat_id, exc)
            return False

    def _notify_admin(self, text: str, markup: dict | None = None) -> bool:
        return self._safe_send(self.registry.admin_id, text, markup)

    def _show(self, chat_id, message_id, text: str, markup: dict | None = None) -> None:
        """Оновлює повідомлення панелі на місці (або шле нове, якщо не вийшло)."""
        if message_id is not None:
            try:
                self.tg.edit_message_text(chat_id, message_id, text, reply_markup=markup)
                return
            except TelegramError as exc:
                if "not modified" in exc.description.lower():
                    return
                log.info("editMessageText не вдалося (%s) — надсилаю нове повідомлення", exc)
            except requests.RequestException as exc:
                log.info("editMessageText (мережа): %s", exc)
        self._safe_send(chat_id, text, markup)

    # ---- екрани адмін-панелі ----
    def _main_screen(self) -> tuple:
        c = self.registry.counts()
        notify_on = self.registry.admin_notify
        text = (
            "🛠 <b>Адмін-панель</b>\n\n"
            f"👤 Користувачів з доступом: <b>{c['users']}</b>\n"
            f"📨 Запитів на доступ: <b>{c['pending']}</b>\n"
            f"💬 Груп/каналів: <b>{c['groups']}</b>\n"
            f"🔔 Мої сповіщення: <b>{'увімкнено' if notify_on else 'вимкнено'}</b>\n"
            f"⏱ Перевірка графіка кожні <b>{self.interval} с</b>"
        )
        rows = [
            [_btn(f"📨 Запити ({c['pending']})", "pend"),
             _btn(f"👤 Користувачі ({c['users']})", "users")],
            [_btn(f"💬 Групи ({c['groups']})", "groups"), _btn("➕ Додати за ID", "add")],
            [_btn("🔕 Вимкнути мої сповіщення" if notify_on else "🔔 Увімкнути мої сповіщення",
                  "tgl")],
        ]
        if self.check_now is not None:
            rows.append([_btn("🔄 Перевірити графік зараз", "chk")])
        return text, _kb(*rows)

    def _pending_screen(self) -> tuple:
        pending = self.registry.data["pending"]
        back = [_btn("⬅️ Назад", "main")]
        if not pending:
            return "📨 <b>Запити на доступ</b>\n\nНових запитів немає.", _kb(back)
        lines, rows = ["📨 <b>Запити на доступ</b>\n"], []
        for uid, rec in list(pending.items())[:PANEL_LIST_LIMIT]:
            lines.append(f"• {fmt_person(uid, rec)}")
            label = str(rec.get("name") or uid)[:18]
            rows.append([_btn(f"✅ {label}", f"ok:{uid}"), _btn(f"❌ {label}", f"no:{uid}")])
        if len(pending) > PANEL_LIST_LIMIT:
            lines.append(f"\n…та ще {len(pending) - PANEL_LIST_LIMIT}")
        rows.append(back)
        return "\n".join(lines), _kb(*rows)

    def _users_screen(self) -> tuple:
        users = self.registry.data["users"]
        back = [_btn("⬅️ Назад", "main")]
        if not users:
            return ("👤 <b>Користувачі з доступом</b>\n\nПоки нікого. Додайте за ID або "
                    "схваліть запит.", _kb(back))
        lines, rows = ["👤 <b>Користувачі з доступом</b>\n"], []
        for uid, rec in list(users.items())[:PANEL_LIST_LIMIT]:
            mark = " ⚠️ неактивний" if rec.get("inactive") else ""
            lines.append(f"• {fmt_person(uid, rec)}{mark}")
            rows.append([_btn(f"🗑 {str(rec.get('name') or uid)[:28]}", f"rm:{uid}")])
        if len(users) > PANEL_LIST_LIMIT:
            lines.append(f"\n…та ще {len(users) - PANEL_LIST_LIMIT}")
        lines.append("\n⚠️ — бот заблоковано або користувач не натискав /start; "
                     "сповіщення відновляться після його /start.")
        rows.append(back)
        return "\n".join(lines), _kb(*rows)

    def _groups_screen(self) -> tuple:
        groups = self.registry.data["groups"]
        back = [_btn("⬅️ Назад", "main")]
        lines = ["💬 <b>Групи та канали</b>\n"]
        rows = []
        if groups:
            for cid, rec in list(groups.items())[:PANEL_LIST_LIMIT]:
                title = html.escape(str(rec.get("title") or cid))
                lines.append(f"• {title} — <code>{cid}</code>")
                rows.append([_btn(f"🗑 {str(rec.get('title') or cid)[:28]}", f"rg:{cid}")])
        else:
            lines.append("Жодної групи не підключено.")
        lines.append("\nЩоб підключити групу чи канал, просто додайте туди бота (для каналу — "
                     "як адміністратора з правом публікації). Це можете зробити лише ви: "
                     "якщо бота додасть хтось інший, він одразу вийде з чату.")
        rows.append(back)
        return "\n".join(lines), _kb(*rows)

    def _send_panel(self, chat_id) -> None:
        text, markup = self._main_screen()
        self._safe_send(chat_id, text, markup)

    # ---- повідомлення ----
    def _on_message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        if msg.get("migrate_to_chat_id"):
            self.registry.migrate_chat(chat.get("id"), msg["migrate_to_chat_id"])
            return
        if chat.get("type") != "private":
            return                       # у групах бот лише розсилає
        user = msg.get("from") or {}
        if not user.get("id") or user.get("is_bot"):
            return
        text = (msg.get("text") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
        if self.registry.is_admin(user["id"]):
            self._admin_message(chat["id"], cmd, arg.strip(), text)
        else:
            self._user_message(chat["id"], user, cmd)

    def _user_message(self, chat_id, user: dict, cmd: str) -> None:
        uid, name, username = user["id"], display_name(user), user.get("username") or ""
        if self.registry.is_allowed(uid):
            self.registry.touch_user(uid, name, username)
            self._safe_send(chat_id, "✅ У вас є доступ. Ви отримуватимете сповіщення про "
                                     "графіки відключень світла.")
            return
        if cmd != "/start":
            self._safe_send(chat_id, "Щоб отримати доступ до сповіщень, натисніть /start.")
            return
        status = self.registry.request_access(uid, name, username)
        if status == "denied":
            self._safe_send(chat_id, "⛔ Адміністратор відхилив запит на доступ.")
        elif status == "pending":
            self._safe_send(chat_id, "⏳ Ваш запит уже надіслано адміністратору. Очікуйте.")
        else:                             # 'new'
            self._safe_send(chat_id, "⏳ Запит на доступ надіслано адміністратору. Ви "
                                     "отримаєте повідомлення, щойно його розглянуть.")
            rec = self.registry.pending_record(uid) or {}
            sent = self._notify_admin(
                f"🆕 <b>Запит на доступ</b>\n\n{fmt_person(uid, rec)}",
                _kb([_btn("✅ Надати", f"ok:{uid}"), _btn("❌ Відхилити", f"no:{uid}")]),
            )
            if not sent:
                log.warning("Запит від %s збережено, але адміну не доставлено — адміністратор "
                            "має спершу написати боту /start", uid)

    def _admin_message(self, chat_id, cmd: str, arg: str, text: str) -> None:
        if cmd in ("/adduser", "/removeuser"):
            self._awaiting_id = False
            if not arg:
                self._safe_send(chat_id, f"Використання: <code>{cmd} 123456789</code>")
            elif cmd == "/adduser":
                self._add_user_by_id(chat_id, arg)
            else:
                self._remove_user_by_id(chat_id, arg)
        elif cmd == "/cancel":
            self._awaiting_id = False
            self._safe_send(chat_id, "Скасовано.")
        elif self._awaiting_id and not cmd:
            self._add_user_by_id(chat_id, text)
        else:                             # /start, /admin, /help або будь-який інший текст
            self._awaiting_id = False
            self._send_panel(chat_id)

    def _parse_uid(self, text: str):
        try:
            uid = int(text.strip())
        except ValueError:
            return None
        return uid if uid > 0 else None

    def _add_user_by_id(self, chat_id, text: str) -> None:
        uid = self._parse_uid(text)
        if uid is None:
            self._safe_send(chat_id, "❗ ID має бути додатним числом, наприклад "
                                     "<code>123456789</code>. Надішліть його ще раз або /cancel.")
            return
        self._awaiting_id = False
        if self.registry.is_admin(uid):
            self._safe_send(chat_id, "Це ваш власний ID — ви вже адміністратор.")
            return
        existed = self.registry.is_allowed(uid)
        self.registry.add_user(uid, by=self.registry.admin_id)
        told = self._safe_send(uid, "✅ <b>Вам надано доступ.</b> Тепер ви отримуватимете "
                                    "сповіщення про графіки відключень світла.")
        note = ("Користувача сповіщено." if told else
                "Бот поки не може йому писати — нехай натисне /start у боті.")
        self._safe_send(chat_id, f"✅ Доступ {'вже був' if existed else 'надано'} "
                                 f"користувачу <code>{uid}</code>. {note}")
        self._send_panel(chat_id)

    def _remove_user_by_id(self, chat_id, text: str) -> None:
        uid = self._parse_uid(text)
        if uid is None:
            self._safe_send(chat_id, "❗ ID має бути додатним числом.")
            return
        if self.registry.remove_user(uid) is None:
            self._safe_send(chat_id, f"Користувача <code>{uid}</code> немає в списку.")
        else:
            self._safe_send(chat_id, f"🗑 Доступ користувача <code>{uid}</code> скасовано.")

    # ---- кнопки ----
    def _on_callback(self, cb: dict) -> None:
        cb_id = cb.get("id", "")
        user = cb.get("from") or {}
        if not self.registry.is_admin(user.get("id")):
            self._answer(cb_id, "⛔ Лише для адміністратора", alert=True)
            return
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id", user.get("id"))
        message_id = msg.get("message_id")
        toast = ""
        try:
            toast = self._dispatch(cb.get("data") or "", chat_id, message_id) or ""
        finally:
            self._answer(cb_id, toast)

    def _answer(self, cb_id: str, text: str = "", alert: bool = False) -> None:
        try:
            self.tg.answer_callback(cb_id, text, alert=alert)
        except (TelegramError, requests.RequestException) as exc:
            log.info("answerCallbackQuery: %s", exc)

    def _show_main(self, chat_id, message_id) -> None:
        self._show(chat_id, message_id, *self._main_screen())

    def _dispatch(self, data: str, chat_id, message_id) -> str:
        action, _, arg = data.partition(":")
        if action == "main":
            self._awaiting_id = False
            self._show_main(chat_id, message_id)
        elif action == "pend":
            self._show(chat_id, message_id, *self._pending_screen())
        elif action == "users":
            self._show(chat_id, message_id, *self._users_screen())
        elif action == "groups":
            self._show(chat_id, message_id, *self._groups_screen())
        elif action == "add":
            self._awaiting_id = True
            self._show(chat_id, message_id,
                       "➕ <b>Додати користувача</b>\n\nНадішліть числовий Telegram-ID "
                       "користувача. Щоб бот міг йому писати, користувач має один раз "
                       "натиснути /start у боті.",
                       _kb([_btn("↩️ Скасувати", "main")]))
        elif action == "tgl":
            self.registry.set_admin_notify(not self.registry.admin_notify)
            self._show_main(chat_id, message_id)
            return "🔔 Увімкнено" if self.registry.admin_notify else "🔕 Вимкнено"
        elif action == "chk":
            if self.check_now is None:
                return "Недоступно"
            self.check_now()
            return "🔄 Перевірку запущено"
        elif action in ("rg", "rgy"):
            return self._group_action(action, arg, chat_id, message_id)
        elif action in ("ok", "no", "rm", "rmy"):
            uid = self._parse_uid(arg)
            if uid is None:
                return "Некоректний ID"
            return self._user_action(action, uid, chat_id, message_id)
        return ""

    def _user_action(self, action: str, uid: int, chat_id, message_id) -> str:
        if action == "ok":
            self.registry.add_user(uid, by=self.registry.admin_id)
            told = self._safe_send(uid, "✅ <b>Доступ надано!</b> Тепер ви отримуватимете "
                                        "сповіщення про графіки відключень світла.")
            self._show(chat_id, message_id, *self._pending_screen())
            return "✅ Доступ надано" if told else "✅ Надано, але користувач недоступний"
        if action == "no":
            if self.registry.deny(uid) is None:
                self._show(chat_id, message_id, *self._pending_screen())
                return "Запит уже оброблено"
            self._safe_send(uid, "⛔ Адміністратор відхилив запит на доступ.")
            self._show(chat_id, message_id, *self._pending_screen())
            return "❌ Відхилено"
        if action == "rm":
            rec = self.registry.data["users"].get(str(uid))
            if rec is None:
                self._show(chat_id, message_id, *self._users_screen())
                return "Користувача вже немає"
            self._show(chat_id, message_id,
                       f"Скасувати доступ користувачу?\n\n{fmt_person(uid, rec)}",
                       _kb([_btn("✅ Так, скасувати", f"rmy:{uid}"),
                            _btn("↩️ Ні", "users")]))
            return ""
        # rmy
        if self.registry.remove_user(uid) is not None:
            self._safe_send(uid, "🚫 Ваш доступ до сповіщень скасовано адміністратором.")
        self._show(chat_id, message_id, *self._users_screen())
        return "🗑 Доступ скасовано"

    def _group_action(self, action: str, cid: str, chat_id, message_id) -> str:
        rec = self.registry.data["groups"].get(cid)
        if rec is None:
            self._show(chat_id, message_id, *self._groups_screen())
            return "Групу вже прибрано"
        if action == "rg":
            title = html.escape(str(rec.get("title") or cid))
            self._show(chat_id, message_id,
                       f"Відключити «{title}» і вийти з чату?",
                       _kb([_btn("✅ Так, вийти", f"rgy:{cid}"), _btn("↩️ Ні", "groups")]))
            return ""
        self.registry.remove_group(cid)
        try:
            self.tg.leave_chat(cid)
        except (TelegramError, requests.RequestException) as exc:
            log.info("leaveChat %s: %s", cid, exc)
        self._show(chat_id, message_id, *self._groups_screen())
        return "🗑 Групу прибрано"

    # ---- зміни статусу бота в чатах ----
    def _on_my_chat_member(self, upd: dict) -> None:
        chat = upd.get("chat") or {}
        actor = upd.get("from") or {}
        new_status = (upd.get("new_chat_member") or {}).get("status")
        cid = chat.get("id")
        if cid is None:
            return
        if chat.get("type") == "private":
            if new_status in ("kicked", "left"):
                if self.registry.mark_user_inactive(cid):
                    log.info("Користувач %s заблокував бота — позначено неактивним", cid)
            elif new_status == "member":
                self.registry.touch_user(cid)
            return

        title = chat.get("title") or str(cid)
        if new_status in ("member", "administrator"):
            if self.registry.has_group(cid):
                return                    # лише зміна прав бота в уже підключеному чаті
            if self.registry.is_admin(actor.get("id")):
                self.registry.add_group(cid, title, chat.get("type", "group"), actor["id"])
                self._safe_send(cid, "✅ Сповіщення про графіки відключень світла підключено "
                                     "до цього чату.")
                self._notify_admin(f"✅ Чат «{html.escape(title)}» (<code>{cid}</code>) "
                                   f"підключено до сповіщень.")
                log.info("Бота додано в чат %s (%s) адміністратором", cid, title)
            else:
                try:
                    self.tg.leave_chat(cid)
                except (TelegramError, requests.RequestException) as exc:
                    log.warning("leaveChat %s: %s", cid, exc)
                who = html.escape(display_name(actor)) if actor else "невідомий"
                uname = f" (@{html.escape(actor['username'])})" if actor.get("username") else ""
                self._notify_admin(f"🚫 Спробу додати бота в чат «{html.escape(title)}» "
                                   f"(<code>{cid}</code>) заблоковано: його додав {who}{uname}, "
                                   f"<code>{actor.get('id', '?')}</code>. Бот вийшов із чату.")
                log.warning("Бота додав не адмін (%s) у чат %s — вийшов", actor.get("id"), cid)
        elif new_status in ("left", "kicked"):
            if self.registry.remove_group(cid) is not None:
                self._notify_admin(f"ℹ️ Бота прибрано з чату «{html.escape(title)}» "
                                   f"(<code>{cid}</code>) — розсилку туди вимкнено.")


# ──────────────────────────────────────────────────────────────────────────────
# Ядро моніторингу
# ──────────────────────────────────────────────────────────────────────────────
class OutageMonitor:
    def __init__(self, api, notifier, state: StateStore, clock=kyiv_now,
                 media_base: str = DEFAULT_API_BASE) -> None:
        self.api, self.notifier, self.state, self.clock = api, notifier, state, clock
        self.media_base = media_base
        self.state.load()

    def _prepare_item(self, item: dict, today: date) -> dict | None:
        """menuItem → нормалізований запис; None — якщо немає ні rawHtml, ні зображення ГПВ."""
        image_rel = (item.get("imageUrl") or "").strip()
        raw_html = item.get("rawHtml") or ""
        if not image_rel and not raw_html.strip():
            return None                                   # порожній елемент

        # Зображення без `_GPV` (або порожнє) — графік скасовано/фото прибрано → лише текст.
        image_url = (absolute_media_url(image_rel, self.media_base)
                     if is_gpv_image(image_rel) else None)
        if image_rel and image_url is None:
            log.info("imageUrl без _GPV (%s) — сповіщення буде текстовим", image_rel)

        text = html_to_text(raw_html)
        if image_url is None and not text:
            log.info("Елемент без зображення ГПВ і без тексту — пропускаю")
            return None

        groups = parse_schedule_from_html(raw_html)
        if not groups:
            log.warning("У rawHtml не знайдено груп із відключеннями (формат змінився або ГПВ скасовано?)")

        sched_date = (extract_schedule_date(text, today)
                      or (extract_date_from_url(image_url) if image_url else None))
        return {"image_url": image_url, "text": text, "groups": groups, "date": sched_date}

    def check_once(self) -> str:
        """Один цикл перевірки ВСІХ menuItems (сьогодні, завтра …), кожна дата — незалежно.
        Повертає найвищий статус: 'sent' > 'stale' > 'unchanged'.
        Якщо обробка котроїсь дати впала — решту все одно обробляємо, а перша помилка
        піднімається в кінці (стан невдалої дати не зберігається → повтор у наступному циклі)."""
        items = self.api.fetch_menu_items()
        now = self.clock()
        today = now.date()

        prepared = [p for p in (self._prepare_item(it, today) for it in items) if p]
        if not prepared:
            log.warning("У menuItems немає елементів з rawHtml чи зображенням ГПВ")
            return "unchanged"

        # Один запис на дату (ключ state.json). Спершу елементи з відомою датою (стабільне
        # сортування зберігає порядок API); без дати — сьогоднішня, якщо вона не зайнята.
        by_date: dict = {}
        for p in sorted(prepared, key=lambda x: x["date"] is None):
            if p["date"] is None:
                p["date"] = today
                log.warning("Не вдалося визначити дату графіка — використовую сьогоднішню (%s)", today)
            if p["date"] in by_date:
                log.warning("Кілька елементів menuItems на %s — обробляю лише перший", p["date"])
                continue
            by_date[p["date"]] = p

        results: list = []
        first_error: Exception | None = None
        for sched_date in sorted(by_date):
            try:
                results.append(self._process(by_date[sched_date], now))
            except Exception as exc:
                log.error("Не вдалося обробити графік на %s: %s", sched_date, exc)
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        for status in ("sent", "stale"):
            if status in results:
                return status
        return "unchanged"

    def _process(self, p: dict, now: datetime) -> str:
        today = now.date()
        tomorrow = today + timedelta(days=1)
        sched_date, image_url, groups = p["date"], p["image_url"], p["groups"]
        fallback_text = p["text"] if not groups else ""   # без груп текст — єдина «суть» графіка

        key = sched_date.isoformat()
        digest = compute_hash(image_url, groups, fallback_text)
        prev = self.state.data.get(key)
        if prev and prev.get("hash") == digest:
            log.info("Без змін (графік на %s)", key)
            return "unchanged"

        entry = {
            "image_url": image_url,
            "hash": digest,
            "parsed_groups": groups,
            "is_first_broadcast": prev is None,
            "updated_at": now.isoformat(timespec="seconds"),
        }

        if sched_date < today:
            log.info("Графік на %s застарів — зберігаю без сповіщення", key)
            self.state.data[key] = entry
            self.state.prune(today)
            self.state.save()
            return "stale"

        if sched_date == tomorrow:
            kind = KIND_TOMORROW_FIRST if prev is None else KIND_TOMORROW_UPDATE
        else:
            kind = KIND_NEW
        diff = diff_schedules(prev.get("parsed_groups") or {}, groups) if prev else None

        title, details = build_notification(kind, sched_date, groups, diff)
        details = append_fallback_text(details, fallback_text)
        log.info("Новий/змінений графік на %s (%s) — надсилаю сповіщення", key, kind)
        self.notifier.send(image_url, title, details)

        # Стан зберігаємо лише після успішної відправки — інакше повторимо в наступному циклі.
        self.state.data[key] = entry
        self.state.prune(today)
        self.state.save()
        return "sent"


# ──────────────────────────────────────────────────────────────────────────────
# Іконка
# ──────────────────────────────────────────────────────────────────────────────
def make_fallback_icon(size: int = 64) -> Image.Image:
    """Жовта блискавка на темному фоні."""
    k = size / 64.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(12 * k), fill=(28, 30, 38, 255))
    bolt = [(30, 4), (46, 4), (37, 24), (50, 24), (22, 60), (29, 34), (16, 34)]
    draw.polygon([(x * k, y * k) for x, y in bolt], fill=(255, 205, 0, 255))
    return img


def load_icon_image() -> Image.Image:
    for path in (resource_path("icon.ico"), os.path.join(APP_DIR, "icon.ico")):
        if os.path.isfile(path):
            try:
                with Image.open(path) as im:
                    im.load()
                    return im.convert("RGBA")
            except Exception as exc:
                log.warning("Не вдалося прочитати %s: %s", path, exc)
    log.info("icon.ico не знайдено — використовую згенеровану іконку")
    return make_fallback_icon(64)


def make_icon_file(path: str) -> None:
    make_fallback_icon(256).save(
        path, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Системний трей + фоновий потік
# ──────────────────────────────────────────────────────────────────────────────
class TrayApp:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_event = threading.Event()
        self._check_lock = threading.Lock()
        self.status = "Запуск…"
        self.icon = None
        self.worker: threading.Thread | None = None
        self.poller: threading.Thread | None = None

        api = LoeApiClient(cfg.api_url, cfg.site_url, cfg.user_agent)
        tg = TelegramClient(cfg.bot_token, cfg.chat_id)
        self.registry = AccessRegistry(ACCESS_PATH, cfg.admin_id, cfg.chat_id)
        self.monitor = OutageMonitor(api, BroadcastNotifier(tg, api, self.registry),
                                     StateStore(STATE_PATH, cfg.state_retention_days),
                                     media_base=cfg.media_base)
        self.controller = BotController(tg, self.registry, cfg.interval, self.stop_event,
                                        check_now=self.on_check_now)

    # ---- статус ----
    @staticmethod
    def _hhmm() -> str:
        return kyiv_now().strftime("%H:%M")

    def set_status(self, text: str) -> None:
        self.status = text
        icon = self.icon
        if icon is None:
            return
        try:
            icon.title = f"{APP_NAME}: {text}"[:127]
            icon.update_menu()
        except Exception:
            pass

    # ---- перевірка ----
    def run_check(self) -> None:
        if not self._check_lock.acquire(blocking=False):
            log.info("Перевірка вже виконується — пропускаю")
            return
        try:
            self.set_status("Перевіряю…")
            try:
                self.monitor.check_once()
                self.set_status(f"Працює, перевірено о {self._hhmm()}")
            except requests.RequestException as exc:
                log.error("Мережева помилка: %s", exc)
                self.set_status(f"Помилка мережі, спроба о {self._hhmm()}")
            except TelegramError as exc:
                log.error("Помилка Telegram: %s", exc)
                self.set_status(f"Помилка Telegram, спроба о {self._hhmm()}")
            except Exception:
                log.exception("Неочікувана помилка під час перевірки")
                self.set_status(f"Помилка (див. bot.log), спроба о {self._hhmm()}")
        finally:
            self._check_lock.release()

    def _worker_loop(self) -> None:
        log.info("Фоновий потік запущено (інтервал %s с)", self.cfg.interval)
        while not self.stop_event.is_set():
            self.run_check()
            if self.stop_event.wait(self.cfg.interval):
                break
        log.info("Фоновий потік зупинено")

    # ---- меню ----
    def on_check_now(self, icon=None, item=None) -> None:
        threading.Thread(target=self.run_check, daemon=True, name="manual-check").start()

    def on_open_site(self, icon=None, item=None) -> None:
        try:
            webbrowser.open(self.cfg.site_url)
        except Exception:
            log.exception("Не вдалося відкрити браузер")

    def on_quit(self, icon=None, item=None) -> None:
        log.info("Вихід за запитом користувача")
        self.stop_event.set()
        if self.icon is not None:
            self.icon.stop()

    def _setup(self, icon) -> None:
        icon.visible = True
        self.worker = threading.Thread(target=self._worker_loop, daemon=True, name="monitor-worker")
        self.worker.start()
        self.poller = threading.Thread(target=self.controller.run, daemon=True, name="bot-commands")
        self.poller.start()

    def run(self) -> None:
        import pystray  # імпорт тут, щоб модуль можна було тестувати без GUI

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self.status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Перевірити зараз", self.on_check_now),
            pystray.MenuItem("Відкрити сайт", self.on_open_site),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Вихід", self.on_quit),
        )
        self.icon = pystray.Icon("loe_outage_monitor", load_icon_image(), APP_NAME, menu)
        self.icon.run(self._setup)       # блокує головний потік до виходу
        self.stop_event.set()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=3)


# ──────────────────────────────────────────────────────────────────────────────
# Точка входу
# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    if "--make-icon" in sys.argv:
        target = os.path.join(APP_DIR, "icon.ico")
        make_icon_file(target)
        print(f"Створено {target}")
        return 0

    setup_logging()
    if not acquire_single_instance():
        show_message(APP_NAME, "Програма вже запущена (шукайте іконку в системному треї).")
        return 0
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.warning("Конфігурація: %s", exc)
        show_message(APP_NAME, str(exc), error=True)
        return 1

    RedactingFormatter.secrets.append(cfg.bot_token)
    log.info("=== %s запущено (інтервал %s с, адмін ID %s) ===", APP_NAME, cfg.interval, cfg.admin_id)
    try:
        TrayApp(cfg).run()
    except Exception:
        log.exception("Критична помилка")
        show_message(APP_NAME, "Критична помилка. Деталі — у файлі bot.log.", error=True)
        return 1
    log.info("=== %s зупинено ===", APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())