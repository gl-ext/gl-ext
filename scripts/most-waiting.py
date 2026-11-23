#!/usr/bin/env python3
"""
most_waiting.py — игроки Global Demonlist по убыванию числа рекордов со статусом waiting.

Примеры:
    python most_waiting.py                 # все уровни
    python most_waiting.py 301             # топ 301 и ниже (301+)
    python most_waiting.py 151 300         # от топ 151 до топ 300
    python most_waiting.py 1 150 --status accepted
    python most_waiting.py --start 1000 --end 2000   # быстрая проверка: только id 1000..2000

Таблица лидеров не используется: user_id перебираются подряд (1, 2, 3, ...),
для каждого вызывается user/record/list. Перебор заканчивается, когда подряд
встретилось --stop-after (100) id без игрока/рекордов, но не раньше --min-scan (50000).

Только стандартная библиотека.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ───────────── НАСТРОЙКИ ─────────────
BASE_URL = "https://api.demonlist.org"
RECORDS_ENDPOINT = "user/record/list"
USER_ENDPOINT = "user/get"
PAGE_LIMIT = 50          # максимум у user/record/list
API_TOKEN = ""           # если нужна авторизация: "Bearer ..."
# ─────────────────────────────────────


class NoSuchUser(Exception):
    """API ответил 400/404 — такого игрока нет."""


def http_get(path, params=None, retries=5):
    url = f"{BASE_URL.rstrip('/')}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "most-waiting/2.0", "Accept": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = API_TOKEN
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise NoSuchUser(url) from None
            last = f"HTTP {e.code}"
            time.sleep(1.5 * (attempt + 1))        # 429 / 5xx — подождать и повторить
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = str(e)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{last} для {url}")


def in_range(placement, lo, hi):
    return placement is not None and placement >= lo and (hi is None or placement <= hi)


def scan_user(uid, status, lo, hi):
    """
    Возвращает (exists, matched):
      exists  — у id есть игрок с рекордами (для условия остановки),
      matched — подходящие записи.
    """
    matched = []
    offset = 0
    total = 0
    while True:
        try:
            resp = http_get(RECORDS_ENDPOINT, {"user_id": uid, "limit": PAGE_LIMIT, "offset": offset})
        except NoSuchUser:
            return False, []
        data = resp.get("data") or {}
        records = data.get("records") or []
        total = data.get("total_count", total) or 0
        for rec in records:
            if rec.get("status") != status:
                continue
            lvl = rec.get("level") or {}
            if in_range(lvl.get("placement"), lo, hi):
                matched.append({
                    "record_id": rec.get("id"),
                    "level": lvl.get("name"),
                    "placement": lvl.get("placement"),
                    "percent": rec.get("percent"),
                    "video_url": rec.get("video_url"),
                })
        offset += PAGE_LIMIT
        if not records or offset >= total:
            break
    matched.sort(key=lambda r: r["placement"])
    return total > 0, matched


def fetch_username(uid):
    try:
        resp = http_get(USER_ENDPOINT, {"user_id": uid})
        d = resp.get("data") or {}
        if isinstance(d.get("user"), dict):
            d = d["user"]
        return d.get("username") or d.get("name") or f"id {uid}"
    except Exception:
        return f"id {uid}"


def main():
    ap = argparse.ArgumentParser(description="Игроки по убыванию числа рекордов в статусе waiting")
    ap.add_argument("min_place", type=int, nargs="?", default=1,
                    help="нижняя граница placement (если указан только он — это 'N и ниже', т.е. N+)")
    ap.add_argument("max_place", type=int, nargs="?", default=None,
                    help="верхняя граница placement (включительно)")
    ap.add_argument("--status", default="waiting", choices=["waiting", "accepted", "rejected"])
    ap.add_argument("--base-url", help="база API, если отличается от https://api.demonlist.org")
    ap.add_argument("--min-scan", type=int, default=50000, help="не останавливаться раньше этого id (50000)")
    ap.add_argument("--stop-after", type=int, default=100, help="сколько пустых id подряд = конец (100)")
    ap.add_argument("--start", type=int, default=1, help="с какого id начать (1)")
    ap.add_argument("--end", type=int, default=0,
                    help="на каком id закончить, включительно (0 = без ограничения, идти до конца)")
    ap.add_argument("--workers", type=int, default=16, help="параллельных запросов (16)")
    ap.add_argument("--out", default="most_waiting", help="имя файла без расширения")
    ap.add_argument("--format", choices=["json", "txt", "both"], default="both")
    ap.add_argument("--top", type=int, default=0, help="оставить только N лучших игроков")
    args = ap.parse_args()

    global BASE_URL
    if args.base_url:
        BASE_URL = args.base_url

    lo, hi = args.min_place, args.max_place
    if hi is not None and hi < lo:
        sys.exit("max_place не может быть меньше min_place")
    range_label = f"top{lo}+" if hi is None else f"top{lo}-{hi}"
    if args.end and args.end < args.start:
        sys.exit("--end не может быть меньше --start")

    chunk = max(args.workers * 8, 64)
    results = []
    empty_streak = 0
    scanned = 0
    errors_in_row = 0
    next_id = args.start
    finished = False
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while not finished:
            last_id = next_id + chunk - 1
            if args.end:
                last_id = min(last_id, args.end)       # не выходим за --end
            ids = list(range(next_id, last_id + 1))
            next_id = last_id + 1
            futs = [(uid, pool.submit(scan_user, uid, args.status, lo, hi)) for uid in ids]
            for uid, fut in futs:                      # разбираем строго по порядку id
                try:
                    exists, recs = fut.result()
                    errors_in_row = 0
                except Exception as e:
                    errors_in_row += 1
                    print(f"  ! id {uid}: {e}", file=sys.stderr)
                    if errors_in_row >= 20:
                        sys.exit("20 ошибок подряд — проверь --base-url / доступ к API")
                    continue                           # ошибка не считается «пустым» id
                scanned += 1
                if exists:
                    empty_streak = 0
                else:
                    empty_streak += 1
                if recs:
                    results.append({"user_id": uid, "username": None, "count": len(recs), "records": recs})
                if uid >= args.min_scan and empty_streak >= args.stop_after:
                    print(f"Конец: {empty_streak} пустых id подряд, последний проверенный id {uid}",
                          file=sys.stderr)
                    finished = True
                    break
            if args.end and next_id > args.end:        # весь диапазон пройден
                finished = True
            print(f"  проверено {scanned} id (до {next_id - 1}), найдено игроков: {len(results)}, "
                  f"{time.time() - t0:.0f}с", file=sys.stderr)

        # ники — только для тех, кто попал в результат
        for r, name in zip(results, pool.map(fetch_username, [r["user_id"] for r in results])):
            r["username"] = name

    results.sort(key=lambda r: (-r["count"], r["username"].lower()))
    if args.top:
        results = results[:args.top]

    if args.format in ("json", "both"):
        with open(f"{args.out}.json", "w", encoding="utf-8") as f:
            json.dump({"status": args.status, "range": range_label, "players": results},
                      f, ensure_ascii=False, indent=2)

    if args.format in ("txt", "both"):
        lines = [f"Рекорды со статусом '{args.status}', {range_label}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['username']} (id {r['user_id']}) — {r['count']}")
            for rec in r["records"]:
                lines.append(f"     #{rec['placement']} {rec['level']} — {rec['percent']}%  {rec['video_url'] or ''}")
            lines.append("")
        with open(f"{args.out}.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    print(f"Готово: {len(results)} игроков → {args.out}.json / {args.out}.txt", file=sys.stderr)


if __name__ == "__main__":
    main()
