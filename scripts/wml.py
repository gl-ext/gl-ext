#!/usr/bin/env python3
"""
World Mobile List -> наш формат, всё в одном файле.

Один запуск делает три шага:
  1. топ игроков (API сайта /api/v1/players/ranking/?after=N)          -> wml/wml_top.json
  2. рекорды каждого игрока (панель игрока, парсится из HTML)          -> wml/wml_records.jsonl
  3. перевод в наш формат (ID игрока по gdr-ids.json, ID уровня по названию, сверху топа вниз):
       wml/wml_achievements.json            {"users": {"ID": [[ID уровня, процент, "ссылка"], ...]}}
       wml/wml_achievements_with_nicks.json {"users": {"ID — ник": [[ID уровня, процент, "название", "ссылка"], ...]}}
       wml/wml_levels_not_found.txt         уровни, ID которых по названию не нашёлся
       wml/wml_players_no_id.txt            игроки, которых нет в gdr-ids.json (в JSON не попадают)

Запуск:
  python wml.py                  всё сразу
  python wml.py --limit 5        проверка: рекорды только у первых 5 игроков
  python wml.py --skip-top       не качать топ заново, взять wml/wml_top.json
  python wml.py --skip-records   не качать рекорды, только перевести готовый wml/wml_records.jsonl
  python wml.py --by-rank        игроки в итоговых JSON в порядке топа (по умолчанию по ID, как в других файлах)
  python wml.py --keep-banned    не выкидывать забаненных игроков (по умолчанию они пропускаются)

Шаг 2 докачивается: если прервать, при новом запуске уже скачанные игроки пропускаются.
Нужен только Python, ничего устанавливать не надо.
"""
import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.request

SITE = "https://worldmobilelist.com"
RANKING_URL = SITE + "/api/v1/players/ranking/?after={after}"   # по 50 игроков за запрос
PAGE_SIZE = 50
# Панель игрока (лёгкий запрос, который сайт делает при клике на игрока, около 9 КБ вместо 42 КБ у всей страницы)
PLAYER_URL = SITE + "/demonlist/statsviewer/pane/player/{id}"

LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"
IDS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/gdr-ids.json"
GLOBAL_LIST_URL = "https://api.demonlist.org/level/classic/list?limit={limit}&offset={offset}"

API_TRIES = 4

# ВАЖНО: такой же список лежит в compare.py и add-video-links.py. Дописали здесь - допишите и там.
SPECIAL_LEVEL_NAMES = {
    "3299": "Sakupen circles (Nick24)",
    "3351": "Thinking Space II (Legacy)",
    "2234": "Deimos (Endlevel)",
}


# =========================
# Общие мелочи
# =========================

def http_get(url, timeout=30, json_accept=False):
    """GET с повторами (до API_TRIES раз). 404/403 и подобное не повторяются. Возвращает текст ответа."""
    last = None
    for attempt in range(1, API_TRIES + 1):
        try:
            # Язык принудительно английский: заголовки блоков ("Main list", "First victories") разбираются по ним
            headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
            if json_accept:
                headers["Accept"] = "application/json"
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        print(f"    попытка {attempt}/{API_TRIES} не удалась: {last}")
        if attempt < API_TRIES:
            time.sleep(2 * attempt)
    raise last


def get_json(url):
    payload = json.loads(http_get(url, timeout=20, json_accept=True))
    time.sleep(0.2)  # небольшая пауза, чтобы не нагружать сервер
    return payload


def normalize(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def normalize_nick(nick):
    nick = re.sub(r'^[\U0001F1E6-\U0001F1FF]{2}\s*', '', nick.strip())
    return normalize(nick)


def strip_clan(nick):
    """'[67] Kristage' -> 'Kristage'."""
    return re.sub(r'^\[[^\]]*\]\s*', '', nick.strip())


YOUTUBE_ID = re.compile(
    r'(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^\s]*?&)?v=|shorts/|live/|embed/|v/))([A-Za-z0-9_-]{11})')


def normalize_link(link):
    """Как в командах: https://www.youtube.com/watch?v=ID. Не YouTube и пустое остаётся как есть."""
    link = str(link).strip()
    m = YOUTUBE_ID.search(link)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else link


def pick(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# =========================
# Шаг 1. Топ игроков
# =========================

def items_of(payload):
    """Список игроков из ответа API: сразу список или внутри словаря (data/players/results...)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "players", "results", "items", "ranking"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def normalize_player(item):
    country = pick(item, "nationality", "country", "country_code", "cc")
    if isinstance(country, dict):
        country = pick(country, "country_code", "code", "cc", "name")
    return {
        "rank": pick(item, "rank", "position", "place"),
        "id": pick(item, "id", "player_id", "user_id"),
        "name": pick(item, "name", "nickname", "username", "nick"),
        "score": round(pick(item, "score", "points", "value"), 2) if isinstance(pick(item, "score", "points", "value"), (int, float)) else pick(item, "score", "points", "value"),
        "country": country,
        "banned": bool(item.get("banned", False)),
    }


def step_top(out_dir, delay, max_pages, keep_banned=False):
    print("=== Шаг 1. Топ игроков ===")
    raw, seen, after = [], set(), 0
    for _ in range(max_pages):
        items = items_of(get_json(RANKING_URL.format(after=after)))
        new = [it for it in items if isinstance(it, dict) and str(pick(it, "id", "player_id", "user_id")) not in seen]
        print(f"after={after}: получено {len(items)}, новых {len(new)}")
        if not new:
            break
        for it in new:
            seen.add(str(pick(it, "id", "player_id", "user_id")))
        raw.extend(new)
        after += PAGE_SIZE
        time.sleep(delay)

    players = [normalize_player(it) for it in raw]
    banned = [p for p in players if p["banned"]]
    if banned and not keep_banned:
        print(f"Забаненных в топе: {len(banned)}, пропускаю (чтобы оставить: --keep-banned)")
        players = [p for p in players if not p["banned"]]
    with open(os.path.join(out_dir, "wml_top_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "wml_top.json"), "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=1)
    print(f"Игроков в топе: {len(players)}\n")
    return players


# =========================
# Шаг 2. Рекорды каждого игрока
# =========================

SECTION = re.compile(
    r'<section class="panel pf-group"><h3>(?P<title>.*?)<small>.*?</small></h3>'
    r'<div class="chips">(?P<body>.*?)</div></section>', re.S)
CHIP = re.compile(
    r'<a class="chip[^"]*" href="(?P<video>[^"]*)"[^>]*>'
    r'<i>#(?P<pos>\d+)</i><span>(?P<name>.*?)</span>(?:<em>(?P<pct>\d+)%</em>)?</a>', re.S)
NAME = re.compile(r'<div class="pf-name"><h2>(.*?)</h2>', re.S)
COUNTRY = re.compile(r'<div class="pf-id">.*?<span data-cc="([A-Z]{2})"', re.S)


def parse_panel(page):
    """Рекорды игрока из HTML панели. Возвращает словарь или None, если панели игрока нет."""
    pane = page[page.find('id="pane-box"'):] if 'id="pane-box"' in page else page
    name = NAME.search(pane)
    if not name and "pf-group" not in pane:
        return None

    records, first = [], set()
    for sec in SECTION.finditer(pane):
        title = html.unescape(sec["title"]).strip()
        chips = list(CHIP.finditer(sec["body"]))
        if title.lower() == "first victories":  # лишь пометка к уже перечисленным прохождениям
            first = {int(c["pos"]) for c in chips}
            continue
        for c in chips:
            progress = c["pct"] is not None
            records.append({
                "list": title,                       # Main list / Extended list / Unbounded list / In progress
                "level_pos": int(c["pos"]),          # место уровня в списке сайта (меняется со временем)
                "level": html.unescape(c["name"]).strip(),
                "percent": int(c["pct"]) if progress else 100,
                "video": html.unescape(c["video"]).strip(),
            })
    for r in records:
        r["first_victory"] = r["percent"] == 100 and r["level_pos"] in first

    cc = COUNTRY.search(pane)
    return {
        "nick": html.unescape(name.group(1)).strip() if name else "",
        "country": cc.group(1) if cc else "",
        "records": records,
    }


def step_records(players, out_dir, url_template, limit, delay):
    print("=== Шаг 2. Рекорды игроков ===")
    path = os.path.join(out_dir, "wml_records.jsonl")
    todo = players[:limit] if limit else players

    done = set()
    if os.path.exists(path):  # докачка
        with open(path, encoding="utf-8") as f:
            done = {str(json.loads(line)["id"]) for line in f if line.strip()}
        print(f"Уже скачано раньше: {len(done)}")

    failed = []
    with open(path, "a", encoding="utf-8") as out:
        for i, p in enumerate(todo, 1):
            pid = str(p["id"])
            if pid in done:
                continue
            try:
                data = parse_panel(http_get(url_template.format(id=pid)))
            except Exception as e:
                print(f"[{i}/{len(todo)}] {p.get('name')} ({pid}): ОШИБКА {e}")
                failed.append(pid)
                continue
            if data is None:
                print(f"[{i}/{len(todo)}] {p.get('name')} ({pid}): панели игрока в ответе нет")
                failed.append(pid)
                continue
            data.update({"id": pid, "rank": p.get("rank"), "score": p.get("score")})
            out.write(json.dumps(data, ensure_ascii=False) + "\n")
            out.flush()
            print(f"[{i}/{len(todo)}] {data['nick'] or p.get('name')}: рекордов {len(data['records'])}")
            time.sleep(delay)

    print(f"Не удалось: {len(failed)}" + (f" ({', '.join(failed[:20])}...)" if failed else "") + "\n")

    with open(path, encoding="utf-8") as f:
        wanted = {str(p["id"]) for p in todo}
        return [rec for rec in (json.loads(line) for line in f if line.strip()) if str(rec["id"]) in wanted]


# =========================
# Шаг 3. Перевод в наш формат
# =========================

class LevelResolver:
    """Название уровня -> ID: levels_tp.json, особые названия, глобал (список качается один раз, если понадобится)."""

    def __init__(self, levels_data):
        self.by_name = {}
        for level_id, data in levels_data.items():
            name = data.get("name")
            if not name:
                continue
            out_id = str(level_id) if str(level_id).startswith("x") else int(level_id)
            self.by_name.setdefault(normalize(name), out_id)
        self.special = {normalize(n): int(i) for i, n in SPECIAL_LEVEL_NAMES.items()}
        self.global_levels = None
        self.stats = {"levels_tp.json": 0, "особые названия": 0, "глобал": 0}

    def _load_global(self):
        result, offset = {}, 0
        print("\nЗагружаю список уровней глобала (для названий, которых нет в levels_tp.json)...")
        try:
            while True:
                levels = (get_json(GLOBAL_LIST_URL.format(limit=100, offset=offset)).get("data") or {}).get("levels") or []
                if not levels:
                    break
                added = 0
                for lv in levels:
                    if lv.get("name") and lv.get("id") is not None and normalize(lv["name"]) not in result:
                        result[normalize(lv["name"])] = int(lv["id"])
                        added += 1
                if added == 0:
                    break
                offset += 100
            print(f"  Уровней загружено из глобала: {len(result)}")
        except Exception as e:
            print(f"  Не удалось загрузить список глобала: {e}")
        self.global_levels = result

    def find(self, name):
        key = normalize(name)
        if key in self.by_name:
            self.stats["levels_tp.json"] += 1
            return self.by_name[key]
        if key in self.special:
            self.stats["особые названия"] += 1
            return self.special[key]
        if self.global_levels is None:
            self._load_global()
        if key in self.global_levels:
            self.stats["глобал"] += 1
            return self.global_levels[key]
        return None


def player_id_sort_key(player_key):
    """Числовые ID по возрастанию, затем pc-ID (как в compare.py)."""
    pid = str(player_key).split(" — ", 1)[0].strip().casefold()
    if pid.startswith("pc"):
        suffix = pid[2:]
        return (1, 0, int(suffix), suffix) if suffix.isdigit() else (1, 1, suffix)
    if pid.isdigit():
        return (0, 0, int(pid), pid)
    return (0, 1, pid)


def save_json(filename, users):
    with open(filename, "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(users.items())
        for i, (key, records) in enumerate(items):
            f.write(f'    {json.dumps(key, ensure_ascii=False)}: [\n')
            for j, rec in enumerate(records):
                f.write(f'      {json.dumps(rec, ensure_ascii=False)}{"," if j < len(records) - 1 else ""}\n')
            f.write(f'    ]{"," if i < len(items) - 1 else ""}\n')
        f.write('  }\n}\n')


def convert(players, nick_to_id, resolver):
    """Возвращает (users, users_nicks, not_found, no_id). Игроки идут от верха топа до низа."""
    users, users_nicks, not_found, no_id, name_cache = {}, {}, {}, [], {}

    for p in sorted(players, key=lambda x: (x.get("rank") is None, x.get("rank") or 0)):
        wml_nick = p.get("nick") or ""
        clean = strip_clan(wml_nick)
        gid = nick_to_id.get(normalize_nick(clean)) or nick_to_id.get(normalize_nick(wml_nick))
        if not gid:
            no_id.append(f"#{p.get('rank')} {wml_nick} (WML id {p.get('id')}), рекордов: {len(p.get('records', []))}")
            continue

        best = {}  # ID уровня -> (процент, название, ссылка): по уровню остаётся лучший процент
        for r in p.get("records", []):
            name = r["level"]
            if name not in name_cache:
                name_cache[name] = resolver.find(name)
            level_id = name_cache[name]
            if level_id is None:
                nf = not_found.setdefault(name, [0, clean])
                nf[0] += 1
                continue
            percent = int(r.get("percent", 100))
            if level_id not in best or percent > best[level_id][0]:
                best[level_id] = (percent, name, normalize_link(r.get("video", "")))

        if best:
            rows = sorted(best.items(), key=lambda kv: str(kv[0]))
            users.setdefault(gid, []).extend([[lid, pct, link] for lid, (pct, _n, link) in rows])
            users_nicks.setdefault(f"{gid} — {clean}", []).extend(
                [[lid, pct, n, link] for lid, (pct, n, link) in rows])
    return users, users_nicks, not_found, no_id


def step_convert(records, out_dir, by_rank):
    print("=== Шаг 3. Перевод в наш формат ===")
    print(f"Игроков с рекордами: {len(records)}")
    print("gdr-ids.json и levels_tp.json: беру с GitHub (raw кешируется до ~5 минут после правки)")
    raw_ids = get_json(IDS_URL)
    nick_to_id = {normalize_nick(n): str(i).strip().casefold() for n, i in raw_ids.items()}
    resolver = LevelResolver(get_json(LEVELS_URL)["levels"])

    users, users_nicks, not_found, no_id = convert(records, nick_to_id, resolver)

    if not by_rank:
        users = dict(sorted(users.items(), key=lambda kv: player_id_sort_key(kv[0])))
        users_nicks = dict(sorted(users_nicks.items(), key=lambda kv: player_id_sort_key(kv[0])))

    p = lambda n: os.path.join(out_dir, n)
    save_json(p("wml_achievements.json"), users)
    save_json(p("wml_achievements_with_nicks.json"), users_nicks)
    with open(p("wml_levels_not_found.txt"), "w", encoding="utf-8") as f:
        for name, (cnt, example) in sorted(not_found.items(), key=lambda kv: -kv[1][0]):
            f.write(f"{name} | рекордов: {cnt} | например у {example}\n")
    with open(p("wml_players_no_id.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(no_id) + ("\n" if no_id else ""))

    print(f"\nИгроков с рекордами в JSON: {len(users)}; записей: {sum(map(len, users.values()))}")
    print(f"Игроков без ID в gdr-ids.json: {len(no_id)}")
    print(f"Уровней, не найденных по названию: {len(not_found)}")
    print("Названия найдены:", {k: v for k, v in resolver.stats.items() if v})
    print(f"Файлы лежат в папке {out_dir}")


# =========================
# Запуск
# =========================

def main():
    ap = argparse.ArgumentParser(description="World Mobile List: топ, рекорды и перевод в наш формат одним запуском")
    ap.add_argument("--out-dir", default="wml", help="папка для всех файлов (создаётся сама). По умолчанию wml")
    ap.add_argument("--url", default=PLAYER_URL, help="адрес панели игрока, {id} заменяется на ID сайта")
    ap.add_argument("--limit", type=int, default=0, help="рекорды только у первых N игроков (для проверки)")
    ap.add_argument("--delay", type=float, default=0.4, help="пауза между запросами к сайту, секунд")
    ap.add_argument("--max-pages", type=int, default=400, help="защита от бесконечного цикла при чтении топа")
    ap.add_argument("--keep-banned", action="store_true", help="не выкидывать забаненных игроков из топа")
    ap.add_argument("--skip-top", action="store_true", help="не качать топ, взять <out-dir>/wml_top.json")
    ap.add_argument("--skip-records", action="store_true", help="не качать рекорды, перевести готовый wml_records.jsonl")
    ap.add_argument("--by-rank", action="store_true", help="игроки в итоговых JSON в порядке топа, а не по ID")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    top_path = os.path.join(args.out_dir, "wml_top.json")
    rec_path = os.path.join(args.out_dir, "wml_records.jsonl")

    if args.skip_records:
        if not os.path.exists(rec_path):
            raise SystemExit(f"Нет {rec_path}: сначала запустите без --skip-records")
        with open(rec_path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
    else:
        if args.skip_top:
            if not os.path.exists(top_path):
                raise SystemExit(f"Нет {top_path}: сначала запустите без --skip-top")
            with open(top_path, encoding="utf-8") as f:
                players = json.load(f)
            print(f"Топ взят из {top_path}: {len(players)} игроков\n")
        else:
            players = step_top(args.out_dir, args.delay, args.max_pages, args.keep_banned)

        if not players or any(p.get("id") is None for p in players):
            raise SystemExit("В ответе API нет ID игроков (или он пустой). Посмотрите wml_top_raw.json: "
                             "нужно поправить названия полей в normalize_player().")
        records = step_records(players, args.out_dir, args.url, args.limit, args.delay)

    step_convert(records, args.out_dir, args.by_rank)


if __name__ == "__main__":
    main()
