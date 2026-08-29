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
from urllib.parse import quote
import requests
import feedparser

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
LAST_TELEGRAM_UPDATE_ID = 0
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")
PUBLISHED_LOCK = threading.Lock()

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
    if not os.path.exists(SETTINGS_PATH):
        save_settings(default_settings)
        return default_settings.copy()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        save_settings(default_settings)
        return default_settings.copy()
    for key, value in default_settings.items():
        if key not in data:
            data[key] = value
    return data


def save_settings(settings):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


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

def load_published():
    with PUBLISHED_LOCK:
        if os.path.exists("published.json"):
            try:
                with open("published.json", "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Ошибка чтения published.json: {e}")
                return []
        return []


def save_published(published_list):
    for attempt in range(3):
        try:
            with PUBLISHED_LOCK:
                with open("published.json", "w", encoding="utf-8") as f:
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

def extract_image_url(entry):
    if "enclosures" in entry:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image"):
                return enc.get("href")
    if "media_content" in entry:
        for media in entry.media_content:
            if media.get("medium") == "image" or "url" in media:
                return media.get("url")
    if "media_thumbnail" in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0].get("url")
    content = entry.get("summary", "")
    if "content" in entry and len(entry.content) > 0:
        content += " " + entry.content[0].get("value", "")
    match = re.search(
        r"https?://[^\s'\"]+\.(?:jpg|jpeg|png|webp)",
        content, re.IGNORECASE
    )
    if match:
        return match.group(0)
    return None


def get_fallback_image_url(title="", source_url=""):
    query = clean_html(title) or "news"
    query = re.sub(r"\s+", " ", query).strip()
    query = re.sub(r"[^A-Za-zА-Яа-я0-9\s-]", " ", query)
    query = query.strip() or "news"

    search_urls = [
        f"https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrsearch={quote(query)}&gsrnamespace=6&gsrlimit=1&prop=imageinfo&iiprop=url&format=json",
        f"https://api.unsplash.com/search/photos?query={quote(query)}&per_page=1"
    ]

    for url in search_urls:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if "unsplash.com" in url:
                headers["Accept-Version"] = "v1"
            response = requests.get(url, headers=headers, timeout=12)
            if response.status_code != 200:
                continue
            payload = response.json()
            if "query" in url and "pages" in payload:
                pages = payload.get("pages", {})
                for page in pages.values():
                    images = page.get("imageinfo", [])
                    if images:
                        return images[0].get("url")
            if "unsplash.com" in url and "results" in payload:
                results = payload.get("results", [])
                if results:
                    return results[0].get("urls", {}).get("regular") or results[0].get("urls", {}).get("small")
        except Exception:
            continue

    return "https://placehold.co/1200x630/png?text=News"

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
            settings = load_settings()
            settings["interval_minutes"] = 120
            save_settings(settings)
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

def send_telegram(text, image_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ОШИБКА: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не установлен!")
        return False

    if image_url:
        resolved_image_url = image_url
    else:
        resolved_image_url = get_fallback_image_url(text)
    if not resolved_image_url:
        resolved_image_url = "https://placehold.co/1200x630/png?text=News"

    send_message_payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    photo_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": resolved_image_url,
        "parse_mode": "HTML",
    }
    if len(text) <= 1024:
        payload["caption"] = text

    try:
        res = requests.post(photo_url, json=payload, timeout=20)
        if res.status_code == 200:
            return True
        print(f"sendPhoto error: {res.text}")
    except Exception as e:
        print(f"Ошибка sendPhoto: {e}")

    message_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = requests.post(message_url, json=send_message_payload, timeout=20)
        if res.status_code == 200:
            print("Фото сломалось, отправка текста выполнена как fallback.")
            return True
        print(f"sendMessage fallback error: {res.text}")
    except Exception as e:
        print(f"Ошибка sendMessage fallback: {e}")

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
            params={"timeout": 0, "limit": 20, "offset": last_processed + 1},
            timeout=20,
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
            settings = load_settings()
            settings["manual_trigger"] = True
            settings["manual_post_done"] = True
            settings["last_processed_update_id"] = update_id
            save_settings(settings)
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
            settings = load_settings()
            settings["interval_minutes"] = minutes
            settings["last_processed_update_id"] = update_id
            save_settings(settings)
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
        settings = load_settings()
        settings["last_processed_update_id"] = update_id
        save_settings(settings)

    if newest_update_id > last_processed:
        settings = load_settings()
        settings["last_processed_update_id"] = newest_update_id
        save_settings(settings)
        LAST_TELEGRAM_UPDATE_ID = newest_update_id

    return processed_any


# ============================================================
# POSTING LOGIC
# ============================================================

def check_and_post():
    settings = load_settings()
    if settings.get("manual_post_done") and not settings.get("manual_trigger"):
        settings["manual_post_done"] = False
        save_settings(settings)
        return False

    if settings.get("manual_trigger"):
        manual_post_done = bool(settings.get("manual_post_done", False))
        if manual_post_done:
            print("Ручной запуск /post_now активирован.")
            publish_news_once()
            settings = load_settings()
            settings["manual_trigger"] = False
            settings["manual_post_done"] = False
            save_settings(settings)
            return True
        settings["manual_trigger"] = False
        save_settings(settings)
        return False

    interval_minutes = max(1, int(settings.get("interval_minutes", 60)))
    last_post_time = settings.get("last_post_time")
    if last_post_time is None:
        should_post = True
    else:
        should_post = (time.time() - float(last_post_time)) >= (interval_minutes * 60)

    if should_post:
        publish_news_once()
        return True
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

def publish_news_once():
    published = load_published()
    all_candidates = []

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Ошибка чтения RSS {feed_url}: {e}")
            continue

        for entry in feed.entries[:5]:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id:
                continue
            if entry_id in published:
                continue
            all_candidates.append(entry)

    if not all_candidates:
        print("Новых новостей не найдено.")
        return False

    best_entry = max(all_candidates, key=news_score)
    title = best_entry.get("title", "")
    summary = best_entry.get("summary") or best_entry.get("description") or title
    article_url = best_entry.get("link") or best_entry.get("id") or ""
    entry_id = best_entry.get("id") or best_entry.get("link")

    if article_url and article_url in published:
        print("Статья уже есть в published.json, пропускаем дубль.")
        return False
    if entry_id and entry_id in published:
        print("ID новости уже есть в published.json, пропускаем дубль.")
        return False

    image_url = extract_image_url(best_entry) or get_fallback_image_url(title, article_url)
    if not image_url:
        image_url = "https://placehold.co/1200x630/png?text=News"

    print(f"\nОбработка новости: {title}")
    rewritten = generate_rewrite(title, summary, article_url)
    if not rewritten:
        print("Не удалось сгенерировать пост.")
        return False

    print("\nСформированный пост:")
    print(rewritten)

    if not send_telegram(rewritten, image_url):
        print("Не удалось отправить пост.")
        return False

    print("Успешно отправлено в Telegram!")

    if entry_id not in published:
        published.append(entry_id)
    if not save_published(published):
        print("Не удалось сохранить published.json после публикации, повторяем попытку через 5 сек...")
        time.sleep(5)
        if not save_published(published):
            print("Опасно: published.json не удалось сохранить после второй попытки. Публикация остановлена, чтобы не создавать дубль.")
            return False

    settings = load_settings()
    settings["last_post_time"] = time.time()
    settings["total_posts"] = int(settings.get("total_posts", 0)) + 1
    settings["total_tokens"] = int(settings.get("total_tokens", 0)) + int(last_usage.get("total_tokens", 0))
    settings["manual_trigger"] = False
    settings["manual_post_done"] = False
    save_settings(settings)

    send_log_to_admin(title, article_url)
    return True


if __name__ == "__main__":
    settings = load_settings()
    if not settings.get("interval_minutes"):
        settings["interval_minutes"] = 60
        save_settings(settings)

    process_updates()
    check_and_post()
    exit(0)
