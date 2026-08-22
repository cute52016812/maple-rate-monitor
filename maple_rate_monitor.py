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

ALERT_THRESHOLD_PCT = 5.0      # 幣值變化超過這個百分比才提醒
DISCORD_WEBHOOK_URL = ""       # 例如 "https://discord.com/api/webhooks/xxxx/yyyy"，留空則不通知
TOP_N_DISPLAY = 8              # 排行榜顯示筆數
HISTORY_POINTS_ON_CHART = 168  # 走勢圖最多顯示幾筆歷史紀錄（168=一週*每小時一筆）

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


def esc(s: str) -> str:
    return html_lib.escape(s, quote=True)


def render_html(summary, listings, history_rows, updated_at: str):
    top = sorted(listings, key=lambda x: x["rate"], reverse=True)[:TOP_N_DISPLAY]

    rows_html = "\n".join(
        f'<tr><td>{i+1}</td><td>1元 = {item["rate"]:.0f} 楓幣</td>'
        f'<td>{esc(item["title"])[:60]}</td>'
        f'<td><a href="{esc(item["url"])}" target="_blank" rel="noopener">查看</a></td></tr>'
        for i, item in enumerate(top)
    )

    chart_rows = history_rows[-HISTORY_POINTS_ON_CHART:]
    labels = [r["timestamp"] for r in chart_rows]
    best_series = [r["best_rate"] for r in chart_rows]
    avg_series = [r["avg_rate"] for r in chart_rows]

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>楓之谷經典服 楓幣幣值監控</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
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
    <canvas id="rateChart" height="90"></canvas>
  </div>

  <h2>目前幣值最高的前 {len(top)} 名賣家</h2>
  <table>
    <thead><tr><th>#</th><th>幣值</th><th>賣家標題</th><th>連結</th></tr></thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>

<script>
  const ctx = document.getElementById('rateChart').getContext('2d');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: {labels!r},
      datasets: [
        {{ label: '最佳幣值', data: {best_series!r}, borderColor: '#4ade80', tension: 0.25, pointRadius: 0 }},
        {{ label: '平均幣值', data: {avg_series!r}, borderColor: '#60a5fa', tension: 0.25, pointRadius: 0 }}
      ]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ labels: {{ color: '#e8e8f0' }} }} }},
      scales: {{
        x: {{ ticks: {{ color: '#9a9ab0', maxTicksLimit: 8 }} }},
        y: {{ ticks: {{ color: '#9a9ab0' }} }}
      }}
    }}
  }});
</script>
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

    html_out = render_html(summary, listings, history_after, now_str)
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
