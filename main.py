# -*- coding: utf-8 -*-
"""
TERA NEWS WATCHER – SILVER PRO EDITION (STRICT FILTER)
1. SADECE GÜMÜŞ (Silver) analiz ve yorumları.
2. KATI DOMAIN FİLTRESİ: Yozgat Hakimiyet vb. yerel siteler engellendi.
   Sadece Bloomberg, Investing, Foreks gibi majör finans sitelerine izin var.
3. Yabancı banka raporlarının Türkçe yansımalarını yakalar.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from typing import NamedTuple, Optional

import requests
import feedparser
from flask import Flask, jsonify, request

# ======================================================
# ENV & AYARLAR
# ======================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CRON_TOKEN         = os.getenv("CRON_TOKEN", "").strip()
TZ_OFFSET          = int(os.getenv("TZ_OFFSET_HOURS", "3"))

SEEN_FILE = "seen_ids.txt"
LAST_NO_NEWS_FILE = "last_no_news_tag.txt"

# Google Bot Koruması İçin Header
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
})

# ======================================================
# KATI GÜVENİLİR SİTE LİSTESİ (BEYAZ LİSTE)
# ======================================================
# Sadece bu uzantılarla biten sitelerden gelen haberler kabul edilir.
TRUSTED_DOMAINS = {
    # Finans Devleri
    "bloomberght.com",
    "investing.com",
    "foreks.com",
    "dunya.com",       # Dünya Gazetesi (Ekonomi için çok önemli)
    "ekonomim.com",    # Ekonomi Gazetesi
    "borsagundem.com",
    "doviz.com",
    "paratic.com",
    "bigpara.hurriyet.com.tr", # Hürriyet Bigpara
    "uzmanpara.milliyet.com.tr", # Milliyet Uzmanpara
    
    # Güvenilir Ulusal Haber Kanalları (Ekonomi Sayfaları)
    "ntv.com.tr",
    "cnnturk.com",
    "haberturk.com",
    "sozcu.com.tr",
    "finans.mynet.com", # Mynet Finans
    "paraajansi.com.tr"
}

# ======================================================
# DATA YAPISI
# ======================================================
class NewsItem(NamedTuple):
    published_dt: datetime
    feed_name: str
    entry: dict
    item_id: str

# ======================================================
# TELEGRAM
# ======================================================
def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        SESSION.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=15,
        )
    except Exception:
        pass

# ======================================================
# DOSYA YÖNETİMİ
# ======================================================
def load_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except Exception:
        return set()

def save_seen(seen: set) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            for _id in list(seen)[-50000:]:
                f.write(_id + "\n")
    except Exception:
        pass

def load_last_no_news_tag() -> Optional[str]:
    if not os.path.exists(LAST_NO_NEWS_FILE):
        return None
    try:
        with open(LAST_NO_NEWS_FILE, "r", encoding="utf-8") as f:
            tag = f.read().strip()
            return tag or None
    except Exception:
        return None

def save_last_no_news_tag(tag: str) -> None:
    try:
        with open(LAST_NO_NEWS_FILE, "w", encoding="utf-8") as f:
            f.write(tag)
    except Exception:
        pass

# ======================================================
# HABER YOK BİLDİRİMİ
# ======================================================
def maybe_send_no_news(now_local: datetime) -> None:
    # Hafta sonu kapalı
    if now_local.weekday() > 4: return
    # Gece 23:00'e kadar takip (ABD piyasaları açık)
    if not (8 <= now_local.hour <= 23): return

    tag = now_local.strftime("%Y-%m-%d %H")
    last_tag = load_last_no_news_tag()

    if last_tag == tag:
        return

    msg = f"⚪ Bugün ({now_local.date()}) Seçkin kaynaklarda yeni Gümüş haberi yok."
    send_telegram(msg)
    save_last_no_news_tag(tag)

# ======================================================
# TARİH AYRIŞTIRMA (Son 36 Saat)
# ======================================================
def parse_date(entry) -> Optional[datetime]:
    if getattr(entry, "published_parsed", None):
        try:
            return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
        except: pass
    if getattr(entry, "updated_parsed", None):
        try:
            return datetime.fromtimestamp(time.mktime(entry.updated_parsed), tz=timezone.utc)
        except: pass
    for field in ["published", "updated", "pubDate"]:
        if field in entry:
            try:
                fake = feedparser.parse(entry[field])
                if fake.entries and getattr(fake.entries[0], "published_parsed", None):
                    return datetime.fromtimestamp(time.mktime(fake.entries[0].published_parsed), tz=timezone.utc)
            except: pass
    return None

def is_recent(dt: datetime) -> bool:
    if not dt: return False
    now_utc = datetime.now(timezone.utc)
    diff = now_utc - dt
    if diff.days < -1: return False
    return diff <= timedelta(hours=36)

# ======================================================
# DOMAIN FİLTRESİ (GÜVENLİK DUVARI)
# ======================================================
def domain_ok(link: str) -> bool:
    """
    Haberin geldiği site, bizim güvenilir listemizde (TRUSTED_DOMAINS) var mı?
    """
    try:
        # Google News yönlendirmesi varsa bazen domain news.google.com görünür.
        # Bu durumda Google'a izin verip içeriğin başlığına güveniriz, 
        # VEYA Google'ın yönlendirdiği asıl domaini çözmeye çalışırız.
        # Basitlik için: Link string'i içinde güvenilir domain geçiyor mu diye bakarız.
        
        link_lower = link.lower()
        return any(d in link_lower for d in TRUSTED_DOMAINS)
    except:
        return False

# ======================================================
# FEED LİSTESİ (SADECE GÜMÜŞ)
# ======================================================
FEEDS = [
    # Yabancı banka tahminleri ve teknik analizler
    ("Gümüş (Analiz & Tahmin)", "https://news.google.com/rss/search?q=Gümüş+fiyatı+tahminleri+yabancı+banka+analiz&hl=tr&gl=TR&ceid=TR:tr"),
    
    # Ons Gümüş Teknik (XAG/USD)
    ("Gümüş (Ons Teknik)", "https://news.google.com/rss/search?q=Gümüş+ons+teknik+analiz+uzman+yorum&hl=tr&gl=TR&ceid=TR:tr"),
    
    # Piyasalar Genel
    ("Gümüş (Piyasa)", "https://news.google.com/rss/search?q=Gümüş+piyasası+son+dakika+Bloomberg+Investing&hl=tr&gl=TR&ceid=TR:tr"),
]

# ======================================================
# FEED ÇEKİCİ
# ======================================================
def fetch_feed(name: str, url: str) -> list[NewsItem]:
    try:
        r = SESSION.get(url, timeout=20)
        feed = feedparser.parse(r.text)
        out = []

        for entry in feed.entries:
            dt = parse_date(entry)
            if not dt: continue
            
            # 1. Tarih Kontrolü
            if not is_recent(dt):
                continue

            # 2. Kalite Kontrolü (Domain Filtresi)
            link = entry.get("link", "") or entry.get("id", "")
            source = entry.get("source", {}).get("title", "").lower() # RSS kaynağının adı
            
            # Linkin içinde veya Kaynak adında güvenilir sitelerden biri geçiyor mu?
            # Örn: Linkte "bloomberght.com" var mı? Veya kaynak adı "Bloomberg HT" mi?
            
            is_trusted_link = any(d in link.lower() for d in TRUSTED_DOMAINS)
            
            # Google News bazen kaynak adını temiz verir, onu da kontrol edelim
            # Örn: 'Milliyet', 'Dünya Gazetesi'
            # Bunu domain listesiyle eşleştirmek zor olabilir, link kontrolü en sağlamıdır.
            
            if not is_trusted_link:
                # Güvenilir listede değilse (Örn: Yozgat Hakimiyet), bu haberi atla.
                continue
            
            _id = entry.get("id") or entry.get("link") or entry.get("title", "")
            out.append(NewsItem(dt, name, entry, _id))

        return out
    except Exception:
        return []

# ======================================================
# JOB
# ======================================================
def job() -> int:
    try:
        seen = load_seen()
        new_items = []

        for name, url in FEEDS:
            items = fetch_feed(name, url)
            for it in items:
                if it.item_id not in seen:
                    new_items.append(it)
                    seen.add(it.item_id)
        
        save_seen(seen)
        new_items.sort(key=lambda x: x.published_dt)
        
        for it in new_items:
            title = it.entry.get('title', 'Başlık Yok')
            link = it.entry.get('link', '#')
            msg = f"⚪ <b>{it.feed_name}</b>\n{title}\n{link}"
            send_telegram(msg)
        
        now_local = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET)
        if not new_items:
            maybe_send_no_news(now_local)
            
        return len(new_items)
    except Exception:
        return 0

# ======================================================
# FLASK
# ======================================================
app = Flask(__name__)

@app.get("/")
def home():
    return "Alive", 200

@app.get("/health")
def health():
    return "ok", 200

@app.get("/cron")
def cron():
    t = request.args.get("token", "")
    if CRON_TOKEN and t != CRON_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    count = job()
    return jsonify({"ok": True, "new_items": count}), 200

@app.get("/test")
def test():
    send_telegram("🧪 Gümüş Bot Test (Filtreli).")
    return "ok", 200
