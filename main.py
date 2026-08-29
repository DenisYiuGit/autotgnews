from dotenv import load_dotenv
load_dotenv()

import os
import re
import json
import html
import datetime
import random
import requests
import feedparser

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

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
    if os.path.exists("published.json"):
        try:
            with open("published.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка чтения published.json: {e}")
            return []
    return []

def save_published(published_list):
    with open("published.json", "w", encoding="utf-8") as f:
        json.dump(published_list, f, ensure_ascii=False, indent=2)

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

    article_text = extract_article_text(article_url)
    if article_text:
        source_text = article_text
        print(f"Полный текст статьи получен: {len(article_text)} символов")
    else:
        source_text = clean_summary
        print("Полный текст не получен. Используется RSS summary.")

    if not source_text:
        source_text = clean_title
    source_text = source_text[:18000]

    prompt = f"""
Ты — редактор новостного Telegram-канала «Наша Раша».

Тебе нужно подготовить короткий новостной пост ИСКЛЮЧИТЕЛЬНО на основании предоставленного материала.

ГЛАВНЫЙ ПРИНЦИП:
никаких выдумок, предположений, оценок и информации, которой нет в исходном материале.

ЗАГОЛОВОК:
Сделай короткий заголовок, передающий главный факт.
Не добавляй новые сведения.
Заголовок должен отличаться от исходного, но смысл должен остаться тем же.

ИНФОРМАЦИЯ:
Напиши 1–2 коротких абзаца.
Здесь нужна КОНКРЕТИКА из новости:
кто, где, когда, что произошло, чем закончилось — но только если это есть в исходном материале.

КАТЕГОРИЧЕСКИ НЕ НАДО:
- повторять заголовок;
- пересказывать всю статью;
- добавлять очевидные или придуманные выводы;
- писать общие фразы ради объёма.

КОММЕНТАРИЙ:
Напиши короткую авторскую реплику для Telegram-канала.
Она должна быть непосредственно связана с фактами новости.
Это НЕ новый факт и НЕ аналитический вывод, требующий дополнительной информации.

Комментарий может быть эмоциональным или разговорным, но не должен выдумывать обстоятельства.

Примеры допустимого направления:
«Вот так и заканчивается попытка разобраться самому.»
«Опасный предмет лучше вообще не трогать.»
«Трагичный исход обычной, на первый взгляд, ситуации.»

Если исходный материал слишком короткий и для комментария не хватает оснований — сделай максимально нейтральную реплику, связанную с описанным событием.

ВАЖНО:
Не используй HTML.
Не используй эмодзи.
Не добавляй ссылки.
Не добавляй подпись канала.
Верни ТОЛЬКО валидный JSON без каких-либо пояснений, размышлений, тегов `<think>` или Markdown.

Вот формат:
{{"title": "заголовок", "info": "информация", "comment": "комментарий"}}

ЗАГОЛОВОК ИЗ RSS:
{clean_title}

МАТЕРИАЛ НОВОСТИ:
{source_text}
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
                data = {'title': clean_title, 'info': clean_summary, 'comment': 'Вот такая история.'}

        ai_title = str(data.get("title", "")).strip()
        ai_info = str(data.get("info", "")).strip()
        ai_comment = str(data.get("comment", "")).strip()

        # Защита от мусорных значений вроде "..."
        if not ai_title or ai_title == "..." or len(ai_title) < 5:
            ai_title = clean_title
        if not ai_info or ai_info == "..." or len(ai_info) < 10:
            ai_info = clean_summary
        if not ai_comment or ai_comment == "..." or len(ai_comment) < 3:
            ai_comment = "Вот такая история."

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

        parts = []
        parts.append(f"⚡️ <b>{safe_title}</b>")
        if safe_info:
            parts.append(safe_info)
        if safe_comment:
            parts.append(f"💬 <i>{safe_comment}</i>")
        if safe_article_url:
            parts.append(f'👉 <a href="{safe_article_url}">Читать источник</a>')
        parts.append(f'<a href="{safe_channel_url}">⚡️ Наша Раша | @nasharusa</a>')

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
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        if len(text) <= 1024:
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": image_url,
                "caption": text,
                "parse_mode": "HTML"
            }
            try:
                res = requests.post(url, json=payload, timeout=20)
                if res.status_code == 200:
                    return True
                print(f"sendPhoto error: {res.text}")
            except Exception as e:
                print(f"Ошибка sendPhoto: {e}")
        else:
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": image_url
            }
            try:
                res = requests.post(url, json=payload, timeout=20)
                if res.status_code == 200:
                    return send_telegram(text, image_url=None)
                print(f"sendPhoto error: {res.text}")
            except Exception as e:
                print(f"Ошибка sendPhoto: {e}")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        res = requests.post(url, json=payload, timeout=20)
        if res.status_code != 200:
            print(f"sendMessage error: {res.text}")
        return res.status_code == 200
    except Exception as e:
        print(f"Ошибка sendMessage: {e}")
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

if __name__ == "__main__":
    published = load_published()
    all_candidates = []

    # Собираем все новые новости со всех лент
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Ошибка чтения RSS {feed_url}: {e}")
            continue

        for entry in feed.entries[:5]:
            entry_id = entry.get("id") or entry.get("link")
            if entry_id in published:
                continue
            all_candidates.append(entry)

    if not all_candidates:
        print("Новых новостей не найдено.")
        exit(0)

    # Выбираем самую «интересную» по очкам
    best_entry = max(all_candidates, key=news_score)

    # Обрабатываем и публикуем
    title = best_entry.get("title", "")
    summary = best_entry.get("summary") or best_entry.get("description") or title
    article_url = best_entry.get("link") or best_entry.get("id") or ""
    image_url = extract_image_url(best_entry)
    entry_id = best_entry.get("id") or best_entry.get("link")

    print(f"\nОбработка новости: {title}")

    rewritten = generate_rewrite(title, summary, article_url)
    if rewritten:
        print("\nСформированный пост:")
        print(rewritten)

        if send_telegram(rewritten, image_url):
            print("Успешно отправлено в Telegram!")
            published.append(entry_id)
            save_published(published)
            send_log_to_admin(title, article_url)
            exit(0)
        else:
            print("Не удалось отправить пост.")
    else:
        print("Не удалось сгенерировать пост.")
