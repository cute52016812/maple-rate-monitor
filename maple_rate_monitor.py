#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楓之谷經典服 (8591) 楓幣幣值監控 — 網頁版
--------------------------------------
每次執行會：
1. 抓取指定的 8591 商品列表頁
2. 從每個商品標題中解析「X元=Y萬楓幣」的比例
3. 計算目前最佳幣值 / 平均幣值 / 最低單價
4. 把結果寫進 CSV 歷史紀錄 (docs/rate_history.csv)
5. 產生一個網頁 (docs/index.html)，顯示目前幣值、排行榜、歷史走勢圖
6. 如果幣值變化超過門檻，可選擇串 Discord webhook 通知

這個版本設計給 GitHub Actions 排程執行 + GitHub Pages host 使用，
所以輸出檔案都放在 docs/ 資料夾（GitHub Pages 可以直接指定用這個資料夾當網站根目錄）。

本機測試：
    pip install requests beautifulsoup4
    python maple_rate_monitor.py
"""

import re
import csv
import os
import html as html_lib
from datetime import datetime, timezone, timedelta

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== 設定區 ==========
TARGET_URL = "https://www.8591.com.tw/v3/mall/list/70657?searchGame=70657&searchServer=70855&searchType=0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "docs")
CSV_PATH = os.path.join(OUTPUT_DIR, "rate_history.csv")
HTML_PATH = os.path.join(OUTPUT_DIR, "index.html")
BROADCAST_JSON_PATH = os.path.join(OUTPUT_DIR, "broadcast_data.json")
BROADCAST_DISPLAY_COUNT = 15   # 網頁上顯示最近幾筆遊戲內廣播

COMPLETED_URL = TARGET_URL + "&completed=1"
COMPLETED_JSON_PATH = os.path.join(OUTPUT_DIR, "completed_trades.json")
COMPLETED_DISPLAY_COUNT = 15    # 網頁上各分類顯示最近幾筆成交紀錄
COMPLETED_MAX_STORED = 500      # 歷史紀錄最多保留幾筆（去重後）

SCROLL_KEYWORDS = ("%", "卷軸", "卷", "敏", "攻擊", "力量", "智力", "幸運", "防禦", "速度", "跳躍")

ALERT_THRESHOLD_PCT = 2.0      # 幣值變化超過這個百分比才提醒
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1540836845359857764/9IwWtfjvAMetwPcbDqwoo2nSdw3m0l5uLJH8qjdMxaJks3JAhWaXbD8ky0ANDIT1-CoV"       # 例如 "https://discord.com/api/webhooks/xxxx/yyyy"，留空則不通知
TOP_N_DISPLAY = 8              # 排行榜顯示筆數

TW_TZ = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

TITLE_RATE_PATTERN = re.compile(r"(\d+)\s*元\s*=\s*(\d+)\s*萬楓幣")


def fetch_html(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.exceptions.SSLError:
        print("[提示] SSL憑證驗證失敗，改用不驗證憑證的方式重試...")
        resp = requests.get(url, headers=HEADERS, timeout=20, verify=False)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def parse_listings(page_html: str):
    soup = BeautifulSoup(page_html, "html.parser")
    listings = []
    links = soup.find_all("a", href=re.compile(r"/v3/mall/detail/\d+"))
    seen_titles = set()

    for a in links:
        title = a.get_text(strip=True) or a.get("title", "")
        if not title or title in seen_titles:
            continue
        m = TITLE_RATE_PATTERN.search(title)
        if not m:
            continue
        price = int(m.group(1))
        wan = int(m.group(2))
        if price <= 0:
            continue
        coins = wan * 10000
        rate = coins / price
        href = a.get("href", "")
        if href.startswith("/"):
            href = "https://www.8591.com.tw" + href
        listings.append({"title": title, "price": price, "coins": coins, "rate": rate, "url": href})
        seen_titles.add(title)

    return listings


COMPLETED_PRICE_LINE_RE = re.compile(r"^([\d,]+)元$")
COMPLETED_TIME_RE = re.compile(r"(\d{2}-\d{2}\s+\d{2}:\d{2})交易完成")
COMPLETED_RATIO_PATTERNS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*萬楓幣\s*=\s*(\d+(?:\.\d+)?)\s*元"), "coins_first"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*元\s*=\s*(\d+(?:\.\d+)?)\s*萬楓幣"), "price_first"),
]


def classify_completed_item(category: str, title: str) -> str:
    if category == "楓幣":
        return "遊戲幣"
    if category == "道具":
        if any(k in title for k in SCROLL_KEYWORDS):
            return "卷軸"
        return "其他道具"
    return "其他"


def extract_completed_rate(title: str):
    for pattern, order in COMPLETED_RATIO_PATTERNS:
        m = pattern.search(title)
        if m:
            if order == "coins_first":
                coins, price = float(m.group(1)) * 10000, float(m.group(2))
            else:
                price, coins = float(m.group(1)), float(m.group(2)) * 10000
            if price > 0:
                return coins / price
    return None


def parse_completed_listings(page_html: str):
    """解析『已完成商品』頁面，回傳分類好的成交紀錄列表。
    這頁是JS動態載入的資料改用?completed=1才拿得到，且沒有穩定的DOM結構可倚賴，
    所以用『攤平成一行行文字，逐行掃描』的方式解析，比較不受HTML標籤細節影響。"""
    soup = BeautifulSoup(page_html, "html.parser")
    text = soup.get_text("\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    results = []
    pending_title = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("新楓之谷：經典版") and "/" in line:
            parts = [p.strip() for p in line.split("/")]
            server = parts[1] if len(parts) > 1 else ""
            category = parts[2] if len(parts) > 2 else ""
            title = pending_title

            j = i + 1
            completed_time = None
            price = None
            limit = min(len(lines), i + 8)
            while j < limit:
                m_t = COMPLETED_TIME_RE.search(lines[j])
                if m_t:
                    completed_time = m_t.group(1)
                m_p = COMPLETED_PRICE_LINE_RE.match(lines[j])
                if m_p and price is None:
                    price = m_p.group(1).replace(",", "")
                j += 1
                if completed_time and price:
                    break

            if title and completed_time and price:
                bucket = classify_completed_item(category, title)
                rate = extract_completed_rate(title) if bucket == "遊戲幣" else None
                results.append({
                    "title": title,
                    "server": server,
                    "category": category,
                    "bucket": bucket,
                    "completed_time": completed_time,
                    "price": price,
                    "rate": rate,
                })
            i = j
            continue
        else:
            pending_title = line
            i += 1

    return results


def load_completed_history():
    if not os.path.exists(COMPLETED_JSON_PATH):
        return []
    try:
        import json
        with open(COMPLETED_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_completed_history(entries):
    import json
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(COMPLETED_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(entries[-COMPLETED_MAX_STORED:], f, ensure_ascii=False, indent=2)


def merge_completed_entries(existing, new_items):
    """用 (完成時間, 標題, 價格) 當作去重鍵，避免重複抓到的同一筆成交被重複記錄"""
    seen = {(e.get("completed_time"), e.get("title"), e.get("price")) for e in existing}
    merged = list(existing)
    for item in new_items:
        key = (item.get("completed_time"), item.get("title"), item.get("price"))
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def summarize(listings):
    if not listings:
        return None
    rates = [x["rate"] for x in listings]
    best = max(listings, key=lambda x: x["rate"])
    return {
        "count": len(listings),
        "best_rate": best["rate"],
        "best_title": best["title"],
        "avg_rate": sum(rates) / len(rates),
        "min_rate": min(rates),
    }


def load_history():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def append_record(summary):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "count", "best_rate", "avg_rate", "min_rate"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "count": summary["count"],
            "best_rate": round(summary["best_rate"], 2),
            "avg_rate": round(summary["avg_rate"], 2),
            "min_rate": round(summary["min_rate"], 2),
        })


def send_discord_alert(message: str):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"[警告] Discord 通知傳送失敗: {e}")


def load_broadcast_data():
    """讀取本地端腳本推送過來的遊戲內廣播資料，檔案不存在就回傳空list"""
    if not os.path.exists(BROADCAST_JSON_PATH):
        return []
    try:
        import json
        with open(BROADCAST_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def esc(s: str) -> str:
    return html_lib.escape(s, quote=True)


CANDLES_ON_CHART = 72  # K線圖最多顯示幾根蠟燭（每根=一次抓取快照）


def build_candles(history_rows):
    """把每小時快照轉成K線資料：
    開盤=上一根收盤, 收盤=這次平均幣值, 最高=這次最佳幣值, 最低=這次最低幣值"""
    candles = []
    prev_close = None
    for row in history_rows:
        try:
            high = float(row["best_rate"])
            low = float(row["min_rate"])
            close = float(row["avg_rate"])
        except (KeyError, ValueError):
            continue
        open_ = prev_close if prev_close is not None else close
        candles.append({"t": row["timestamp"], "open": open_, "high": high, "low": low, "close": close})
        prev_close = close
    return candles


def render_candlestick_svg(candles, width=900, height=320):
    candles = candles[-CANDLES_ON_CHART:]
    if len(candles) < 2:
        return '<p style="color:#9a9ab0;">歷史資料還太少，累積幾個小時後K線圖就會出現。</p>'

    values = [c["high"] for c in candles] + [c["low"] for c in candles]
    vmin, vmax = min(values), max(values)
    pad = (vmax - vmin) * 0.1 or max(vmax * 0.02, 1)
    vmin -= pad
    vmax += pad

    n = len(candles)
    margin_left, margin_right, margin_top, margin_bottom = 55, 10, 10, 26
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    candle_w = plot_w / n
    body_w = max(candle_w * 0.6, 1.5)

    def y(v):
        return margin_top + (vmax - v) / (vmax - vmin) * plot_h

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;">']

    for gy_val in (vmax, (vmax + vmin) / 2, vmin):
        gy = y(gy_val)
        parts.append(f'<line x1="{margin_left}" y1="{gy:.1f}" x2="{width - margin_right}" y2="{gy:.1f}" stroke="#2a2f4a" stroke-width="1" />')
        parts.append(f'<text x="4" y="{gy + 4:.1f}" font-size="11" fill="#9a9ab0">{gy_val:.0f}</text>')

    for i, c in enumerate(candles):
        cx = margin_left + i * candle_w + candle_w / 2
        rising = c["close"] >= c["open"]
        color = "#4ade80" if rising else "#f87171"
        parts.append(f'<line x1="{cx:.1f}" y1="{y(c["high"]):.1f}" x2="{cx:.1f}" y2="{y(c["low"]):.1f}" stroke="{color}" stroke-width="1.4" />')
        top = y(max(c["open"], c["close"]))
        bot = y(min(c["open"], c["close"]))
        h = max(bot - top, 1.5)
        parts.append(f'<rect x="{cx - body_w/2:.1f}" y="{top:.1f}" width="{body_w:.1f}" height="{h:.1f}" fill="{color}" />')

    label_every = max(1, n // 6)
    for i, c in enumerate(candles):
        if i % label_every == 0 or i == n - 1:
            cx = margin_left + i * candle_w + candle_w / 2
            short = c["t"][5:16]
            parts.append(f'<text x="{cx:.1f}" y="{height - 8}" font-size="9" fill="#9a9ab0" text-anchor="middle">{esc(short)}</text>')

    parts.append("</svg>")
    return "".join(parts)


def render_html(summary, listings, history_rows, updated_at: str, broadcast_entries=None, completed_entries=None):
    broadcast_entries = broadcast_entries or []
    completed_entries = completed_entries or []
    top = sorted(listings, key=lambda x: x["rate"], reverse=True)[:TOP_N_DISPLAY]

    rows_html = "\n".join(
        f'<tr><td>{i+1}</td><td>1元 = {item["rate"]:.0f} 楓幣</td>'
        f'<td>{esc(item["title"])[:60]}</td>'
        f'<td><a href="{esc(item["url"])}" target="_blank" rel="noopener">查看</a></td></tr>'
        for i, item in enumerate(top)
    )

    candles = build_candles(history_rows)
    candle_svg = render_candlestick_svg(candles)

    def fmt_rate(b):
        r = b.get("rate")
        return f"1元 = {r:.0f} 楓幣" if r is not None else "（未抓到比例）"

    recent_broadcasts = list(reversed(broadcast_entries[-BROADCAST_DISPLAY_COUNT:]))
    if recent_broadcasts:
        broadcast_rows_html = "\n".join(
            f'<tr><td>{esc(b.get("timestamp",""))}</td>'
            f'<td>{"收購" if b.get("type")=="buy" else ("販售" if b.get("type")=="sell" else "未知")}</td>'
            f'<td>{fmt_rate(b)}</td>'
            f'<td>{esc(b.get("quantity_note",""))}</td>'
            f'<td>{esc(b.get("channel_note",""))}</td>'
            f'<td style="color:#6b7094;">{esc(b.get("raw_text",""))[:40]}</td></tr>'
            for b in recent_broadcasts
        )
        broadcast_section = f"""
  <h2>🗣️ 遊戲內廣播（即時擷取）</h2>
  <table>
    <thead><tr><th>時間</th><th>類型</th><th>幣值</th><th>數量備註</th><th>交易方式</th><th>原文</th></tr></thead>
    <tbody>
      {broadcast_rows_html}
    </tbody>
  </table>
"""
    else:
        broadcast_section = """
  <h2>🗣️ 遊戲內廣播（即時擷取）</h2>
  <p style="color:#9a9ab0;">目前還沒有廣播資料，本地端監控腳本開始執行後會陸續出現在這裡。</p>
"""

    def completed_table(bucket_name, icon):
        items = [c for c in reversed(completed_entries) if c.get("bucket") == bucket_name][:COMPLETED_DISPLAY_COUNT]
        if not items:
            return f'<p style="color:#9a9ab0;">目前還沒有{bucket_name}的成交紀錄。</p>'
        if bucket_name == "遊戲幣":
            def rate_cell(c):
                r = c.get("rate")
                return f'{r:.0f} 楓幣/元' if r is not None else "（未抓到比例）"
            rows = "\n".join(
                f'<tr><td>{esc(c.get("completed_time",""))}</td>'
                f'<td>{rate_cell(c)}</td>'
                f'<td>{esc(c.get("price",""))}元</td>'
                f'<td style="color:#6b7094;">{esc(c.get("title",""))[:45]}</td></tr>'
                for c in items
            )
            header = "<tr><th>成交時間</th><th>幣值</th><th>成交價</th><th>標題</th></tr>"
        else:
            rows = "\n".join(
                f'<tr><td>{esc(c.get("completed_time",""))}</td>'
                f'<td>{esc(c.get("price",""))}元</td>'
                f'<td style="color:#6b7094;">{esc(c.get("title",""))[:45]}</td></tr>'
                for c in items
            )
            header = "<tr><th>成交時間</th><th>成交價</th><th>標題</th></tr>"
        return f'<table><thead>{header}</thead><tbody>{rows}</tbody></table>'

    completed_section = f"""
  <h2>✅ 已完成成交紀錄</h2>
  <h3 style="color:#9a9ab0;font-weight:600;">💰 遊戲幣</h3>
  {completed_table("遊戲幣", "💰")}
  <h3 style="color:#9a9ab0;font-weight:600;margin-top:20px;">📜 卷軸</h3>
  {completed_table("卷軸", "📜")}
"""

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>楓之谷經典服 楓幣幣值監控</title>
<script>
  // 自動加上隨機參數，避免瀏覽器/CDN快取顯示舊資料
  if (!location.search.includes('_t=')) {{
    location.replace(location.pathname + '?_t=' + Date.now());
  }}
</script>
<style>
  body {{ font-family: -apple-system, "Microsoft JhengHei", sans-serif; background:#0f1220; color:#e8e8f0; margin:0; padding:24px; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  .updated {{ color:#9a9ab0; font-size:0.85rem; margin-bottom:20px; }}
  .cards {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:28px; }}
  .card {{ background:#1a1e33; border-radius:12px; padding:16px 20px; min-width:160px; flex:1; }}
  .card .label {{ color:#9a9ab0; font-size:0.8rem; }}
  .card .value {{ font-size:1.6rem; font-weight:700; margin-top:4px; }}
  .best .value {{ color:#4ade80; }}
  .avg .value {{ color:#60a5fa; }}
  .min .value {{ color:#f87171; }}
  table {{ width:100%; border-collapse:collapse; background:#1a1e33; border-radius:12px; overflow:hidden; }}
  th, td {{ padding:10px 12px; text-align:left; border-bottom:1px solid #2a2f4a; font-size:0.9rem; }}
  th {{ color:#9a9ab0; font-weight:600; }}
  a {{ color:#60a5fa; }}
  .chart-wrap {{ background:#1a1e33; border-radius:12px; padding:16px; margin-bottom:28px; }}
  .legend {{ color:#9a9ab0; font-size:0.8rem; margin-top:8px; }}
  .legend .up {{ color:#4ade80; }}
  .legend .down {{ color:#f87171; }}
</style>
</head>
<body>
  <h1>🍁 楓之谷經典服（菇菇寶貝）楓幣幣值監控</h1>
  <div class="updated">最後更新：{esc(updated_at)}（每小時自動更新一次）</div>

  <div class="cards">
    <div class="card best"><div class="label">目前最佳幣值</div><div class="value">{summary['best_rate']:.0f}</div><div class="label">楓幣 / 元</div></div>
    <div class="card avg"><div class="label">平均幣值</div><div class="value">{summary['avg_rate']:.0f}</div><div class="label">楓幣 / 元</div></div>
    <div class="card min"><div class="label">最低幣值</div><div class="value">{summary['min_rate']:.0f}</div><div class="label">楓幣 / 元</div></div>
    <div class="card"><div class="label">目前擷取賣家數</div><div class="value">{summary['count']}</div></div>
  </div>

  <div class="chart-wrap">
    {candle_svg}
    <div class="legend">每根蠟燭代表一次抓取（約一小時）：<span class="up">綠色=幣值比上次高</span>／<span class="down">紅色=比上次低</span>，影線頂端/底端為當次最佳/最低幣值</div>
  </div>

  <h2>目前幣值最高的前 {len(top)} 名賣家</h2>
  <table>
    <thead><tr><th>#</th><th>幣值</th><th>賣家標題</th><th>連結</th></tr></thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
{broadcast_section}
{completed_section}
</body>
</html>
"""


def run_once():
    now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== {now_str} 開始抓取 ===")

    try:
        page_html = fetch_html(TARGET_URL)
    except Exception as e:
        print(f"[錯誤] 抓取頁面失敗: {e}")
        return

    listings = parse_listings(page_html)
    summary = summarize(listings)

    if summary is None:
        print("[警告] 這次沒解析到任何商品，可能是頁面結構改變了，或被擋。")
        return

    print(f"共擷取 {summary['count']} 筆商品")
    print(f"目前最佳幣值：1元 = {summary['best_rate']:.0f} 楓幣")
    print(f"平均幣值：1元 = {summary['avg_rate']:.0f} 楓幣")

    history_before = load_history()
    last = history_before[-1] if history_before else None

    append_record(summary)
    history_after = load_history()

    try:
        completed_html = fetch_html(COMPLETED_URL)
        completed_new = parse_completed_listings(completed_html)
        print(f"已完成商品：這次抓到 {len(completed_new)} 筆")
    except Exception as e:
        print(f"[警告] 抓取已完成商品失敗: {e}")
        completed_new = []

    completed_existing = load_completed_history()
    completed_merged = merge_completed_entries(completed_existing, completed_new)
    save_completed_history(completed_merged)

    html_out = render_html(
        summary, listings, history_after, now_str,
        broadcast_entries=load_broadcast_data(),
        completed_entries=completed_merged,
    )
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"已產生網頁：{HTML_PATH}")

    if last:
        try:
            last_best = float(last["best_rate"])
            change_pct = (summary["best_rate"] - last_best) / last_best * 100
            if abs(change_pct) >= ALERT_THRESHOLD_PCT:
                direction = "上漲" if change_pct > 0 else "下跌"
                msg = (
                    f"⚠️ 楓之谷經典服楓幣幣值{direction} {abs(change_pct):.1f}%\n"
                    f"上次：1元={last_best:.0f}楓幣 → 現在：1元={summary['best_rate']:.0f}楓幣"
                )
                print(msg)
                send_discord_alert(msg)
        except (KeyError, ValueError):
            pass


if __name__ == "__main__":
    run_once()
