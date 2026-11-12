#!/usr/bin/env python3
"""
Собирает links.json: находит игроков, у которых на demonlist и Pointercrate
одно и то же видео, но разные аккаунты.

В links.json попадают ВСЕ аккаунты:
  - каждый аккаунт глобала (ключ = его id), даже если Pointercrate у него нет;
  - каждый игрок Pointercrate, которого нет в глобале, под ключом "pc<id на Pointercrate>".
    Если у одного человека несколько аккаунтов Pointercrate (у них общее видео),
    они лежат в одной записи, а ключ берётся по наименьшему id.

Склейка по каналам: если один и тот же канал (YouTube/Twitch/...) найден у разных записей,
это один человек, и записи склеиваются (аккаунты Pointercrate и каналы переезжают в главную).
Главная: запись глобала, а если глобала нет, запись с наименьшим id Pointercrate.
Две и больше записей ГЛОБАЛА с общим каналом не склеиваются, они попадают в отдельный файл.

Запуск:   python make_links.py
Скачивание можно прервать в любой момент (Ctrl+C, выключили свет, закрыли окно).
Прогресс сохраняется в файлы cache_*, и при следующем запуске скрипт продолжит
с того места, где остановился. Просто запускай его той же командой, пока
не напишет, что всё скачано.

Параметры:
  --workers=N   сколько запросов делать одновременно (по умолчанию 5)
  --refresh     удалить всё скачанное и начать заново
  --no-channels не искать каналы игроков (YouTube/Twitch/Bilibili/...), только аккаунты
  --no-yt-id    не искать постоянные номера каналов YouTube (UC...), остальные каналы искать
Нужен только Python, ничего устанавливать не надо.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

DL = "https://api.demonlist.org/"
PC = "https://pointercrate.com/api/"
PAUSE = 0.2  # пауза после каждого запроса (в каждом потоке отдельно)
WORKERS = 10
PC_SPAN = 2500  # Pointercrate качается кусками по стольким id параллельно
SAVE_EVERY = 10  # сохранять прогресс каждые N уровней
MAX_ROUNDS = 30  # сколько раз повторять после неудачи (с нарастающей паузой до 2 минут)

CACHE_LEVELS = "cache_levels.json"  # список id уровней
# {id уровня: {name, verifier_id, verifier, vurl}}: названия уровней и их вериферы
CACHE_LEVEL_INFO = "cache_level_info.json"
PROGRESS_DL = "cache_dl_progress.json"  # {id уровня: рекорды}, пополняется по ходу
PROGRESS_PC = "cache_pc_progress.json"  # {"after": ..., "records": [...]}
CACHE_DL = "cache_dl_records.json"  # итог: все рекорды demonlist
CACHE_PC = "cache_pc_records.json"  # итог: все рекорды Pointercrate
CACHE_PC_PLAYERS = "cache_pc_players.json"  # все игроки Pointercrate (id -> ник), даже без рекордов
# Рекорды отдельных игроков из глобала, их пишет top_missing.py (user/record/list).
# Здесь они только дополняют данные по уровням, но не заменяют их.
CACHE_USERS = "cache_dl_users.json"
USERS_MAX_AGE = 7 * 24 * 3600  # записи старше недели не используем
OUT = "links.json"  # готовый результат
REVIEW = "links_review.txt"  # спорные случаи для ручной проверки
MERGES_LOG = "links_channel_merges.txt"  # что и когда склеено по общим каналам (дописывается)
CHANNEL_CONFLICTS = "links_channel_conflicts.txt"  # общие каналы, которые НЕ склеены (пишется заново)
CHANNELS_IGNORE = "channels_ignore.json"  # список ссылок на каналы, по которым склеивать не надо
MAX_MERGE_ENTRIES = 3  # больше записей с общим каналом: скорее всего чужой общий канал, не склеиваем

FIX = "fix.json"

# Видео YouTube (ID всегда 11 символов). Остальные ссылки сравнивать ненадёжно.
YT = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})",
    re.I,
)


# Параметры ссылок, которые не влияют на то, какое это видео
TRACK_PARAMS = {
    "si", "feature", "fbclid", "gclid", "igshid", "ref", "ref_src", "ref_url", "share",
    "t", "start", "time_continue", "list", "index", "pp", "ab_channel", "app", "usp",
    "spm_id_from", "vd_source", "share_source", "share_medium", "share_plat",
    "share_session_id", "from_spmid", "unique_k", "timestamp", "bbid", "ts",
}
# На этих сайтах видео определяется одним путём, а всё после "?" только мусор
NO_QUERY_HOSTS = {
    "twitch.tv", "clips.twitch.tv", "streamable.com", "medal.tv", "vimeo.com",
    "tiktok.com", "x.com", "cdn.discordapp.com", "media.discordapp.net",
}
DISPLAY = {}  # ключ видео (не YouTube) -> ссылка в том виде, как её дали


def other_video_key(url):
    """Ключ для ссылок не с YouTube: сайт + путь без мусора. None, если это не ссылка на видео."""
    u = str(url or "").strip()
    if not u:
        return None
    if not re.match(r"^https?://", u, re.I):
        if not re.match(r"^[\w.-]+\.[a-z]{2,}/", u, re.I):
            return None
        u = "https://" + u
    try:
        sp = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    host = (sp.hostname or "").lower()
    for pre in ("www.", "m.", "mobile."):
        if host.startswith(pre):
            host = host[len(pre):]
    if host == "twitter.com":
        host = "x.com"
    if not host or host.endswith("youtube.com") or host == "youtu.be":
        return None  # YouTube без нормального ID (канал, плейлист) не считаем
    path = sp.path.rstrip("/")
    if len(path) < 2:
        return None  # главная страница сайта, а не видео
    query = ""
    if host not in NO_QUERY_HOSTS:
        q = sorted(
            (k, v)
            for k, v in urllib.parse.parse_qsl(sp.query)
            if k.lower() not in TRACK_PARAMS and not k.lower().startswith("utm_")
        )
        query = urllib.parse.urlencode(q)
    key = "x:" + host + path + ("?" + query if query else "")
    DISPLAY.setdefault(key, u)
    return key


def video_id(url):
    """Ключ видео: 11 символов ID для YouTube, иначе нормализованная ссылка."""
    m = YT.search(str(url or ""))
    return m.group(1) if m else other_video_key(url)


def vurls(vids):
    """Ссылки на все общие видео для файла проверки."""
    return "\n".join(
        "        " + (DISPLAY[v] if v in DISPLAY else f"https://youtu.be/{v}")
        for v in sorted(vids)
    )


def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            print(f"  файл {path} повреждён, начинаю его заново")
    return default


def save_json(path, data):
    """Сначала пишем во временный файл, потом переименовываем.
    Так, даже если свет выключится посреди записи, старый файл останется целым."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def get_json(url, tries=5):
    """Скачивает JSON. При 429 и ошибках сервера повторяет с нарастающей паузой.
    Возвращает None, если не удалось."""
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "links-builder/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            time.sleep(PAUSE)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** i)
                continue
            print(f"  ошибка {e.code}: {url}")
            return None
        except Exception:
            time.sleep(2 ** i)
    print("  не удалось скачать:", url)
    return None

def get_json_patient(url, rounds=MAX_ROUNDS):
    """Как get_json, но при неудаче ждёт подольше и пробует снова, а не сдаётся."""
    for n in range(rounds):
        d = get_json(url)
        if d is not None:
            return d
        wait = min(120, 15 * (n + 1))
        print(f"  сервер не отвечает, жду {wait} с и пробую снова ({n + 1}/{rounds})")
        time.sleep(wait)
    return None


def merge_user_cache(dl):
    """Добавляет к рекордам demonlist те, что скачал top_missing.py (cache_dl_users.json).
    Только свежие записи, дубли (тот же игрок и видео) пропускаются."""
    cache = load_json(CACHE_USERS, {}) or {}
    if not cache:
        return dl
    names = {r["u"]: r["n"] for r in dl if r.get("n")}
    have = {(r["u"], r["v"]) for r in dl}
    now = time.time()
    added, used = 0, 0
    out = list(dl)
    for uid, entry in cache.items():
        if now - entry.get("time", 0) > USERS_MAX_AGE:
            continue
        used += 1
        u = int(uid) if str(uid).isdigit() else uid
        for r in entry.get("records", []):
            if not r.get("v") or (u, r["v"]) in have:
                continue
            have.add((u, r["v"]))
            extra = {k: x for k, x in r.items() if k not in ("u", "n", "v")}  # вдруг там есть уровень
            out.append({"u": u, "n": names.get(u), "v": r["v"], "src": CACHE_USERS, "info": extra})
            added += 1
    print(f"  {CACHE_USERS}: игроков {used}, добавлено рекордов {added}")
    return out


def fetch_level_info():
    """{id уровня (строкой): {name, verifier_id, verifier, vurl}} по всему списку."""
    info = {}
    for list_type in [None, "main", "extended", "advanced", "unbounded"]:
        offset = 0
        while True:
            q = {"limit": 50, "offset": offset}
            if list_type:
                q["list_type"] = list_type
            d = get_json(DL + "level/classic/list?" + urllib.parse.urlencode(q))
            batch = ((d or {}).get("data") or {}).get("levels") or []
            new = [lv for lv in batch if str(lv["id"]) not in info]
            for lv in batch:
                v = lv.get("verifier") or {}
                info.setdefault(
                    str(lv["id"]),
                    {
                        "name": lv.get("name"),
                        "verifier_id": v.get("user_id"),
                        "verifier": v.get("username"),
                        "vurl": lv.get("verification_url"),
                    },
                )
            if not batch or not new:
                break
            offset += len(batch)
    return info


def load_level_info():
    """Названия уровней и вериферы: из кэша или скачиваем (один раз, около минуты)."""
    info = load_json(CACHE_LEVEL_INFO)
    if info:
        return info
    print("  скачиваю список уровней с вериферами...")
    info = fetch_level_info()
    if info:
        save_json(CACHE_LEVEL_INFO, info)
    else:
        print("  не удалось получить вериферов, продолжаю без них")
    return info or {}


def merge_verifiers(dl):
    """Добавляет вериферов как рекорды: ник верифера и видео верификации.
    Дубли (тот же игрок и то же видео, что уже есть в рекордах) пропускаются."""
    info = load_level_info()
    names = {r["u"]: r["n"] for r in dl if r.get("n")}
    have = {(r["u"], r["v"]) for r in dl}
    known = {r["u"] for r in dl}  # у кого уже есть хоть одна запись
    out = list(dl)
    added = 0
    for lid, i in info.items():
        u, v = i.get("verifier_id"), i.get("vurl")
        if u is None:
            continue
        if not v:
            # Видео верификации нет, но аккаунт существует. Запись без видео ни на что
            # не влияет, зато игрок попадёт в links.json.
            if u not in known:
                known.add(u)
                out.append({
                    "u": u,
                    "n": i.get("verifier") or names.get(u),
                    "v": None,
                    "src": "verifier",
                    "lvl": int(lid) if str(lid).isdigit() else lid,
                })
            continue
        if (u, v) in have:
            continue
        have.add((u, v))
        out.append({
            "u": u,
            "n": i.get("verifier") or names.get(u),
            "v": v,
            "src": "verifier",
            "lvl": int(lid) if str(lid).isdigit() else lid,
        })
        added += 1
    print(f"  вериферов добавлено: {added} (уровней в списке: {len(info)})")
    return out


def load_fix():
    """Читает fix.json. Если файла нет, работаем без него."""
    fix = load_json(FIX, {})
    if not fix:
        print(f"  {FIX} не найден, работаю без него")
    return fix


def apply_fix(dl, fix):
    """Склеивает аккаунты demonlist по merges: absorb -> keep."""
    to_keep = {}      # id поглощённого -> id основного
    keep_names = {}   # id основного -> его ник
    for m in fix.get("merges", []):
        keep_names[m["keep"]] = m.get("keep_name")
        for a in m.get("absorb", []):
            to_keep[a["id"]] = m["keep"]

    def resolve(uid):  # на случай цепочек a -> b -> c
        seen = set()
        while uid in to_keep and uid not in seen:
            seen.add(uid)
            uid = to_keep[uid]
        return uid

    out = []
    for r in dl:
        uid = resolve(r["u"]) if r["u"] is not None else None
        name = keep_names.get(uid) or r["n"]
        out.append({**r, "u": uid, "n": name})
    print(f"  fix.json: склеено аккаунтов {len(to_keep)}")
    return out

# ---------- demonlist ----------

def fetch_level_ids():
    """Список id всех уровней (внутренние id demonlist)."""
    ids = set()
    # без фильтра и по каждому типу списка отдельно, дубли отсеются
    for list_type in [None, "main", "extended", "advanced", "unbounded"]:
        offset = 0
        while True:
            q = {"limit": 50, "offset": offset}
            if list_type:
                q["list_type"] = list_type
            d = get_json(DL + "level/classic/list?" + urllib.parse.urlencode(q))
            batch = ((d or {}).get("data") or {}).get("levels") or []
            new = [lv["id"] for lv in batch if lv["id"] not in ids]
            ids.update(lv["id"] for lv in batch)
            if not batch or not new:
                break
            offset += len(batch)
        print(f"  уровней собрано: {len(ids)} (после {list_type or 'без фильтра'})")
    return sorted(ids)


def fetch_level_records(level_id):
    """Все рекорды одного уровня. Возвращает None, если скачать не удалось."""
    out, offset = [], 0
    while True:
        q = {"level_id": level_id, "limit": 50, "offset": offset}
        d = get_json(DL + "level/classic/record/list?" + urllib.parse.urlencode(q))
        if d is None:
            return None  # уровень не считаем скачанным, повторим в следующий раз
        data = d.get("data") or {}
        recs = data.get("records") or []
        for r in recs:
            u = r.get("user") or {}
            out.append({"u": u.get("id"), "n": u.get("username"), "v": r.get("video_url")})
        if not recs:
            break
        offset += len(recs)
        if offset >= (data.get("total_count") or 0):
            break
    return out


def load_dl(workers):
    done = load_json(CACHE_DL)
    if done is not None:
        print(f"demonlist: беру готовое из {CACHE_DL}")
        return done

    level_ids = load_json(CACHE_LEVELS)
    if not level_ids:
        print("  скачиваю список уровней...")
        level_ids = fetch_level_ids()
        if not level_ids:
            print("Не удалось получить список уровней. Проверь интернет и запусти снова.")
            sys.exit(1)
        save_json(CACHE_LEVELS, level_ids)

    progress = load_json(PROGRESS_DL, {})  # {"id уровня": [рекорды]}
    todo = [i for i in level_ids if str(i) not in progress]
    print(
        f"  уровней всего: {len(level_ids)}, уже скачано: {len(progress)}, осталось: {len(todo)}"
    )

    for round_no in range(MAX_ROUNDS):
        todo = [i for i in level_ids if str(i) not in progress]
        if not todo:
            break
        if round_no:
            wait = min(120, 15 * round_no)
            print(f"  не скачано уровней: {len(todo)}, жду {wait} с и пробую снова ({round_no}/{MAX_ROUNDS})")
            time.sleep(wait)
        count = 0
        ex = ThreadPoolExecutor(max_workers=workers)
        futures = {ex.submit(fetch_level_records, i): i for i in todo}
        try:
            for fut in as_completed(futures):
                lid = futures[fut]
                recs = fut.result()
                if recs is not None:
                    progress[str(lid)] = recs
                count += 1
                if count % SAVE_EVERY == 0:
                    save_json(PROGRESS_DL, progress)
                    total = sum(len(v) for v in progress.values())
                    print(f"  demonlist: {len(progress)}/{len(level_ids)} уровней, рекордов {total}")
        except KeyboardInterrupt:
            print("\nОстанавливаюсь, сохраняю прогресс (подожди пару секунд)...")
            ex.shutdown(wait=False, cancel_futures=True)
            save_json(PROGRESS_DL, progress)
            print("Сохранено. Запусти скрипт снова, чтобы продолжить.")
            sys.exit(1)
        ex.shutdown()
        save_json(PROGRESS_DL, progress)

    left = [i for i in level_ids if str(i) not in progress]
    if left:
        print(f"Не удалось скачать уровней: {len(left)}. Запусти скрипт ещё раз, он докачает их.")
        sys.exit(1)

    flat = [r for recs in progress.values() for r in recs]
    save_json(CACHE_DL, flat)
    print(f"  demonlist готов: {len(flat)} рекордов")
    return flat


# ---------- pointercrate ----------

def pc_page(after):
    q = {"limit": 100, "after": after, "status": "APPROVED"}
    return get_json_patient(PC + "v1/records/?" + urllib.parse.urlencode(q))


def pc_find_upper():
    """Граница id (кратная PC_SPAN), после которой записей нет. None, если сервер не ответил."""
    hi = PC_SPAN
    while True:  # расширяем вверх, пока после hi ещё что-то есть
        d = pc_page(hi)
        if d is None:
            return None
        if not d:
            break
        hi *= 2
    lo = 0
    while hi - lo > PC_SPAN:  # бинарный поиск с точностью до одного куска
        mid = (lo + hi) // 2 // PC_SPAN * PC_SPAN
        if mid <= lo:
            break
        d = pc_page(mid)
        if d is None:
            return None
        if d:
            lo = mid
        else:
            hi = mid
    return hi


def pc_chunk(lo):
    """Все одобренные рекорды с id в (lo, lo + PC_SPAN]. None, если скачать не удалось."""
    hi = lo + PC_SPAN
    out, after = [], lo
    while True:
        d = pc_page(after)
        if d is None:
            return None
        if not isinstance(d, list) or not d:
            break
        last = max(int(r.get("id") or 0) for r in d)
        for r in d:
            if int(r.get("id") or 0) <= hi:
                p = r.get("player") or {}
                level = (r.get("demon") or {}).get("name")  # название уровня (в старых кэшах его нет)
                out.append({"p": p.get("id"), "n": p.get("name"), "v": r.get("video"), "l": level})
        if len(d) < 100 or last >= hi or last <= after:
            break
        after = last
    return out


def load_pc(workers=WORKERS):
    done = load_json(CACHE_PC)
    if done is not None:
        print(f"pointercrate: беру готовое из {CACHE_PC}")
        return done

    state = load_json(PROGRESS_PC, {})
    if state.get("span") != PC_SPAN or "chunks" not in state:
        if state:
            print("  старый прогресс Pointercrate другого формата, качаю заново")
        state = {"span": PC_SPAN, "chunks": {}}
    chunks = state["chunks"]  # {"lo": [рекорды]}, последний кусок сюда не пишем (он может расти)

    print("  ищу, сколько записей на Pointercrate...")
    upper = pc_find_upper()
    if upper is None:
        print("Pointercrate так и не ответил. Запусти скрипт позже.")
        sys.exit(1)
    los = list(range(0, upper, PC_SPAN))
    last_lo = los[-1]
    todo = [lo for lo in los if str(lo) not in chunks]
    print(f"  кусков всего: {len(los)}, уже скачано: {len(los) - len(todo)}, осталось: {len(todo)}")

    lock = threading.Lock()
    results = {int(k): v for k, v in chunks.items()}
    failed = count = 0
    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {ex.submit(pc_chunk, lo): lo for lo in todo}
    try:
        for fut in as_completed(futures):
            lo = futures[fut]
            recs = fut.result()
            with lock:
                if recs is None:
                    failed += 1
                else:
                    results[lo] = recs
                    if lo != last_lo:
                        chunks[str(lo)] = recs
                count += 1
                if count % 3 == 0:
                    save_json(PROGRESS_PC, state)
                    total = sum(len(v) for v in results.values())
                    print(f"  pointercrate: кусков {len(results)}/{len(los)}, рекордов {total}")
    except KeyboardInterrupt:
        print("\nОстанавливаюсь, сохраняю прогресс (подожди пару секунд)...")
        ex.shutdown(wait=False, cancel_futures=True)
        save_json(PROGRESS_PC, state)
        print("Сохранено. Запусти скрипт снова, чтобы продолжить.")
        sys.exit(1)
    ex.shutdown()
    save_json(PROGRESS_PC, state)

    if failed:
        print(f"Pointercrate: не скачано кусков {failed}. Прогресс сохранён, запусти скрипт ещё раз.")
        sys.exit(1)

    out = [r for lo in los for r in results[lo]]
    save_json(CACHE_PC, out)
    print(f"  pointercrate готов: {len(out)} рекордов")
    return out


def fetch_pc_players():
    """Все игроки Pointercrate: {id: ник}. В списке рекордов есть только те, у кого есть
    одобренные рекорды, а здесь все, в том числе без рекордов и забаненные.
    Второй проход с banned=true на случай, если сервер по умолчанию забаненных не отдаёт.
    None, если сервер не ответил."""
    players = {}
    for extra in ({}, {"banned": "true"}):
        after = 0
        while True:
            q = {"limit": 100, "after": after, **extra}
            d = get_json_patient(PC + "v1/players/?" + urllib.parse.urlencode(q))
            if d is None:
                return None
            if not isinstance(d, list) or not d:
                break
            for p in d:
                if p.get("id") is not None:
                    players.setdefault(p["id"], p.get("name"))
            last = max(int(p.get("id") or 0) for p in d)
            if len(d) < 100 or last <= after:
                break
            after = last
    return players


def load_pc_players():
    done = load_json(CACHE_PC_PLAYERS)
    if done is not None:
        print(f"игроки pointercrate: беру готовое из {CACHE_PC_PLAYERS}")
        return {int(k): v for k, v in done.items()}
    print("  скачиваю список всех игроков Pointercrate...")
    players = fetch_pc_players()
    if players is None:
        print("Pointercrate не отдал список игроков. Запусти скрипт позже.")
        sys.exit(1)
    save_json(CACHE_PC_PLAYERS, {str(k): v for k, v in players.items()})
    print(f"  игроков Pointercrate: {len(players)}")
    return players


# ---------- каналы игроков ----------

CACHE_CH = "cache_channels.json"  # {"yt:ID видео": "ссылка на канал", "ytid:ID видео": "UC..."}, чтобы не спрашивать дважды
MAX_CHECKS = 3  # сколько видео игрока проверять на YouTube и на Bilibili (больше = дольше)

# Сайты, где имя канала уже лежит прямо в ссылке на видео. Запросы не нужны.
# (платформа, как найти имя в ссылке, как собрать ссылку на канал)
IN_URL = [
    ("twitch", re.compile(r"twitch\.tv/([A-Za-z0-9_]{3,25})/(?:clip|v)/", re.I), "https://www.twitch.tv/{}"),
    ("x", re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/", re.I), "https://x.com/{}"),
    ("tiktok", re.compile(r"tiktok\.com/@([\w.]+)/video/", re.I), "https://www.tiktok.com/@{}"),
]
# Мягкий вариант шаблона YouTube только для поиска каналов: понимает и кривые ссылки,
# которые люди вписывают на demonlist (youtu.be/watch?v=ID, youtube.be, yotu.be).
YT_LOOSE = re.compile(
    r"(?:(?:youtu\.be|youtube\.be|yotu\.be)/(?:watch\?(?:.*&)?v=)?"
    r"|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/))"
    r"([A-Za-z0-9_-]{11})"
)


def yt_video_id(url):
    """ID видео YouTube из ссылки (в том числе кривой) или из голого ID. Иначе None."""
    m = YT_LOOSE.search(url)
    if m:
        return m.group(1)
    u = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", u):  # в поле записан только ID, без ссылки
        return u
    return None


BILI = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]{10})", re.I)
BILI_AV = re.compile(r"bilibili\.com/video/av(\d+)", re.I)  # старый формат номера видео
# Короткие ссылки: сами по себе ничего не говорят, надо пройти по перенаправлению.
SHORT = re.compile(
    r"(?://|^)(?:www\.)?(?:b23\.tv|bili\.im|vt\.tiktok\.com|vm\.tiktok\.com)/|tiktok\.com/t/", re.I
)
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"


def bili_video_id(url):
    """'BV...' или 'av123' из ссылки на видео Bilibili. Иначе None."""
    m = BILI.search(url)
    if m:
        return m.group(1)
    m = BILI_AV.search(url)
    if m:
        return "av" + m.group(1)
    return None


PLATFORM = {"yt": "youtube", "bili": "bilibili"}  # префикс ключа кэша -> название в links.json


def ask_channel(url, pick, page=False):
    """Спрашивает сайт про одно видео. pick(данные) достаёт из ответа ссылку на канал.
    page=False: ответ это JSON. page=True: ответ это обычная веб-страница (текст).
    Возвращает: результат pick; "" если видео удалено или закрыто (повторять нет смысла);
    None если сайт сейчас не ответил (попробуем при следующем запуске)."""
    for i in range(3):
        try:
            # для страницы представляемся браузером, иначе YouTube может отдать урезанную
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36" if page else "links-builder/1.0"
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if page:
                    # Читаем страницу кусками по 64 КБ и останавливаемся, как только pick
                    # нашёл ответ. Так не качаем всю страницу (она весит около мегабайта).
                    buf = b""
                    while len(buf) < 1_500_000:
                        part = r.read(65536)
                        if not part:
                            break
                        buf += part
                        if pick(buf.decode("utf-8", "replace")):
                            break
                    data = buf.decode("utf-8", "replace")
                else:
                    data = json.load(r)
            time.sleep(PAUSE)
            return pick(data) or ""
        except urllib.error.HTTPError as e:
            if e.code in (412, 429) or e.code >= 500:  # перегрузка или защита от ботов
                time.sleep(2 ** i)
                continue
            return ""  # 401/403/404: видео нет
        except Exception:
            time.sleep(2 ** i)
    return None


def yt_channel(video):
    """YouTube отдаёт автора видео через oEmbed, ключ API не нужен."""
    url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {"url": "https://youtu.be/" + video, "format": "json"}
    )
    return ask_channel(url, lambda d: d.get("author_url"))


# Номер канала YouTube (UC + 22 символа) в коде страницы видео. Два места на случай,
# если YouTube поменяет одно из них.
YT_ID_PATTERNS = [
    re.compile(r'itemprop="channelId"\s+content="(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
]


def yt_id(video):
    """Постоянный номер канала (UC...). Он не меняется, даже если канал сменил имя."""
    def pick(html):
        for rx in YT_ID_PATTERNS:
            m = rx.search(html)
            if m:
                return m.group(1)
        return None

    res = ask_channel("https://www.youtube.com/watch?v=" + video, pick, page=True)
    # Это видео уже подтвердилось как живое (через oEmbed), поэтому "не нашёл номер" значит
    # сбой, а не "видео удалено": не запоминаем пустоту, спросим снова в следующий раз.
    return res or None


def resolve_short(url):
    """Проходит по перенаправлению короткой ссылки и возвращает итоговый адрес.
    "" если ссылка мёртвая, None если сервер не ответил (спросим в следующий раз)."""
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                final = r.geturl()  # адрес, на котором мы оказались после всех перенаправлений
            time.sleep(PAUSE)
            return final
        except urllib.error.HTTPError as e:
            # сайт мог перенаправить и уже потом ответить ошибкой, адрес всё равно полезен
            final = e.geturl() or ""
            if final and final != url:
                return final
            if e.code in (412, 429) or e.code >= 500:
                time.sleep(2 ** i)
                continue
            return ""
        except Exception:
            time.sleep(2 ** i)
    return None


def bili_channel(vid):
    """vid: 'BV...' или 'av123'. Возвращает ссылку на канал автора."""
    def pick(d):
        owner = (d.get("data") or {}).get("owner") or {}
        return f"https://space.bilibili.com/{owner['mid']}" if owner.get("mid") else None

    query = "aid=" + vid[2:] if vid.lower().startswith("av") else "bvid=" + vid
    return ask_channel("https://api.bilibili.com/x/web-interface/view?" + query, pick)


def offline_channel(url):
    """Канал из самой ссылки (Twitch, X, TikTok). Возвращает (платформа, ссылка) или None."""
    for platform, rx, template in IN_URL:
        m = rx.search(url)
        if m:
            return platform, template.format(m.group(1).lower())
    return None


def run_jobs(todo, cache, workers):
    """Выполняет запросы параллельно и складывает ответы в cache (и в файл CACHE_CH).
    todo: {ключ кэша: (функция, аргумент)}."""
    if not todo:
        return
    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {ex.submit(fn, arg): ck for ck, (fn, arg) in todo.items()}
    try:
        for n, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if res is not None:  # None = сайт не ответил, спросим в следующий раз
                cache[futures[fut]] = res
            if n % 50 == 0:
                save_json(CACHE_CH, cache)
                print(f"  каналы: {n}/{len(todo)}")
    except KeyboardInterrupt:
        print("\nОстанавливаюсь, сохраняю прогресс (подожди пару секунд)...")
        ex.shutdown(wait=False, cancel_futures=True)
        save_json(CACHE_CH, cache)
        print("Сохранено. Запусти скрипт снова, чтобы продолжить.")
        sys.exit(1)
    ex.shutdown()
    save_json(CACHE_CH, cache)


def add_channels(result, dl, pc, workers):
    """Для каждого игрока из result находит каналы по его видео и дописывает в
    result[id]["channels"] = {"youtube": [...], "twitch": [...], ...}.
    Уже записанное (в том числе твои ручные правки) не трогает, только дополняет."""
    # 1. Все видео каждого игрока: с demonlist (по его id) и с его аккаунтов Pointercrate
    dl_vids, pc_vids = defaultdict(list), defaultdict(list)
    for r in dl:
        if r["u"] is not None and r["v"]:
            dl_vids[str(r["u"])].append(str(r["v"]))
    for r in pc:
        if r["p"] is not None and r["v"]:
            pc_vids[r["p"]].append(str(r["v"]))

    cache = load_json(CACHE_CH, {}) or {}
    found = {}   # id игрока -> счётчик {(платформа, ссылка на канал): сколько видео}
    wanted = {}  # id игрока -> ключи кэша, которые нужны для его видео
    todo = {}    # ключ кэша -> (функция, аргумент): то, чего ещё нет в кэше
    yt_videos = set()  # id всех YouTube-видео, которые проверяем

    # 2. Разбираем ссылки: что видно сразу, а про что надо спросить сайт
    for key, entry in result.items():
        urls = list(dl_vids.get(key, []))
        for p in entry.get("pointercrate", []):
            urls += pc_vids.get(p, [])
        urls = list(dict.fromkeys(urls))  # убрали повторы, порядок сохранили

        found[key], wanted[key] = Counter(), []
        checks = Counter()  # сколько видео этого игрока уже отправлено на проверку
        for u in urls:
            hit = offline_channel(u)
            if hit:
                found[key][hit] += 1
                continue
            yid = yt_video_id(u)
            if yid:
                prefix, fn, arg = "yt", yt_channel, yid
            else:
                bid = bili_video_id(u)
                if bid:
                    prefix, fn, arg = "bili", bili_channel, bid
                elif SHORT.search(u):
                    prefix, fn, arg = "short", resolve_short, u
                else:
                    continue
            if checks[prefix] >= MAX_CHECKS:
                continue
            checks[prefix] += 1
            if prefix == "yt":
                yt_videos.add(arg)
            ck = f"{prefix}:{arg}"
            wanted[key].append(ck)
            if ck not in cache:
                todo[ck] = (fn, arg)

    # 3. Спрашиваем сайты (параллельно). Прогресс сохраняем, как и в остальном скрипте.
    print(f"  видео для проверки: {len(todo)} (остальное уже в {CACHE_CH})")
    run_jobs(todo, cache, workers)

    # 3а. Короткие ссылки раскрылись. Если за ними видео Bilibili, спрашиваем и про него.
    todo_b = {}
    for cks in wanted.values():
        for ck in cks:
            if ck.startswith("short:"):
                bid = bili_video_id(cache.get(ck) or "")
                if bid and f"bili:{bid}" not in cache:
                    todo_b[f"bili:{bid}"] = (bili_channel, bid)
    run_jobs(todo_b, cache, workers)

    # 3б. YouTube, второй проход: постоянный номер канала. Только для видео, которые
    # живы (на первом проходе нашёлся канал) и номер которых мы ещё не узнали.
    todo_id = {
        f"ytid:{v}": (yt_id, v)
        for v in yt_videos
        if cache.get(f"yt:{v}") and f"ytid:{v}" not in cache
    }
    print(f"  YouTube, номеров каналов для проверки: {len(todo_id)}")
    if todo_id and "--no-yt-id" in sys.argv:
        print("  пропускаю (флаг --no-yt-id)")
        todo_id = {}
    if todo_id:
        # Пробуем 5 видео. Если YouTube не отдал номер ни по одному, остальное бессмысленно:
        # иначе потратим часы на запросы, которые все закончатся ничем.
        got = 0
        for ck, (fn, arg) in list(todo_id.items())[:5]:
            res = fn(arg)
            if res:
                cache[ck] = res
                got += 1
                del todo_id[ck]
        save_json(CACHE_CH, cache)
        if got == 0:
            print("  YouTube не отдал номер канала ни по одному из 5 пробных видео.")
            print("  Второй проход пропускаю, чтобы не тратить время. Это не поломка скрипта,")
            print("  сайт отвечает иначе, чем ожидалось (нужна диагностика, см. переписку).")
            todo_id = {}
    run_jobs(todo_id, cache, workers)

    # 4. Собираем ответы и дописываем в result
    added = 0
    for key, entry in result.items():
        for ck in wanted[key]:
            prefix, arg = ck.split(":", 1)
            if prefix == "short":  # итоговый адрес короткой ссылки: TikTok с именем или видео Bilibili
                final = cache.get(ck) or ""
                hit = offline_channel(final)
                bid = bili_video_id(final)
                if hit:
                    found[key][hit] += 1
                elif bid and cache.get(f"bili:{bid}"):
                    found[key][("bilibili", cache[f"bili:{bid}"])] += 1
                continue
            link = cache.get(ck)
            if link:
                found[key][(PLATFORM[prefix], link)] += 1
            if prefix == "yt" and cache.get(f"ytid:{arg}"):
                found[key][("youtube_id", cache[f"ytid:{arg}"])] += 1
        if not found[key]:
            continue
        channels = entry.setdefault("channels", {})
        for (platform, link), _ in found[key].most_common():  # чаще встречающиеся первыми
            lst = channels.setdefault(platform, [])
            if link not in lst:
                lst.append(link)
                added += 1
    return added


OTHER_SITES = "links_other_sites.txt"  # полный список непонятных ссылок
LEVEL_NAMES = "cache_level_names.json"  # {id уровня: название}, чтобы не спрашивать дважды


def site_of(url):
    """Название сайта из ссылки (без www.), например 'vk.com'. None, если это не ссылка."""
    u = str(url or "").strip()
    if not u:
        return None
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    try:
        host = (urllib.parse.urlsplit(u).hostname or "").lower()
    except ValueError:
        return None
    for pre in ("www.", "m.", "mobile."):
        if host.startswith(pre):
            host = host[len(pre):]
    return host or None


def link_kind(url, cache):
    """В какую группу отчёта попадает ссылка. None, если скрипт её понял и всё в порядке.
    Возвращает (раздел, порядок, название группы).
    Раздел 1: нерабочие (человек их тоже не откроет). Раздел 2: человеку открыть можно."""
    u = url.strip()
    if re.search(r"https?://.*https?://", u, re.I):
        return 1, 1, "Ссылка внутри другой ссылки (так её не открыть, скорее всего ошибка при вводе)"
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", u):
        return 2, 1, "Голый ID без ссылки (похоже на ID видео YouTube, 11 символов)"

    try:
        sp = urllib.parse.urlsplit(u if re.match(r"^https?://", u, re.I) else "https://" + u)
    except ValueError:
        return 1, 3, "Не похоже на ссылку (нет адреса сайта)"
    host = site_of(u)
    if not host or "." not in host:
        return 1, 3, "Не похоже на ссылку (нет адреса сайта)"
    if sp.path in ("", "/") and not sp.query and not sp.fragment:
        return 1, 2, "Пустая ссылка: есть только адрес сайта, самого видео нет"

    if SHORT.search(u):
        final = cache.get("short:" + url)  # итоговый адрес, если мы по ней уже ходили
        if final is not None and final.rstrip("/").split("://")[-1] in ("", u.rstrip("/").split("://")[-1]):
            return 1, 0, "Мёртвая короткая ссылка (сервер ничего не вернул или ответил «не найдено»)"

    yid = yt_video_id(u)
    if yid and not YT.search(u):  # скрипт прочитал, но запись с ошибкой
        return 2, 0, "Запись с ошибкой в адресе (youtube.be, yotu.be, youtu.be/watch?v=...): открыть можно, скрипт прочитал, на demonlist стоит поправить"
    if offline_channel(u) or yid or bili_video_id(u) or SHORT.search(u):
        return None
    return 2, 2, "Сайт: " + host


def levels_by_video():
    """{ссылка на видео: [id уровней]}. Берём из cache_dl_progress.json, где рекорды
    лежат по уровням (в итоговом cache_dl_records.json уровень уже потерян)."""
    progress = load_json(PROGRESS_DL, {}) or {}
    out = defaultdict(list)
    for lid, recs in progress.items():
        for r in recs:
            if r.get("v"):
                out[str(r["v"])].append(int(lid) if str(lid).isdigit() else lid)
    return out


def fetch_level_names():
    """Названия уровней demonlist: {id: название}. Список уровней листаем так же, как в
    fetch_level_ids. Если у сервера в ответе нет поля name, вернётся пустой словарь."""
    names = {}
    for list_type in [None, "main", "extended", "advanced", "unbounded"]:
        offset = 0
        while True:
            q = {"limit": 50, "offset": offset}
            if list_type:
                q["list_type"] = list_type
            d = get_json(DL + "level/classic/list?" + urllib.parse.urlencode(q))
            batch = ((d or {}).get("data") or {}).get("levels") or []
            new = [lv for lv in batch if lv["id"] not in names]
            for lv in batch:
                if lv.get("name"):
                    names.setdefault(lv["id"], lv["name"])
            if not batch or not new:
                break
            offset += len(batch)
    return names


def level_label(lids, names):
    """Список уровней одной строкой: 'Имя (id 5), Имя2 (id 9), ещё 2'."""
    parts = [f"{names[l]} (id {l})" if l in names else f"id {l}" for l in lids[:3]]
    if len(lids) > 3:
        parts.append(f"ещё {len(lids) - 3}")
    return ", ".join(parts)


SECTIONS = {
    1: "НЕРАБОЧИЕ ссылки: человек их тоже не откроет",
    2: "Человеку открыть можно, но запись кривая или скрипт канал из неё не берёт",
}


def report_other_sites(dl, pc, workers=WORKERS):
    """Записывает в OTHER_SITES все ссылки, с которыми что-то не так. Сверху нерабочие,
    ниже те, что человек откроет, а скрипт канал из них не берёт. У каждой записи игрок,
    его id, уровень и откуда запись. В консоль выводится краткая сводка."""
    cache = load_json(CACHE_CH, {}) or {}  # для коротких ссылок: что ответил сервер
    # Короткие ссылки игроков, которых нет в links.json, мы ещё не раскрывали. Раскрываем
    # их здесь, иначе не узнаем, какие из них мёртвые (их немного, это быстро).
    shorts = {}
    for r in list(dl) + list(pc):
        url = str(r.get("v") or "")
        if SHORT.search(url) and f"short:{url}" not in cache:
            shorts[f"short:{url}"] = (resolve_short, url)
    if shorts:
        print(f"Раскрываю короткие ссылки ({len(shorts)}), чтобы найти мёртвые...")
        run_jobs(shorts, cache, workers)
    by_video = levels_by_video()  # для demonlist: на каких уровнях лежит это видео
    items, seen = [], set()
    rows = [("demonlist", r.get("u"), r.get("n"), r.get("v"), r) for r in dl]
    rows += [("pointercrate", r.get("p"), r.get("n"), r.get("v"), r) for r in pc]
    for site, pid, name, url, r in rows:
        if not url:
            continue
        url = str(url)
        kind = link_kind(url, cache)
        if kind is None:
            continue  # скрипт эту ссылку понял, с ней всё в порядке
        if (site, pid, url) in seen:
            continue  # одна и та же запись два раза
        seen.add((site, pid, url))
        items.append((kind, site, pid, name, url, r))

    print()
    if not items:
        print("Непонятных ссылок нет.")
        return

    # названия нужны только тем уровням, которые встретились в этих записях
    needed = {l for it in items if it[1] == "demonlist" for l in by_video.get(it[4], [])}
    names = load_json(LEVEL_NAMES)
    if names is None and needed:
        print("Скачиваю названия уровней (один раз)...")
        names = fetch_level_names()
        save_json(LEVEL_NAMES, {str(k): v for k, v in names.items()})
        if not names:
            print("  сервер не отдал названия, в файле будут только id уровней")
    names = {int(k) if str(k).isdigit() else k: v for k, v in (names or {}).items()}

    groups = defaultdict(list)  # (раздел, порядок, название) -> [(id игрока, строка)]
    for kind, site, pid, name, url, r in items:
        where = site + (f", из {r['src']}" if r.get("src") else "")
        if site == "pointercrate":
            level = r.get("l") or "неизвестен"
        else:
            lids = by_video.get(url, [])
            if not lids and r.get("lvl") is not None:
                lids = [r["lvl"]]  # верифер: уровень известен сразу
            level = level_label(lids, names) if lids else "неизвестен"
        line = f"{where} | id {pid} | {name} | уровень: {level} | {url}"
        if r.get("info"):  # лишние поля из cache_dl_users.json
            line += " | " + json.dumps(r["info"], ensure_ascii=False)[:150]
        groups[kind].append((str(pid), line))

    # раздел 1 выше раздела 2; внутри раздела по порядку, потом сайты по числу ссылок
    ordered = sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], -len(kv[1]), kv[0][2]))
    count = {sec: sum(len(v) for k, v in groups.items() if k[0] == sec) for sec in SECTIONS}

    print(f"Ссылок с проблемами: {len(items)}. Подробности по каждой записи в {OTHER_SITES}")
    shown, last = 0, None
    for (sec, _, title), lines in ordered:
        if sec != last:
            print(f"  {SECTIONS[sec]}: {count[sec]}")
            last = sec
        if shown < 20:
            print(f"    {len(lines):6d}  {title}")
            shown += 1
    if len(ordered) > shown:
        print(f"    ... и ещё {len(ordered) - shown} групп")

    with open(OTHER_SITES, "w", encoding="utf-8") as f:
        f.write(f"Ссылок с проблемами: {len(items)}\n")
        f.write("Формат строки: сайт | id игрока | ник | уровень | то, что записано в поле видео\n")
        last = None
        for (sec, _, title), lines in ordered:
            if sec != last:
                f.write(f"\n########## {SECTIONS[sec]} ({count[sec]}) ##########\n")
                last = sec
            f.write(f"\n=== {title} ({len(lines)}) ===\n")
            for _, line in sorted(lines, key=lambda x: x[0]):
                f.write(line + "\n")


# ---------- склейка по общим каналам ----------

def norm_channel(link):
    return str(link).strip().rstrip("/").lower()


def merge_by_channels(result):
    """Склеивает записи, у которых найден один и тот же канал.
    - одна запись глобала + записи pc...: всё уходит в запись глобала;
    - только записи pc...: всё уходит в запись с наименьшим id;
    - две и больше записей глобала с общим каналом: НЕ склеиваем, пишем в CHANNEL_CONFLICTS;
    - больше MAX_MERGE_ENTRIES записей в одной группе: НЕ склеиваем, пишем туда же.
    Всё, что склеено, дописывается в MERGES_LOG. Возвращает (поглощено записей, групп,
    общих каналов у аккаунтов глобала)."""
    ignore = {norm_channel(x) for x in (load_json(CHANNELS_IGNORE, []) or [])}
    names = {k: str(e.get("_name") or "") for k, e in result.items()}

    def is_global(k):
        return not str(k).startswith("pc")

    def order(k):  # сначала записи глобала, потом pc...; внутри по номеру
        digits = str(k) if is_global(k) else str(k)[2:]
        return (not is_global(k), int(digits) if digits.isdigit() else 0, str(k))

    # канал -> какие записи его содержат
    by_channel, shown = defaultdict(set), {}
    for key, entry in result.items():
        for platform, links in (entry.get("channels") or {}).items():
            for link in links:
                n = norm_channel(link)
                if n in ignore:
                    continue
                by_channel[(platform, n)].add(key)
                shown.setdefault((platform, n), link)

    parent = {}  # для группировки: у кого общий канал, те в одной группе

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    global_overlaps, eligible = [], []
    for ch, keys in by_channel.items():
        if len(keys) < 2:
            continue
        ks = sorted(keys, key=order)
        if sum(is_global(k) for k in ks) >= 2:
            global_overlaps.append((ch, ks))
            continue
        eligible.append((ch, ks))
        for k in ks[1:]:
            parent[find(k)] = find(ks[0])

    comps, comp_ch = defaultdict(list), defaultdict(list)
    for k in list(parent):
        comps[find(k)].append(k)
    for ch, ks in eligible:
        comp_ch[find(ks[0])].append(shown[ch])

    moved, merged_log, chains, too_big = {}, [], [], []
    absorbed = 0
    for root, keys in sorted(comps.items(), key=lambda kv: order(min(kv[1], key=order))):
        keys.sort(key=order)
        chans = sorted(set(comp_ch[root]))
        if sum(is_global(k) for k in keys) >= 2:
            chains.append((keys, chans))  # два глобала связаны через запись pc...
            continue
        if len(keys) > MAX_MERGE_ENTRIES:
            too_big.append((keys, chans))
            continue
        primary = keys[0]  # запись глобала, а если её нет, pc... с наименьшим id
        main = result[primary]
        lines = [f"СКЛЕЕНО В {primary} ({names[primary]})", "    общие каналы: " + ", ".join(chans)]
        for k in keys[1:]:
            other = result.pop(k)
            cur = main.setdefault("pointercrate", [])
            for pid in other.get("pointercrate", []):
                if pid not in cur:
                    cur.append(pid)
            for platform, links in (other.get("channels") or {}).items():
                lst = main.setdefault("channels", {}).setdefault(platform, [])
                for link in links:
                    if link not in lst:
                        lst.append(link)
            moved[k] = primary
            absorbed += 1
            lines.append(f"    поглощено: {k} ({names[k]}), pointercrate: {other.get('pointercrate', [])}")
        merged_log.append("\n".join(lines))

    if merged_log:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        with open(MERGES_LOG, "a", encoding="utf-8") as f:
            f.write(f"===== {stamp}: склеено групп {len(merged_log)}, поглощено записей {absorbed} =====\n")
            f.write("\n\n".join(merged_log) + "\n\n")

    def who(keys):  # записи, поглощённые выше, показываем под тем ключом, куда они ушли
        out = []
        for k in keys:
            k = moved.get(k, k)
            if k not in out:
                out.append(k)
        return ", ".join(f"{k} ({names.get(k, '')})" for k in out)

    with open(CHANNEL_CONFLICTS, "w", encoding="utf-8") as f:
        f.write("Общие каналы, которые скрипт НЕ склеил. Файл пишется заново при каждом запуске.\n")
        f.write(f"Если канал общий, но люди разные (командный канал), впиши его ссылку в {CHANNELS_IGNORE}\n")
        f.write("(список строк в кавычках), и такой канал больше не будет склеивать записи.\n")
        f.write("Уже склеенные записи это не разделит, их надо разделить в links.json вручную.\n")
        f.write(f"\n########## ГЛОБАЛ: один канал у нескольких аккаунтов глобала ({len(global_overlaps)}) ##########\n")
        f.write("Склейка аккаунтов глобала делается через fix.json (merges).\n")
        for (platform, _), ks in sorted(global_overlaps, key=lambda x: order(x[1][0])):
            f.write(f"\n{shown[(platform, _)]}  [{platform}]\n    {who(ks)}\n")
        if chains:
            f.write(f"\n########## Два аккаунта глобала связаны через запись pc..., не склеены ({len(chains)}) ##########\n")
            for keys, chans in chains:
                f.write(f"\n{who(keys)}\n    каналы: {', '.join(chans)}\n")
        if too_big:
            f.write(f"\n########## Слишком много записей с общим каналом (больше {MAX_MERGE_ENTRIES}), не склеены ({len(too_big)}) ##########\n")
            for keys, chans in too_big:
                f.write(f"\n{who(keys)}\n    каналы: {', '.join(chans)}\n")
        if not (global_overlaps or chains or too_big):
            f.write("\nКонфликтов нет.\n")
    return absorbed, len(merged_log), len(global_overlaps)


# ---------- основная логика ----------

def main():
    workers = WORKERS
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a.startswith("--workers="):
            workers = max(1, int(a.split("=", 1)[1]))
        elif a in ("--workers", "-w") and i + 1 < len(argv) and argv[i + 1].isdigit():
            workers = max(1, int(argv[i + 1]))
    if "--refresh" in sys.argv:
        for p in (CACHE_LEVELS, CACHE_LEVEL_INFO, PROGRESS_DL, PROGRESS_PC, CACHE_DL, CACHE_PC,
                  CACHE_PC_PLAYERS):
            if os.path.exists(p):
                os.remove(p)
        print("Старые данные удалены, качаю заново.")

    print("1/3 pointercrate")
    pc = load_pc(workers)
    pc_players = load_pc_players()
    print("2/3 demonlist")
    dl = load_dl(workers)
    dl = merge_user_cache(dl)
    dl = merge_verifiers(dl)
    dl = apply_fix(dl, load_fix())

    print("3/3 сопоставление")
    dl_by_vid = defaultdict(dict)  # видео -> {id игрока: ник}
    pc_by_vid = defaultdict(dict)
    skipped = 0
    for r in dl:
        v = video_id(r["v"])
        if v and r["u"] is not None:
            cur = dl_by_vid[v]
            if r["n"] or r["u"] not in cur:  # пустым ником хороший не затираем
                cur[r["u"]] = r["n"]
        else:
            skipped += 1
    for r in pc:
        v = video_id(r["v"])
        if v and r["p"] is not None:
            pc_by_vid[v][r["p"]] = r["n"]
        else:
            skipped += 1

    # пара (id demonlist, id pointercrate) -> общие видео
    pairs = defaultdict(set)
    dl_names, pc_names = {}, {}
    for v in set(dl_by_vid) & set(pc_by_vid):
        for uid, un in dl_by_vid[v].items():
            for pid, pn in pc_by_vid[v].items():
                pairs[(uid, pid)].add(v)
                dl_names[uid], pc_names[pid] = un, pn

    # один аккаунт Pointercrate не может принадлежать двум людям с demonlist
    pc_to_dl = defaultdict(set)
    for uid, pid in pairs:
        pc_to_dl[pid].add(uid)
    conflicts = {pid for pid, uids in pc_to_dl.items() if len(uids) > 1}

    links = defaultdict(list)
    review = []
    for (uid, pid), vids in sorted(pairs.items()):
        if pid in conflicts:
            continue  # конфликты выпишем ниже одним блоком на аккаунт
        links[uid].append(pid)

    # КОНФЛИКТЫ: один аккаунт Pointercrate -> несколько игроков demonlist
    # Видео, которые у нескольких аккаунтов demonlist сразу, уже выписаны в блоках
    # "ДУБЛЬ В DEMONLIST", поэтому здесь их не повторяем.
    dup_vids = {v for v, users in dl_by_vid.items() if len(users) > 1}
    for pid in sorted(conflicts):
        block = [
            f"pointercrate {pid} ({pc_names[pid]}) совпал с несколькими "
            f"игроками demonlist:"
        ]
        shown = 0
        for u in sorted(pc_to_dl[pid]):
            all_vids = pairs[(u, pid)]
            vids = all_vids - dup_vids
            if not vids:
                continue  # все видео игрока уже в блоках дублей, не выводим его
            shown += 1
            hidden = len(all_vids) - len(vids)
            line = f"    demonlist {u} ({dl_names[u]}), общих видео: {len(vids)}"
            if hidden:
                line += f" (ещё {hidden} в прошлом файле)"
            block.append(line)
            block.append(vurls(vids))
        if shown == 0:
            continue  # выводить нечего, весь конфликт уже в блоках дублей
        review.append("\n".join(block))

    # Несколько аккаунтов у одного человека: редкость, а больше 3 почти наверняка ошибка
    MAX_ACCOUNTS = 3
    for uid in list(links):
        pids = sorted(set(links[uid]))
        too_many = len(pids) > MAX_ACCOUNTS
        if too_many or len(pids) > 1:
            title = (
                f"СЛИШКОМ МНОГО АККАУНТОВ ({len(pids)}), НЕ записано: "
                if too_many
                else f"ПРОВЕРЬ, несколько аккаунтов ({len(pids)}), записано: "
            )
            block = [f"{title}demonlist {uid} ({dl_names[uid]})"]
            for p in pids:
                vids = pairs[(uid, p)]
                block.append(f"    pointercrate {p} ({pc_names[p]}), общих видео: {len(vids)}")
                block.append(vurls(vids))
            review.append("\n".join(block))
        if too_many:
            del links[uid]

    # Разные аккаунты внутри demonlist с одним и тем же видео (Pointercrate не нужен).
    # Одно видео у двух аккаунтов бывает и законно (командные прохождения,
    # чужое видео), поэтому такие пары только выписываем, не склеиваем.
    dl_all_names = {}
    for users in dl_by_vid.values():
        dl_all_names.update(users)
    dl_pairs = defaultdict(set)
    for v, users in dl_by_vid.items():
        if len(users) > 1:
            ids = sorted(users)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    dl_pairs[(ids[i], ids[j])].add(v)
    for (a, b), vids in sorted(dl_pairs.items()):
        review.append(
            f"{a} ({dl_all_names[a]}) и "
            f"{b} ({dl_all_names[b]}), общих видео: {len(vids)}\n"
            + vurls(vids)
        )

    # Дописываем в существующий links.json, а не перезаписываем его.
    # Старые записи (и твои ручные правки) остаются как есть и в том же порядке,
    # новые игроки добавляются в конец, новые аккаунты Pointercrate у уже
    # существующих игроков дописываются в конец их списка.
    result = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                result = json.load(f)
        except Exception:
            print(f"Не могу прочитать {OUT}, чтобы не затереть его, останавливаюсь.")
            print("Проверь файл (или переименуй его) и запусти снова.")
            sys.exit(1)

    # какие аккаунты Pointercrate уже закреплены за кем-то
    pc_owner = {}
    for k, v in result.items():
        for p in v.get("pointercrate", []):
            pc_owner[p] = k

    added_players = added_links = taken = 0
    taken_list = []  # сюда записываем подробности о каждом пропущенном случае
    for uid, pids in sorted(links.items()):
        key = str(uid)
        entry = result.get(key)
        is_new = entry is None
        if is_new:
            entry = {"_name": str(dl_names[uid] or ""), "pointercrate": []}
            result[key] = entry
        cur = entry.setdefault("pointercrate", [])
        for p in sorted(set(pids)):
            if p in cur:
                continue
            owner = pc_owner.get(p, key)  # кто уже владеет этим аккаунтом
            if owner != key:
                taken += 1  # этот аккаунт Pointercrate уже записан у другого игрока
                owner_name = result.get(owner, {}).get("_name", "")
                taken_list.append(
                    f"УЖЕ ЗАНЯТ: pointercrate {p} ({pc_names.get(p)}) "
                    f"записан у demonlist {owner} ({owner_name}), "
                    f"а скрипт хотел дать его demonlist {uid} ({dl_names[uid]})"
                )
                continue
            cur.append(p)
            pc_owner[p] = key
            added_links += 1
        if is_new:
            if cur:
                added_players += 1
            else:  # ничего не добавилось, пустую запись не создаём
                del result[key]

    # ---- Все остальные аккаунты ----
    # 1) Каждый аккаунт глобала получает запись, даже если Pointercrate у него нет.
    dl_all = {}  # id -> ник
    for r in dl:
        if r["u"] is not None and (r["n"] or r["u"] not in dl_all):
            dl_all[r["u"]] = r["n"]
    added_dl_only = 0
    for uid, name in sorted(dl_all.items()):
        key = str(uid)
        if key not in result:
            result[key] = {"_name": str(name or ""), "pointercrate": []}
            added_dl_only += 1

    # 2) Игроки Pointercrate, которых нет в глобале, получают запись с ключом "pc<id>".
    # Аккаунты с общим видео считаем одним человеком (так же, как при сопоставлении с
    # глобалом) и кладём в одну запись. Новый аккаунт, у которого общее видео с уже
    # существующей записью "pc...", дописывается в неё.
    pc_all = dict(pc_players)  # id -> ник; рекорды дополняют список на всякий случай
    for r in pc:
        if r["p"] is not None and r["p"] not in pc_all:
            pc_all[r["p"]] = r["n"]

    def is_fake(key):
        return str(key).startswith("pc")

    # в обработку идут свободные аккаунты и те, что уже лежат в записях "pc..."
    pool = [p for p in pc_all if p not in pc_owner or is_fake(pc_owner[p])]
    pool_set = set(pool)
    parent = {p: p for p in pool}  # для склейки: у кого общее видео, те в одной группе

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pid_vids = defaultdict(set)  # аккаунт Pointercrate -> его видео
    for v, users in pc_by_vid.items():
        ids = [p for p in users if p in pool_set]
        for p in ids:
            pid_vids[p].add(v)
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])

    def shared(comp):
        out = set()
        for i, a in enumerate(comp):
            for b in comp[i + 1:]:
                out |= pid_vids[a] & pid_vids[b]
        return out

    def fake_entry(pids):
        key = f"pc{pids[0]}"  # pids отсортированы: ключ по наименьшему id
        entry = result.setdefault(key, {"_name": str(pc_all.get(pids[0]) or ""), "pointercrate": []})
        for p in pids:
            if p not in entry["pointercrate"]:
                entry["pointercrate"].append(p)
            pc_owner[p] = key
        return key

    comps = defaultdict(list)
    for p in pool:
        comps[find(p)].append(p)
    added_pc_only = joined_pc = 0
    for comp in comps.values():
        comp.sort()
        free = [p for p in comp if p not in pc_owner]
        if not free:
            continue  # все уже разложены по записям
        owners = {pc_owner[p] for p in comp if p in pc_owner}
        who = ", ".join(f"{p} ({pc_all.get(p)})" for p in comp)
        if len(owners) == 1 and len(result[next(iter(owners))]["pointercrate"]) + len(free) <= MAX_ACCOUNTS:
            key = next(iter(owners))
            result[key]["pointercrate"].extend(free)
            for p in free:
                pc_owner[p] = key
            joined_pc += len(free)
            review.append(f"PC БЕЗ ГЛОБАЛА, добавлено к записи {key}: {who}\n" + vurls(shared(comp)))
        elif not owners and len(free) <= MAX_ACCOUNTS:
            fake_entry(free)
            added_pc_only += 1
            if len(free) > 1:
                review.append(f"PC БЕЗ ГЛОБАЛА, склеены по общим видео: {who}\n" + vurls(shared(comp)))
        else:
            why = (
                f"СЛИШКОМ МНОГО PC-АККАУНТОВ С ОБЩИМИ ВИДЕО ({len(free)})"
                if not owners
                else "PC-АККАУНТЫ С ОБЩИМИ ВИДЕО, а запись уже есть или в ней нет места"
            )
            review.append(f"{why}, НЕ склеены (каждый отдельной записью): {who}\n" + vurls(shared(comp)))
            for p in free:
                fake_entry([p])
                added_pc_only += 1

    added_channels = None
    if "--no-channels" not in sys.argv:
        print("Ищу каналы игроков (YouTube/Twitch/Bilibili/...)")
        added_channels = add_channels(result, dl, pc, workers)

    # Один и тот же канал у разных записей: это один человек, склеиваем
    absorbed, merged_groups, global_shared = merge_by_channels(result)

    save_tmp = OUT + ".tmp"
    with open(save_tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.replace(save_tmp, OUT)

    review.extend(taken_list)  # подробности попадут в links_review.txt
    review = sorted(set(review))
    with open(REVIEW, "w", encoding="utf-8") as f:
        f.write("\n\n".join(review) if review else "Спорных случаев нет.")

    print()
    print(f"Общих видео на обоих сайтах:     {len(set(dl_by_vid) & set(pc_by_vid))}")
    print(f"Всего игроков в {OUT}:  {len(result)}")
    print(f"  добавлено новых игроков: {added_players}, новых ссылок на Pointercrate: {added_links}")
    print(f"  аккаунтов глобала без Pointercrate (новых записей): {added_dl_only}")
    print(f"  игроков Pointercrate без глобала: новых записей pc...: {added_pc_only}, "
          f"аккаунтов дописано в существующие записи: {joined_pc}")
    if added_channels is not None:
        print(f"  добавлено новых ссылок на каналы: {added_channels}")
    print(f"  склейка по общим каналам: групп {merged_groups}, поглощено записей {absorbed} (подробности в {MERGES_LOG})")
    print(f"  общих каналов у нескольких аккаунтов глобала: {global_shared} (список в {CHANNEL_CONFLICTS})")
    if taken:
        print(f"  пропущено, аккаунт Pointercrate уже записан у другого игрока: {taken}")
        for line in taken_list:  # выводим каждый случай в консоль
            print("    " + line)
    print(f"Спорных случаев в {REVIEW}: {len(review)}")
    print(f"  из них дублей внутри demonlist: {len(dl_pairs)}")
    print(f"Записей без ссылки на видео (не использовались): {skipped}")
    report_other_sites(dl, pc, workers)


if __name__ == "__main__":
    main()
