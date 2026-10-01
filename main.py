from dotenv import load_dotenv
load_dotenv()

import os
import re
import json
import html
import time
import threading
import datetime
import random
from urllib.parse import quote, urljoin
import requests
import feedparser

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
LAST_TELEGRAM_UPDATE_ID = 0
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")
PUBLISHED_LOCK = threading.Lock()
SETTINGS_LOCK = threading.RLock()

# Кэш уже скачанных страниц статей и проверенных картинок за один запуск:
# без него один и тот же URL скачивается десятки раз и бот не успевает
# уложиться в часовой лимит GitHub Actions.
_ARTICLE_PAGE_CACHE = {}
_IMAGE_CHECK_CACHE = {}

# Момент, после которого сетевые запросы прекращаются (None = без лимита).
_DEADLINE = None


def set_deadline(seconds):
    """Ограничивает суммарное время сетевой работы текущего запуска."""
    global _DEADLINE
    _DEADLINE = time.time() + seconds if seconds else None


def clear_caches():
    _ARTICLE_PAGE_CACHE.clear()
    _IMAGE_CHECK_CACHE.clear()

# ============================================================
# НАСТРОЙКИ
# ============================================================

def sanitize_channel_link(raw_link):
    if not raw_link or not str(raw_link).strip():
        return "https://t.me/nasharusa"
    link = str(raw_link).strip()
    if link.startswith("@"):
        return f"https://t.me/{link[1:]}"
    if "t.me/" in link:
        if not link.startswith(("http://", "https://")):
            return f"https://{link}"
        return link
    if not link.startswith(("http://", "https://")):
        return f"https://t.me/{link}"
    return link

TELEGRAM_CHANNEL_LINK = sanitize_channel_link(
    os.environ.get("TELEGRAM_CHANNEL_LINK", "")
)
TELEGRAM_CHANNEL_NAME = (
    os.environ.get("TELEGRAM_CHANNEL_NAME")
    or os.environ.get("TELEGRAM_CHANNEL_LINK")
    or "⚡️ Наша Раша"
).strip()
if not TELEGRAM_CHANNEL_NAME:
    TELEGRAM_CHANNEL_NAME = "⚡️ Наша Раша"
if TELEGRAM_CHANNEL_NAME.startswith("https://") or TELEGRAM_CHANNEL_NAME.startswith("http://"):
    TELEGRAM_CHANNEL_NAME = "⚡️ Наша Раша"
if not TELEGRAM_CHANNEL_NAME.startswith("⚡️") and not TELEGRAM_CHANNEL_NAME.startswith("@"):
    TELEGRAM_CHANNEL_NAME = f"⚡️ {TELEGRAM_CHANNEL_NAME}"
if TELEGRAM_CHANNEL_LINK and "@" not in TELEGRAM_CHANNEL_NAME and "Наша Раша" not in TELEGRAM_CHANNEL_NAME:
    default_slug = TELEGRAM_CHANNEL_LINK.rstrip("/").split("/")[-1]
    if default_slug:
        TELEGRAM_CHANNEL_NAME = f"⚡️ {default_slug}"


def _write_settings(settings):
    """Атомарная запись: сначала во временный файл, потом переименование.
    Нужно, чтобы два потока (опрос команд и публикация) не оставили
    settings.json в обрезанном виде."""
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, SETTINGS_PATH)


def load_settings():
    default_settings = {
        "interval_minutes": 60,
        "last_post_time": None,
        "total_posts": 0,
        "total_tokens": 0,
        "manual_trigger": False,
        "manual_post_done": False,
        "last_processed_update_id": 0,
    }
    with SETTINGS_LOCK:
        if not os.path.exists(SETTINGS_PATH):
            _write_settings(default_settings)
            return default_settings.copy()
        return _read_settings_unlocked()


def save_settings(settings):
    with SETTINGS_LOCK:
        _write_settings(settings)


def _read_settings_unlocked():
    default_settings = {
        "interval_minutes": 60,
        "last_post_time": None,
        "total_posts": 0,
        "total_tokens": 0,
        "manual_trigger": False,
        "manual_post_done": False,
        "last_processed_update_id": 0,
    }
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default_settings.copy()
    if not isinstance(data, dict):
        return default_settings.copy()
    for key, value in default_settings.items():
        if key not in data:
            data[key] = value
    return data


def update_settings(**changes):
    """Читает настройки, меняет указанные поля и сохраняет — атомарно.
    Нужно потокам, чтобы не затереть правки друг друга."""
    with SETTINGS_LOCK:
        data = _read_settings_unlocked()
        data.update(changes)
        _write_settings(data)
        return data


def bump_settings(**counters):
    """Прибавляет к числовым счётчикам, остальные поля просто ставит."""
    with SETTINGS_LOCK:
        data = _read_settings_unlocked()
        for key, delta in counters.items():
            try:
                current = int(data.get(key, 0) or 0)
            except (TypeError, ValueError):
                current = 0
            data[key] = current + delta
        _write_settings(data)
        return data


# Расширенный список RSS-лент (новости, экономика, IT)
RSS_FEEDS = [
    "https://lenta.ru/rss/news",
    "https://ria.ru/export/rss2/archive/index.xml",
    "https://www.gazeta.ru/export/rss/lenta.xml",
    "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "https://www.kommersant.ru/RSS/news.xml",
    "https://www.vedomosti.ru/rss/news",
    "https://www.forbes.ru/rss/news.xml",
    "https://3dnews.ru/news/rss/",
    "https://habr.com/ru/rss/articles/?limit=50",
    "https://tproger.ru/feed/",
    "https://www.ixbt.com/export/rss.xml",
    "https://www.sports.ru/rss/all_news.xml",
    "https://www.championat.com/rss/news/"
]

# ============================================================
# ВЫБОР РАБОЧЕЙ МОДЕЛИ GROQ
# ============================================================

def get_available_models():
    """Получает список доступных моделей из Groq API."""
    if not GROQ_API_KEY:
        return []
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            return [m["id"] for m in r.json().get("data", [])]
    except Exception as e:
        print(f"Ошибка получения списка моделей: {e}")
    return []

def select_model():
    """Выбирает подходящую текстовую модель из доступных, исходя из предпочтений."""
    preferred = [
        "qwen/qwen3.6-27b",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "groq/compound-mini",
        "groq/compound",
        # старые, на случай если вернутся
        "llama-3.1-70b-versatile",
        "llama-3.2-70b-versatile",
        "llama-3.3-70b-versatile",
        "mixtral-8x7b-32768",
        "gemma2-9b-it"
    ]
    available = get_available_models()
    if available:
        # сначала точное совпадение с предпочтениями
        for model in preferred:
            if model in available:
                print(f"Выбрана модель: {model}")
                return model
        # если точных нет, ищем любую текстовую
        for model in available:
            # пропускаем явно неподходящие
            if any(skip in model for skip in ["whisper", "safeguard", "guard", "orpheus", "prompt-guard"]):
                continue
            if any(kw in model for kw in ["qwen", "gpt-oss", "compound", "llama", "mixtral", "gemma"]):
                print(f"Выбрана fallback модель: {model}")
                return model
    # если API не ответил или подходящих нет
    print("Не удалось выбрать модель автоматически, использую qwen/qwen3.6-27b")
    return "qwen/qwen3.6-27b"

selected_model = select_model()

# ============================================================
# PUBLISHED
# ============================================================

PUBLISHED_PATH = os.path.join(os.path.dirname(__file__), "published.json")


def load_published():
    with PUBLISHED_LOCK:
        if os.path.exists(PUBLISHED_PATH):
            try:
                with open(PUBLISHED_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Ошибка чтения published.json: {e}")
                return []
        return []


def save_published(published_list):
    for attempt in range(3):
        try:
            with PUBLISHED_LOCK:
                with open(PUBLISHED_PATH, "w", encoding="utf-8") as f:
                    json.dump(published_list, f, ensure_ascii=False, indent=2)
            print("Статистика опубликована успешно")
            return True
        except Exception as e:
            print(f"Ошибка записи published.json (попытка {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(5)
    return False

# ============================================================
# TEXT CLEANING
# ============================================================

def clean_html(raw_html):
    if not raw_html:
        return ""
    text = html.unescape(str(raw_html))
    text = re.sub(
        r"<(?:script|style).*?>.*?</(?:script|style)>",
        " ", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()

def clean_article_text(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ============================================================
# ПОЛУЧЕНИЕ ПОЛНОГО ТЕКСТА СТАТЬИ
# ============================================================

def extract_article_text(article_url):
    if not article_url:
        return ""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"
        }
        response = requests.get(article_url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"Не удалось открыть статью: HTTP {response.status_code}")
            return ""
        response.encoding = response.apparent_encoding or "utf-8"
        page = response.text
    except Exception as e:
        print(f"Ошибка загрузки статьи: {e}")
        return ""

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg",
                         "header", "footer", "nav", "aside", "form"]):
            tag.decompose()

        paragraphs = []
        selectors = [
            "article", "[itemprop='articleBody']",
            ".topic-body__content", ".article__body",
            ".article-body", ".article__text",
            ".article-text", ".article-body__content",
            ".content__body"
        ]
        container = None
        for selector in selectors:
            found = soup.select_one(selector)
            if found:
                container = found
                break

        if container:
            for p in container.find_all(["p", "h2", "h3", "li"]):
                text = p.get_text(" ", strip=True)
                if len(text) >= 30:
                    paragraphs.append(text)
        if not paragraphs:
            for p in soup.find_all("p"):
                text = p.get_text(" ", strip=True)
                if len(text) >= 40:
                    paragraphs.append(text)
        if paragraphs:
            result = "\n\n".join(paragraphs)
            return clean_article_text(result[:20000])
    except ImportError:
        print("BeautifulSoup не установлен. Используется fallback-парсер.")
    except Exception as e:
        print(f"Ошибка BeautifulSoup: {e}")

    try:
        json_matches = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page, flags=re.IGNORECASE | re.DOTALL
        )
        for raw_json in json_matches:
            try:
                data = json.loads(raw_json.strip())
                objects = data if isinstance(data, list) else [data]
                for obj in objects:
                    if not isinstance(obj, dict):
                        continue
                    article_body = obj.get("articleBody")
                    if article_body and len(article_body) > 100:
                        return clean_article_text(article_body[:20000])
            except Exception:
                continue
    except Exception as e:
        print(f"Ошибка JSON-LD: {e}")

    return ""

# ============================================================
# IMAGE
# ============================================================

IMAGE_ACCEPT = (
    "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
)

IMAGE_MIN_BYTES = 15000
IMAGE_MIN_SIDE = 300
IMAGE_MAX_BYTES = 15 * 1024 * 1024
IMAGE_TIMEOUT = 10

# Сколько кандидатов-картинок проверяем на одну новость и сколько
# заранее валидных картинок оставляем про запас (повтор при отказе Telegram).
IMAGE_CANDIDATE_LIMIT = 6
IMAGE_GOOD_LIMIT = 2

# Сколько записей берём из каждой RSS-ленты: чем больше, тем выше шанс
# найти новость с нормальной картинкой.
FEED_ENTRIES_PER_SOURCE = 12

# Границы времени. GitHub Actions убивает джобу, если она идёт дольше
# часа, а следующий cron-запуск с cancel-in-progress убивает текущий ещё
# раньше. Поэтому работаем в жёстком бюджете времени.
PUBLISH_BUDGET_SECONDS = int(os.environ.get("PUBLISH_BUDGET_SECONDS", "1500"))
ARTICLE_FETCH_TIMEOUT = 10
SERVICE_RUN_SECONDS = int(os.environ.get("BOT_RUN_SECONDS", "3000"))

# Пауза между опросами команд. getUpdates идёт в long polling и возвращает
# результат сразу, как только команда пришла, поэтому реальная задержка
# ответа упирается только в эту паузу — держим её маленькой.
INTERVAL_POLL_SECONDS = int(os.environ.get("BOT_POLL_SECONDS", "5"))

# Long polling getUpdates: сколько секунд ждать новую команду от Telegram.
# Telegram допускает максимум 50.
LONG_POLL_TIMEOUT = int(os.environ.get("BOT_LONG_POLL", "50"))

BAD_IMAGE_TOKENS = (
    "logo", "icon", "avatar", "sprite", "emoji", "placeholder",
    "blank", "spacer", "favicon", "1x1", "/pixel", "pixel.gif",
    "watermark", "noimage", "no_image", "no-image", "default_img",
)


def extract_image_url(entry):
    """Быстрый способ достать ссылку на картинку из RSS-записи (без сети)."""
    candidates = collect_entry_image_urls(entry)
    for candidate in candidates:
        if is_valid_image_url(candidate):
            return candidate
    return None


def is_valid_image_url(url):
    if not url or not isinstance(url, str):
        return False
    cleaned = url.strip()
    if not cleaned.startswith(("http://", "https://")):
        return False
    lowered = cleaned.lower()
    if any(token in lowered for token in BAD_IMAGE_TOKENS):
        return False
    if any(lowered.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
        return True
    if any(token in lowered for token in ("img", "photo", "/images/", "cdn", "news", "media")):
        return True
    return False


def normalize_image_url(raw_url, base_url=None):
    """Приводит ссылку к абсолютному http(s)-виду. data:-URI и мусор отбрасывает."""
    if not raw_url or not isinstance(raw_url, str):
        return None
    url = raw_url.strip().replace("&amp;", "&")
    if not url or url.startswith("data:") or url.startswith("blob:"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    if base_url:
        try:
            return urljoin(base_url, url)
        except Exception:
            return None
    return None


# --- разбор размеров картинки по сигнатуре файла (без внешних библиотек) ---

def _png_size(data):
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _gif_size(data):
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    return None


def _jpeg_size(data):
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    sof_markers = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                   0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
    index = 2
    total = len(data)
    while index + 4 <= total:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9:
            return None
        seg_len = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in sof_markers:
            if index + 9 > total:
                return None
            return (
                int.from_bytes(data[index + 7:index + 9], "big"),
                int.from_bytes(data[index + 5:index + 7], "big"),
            )
        if seg_len < 2:
            return None
        index += 2 + seg_len
    return None


def _webp_size(data):
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        return (
            1 + int.from_bytes(data[24:27], "little"),
            1 + int.from_bytes(data[27:30], "little"),
        )
    return None


def _bmp_size(data):
    if len(data) >= 26 and data[:2] == b"BM":
        return int.from_bytes(data[18:22], "little"), int.from_bytes(data[22:26], "little")
    return None


def _avif_size(data):
    if len(data) < 16 or data[4:8] != b"ftyp":
        return None
    if data[8:12] not in (b"avif", b"avis", b"av01", b"mif1", b"msf1"):
        return None
    index = 0
    total = len(data)
    while index + 8 <= total:
        box_size = int.from_bytes(data[index:index + 4], "big")
        box_type = data[index + 4:index + 8]
        if box_size < 8:
            break
        if box_type == b"ispe" and index + 20 <= total:
            return (
                int.from_bytes(data[index + 12:index + 16], "big"),
                int.from_bytes(data[index + 16:index + 20], "big"),
            )
        index += box_size
    return None


def _image_size_with_pillow(data):
    try:
        import io
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            width, height = opened.size
        if width and height:
            return int(width), int(height)
    except Exception:
        return None
    return None


def image_dimensions(data):
    """Возвращает (ширина, высота) по сигнатуре файла либо через Pillow."""
    for parser in (_png_size, _jpeg_size, _gif_size, _webp_size, _avif_size, _bmp_size):
        try:
            size = parser(data)
        except Exception:
            size = None
        if size:
            return size
    return _image_size_with_pillow(data)


def _image_is_blank_with_pillow(data):
    """True — картинка почти одноцветная (пустая/белая). None — проверить нечем."""
    try:
        import io
        from PIL import Image, ImageStat
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            small = opened.convert("RGB").resize((32, 32))
            stddev = sum(ImageStat.Stat(small).stddev) / 3.0
            gray = list(small.convert("L").getdata())
            mean = sum(gray) / len(gray)
        solid = stddev < 3.0
        near_white = mean > 245 and stddev < 8.0
        return bool(solid or near_white)
    except Exception:
        return None


def _image_is_blank_heuristic(data, width, height):
    """Запасная проверка без Pillow: однотонная картинка сжимается почти в ноль."""
    pixels = width * height
    if pixels <= 0:
        return False
    return (len(data) / float(pixels)) < 0.02


def fetch_and_check_image(url):
    """Скачивает картинку и проверяет, что она настоящая и не пустая.
    Возвращает байты картинки или None. Результат кэшируется на запуск."""
    if not is_valid_image_url(url):
        return None
    if url in _IMAGE_CHECK_CACHE:
        return _IMAGE_CHECK_CACHE[url]
    if _DEADLINE and time.time() > _DEADLINE:
        return None

    result = _fetch_and_check_image_uncached(url)
    _IMAGE_CHECK_CACHE[url] = result
    return result


def _fetch_and_check_image_uncached(url):
    headers = dict(BROWSER_HEADERS)
    headers["Accept"] = IMAGE_ACCEPT
    try:
        response = requests.get(url, headers=headers, timeout=IMAGE_TIMEOUT, stream=True)
    except Exception as e:
        print(f"Не удалось скачать картинку: {e}")
        return None

    try:
        if response.status_code != 200:
            print(f"Картинка вернула HTTP {response.status_code}")
            return None

        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type.startswith("text/") or content_type in (
            "application/json", "application/xml", "application/javascript"
        ):
            print(f"По ссылке отдаётся не картинка, а {content_type}")
            return None

        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > IMAGE_MAX_BYTES:
            print("Картинка слишком большая, пропускаем")
            return None

        data = response.content
    finally:
        response.close()

    if not data or len(data) < IMAGE_MIN_BYTES:
        size = len(data) if data else 0
        print(f"Картинка слишком маленькая ({size} байт) — похоже на заглушку")
        return None

    size = image_dimensions(data)
    if not size:
        print("Не удалось распознать формат картинки, пропускаем")
        return None

    width, height = size
    if width < IMAGE_MIN_SIDE or height < IMAGE_MIN_SIDE:
        print(f"Картинка слишком маленькая ({width}x{height}), пропускаем")
        return None

    blank = _image_is_blank_with_pillow(data)
    if blank is None:
        blank = _image_is_blank_heuristic(data, width, height)
    if blank:
        print("Картинка пустая или одноцветная, пропускаем")
        return None

    return data


def _pick_best_srcset(srcset):
    """Из srcset выбирает самую крупную картинку."""
    best = None
    best_weight = -1
    for part in str(srcset).split(","):
        pieces = part.strip().split()
        if not pieces:
            continue
        weight = 0
        if len(pieces) > 1:
            descriptor = pieces[1].lower()
            match = re.match(r"^([\d.]+)(w|x)$", descriptor)
            if match:
                value = float(match.group(1))
                weight = value if match.group(2) == "w" else value * 1000
        if weight >= best_weight:
            best_weight = weight
            best = pieces[0]
    return [best] if best else []


def _entry_get(entry, key, default=None):
    """Читает поле RSS-записи, работает и с dict, и с объектом feedparser."""
    if entry is None:
        return default
    try:
        value = entry.get(key, default)
    except AttributeError:
        value = getattr(entry, key, default)
    return default if value is None else value


def collect_entry_image_urls(entry):
    """Ссылки на картинки из RSS-записи: enclosures, media, og внутри summary."""
    urls = []
    for enc in _entry_get(entry, "enclosures", []) or []:
        if str(_entry_get(enc, "type", "")).startswith("image"):
            href = _entry_get(enc, "href") or _entry_get(enc, "url")
            if href:
                urls.append(href)

    for media in _entry_get(entry, "media_content", []) or []:
        if _entry_get(media, "medium") == "image" or _entry_get(media, "url"):
            media_url = _entry_get(media, "url")
            if media_url:
                urls.append(media_url)

    for thumb in _entry_get(entry, "media_thumbnail", []) or []:
        thumb_url = _entry_get(thumb, "url")
        if thumb_url:
            urls.append(thumb_url)

    content = _entry_get(entry, "summary", "") or ""
    for block in _entry_get(entry, "content", []) or []:
        content += " " + (_entry_get(block, "value", "") or "")
    for meta in _entry_get(entry, "summary_detail", []) or []:
        content += " " + str(_entry_get(meta, "value", "") or "")

    urls.extend(re.findall(
        r"https?://[^\s'\"<>]+/[^<>]*?\.(?:jpg|jpeg|png|webp|gif|avif)(?:\?[^<>]*)?",
        content, re.IGNORECASE
    ))
    return urls


def _collect_jsonld_images(page):
    urls = []

    def walk(node, depth=0):
        if depth > 4:
            return
        if isinstance(node, dict):
            for key in ("image", "thumbnailUrl", "contentUrl"):
                value = node.get(key)
                if isinstance(value, str):
                    urls.append(value)
                elif isinstance(value, dict):
                    url = value.get("url")
                    if isinstance(url, str):
                        urls.append(url)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            urls.append(item)
                        elif isinstance(item, dict) and isinstance(item.get("url"), str):
                            urls.append(item["url"])
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    for raw_json in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page, flags=re.IGNORECASE | re.DOTALL
    ):
        try:
            walk(json.loads(raw_json.strip()))
        except Exception:
            continue
    return urls


def fetch_article_page(article_url):
    """Скачивает HTML статьи один раз и кладёт в кэш (общий на все кандидаты)."""
    if not article_url or not isinstance(article_url, str):
        return None
    article_url = article_url.strip()
    if not article_url.startswith(("http://", "https://")):
        return None
    if article_url in _ARTICLE_PAGE_CACHE:
        return _ARTICLE_PAGE_CACHE[article_url]
    if _DEADLINE and time.time() > _DEADLINE:
        return None
    try:
        response = requests.get(
            article_url,
            headers=BROWSER_HEADERS,
            timeout=ARTICLE_FETCH_TIMEOUT,
            allow_redirects=True,
        )
        if response.status_code != 200:
            _ARTICLE_PAGE_CACHE[article_url] = None
            return None
        response.encoding = response.apparent_encoding or "utf-8"
        page = response.text
        _ARTICLE_PAGE_CACHE[article_url] = page
        return page
    except Exception as e:
        print(f"Ошибка загрузки страницы статьи: {e}")
        _ARTICLE_PAGE_CACHE[article_url] = None
        return None


def collect_article_image_urls(article_url):
    """Ссылки на картинки со страницы статьи, от лучших к худшим."""
    page = fetch_article_page(article_url)
    if not page:
        return []

    urls = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page, "html.parser")

        meta_selectors = [
            'meta[property="og:image:secure_url"]',
            'meta[property="og:image:url"]',
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            'meta[name="twitter:image:src"]',
            'meta[itemprop="image"]',
            'link[rel="image_src"]',
        ]
        for selector in meta_selectors:
            for tag in soup.select(selector):
                value = tag.get("content") or tag.get("value") or tag.get("href")
                if value:
                    urls.append(value)

        img_selectors = [
            "article img", ".article img", ".content img", "main img",
            "picture source", "article source", "figure img", "body img",
        ]
        for selector in img_selectors:
            for tag in soup.select(selector):
                for attribute in ("src", "data-src", "data-original",
                                  "data-lazy-src", "data-hi-res-src", "data-srcset", "srcset"):
                    value = tag.get(attribute)
                    if not value:
                        continue
                    if "srcset" in attribute:
                        urls.extend(_pick_best_srcset(value))
                    else:
                        urls.append(value)
    except ImportError:
        print("BeautifulSoup не установлен, картинки ищем только через og:image и JSON-LD.")
    except Exception as e:
        print(f"Ошибка парсинга HTML статьи: {e}")

    urls.extend(_collect_jsonld_images(page))
    return urls


def resolve_image_urls(entry, article_url):
    """Полный упорядоченный список ссылок на картинки: из RSS и со страницы статьи."""
    seen = set()
    ordered = []
    raw_urls = collect_entry_image_urls(entry)
    raw_urls.extend(collect_article_image_urls(article_url))
    for raw in raw_urls:
        url = normalize_image_url(raw, article_url)
        if not url or not is_valid_image_url(url):
            continue
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        ordered.append(url)
    return ordered


def pick_valid_images(image_urls):
    """Возвращает список реально пригодных картинок (до IMAGE_GOOD_LIMIT).
    Первой будет лучшая — её и отправляем в канал."""
    good = []
    if not image_urls:
        print("Ни в RSS, ни на странице статьи картинка не нашлась.")
        return good

    checked = 0
    for url in image_urls:
        if len(good) >= IMAGE_GOOD_LIMIT:
            break
        if checked >= IMAGE_CANDIDATE_LIMIT:
            print(f"Проверили {checked} кандидатов, хватит.")
            break
        if _DEADLINE and time.time() > _DEADLINE:
            print("Время вышло, прекращаем проверку картинок.")
            break
        checked += 1
        if fetch_and_check_image(url):
            print(f"Картинка подходит: {url}")
            good.append(url)
        else:
            print("Картинка не подходит, пробуем следующую.")

    if not good:
        print("Ни одна картинка не прошла проверку.")
    return good


# ============================================================
# БЕЗОПАСНЫЙ TELEGRAM HTML
# ============================================================

def escape_for_telegram(text):
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def sanitize_telegram_html(text):
    if not text:
        return ""
    allowed_tags = ["b", "/b", "i", "/i", "blockquote", "/blockquote", "a", "/a"]
    def replace_tag(match):
        tag = match.group(1).lower()
        if tag in allowed_tags:
            return match.group(0)
        return ""
    text = re.sub(r"<\s*/?\s*([a-zA-Z0-9]+)(?:\s[^>]*)?>", replace_tag, text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ============================================================
# AI
# ============================================================

last_usage = {}

def extract_json_from_text(text):
    """Извлекает первый валидный JSON-объект из произвольного текста."""
    if not text:
        return None
    # Удаляем блок <think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Убираем Markdown-обёртку ```json ... ```
    text = re.sub(r'```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```', '', text)

    # Ищем первый валидный JSON-объект
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        try:
            obj, end = decoder.raw_decode(text[idx:])
            return obj
        except json.JSONDecodeError:
            idx += 1
    return None

def generate_rewrite(title, summary, article_url):
    global last_usage
    if not GROQ_API_KEY:
        print("ОШИБКА: GROQ_API_KEY не установлен!")
        return None

    clean_title = clean_html(title)
    clean_summary = clean_html(summary)

    if not clean_title and not clean_summary:
        return None

    source_text = clean_summary or clean_title or ""
    if source_text:
        source_text = source_text[:2000]
        print(f"Используется краткое описание RSS: {len(source_text)} символов")

    if not source_text:
        source_text = clean_title
    source_text = source_text[:2000]

    prompt = f"""
Ты — редактор новостного Telegram-канала.
Твоя задача: сделать короткий, ясный пост по фактам из материала ниже.

Правила:
- Пиши только по исходному материалу. Никаких домыслов и новых деталей.
- Сохраняй главный факт новости.
- title: короткий заголовок без повторения исходника.
- info: 1-2 коротких абзаца по сути события.
- comment: короткий комментарий канала, саркастичный, едкий или ироничный, напрямую связан с фактом. Не аналитика и не мнение от первого лица. Без воды и общих фраз.
- Не используй HTML, эмодзи, ссылки, подпись канала.
- Верни только валидный JSON: {{"title": "...", "info": "...", "comment": "..."}}

Заголовок из RSS: {clean_title}
Материал: {source_text}
"""

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": selected_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=40)
        if res.status_code == 429:
            update_settings(interval_minutes=120)
            print("Превышен лимит Groq (429). Интервал увеличен до 120 минут.")
            return None
        if res.status_code != 200:
            print(f"Ошибка Groq API ({res.status_code}): {res.text}")
            return None

        response_json = res.json()
        body = response_json["choices"][0]["message"]["content"]
        last_usage = response_json.get("usage", {})

        print("Ответ модели (первые 500 символов):", body[:500])

        # Извлекаем JSON
        data = extract_json_from_text(body)

        if not data or not isinstance(data, dict):
            # fallback: пробуем извлечь поля через регулярки
            print("Не удалось извлечь JSON, пробуем регулярки...")
            fields = {}
            title_match = re.search(r'["\']title["\']\s*:\s*["\'](.*?)["\']', body)
            info_match = re.search(r'["\']info["\']\s*:\s*["\'](.*?)["\']', body)
            comment_match = re.search(r'["\']comment["\']\s*:\s*["\'](.*?)["\']', body)
            if title_match:
                fields['title'] = title_match.group(1)
            if info_match:
                fields['info'] = info_match.group(1)
            if comment_match:
                fields['comment'] = comment_match.group(1)
            if fields:
                data = fields
            else:
                # совсем ничего нет — используем оригинал
                print("Не удалось извлечь поля, используем исходные title/summary.")
                data = {'title': clean_title, 'info': clean_summary, 'comment': ''}

        ai_title = str(data.get("title", "")).strip()
        ai_info = str(data.get("info", "")).strip()
        ai_comment = str(data.get("comment", "")).strip()

        # Защита от мусорных значений вроде "..."
        if not ai_title or ai_title == "..." or len(ai_title) < 5:
            ai_title = clean_title
        if not ai_info or ai_info == "..." or len(ai_info) < 10:
            ai_info = clean_summary
        if not ai_comment or ai_comment == "..." or len(ai_comment) < 3:
            ai_comment = "Какой день, такой и вечер."

        # Дополнительная очистка HTML
        ai_title = clean_html(ai_title)
        ai_info = clean_html(ai_info)
        ai_comment = clean_html(ai_comment)

        normalized_title = re.sub(r"\s+", " ", ai_title.lower()).strip()
        normalized_info = re.sub(r"\s+", " ", ai_info.lower()).strip()
        if normalized_info == normalized_title:
            ai_info = clean_summary

        safe_title = escape_for_telegram(ai_title)
        safe_info = escape_for_telegram(ai_info)
        safe_comment = escape_for_telegram(ai_comment)

        safe_article_url = html.escape(article_url or "", quote=True)
        safe_channel_url = html.escape(TELEGRAM_CHANNEL_LINK, quote=True)
        safe_channel_name = html.escape(TELEGRAM_CHANNEL_NAME)

        parts = []
        parts.append(f"⚡️ <b>{safe_title}</b>")
        if safe_info:
            parts.append(safe_info)
        if safe_comment and safe_comment != "Какой день, такой и вечер.":
            parts.append(f"💬 <i>{safe_comment}</i>")
        if safe_article_url:
            parts.append(f'👉 <a href="{safe_article_url}">Читать источник</a>')
        parts.append(f'<a href="{safe_channel_url}">{safe_channel_name}</a>')

        return "\n\n".join(parts)

    except Exception as e:
        print(f"Исключение при запросе к Groq: {e}")
        return None

# ============================================================
# TELEGRAM SENDING
# ============================================================

def send_telegram(text, image_urls=None):
    """Отправляет пост строго с картинкой.
    Без картинки и при неудаче со всеми картинками возвращает False —
    вызывающий код берёт другую новость."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ОШИБКА: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не установлен!")
        return False

    if isinstance(image_urls, str):
        image_urls = [image_urls]
    image_urls = [url for url in (image_urls or []) if isinstance(url, str) and url.strip()]
    if not image_urls:
        print("Нет ни одной подходящей картинки, пост не отправляем.")
        return False

    photo_endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    for image_url in image_urls:
        resolved_image_url = image_url.strip()
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": resolved_image_url,
            "parse_mode": "HTML",
        }
        if len(text) <= 1024:
            payload["caption"] = text

        try:
            res = requests.post(photo_endpoint, json=payload, timeout=20)
            if res.status_code == 200:
                return True
            print(f"sendPhoto error: {res.text}")
        except Exception as e:
            print(f"Ошибка sendPhoto: {e}")

        print("Telegram не принял эту картинку, пробуем другую.")

    print("Ни одна картинка не отправилась. Пост пропущен, берём другую новость.")
    return False


def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=20)
        return res.status_code == 200
    except Exception as e:
        print(f"Ошибка отправки сообщения в Telegram: {e}")
        return False

# ============================================================
# LOG TO ADMIN
# ============================================================

def send_log_to_admin(post_title, article_url):
    if not ADMIN_CHAT_ID or not TELEGRAM_BOT_TOKEN:
        return
    try:
        int(ADMIN_CHAT_ID)
    except ValueError:
        print(f"ADMIN_CHAT_ID не число: {ADMIN_CHAT_ID}")
        return

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    total_tokens = last_usage.get("total_tokens", 0)
    prompt_tokens = last_usage.get("prompt_tokens", 0)
    completion_tokens = last_usage.get("completion_tokens", 0)

    safe_title = html.escape(str(post_title)[:100]) if post_title else "Без названия"
    safe_url = html.escape(article_url or "")

    text = f"""📊 <b>Опубликована новость</b>

<b>Заголовок:</b> {safe_title}
<b>Время:</b> {now}

🤖 <b>Токены Groq:</b>
  · Запрос: {prompt_tokens}
  · Ответ: {completion_tokens}
  · Всего: {total_tokens}

🔗 <a href="{safe_url}">Ссылка на источник</a>"""

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=8)
        if res.status_code != 200:
            print(f"Ошибка отправки лога админу: {res.status_code} {res.text}")
        else:
            print("Лог отправлен администратору.")
    except Exception as e:
        print(f"Исключение при отправке лога: {e}")

# ============================================================
# ADMIN COMMANDS
# ============================================================

def get_admin_chat_id():
    try:
        return int(str(ADMIN_CHAT_ID).strip()) if str(ADMIN_CHAT_ID).strip() else None
    except Exception:
        return None


def process_updates():
    global LAST_TELEGRAM_UPDATE_ID
    if not TELEGRAM_BOT_TOKEN:
        return False
    admin_id = get_admin_chat_id()
    if admin_id is None:
        return False

    settings = load_settings()
    last_processed = int(settings.get("last_processed_update_id", 0) or 0)
    LAST_TELEGRAM_UPDATE_ID = last_processed

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(
            url,
            params={"timeout": LONG_POLL_TIMEOUT, "limit": 20, "offset": last_processed + 1},
            timeout=LONG_POLL_TIMEOUT + 15,
        )
        response.raise_for_status()
        updates = response.json().get("result", [])
    except Exception as e:
        print(f"Ошибка получения обновлений Telegram: {e}")
        return False

    if not updates:
        return False

    newest_update_id = last_processed
    processed_any = False

    for update in updates:
        update_id = int(update.get("update_id", 0))
        if update_id <= last_processed:
            continue
        newest_update_id = max(newest_update_id, update_id)
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user = message.get("from") or {}
        user_id = user.get("id")
        text = (message.get("text") or "").strip()
        try:
            chat_id_int = int(chat_id)
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            continue

        if not text or chat_id_int != admin_id or user_id_int != admin_id:
            continue

        command = text.split()[0].lower()
        if command == "/start":
            help_text = """<b>Telegram News Bot</b>\n\nКоманды:\n/start — приветствие и справка\n/post_now — опубликовать новость немедленно\n/set_interval 30 — задать интервал в минутах\n/stats — показать статистику\n/help — список команд"""
            send_telegram_message(chat_id, help_text)
        elif command == "/post_now":
            update_settings(manual_trigger=True, manual_post_done=True)
            send_telegram_message(chat_id, "<b>Ручной запуск активирован.</b>")
        elif command.startswith("/set_interval"):
            args = text.split()
            if len(args) < 2:
                send_telegram_message(chat_id, "Формат: <code>/set_interval 30</code>")
                continue
            try:
                minutes = int(args[1])
                if minutes <= 0:
                    raise ValueError
            except ValueError:
                send_telegram_message(chat_id, "Неверное значение. Используйте целое число минут больше 0.")
                continue
            update_settings(interval_minutes=minutes)
            send_telegram_message(chat_id, f"<b>Интервал установлен:</b> {minutes} минут.")
        elif command == "/stats":
            settings = load_settings()
            stats_text = (
                "<b>Статистика</b>\n\n"
                f"Опубликовано новостей: <b>{settings.get('total_posts', 0)}</b>\n"
                f"Потрачено токенов: <b>{settings.get('total_tokens', 0)}</b>\n"
                f"Текущий интервал: <b>{settings.get('interval_minutes', 60)}</b> минут\n"
                f"Время последнего поста: <b>{settings.get('last_post_time') or 'нет'}</b>"
            )
            send_telegram_message(chat_id, stats_text)
        elif command == "/help":
            help_text = """<b>Команды:</b>\n/start\n/post_now\n/set_interval 30\n/stats\n/help"""
            send_telegram_message(chat_id, help_text)
        else:
            send_telegram_message(chat_id, "Неизвестная команда. Используйте /help.")

        processed_any = True
        update_settings(last_processed_update_id=update_id)

    if newest_update_id > last_processed:
        update_settings(last_processed_update_id=newest_update_id)
        LAST_TELEGRAM_UPDATE_ID = newest_update_id

    return processed_any


# ============================================================
# POSTING LOGIC
# ============================================================

def check_and_post():
    settings = load_settings()
    if settings.get("manual_post_done") and not settings.get("manual_trigger"):
        update_settings(manual_post_done=False)
        return False

    if settings.get("manual_trigger"):
        manual_post_done = bool(settings.get("manual_post_done", False))
        if manual_post_done:
            print("Ручной запуск /post_now активирован.")
            posted = publish_news_once()
            update_settings(manual_trigger=False, manual_post_done=False)
            if not posted:
                print("Ручной запуск не удалось: не нашлось новости с нормальной картинкой.")
            return posted
        update_settings(manual_trigger=False)
        return False

    interval_minutes = max(1, int(settings.get("interval_minutes", 60)))
    last_post_time = settings.get("last_post_time")
    if last_post_time is None:
        should_post = True
    else:
        should_post = (time.time() - float(last_post_time)) >= (interval_minutes * 60)

    if should_post:
        return publish_news_once()
    return False


# ============================================================
# SMART NEWS SELECTION
# ============================================================

def news_score(entry):
    source_priority = {
        "lenta.ru": 5, "ria.ru": 5, "gazeta.ru": 4,
        "rbc.ru": 6, "kommersant.ru": 6, "vedomosti.ru": 5, "forbes.ru": 5,
        "3dnews.ru": 5, "habr.com": 5, "tproger.ru": 4, "ixbt.com": 4,
        "sports.ru": 3, "championat.com": 3
    }
    title = entry.get("title", "")
    score = 0

    # Приоритет источника
    domain = ""
    if entry.get("link"):
        match = re.search(r'https?://(?:www\.)?([^/]+)', entry.get("link"))
        domain = match.group(1) if match else ""
    score += source_priority.get(domain, 1) * 2

    # Длина заголовка
    if 30 <= len(title) <= 80:
        score += 3

    # Наличие картинки
    if extract_image_url(entry):
        score += 2

    # Ключевые слова для привлечения внимания
    hot_words = [
        "взрыв", "катастрофа", "авария", "теракт", "удар", "угроза",
        "гибель", "обрушение", "отравление", "захват", "хакер",
        "секрет", "тайна", "разоблачение", "скандал", "утечка",
        "шок", "шокирует", "запрет", "расследование", "компромат",
        "миллиард", "миллион", "прибыль", "золото", "криптовалюта",
        "биткоин", "выплаты", "богатство", "премия",
        "срочно", "молния", "только что", "прорыв", "сенсация",
        "впервые", "эксклюзив",
        "все говорят", "вирусный", "паника", "ажиотаж", "резонанс",
        "нейросеть", "ИИ", "искусственный интеллект", "робот",
        "блокчейн", "квантовый", "стартап", "Илон Маск", "инновация"
    ]
    title_lower = title.lower()
    for word in hot_words:
        if word in title_lower:
            score += 2

    # ОСОБЫЙ ПРИОРИТЕТ: способы заработка, инвестиции, финансы
    earning_words = [
        "заработок", "доход", "заработать", "заработка",
        "пассивный доход", "инвестиции", "инвестиция", "акции",
        "биржа", "трейдинг", "крипта", "биткоин", "эфир",
        "бизнес", "стартап", "миллион", "миллиард", "прибыль",
        "премия", "выплаты", "бонус", "зарплата", "повышение",
        "финансы", "экономия", "богатство", "бюджет"
    ]
    for word in earning_words:
        if word in title_lower:
            score += 10   # самый высокий вес
            break         # одного слова достаточно

    # Элемент случайности
    score += random.randint(0, 5)
    return score

# ============================================================
# MAIN
# ============================================================

def collect_candidates(published):
    """Собирает свежие новости из всех лент."""
    all_candidates = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Ошибка чтения RSS {feed_url}: {e}")
            continue

        entries = getattr(feed, "entries", []) or []
        if not entries:
            print(f"Лента пустая: {feed_url}")
            continue

        for entry in entries[:FEED_ENTRIES_PER_SOURCE]:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in published:
                continue
            all_candidates.append(entry)
    return all_candidates


def publish_news_once():
    published = load_published()
    all_candidates = collect_candidates(published)

    if not all_candidates:
        print("Новых новостей не найдено.")
        return False

    print(f"Всего кандидатов: {len(all_candidates)}")
    checked_without_image = 0

    for candidate in sorted(all_candidates, key=news_score, reverse=True):
        title = candidate.get("title", "")
        summary = candidate.get("summary") or candidate.get("description") or title
        article_url = candidate.get("link") or candidate.get("id") or ""
        entry_id = candidate.get("id") or candidate.get("link")

        if article_url and article_url in published:
            print("Статья уже есть в published.json, пропускаем дубль.")
            continue
        if entry_id and entry_id in published:
            print("ID новости уже есть в published.json, пропускаем дубль.")
            continue

        # Никаких лимитов на количество перепробованных новостей:
        # если у этой нет картинки — просто берём следующую.
        if _DEADLINE and time.time() > _DEADLINE:
            print("Исчерпан лимит времени на поиск новости с картинкой.")
            return False

        image_candidates = resolve_image_urls(candidate, article_url)
        good_images = pick_valid_images(image_candidates)
        if not good_images:
            checked_without_image += 1
            print(
                f"У новости не нашлось нормальной картинки — сразу берём другую: {title}"
            )
            continue

        print(f"\nОбработка новости: {title}")
        rewritten = generate_rewrite(title, summary, article_url)
        if not rewritten:
            print("Не удалось сгенерировать пост, пробуем следующую новость.")
            continue

        print("\nСформированный пост:")
        print(rewritten)

        if not send_telegram(rewritten, good_images):
            print("Пост с картинкой не ушёл, пробуем следующую новость.")
            continue

        print("Успешно отправлено в Telegram!")

        if entry_id not in published:
            published.append(entry_id)
        if not save_published(published):
            print("Не удалось сохранить published.json после публикации, повторяем попытку через 5 сек...")
            time.sleep(5)
            if not save_published(published):
                print("Опасно: published.json не удалось сохранить после второй попытки. Публикация остановлена, чтобы не создавать дубль.")
                return False

        print("Статистика опубликована успешно")

        bump_settings(
            total_posts=1,
            total_tokens=int(last_usage.get("total_tokens", 0)),
            last_post_time=time.time(),
        )
        update_settings(manual_trigger=False, manual_post_done=False)

        send_log_to_admin(title, article_url)
        return True

    print(
        f"Подходящих новостей с картинкой не найдено "
        f"(проверено без картинки: {checked_without_image})."
    )
    return False


def _command_poller(stop_event):
    """Фоновый поток: только опрашивает команды в ЛС и отвечает на них.

    Раньше команды обрабатывались в том же цикле, что и публикация, поэтому
    ответ ждал окончания поиска новости и картинки — это минуты. Вынесено в
    отдельный поток, чтобы /help или /stats отвечали мгновенно.
    """
    while not stop_event.is_set():
        try:
            process_updates()
        except Exception as e:
            print(f"Сбой при обработке команд: {e}")
        stop_event.wait(INTERVAL_POLL_SECONDS)


def _publisher(stop_event):
    """Фоновый поток: публикует новость, когда подошёл интервал."""
    next_check = time.time()
    while not stop_event.is_set():
        if time.time() < next_check:
            stop_event.wait(min(5, max(0.5, next_check - time.time())))
            continue
        try:
            set_deadline(PUBLISH_BUDGET_SECONDS)
            check_and_post()
        except Exception as e:
            print(f"Сбой при публикации: {e}")
        finally:
            set_deadline(None)
            clear_caches()
        # следующая проверка не раньше, чем через минуту: зачем дёргать
        # RSS-ленты чаще, если интервал постинга измеряется десятками минут
        next_check = time.time() + 60


def run_service_loop():
    """Держит бота живым: отдельный поток отвечает на команды в ЛС сразу,
    отдельный — публикует новости по расписанию.

    Раньше скрипт делал один process_updates() и сразу exit(0) — бот жил
    несколько секунд в час и команды в личку просто не успевал забрать.
    """
    if not load_settings().get("interval_minutes"):
        update_settings(interval_minutes=60)

    stop_event = threading.Event()
    poller = threading.Thread(target=_command_poller, args=(stop_event,), daemon=True)
    publisher = threading.Thread(target=_publisher, args=(stop_event,), daemon=True)
    poller.start()
    publisher.start()

    print(f"Бот работает, выход через {SERVICE_RUN_SECONDS} сек.")
    time.sleep(SERVICE_RUN_SECONDS)

    print("Время работы истекло, сохраняем настройки и выходим.")
    stop_event.set()
    return 0


if __name__ == "__main__":
    if not load_settings().get("interval_minutes"):
        update_settings(interval_minutes=60)

    clear_caches()
    run_service_loop()
    exit(0)
