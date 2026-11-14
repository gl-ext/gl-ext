# Рейтинг по геометрическому среднему N лучших записей (меньше = лучше).
# 100% = h (место уровня); иначе f = h*a*(T/c)^(ln(b/a)/ln(T/t)), T и t из levels_tp.json (только с GitHub).
# Источники: глобал (всегда), pointercrate (напрямую, в т.ч. игроки без линка, помечены [PC]), my_extras.
# Для игроков, которые есть только в Pointercrate, флаг берётся из их профиля на Pointercrate.
#
#   python gd-rating.py --src all
#   python gd-rating.py --src pointercrate --levels 50
#   python gd-rating.py --add 13506:3409:91 --add 13506:x1:95
#   python gd-rating.py --long                 подробный вывод (по строке на запись)
import argparse
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DL = "https://api.demonlist.org/"
PC = "https://pointercrate.com/api/"
RAW = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/"
FILE_SOURCES = {}
ALL_SOURCES = {"pointercrate", "my_extras"}
A, B = 2, 4

# Особые названия: ID уровня в глобале -> как его писать в рейтинге.
# Эти названия подставляются вместо тех, что приходят из API.
# Чтобы добавить ещё один, допишите новую строку по тому же образцу.
SPECIAL_LEVEL_NAMES = {
    "3351": "Thinking Space II (Legacy)",
    "2234": "Deimos (EndLevel)",
}

p = argparse.ArgumentParser()
p.add_argument("--src", default="global", help="pointercrate, my_extras или all (через запятую)")
p.add_argument("--n", type=int, default=6, help="сколько лучших записей берём")
p.add_argument("--cap", type=float, default=300, help="макс. стоимость записи; недостающие добираются этим значением")
p.add_argument("--cap-place", dest="cap_place", type=float, default=300, help="проверять уровни до этого места")
p.add_argument("--levels", type=int, default=0, help="только первые N уровней (быстрый тест)")
p.add_argument("--tp-file", dest="tp_file", default="",
               help="путь к свежему levels_tp.json (его передаёт sync-extras.py); по умолчанию файл берётся с GitHub")
p.add_argument("--show", type=int, default=50, help="сколько игроков печатать")
p.add_argument("--long", action="store_true", help="подробный вывод (по строке на запись)")
p.add_argument("--conc", type=int, default=20, help="параллельных запросов")
p.add_argument("--save", type=int, default=10, help="сохранять кеш каждые N задач")
p.add_argument("--ttl", type=int, default=3600, help="секунд, пока кеш уровня свежий")
p.add_argument("--add", action="append", default=[], help="своя запись: ID_игрока:ID_уровня:процент")
p.add_argument("--ids", action="store_true", help="добавлять ID игрока в конец строки")
p.add_argument("--min-score", dest="min_score", type=float, default=None,
               help="показывать игроков с очками не меньше этого (меньше = лучше)")
p.add_argument("--max-score", dest="max_score", type=float, default=None,
               help="показывать игроков с очками не больше этого, например 160; "
                    "подсчёт рекордов только с Pointercrate тогда тоже идёт только по ним")
p.add_argument("--pc-conc", dest="pc_conc", type=int, default=20,
               help="сколько запросов к Pointercrate одновременно (при 429 уменьшите)")
p.add_argument("--fresh-country", dest="fresh_country", action="store_true",
               help="не брать страны Pointercrate из кеша (перезапросить)")
args = p.parse_args()

N = args.n
wanted = [s.strip() for s in args.src.split(",") if s.strip()]
SOURCES = set(ALL_SOURCES) if "all" in wanted else set(wanted)
SOURCES.discard("global")


def get_json(url, tries=8):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gl-ext-top6"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                break
        except Exception as e:
            last = e
        time.sleep(min(2 ** i, 30) + 1)
    raise last


def load_paged(path, key, step, stop=None):
    """Грузит список постранично. stop(arr) -> True, когда дальше качать не нужно."""
    out, offset = [], 0
    while True:
        d = get_json(f"{DL}{path}?limit={step}&offset={offset}")
        arr = (d.get("data") or {}).get(key) or []
        if not arr:
            return out
        out += arr
        if stop and stop(arr):
            return out
        offset += step


def levels_past_cap(arr):
    """Страница отсортирована по месту и её последний уровень уже ниже порога - дальше не нужно."""
    pl = [float(l["placement"]) for l in arr]
    return pl == sorted(pl) and pl[-1] > args.cap_place


# ---------- формула ----------
def score(h, c, T, t):
    if c >= 100:
        return h
    if not (t < T) or c < t or c <= 0:
        return None
    return h * A * (T / c) ** (math.log(B / A) / math.log(T / t))


def geo_mean(values):
    return math.exp(sum(math.log(x) for x in values) / len(values))


# ---------- файлы репозитория ----------
def load_repo_json(name):
    f = HERE / name
    try:
        if f.exists():
            print(f"{name}: беру ЛОКАЛЬНЫЙ файл {f}")
            return json.loads(f.read_text("utf-8"))
        print(f"{name}: беру с GitHub (raw кешируется до ~5 минут после правки)")
        return get_json(RAW + name)
    except Exception as e:
        print(f"{name}: {e}")
        return {}


def load_tp():
    """levels_tp.json берём ТОЛЬКО с GitHub. Исключение - ключ --tp-file: его передаёт sync-extras.py,
    который только что обновил список уровней, а GitHub (raw кешируется ~5 минут) отдал бы старый.
    Без файла T/t были бы запасными для всех уровней и рейтинг получился бы неверным, поэтому при сбое выходим."""
    try:
        if args.tp_file:
            print(f"levels_tp.json: беру свежий файл {args.tp_file} (передан из sync-extras)")
            levels = json.loads(Path(args.tp_file).read_text("utf-8")).get("levels", {})
        else:
            print("levels_tp.json: беру с GitHub (raw кешируется до ~5 минут после правки)")
            levels = get_json(RAW + "levels_tp.json").get("levels", {})
    except Exception as e:
        raise SystemExit(f"levels_tp.json: не удалось загрузить ({e}). Останавливаюсь.")
    if not levels:
        raise SystemExit("levels_tp.json: на GitHub пустой список уровней. Останавливаюсь.")
    return levels


def load_links():
    """Возвращает две функции:
    pc_to_dl(pid)  - ID игрока Pointercrate -> главный ID на Demonlist (или None);
    canon(uid)     - любой ID Demonlist -> главный ID (склейки fix.json + доп. аккаунты из links.json).
    """
    links = load_repo_json("links.json")
    fix = load_repo_json("fix.json")
    absorb = {}
    for m in fix.get("merges", []):
        for a in m.get("absorb", []):
            absorb[str(a["id"])] = str(m["keep"])

    def canon0(i):
        i, seen = str(i), set()
        while i in absorb and i not in seen:  # цепочки склеек A->B->C
            seen.add(i)
            i = absorb[i]
        return i

    pc_map, alias = {}, {}
    people = {}  # главный ID -> {"dl": [все аккаунты Demonlist, главный первым], "pc": [аккаунты Pointercrate]}

    def grp(main):
        return people.setdefault(main, {"dl": [], "pc": []})

    def add_uniq(lst, x):
        x = str(x)
        if x not in lst:
            lst.append(x)

    for main_id, entry in links.items():
        main = canon0(main_id)
        g = grp(main)
        add_uniq(g["dl"], main)
        add_uniq(g["dl"], main_id)
        sites = entry if isinstance(entry, dict) else {"pointercrate": entry}
        for site, ids in sites.items():
            for x in (ids if isinstance(ids, list) else [ids]):
                if x is None:
                    continue
                if site == "pointercrate":
                    pc_map[str(x)] = main
                    add_uniq(g["pc"], x)
                elif any(s in str(site).lower() for s in ("demonlist", "global")):
                    alias[str(x)] = main  # доп. аккаунты того же человека на Demonlist
                    add_uniq(g["dl"], x)
    for main_id in links:
        alias.setdefault(str(main_id), canon0(main_id))
    # аккаунты, склеенные через fix.json, тоже считаются аккаунтами этого человека
    for a in absorb:
        main = canon0(a)
        g = grp(main)
        add_uniq(g["dl"], main)
        add_uniq(g["dl"], a)

    def accounts(uid):
        """Все известные аккаунты человека: {"dl": [...], "pc": [...]} (главный Demonlist-аккаунт первым)."""
        return people.get(canon(uid), {"dl": [str(uid)], "pc": []})

    def canon(uid):
        uid = str(uid)
        return canon0(alias.get(uid, uid))

    # removals: ключ - ID глобала, videos - ID ютуб-видео, записи с ними у этого игрока не учитываются.
    # shared_with игнорируем.
    removals = {}
    for r in fix.get("removals", []):
        removals.setdefault(str(r["user"]), []).extend(str(v) for v in r.get("videos", []) if v)

    print(f"links.json: людей {len(links)}, аккаунтов Pointercrate {len(pc_map)}, "
          f"доп. аккаунтов Demonlist {len(alias) - len(links)}, склеек fix.json {len(absorb)}, "
          f"игроков с удалением видео {len(removals)}")
    if not links:
        print("  ВНИМАНИЕ: links.json пустой или не загрузился - склейки аккаунтов не будет!")
    return (lambda pid: pc_map.get(str(pid))), canon, removals, accounts


def is_removed(removals, uid, video):
    """True, если видео записи входит в removals именно этого игрока."""
    vids = removals.get(str(uid))
    return bool(vids and video and any(v in str(video) for v in vids))


def merge_accounts(best_by, info, canon):
    """Склеивает все ID одного человека в один: лучший процент по каждому уровню."""
    new_best, new_info = {}, {}
    for uid, m in best_by.items():
        cu = canon(uid)
        nb = new_best.setdefault(cu, {})
        for k, v in m.items():
            nb[k] = max(nb.get(k, -1), v)
        n = info.get(uid)
        if not n:
            continue
        if cu not in new_info or uid == cu:
            new_info[cu] = n
        elif n[1] and not new_info[cu][1]:
            new_info[cu] = (new_info[cu][0], n[1])
    for uid in new_best:
        new_info.setdefault(uid, ("#" + uid, None))
    return new_best, new_info


def load_extras():
    merged = {}

    def put(uid, lvl, pct):
        m = merged.setdefault(str(uid), {})
        m[str(lvl)] = max(m.get(str(lvl), -1), float(pct))

    for src in SOURCES:
        name = FILE_SOURCES.get(src)
        if not name:
            continue
        try:
            users = get_json(RAW + name).get("users", {})
        except Exception as e:
            print(f"{name}: {e}")
            continue
        for uid, rows in users.items():
            for r in rows:
                k = r[0] if r[0] is not None else (r[7] if str(r[7]).startswith("x") else None)
                if k is not None:
                    put(uid, k, r[3])
    if "my_extras" in SOURCES:
        try:
            raw = get_json(RAW + "my_extras.json").get("users", {})
        except Exception as e:
            print(f"my_extras.json: {e}")
            raw = {}
        for uid, rows in raw.items():
            for r in rows:
                if isinstance(r, list) and len(r) >= 2:
                    put(uid, r[0], r[1])
    return merged


def apply_cli_adds(best_by, info, canon):
    """Записи из --add ID_игрока:ID_уровня:процент. Применяются САМЫМИ ПОСЛЕДНИМИ,
    после склейки аккаунтов и после corrections.json, поэтому правки их не затрагивают.
    ID игрока приводится к главному через canon() (подойдёт ID любого его аккаунта)."""
    added = 0
    for item in args.add:
        uid, lvl, pct = item.split(":")
        cu = canon(uid)
        m = best_by.setdefault(cu, {})
        m[str(lvl)] = max(m.get(str(lvl), -1), float(pct))
        info.setdefault(cu, ("#" + cu, None))
        added += 1
    if added:
        print(f"--add: добавлено записей {added} (после corrections.json)")


def apply_corrections(best_by, info, canon):
    """Правки из corrections.json (применяются ПОСЛЕ склейки аккаунтов, всегда, при любом --src).
    Формат: {"delete": {"users": {ID: [[уровень, процент, ""], ...]}}, "add": {"users": {...}}}
    ID из файла приводятся к главному ID человека через canon(), так что подойдёт ID любого его аккаунта.
    delete - убрать запись, если у игрока по этому уровню лучший процент совпадает.
    add    - ИСПРАВЛЕНИЕ завышенной записи: добавляется, только если у этого игрока
             по этому же уровню запись реально была удалена через delete.
             Иначе (удалять было нечего) add пропускается, чтобы не появлялись лишние рекорды.
    Всё, что не сработало, печатается с причиной - так видно, где опечатка."""
    data = load_repo_json("corrections.json")
    if not data:
        print("corrections.json: пусто или не загрузился (см. строку с ошибкой выше) - правок НЕ будет")
        return

    def users_of(section):
        sec = data.get(section) or {}
        # допускаем и {"users": {...}}, и сразу {ID: [...]}
        return sec.get("users", sec) if isinstance(sec, dict) else {}

    deleted_keys = set()   # пары (главный ID игрока, ID уровня), которые реально удалены
    deleted = added = skipped = 0
    for uid, rows in users_of("delete").items():
        cu = canon(uid)
        mine = best_by.get(cu)
        for r in rows:
            if not isinstance(r, list) or len(r) < 2:
                continue
            k = str(r[0])
            if (cu, k) in deleted_keys:
                continue  # этот рекорд уже удалён через другой аккаунт того же человека
            if mine is None:
                print(f"  delete {uid} (-> {cu}): игрока нет в топе")
            elif k not in mine:
                print(f"  delete {uid} (-> {cu}): уровня {k} у него нет")
            elif abs(mine[k] - float(r[1])) > 1e-9:
                print(f"  delete {uid} (-> {cu}) уровень {k}: у него {mine[k]:g}%, а в файле {float(r[1]):g}% - не совпало")
            else:
                del mine[k]
                deleted_keys.add((cu, k))
                deleted += 1
    for uid, rows in users_of("add").items():
        cu = canon(uid)
        for r in rows:
            if not isinstance(r, list) or len(r) < 2:
                continue
            k = str(r[0])
            if (cu, k) not in deleted_keys:
                print(f"  add {uid} (-> {cu}) уровень {k}: пропущено - у него не удалялась запись по этому уровню")
                skipped += 1
                continue
            mine = best_by.setdefault(cu, {})
            mine[k] = max(mine.get(k, -1), float(r[1]))
            info.setdefault(cu, ("#" + cu, None))
            added += 1
    print(f"corrections.json: удалено записей {deleted}, добавлено {added}, add пропущено {skipped}")


# ---------- кеш ----------
CACHE_FILE = HERE / "levelcache.json"
cache = json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.exists() else {}
lock = threading.Lock()


def save_cache():
    """Запись через временный файл; под блокировкой, чтобы потоки не мешали друг другу."""
    with lock:
        data = json.dumps(cache)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(data, "utf-8")
        os.replace(tmp, CACHE_FILE)


def level_records(lvl_id):
    def page(offset):
        d = get_json(f"{DL}level/classic/record/list?level_id={lvl_id}&limit=50&offset={offset}")["data"]
        return d.get("records") or [], int(d.get("total_count") or 0)

    first, total = page(0)
    c = cache.get(str(lvl_id))
    # len(x) >= 5: в старом кеше не было ссылки на видео - такие уровни перекачиваем
    if (c and c["total"] == total and time.time() - c["at"] < args.ttl
            and all(len(x) >= 5 for x in c["recs"])):
        return c["recs"]
    records = list(first)
    while len(records) < total:
        more, _ = page(len(records))
        if not more:
            break
        records += more
    out = []
    for r in records:
        if str(r.get("status") or "accepted").lower() != "accepted":
            continue
        u = r.get("user") or {}
        if u.get("id") is None:
            continue
        pct = r.get("percent")
        out.append([str(u["id"]), u.get("username"), u.get("country"),
                    float(pct if pct is not None else 100), r.get("video_url")])
    with lock:
        cache[str(lvl_id)] = {"total": total, "at": time.time(), "recs": out}
    return out


def verifier_of(lvl):
    v = lvl.get("verification")
    if not v:
        key = "v" + str(lvl["id"])
        c = cache.get(key)
        if c and time.time() - c["at"] < 7 * 86400:
            v = c["v"]
        else:
            d = get_json(f"{DL}level/classic/get?id={lvl['id']}")["data"]
            v = d.get("verification")
            with lock:
                cache[key] = {"at": time.time(), "v": v}
    if v and v.get("user_id") is not None:
        return [str(v["user_id"]), v.get("username"), v.get("video_url")]
    return None


def clean_country(c):
    """'Unknown' и пустое значение на глобале означают 'страны нет'."""
    c = (c or "").strip()
    return None if c.lower() in ("", "unknown") else c


def dl_profile(uid):
    """Профиль игрока на глобале: GET user/get?id=<id> -> data.username, data.country.
    Возвращает (ник, страна) или None, если профиля нет. Кешируется на 30 дней."""
    key = "u" + str(uid)
    c = cache.get(key)
    if c and time.time() - c["at"] < 30 * 86400 and c.get("n"):
        return (c["n"], clean_country(c.get("c")))
    try:
        d = get_json(f"{DL}user/get?id={uid}").get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):  # такого игрока на глобале нет
            return None
        raise
    name = d.get("username")
    if not name:
        return None
    country = d.get("country")
    with lock:
        cache[key] = {"at": time.time(), "n": name, "c": country}
    return (name, clean_country(country))


# ---------- Pointercrate ----------
def pc_demons():
    out, seen, after = [], set(), 0
    while True:
        arr = get_json(f"{PC}v2/demons/?limit=100&after={after}")
        if not isinstance(arr, list) or not arr:
            return out
        new = [d for d in arr if d["id"] not in seen]
        for d in new:
            seen.add(d["id"])
        out += new
        last = max(int(d["id"]) for d in arr)
        if not new or len(arr) < 100 or last <= after:
            return out
        after = last


def pc_records(demon_id):
    key = "pc" + str(demon_id)
    c = cache.get(key)
    if c and time.time() - c["at"] < args.ttl and all(len(x) >= 4 for x in c["recs"]):
        return c["recs"]
    out, after = [], 0
    while True:
        arr = get_json(f"{PC}v1/records/?demon_id={demon_id}&limit=100&after={after}")
        if not isinstance(arr, list) or not arr:
            break
        for r in arr:
            st = str(r.get("status") or "APPROVED").upper()
            pl = r.get("player") or {}
            if st == "APPROVED" and pl.get("id") is not None and r.get("progress") is not None:
                out.append([str(pl["id"]), pl.get("name"), float(r["progress"]), r.get("video")])
        last = max(int(r["id"]) for r in arr)
        if len(arr) < 100 or last <= after:
            break
        after = last
    with lock:
        cache[key] = {"at": time.time(), "recs": out}
    return out


pc_sem = threading.Semaphore(max(1, args.pc_conc))  # не заваливаем Pointercrate запросами
pc_style = ["/"]   # какой вариант URL работает: со слэшем в конце или без


def pc_fetch(url, tries=4):
    """GET к Pointercrate. Возвращает (HTTP-код, текст ответа).
    429 и 5xx повторяем (с учётом Retry-After); остальные коды возвращаем как есть.
    Исключение - только если сеть не отвечала все попытки."""
    last = None
    for i in range(tries):
        try:
            with pc_sem:
                req = urllib.request.Request(url, headers={"User-Agent": "gl-ext-top6",
                                                           "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:   # сюда попадаем уже ВНЕ семафора
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if e.code == 429 or e.code >= 500:
                last = (e.code, body)
                try:
                    wait = float(e.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    wait = 3 * (i + 1)
                time.sleep(min(wait, 60))
                continue
            return e.code, body
        except Exception as e:
            last = e
            time.sleep(min(2 ** i, 10))
    if isinstance(last, tuple):
        return last
    raise last


def pc_player_country(pid):
    """Код страны (ISO-2) игрока Pointercrate или None. Кешируется на 30 дней.
    Если запрос не удался - бросает исключение (и ничего не кеширует)."""
    pid = str(pid)
    key = "pcn3" + pid  # новый ключ: старые pcn/pcn2 могли хранить "страны нет" от кривого ответа
    c = cache.get(key)
    if c and not args.fresh_country and time.time() - c["at"] < 30 * 86400:
        return c["c"]

    variants = [f"{PC}v1/players/{pid}/", f"{PC}v1/players/{pid}"]
    if pc_style[0] == "":
        variants.reverse()
    status, body, errs = None, "", []
    for u in variants:
        try:
            st, bd = pc_fetch(u)
        except Exception as e:
            errs.append(f"{u} -> сеть: {str(e)[:60]}")
            status = None
            continue
        errs.append(f"{u} -> HTTP {st}")
        status, body = st, bd
        if st == 200:
            pc_style[0] = "/" if u.endswith("/") else ""
            break
    if status != 200:
        raise RuntimeError("; ".join(errs))

    try:
        d = json.loads(body)
    except ValueError:
        raise RuntimeError("ответ не JSON")
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        d = d["data"]
    if not isinstance(d, dict) or "id" not in d:
        return None  # структура не та - НЕ кешируем, чтобы не запомнить ложное "страны нет"
    nat = d.get("nationality")
    code = nat.get("country_code") if isinstance(nat, dict) else None
    with lock:
        cache[key] = {"at": time.time(), "c": code}
    return code


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600}ч {sec % 3600 // 60}м" if sec >= 3600 else f"{sec // 60}м {sec % 60}с"


def run_tasks(items, fn, label):
    total = len(items)
    state = {"done": 0}
    errors = []
    failed = set()  # id() задач, которые не удались
    t0 = time.time()

    def item_name(item):
        return str(item[0] if isinstance(item, tuple) else item.get("name", item))

    def progress(final=False):
        d = state["done"]
        left = (total - d) / (d / max(time.time() - t0, 1)) if d else 0
        print(f"\r  {label} {d}/{total} ({d * 100 // max(total, 1)}%)  ошибок: {len(errors)}  "
              f"осталось ~{fmt_time(left)}   ", end="\n" if final else "", flush=True)

    def wrap(item):
        res = None
        try:
            res = fn(item)
        except Exception as e:
            with lock:
                errors.append((item_name(item), str(e)[:80]))
                failed.add(id(item))
        with lock:
            state["done"] += 1
            d = state["done"]
        if d % args.save == 0:
            save_cache()
        progress()
        return item, res

    ex = ThreadPoolExecutor(args.conc)
    try:
        results = list(ex.map(wrap, items))
        ex.shutdown()
    except KeyboardInterrupt:
        print("\n\nОстановлено. Сохраняю кеш...")
        ex.shutdown(wait=False, cancel_futures=True)
        save_cache()
        raise SystemExit
    finally:
        save_cache()
    progress(final=True)

    # Автоповтор упавших задач (обрывы SSL, 5xx и т.п.): по одной, с паузой, до 3 кругов
    for rnd in range(1, 4):
        bad = [i for i, (it, _) in enumerate(results) if id(it) in failed]
        if not bad:
            break
        print(f"  Повтор {rnd}/3: неудачных {len(bad)}, пауза 5с...")
        time.sleep(5)
        errors.clear()
        for i in bad:
            it = results[i][0]
            try:
                results[i] = (it, fn(it))
                failed.discard(id(it))
            except Exception as e:
                errors.append((item_name(it), str(e)[:80]))
        save_cache()
        print(f"  После повтора {rnd}: осталось неудачных {len(failed)}")

    if errors:
        print(f"  Не скачались {len(errors)} (запусти ещё раз, докачает только их):")
        for name, msg in errors[:10]:
            print(f"    {name}: {msg}")
    return results


# ---------- вывод ----------
COUNTRY = {
    "unitedstates": "US", "usa": "US", "newzealand": "NZ", "russia": "RU", "armenia": "AM", "finland": "FI",
    "spain": "ES", "southkorea": "KR", "kazakhstan": "KZ", "brazil": "BR", "poland": "PL", "portugal": "PT",
    "moldova": "MD", "canada": "CA", "germany": "DE", "peru": "PE", "chile": "CL", "unitedkingdom": "GB",
    "australia": "AU", "japan": "JP", "romania": "RO", "norway": "NO", "denmark": "DK", "netherlands": "NL",
    "austria": "AT", "ukraine": "UA", "israel": "IL", "estonia": "EE", "uzbekistan": "UZ", "serbia": "RS",
    "france": "FR", "italy": "IT", "belgium": "BE", "china": "CN", "colombia": "CO", "argentina": "AR",
    "philippines": "PH", "panama": "PA", "albania": "AL", "czechrepublic": "CZ", "czechia": "CZ",
    "lithuania": "LT", "sweden": "SE", "hungary": "HU", "belarus": "BY", "mexico": "MX", "india": "IN",
    "venezuela": "VE", "pakistan": "PK", "greece": "GR", "ireland": "IE", "unitedarabemirates": "AE",
    "uae": "AE", "indonesia": "ID", "georgia": "GE", "nigeria": "NG", "paraguay": "PY", "iceland": "IS",
    "costarica": "CR", "slovakia": "SK", "uruguay": "UY", "turkey": "TR", "bosniaandherzegovina": "BA",
    "kosovo": "XK", "laos": "LA", "southafrica": "ZA", "latvia": "LV", "bulgaria": "BG", "ecuador": "EC",
    "vietnam": "VN", "cambodia": "KH", "iran": "IR", "dominicanrepublic": "DO", "malta": "MT",
    "palestine": "PS", "turkmenistan": "TM", "algeria": "DZ", "cuba": "CU", "egypt": "EG", "taiwan": "TW",
    "azerbaijan": "AZ", "tunisia": "TN", "kyrgyzstan": "KG", "croatia": "HR", "morocco": "MA",
    "bolivia": "BO", "switzerland": "CH", "hongkong": "HK", "slovenia": "SI", "iraq": "IQ", "thailand": "TH",
    "nicaragua": "NI", "jamaica": "JM", "luxembourg": "LU", "guatemala": "GT", "ghana": "GH",
    "puertorico": "PR", "mongolia": "MN", "montenegro": "ME", "elsalvador": "SV", "syria": "SY",
    "somalia": "SO", "greenland": "GL", "saudiarabia": "SA", "malaysia": "MY", "afghanistan": "AF",
    "northmacedonia": "MK", "macedonia": "MK", "bahrain": "BH", "maldives": "MV", "libya": "LY",
    "senegal": "SN", "capeverde": "CV", "cyprus": "CY", "honduras": "HN", "singapore": "SG", "yemen": "YE",
    "lebanon": "LB", "oman": "OM", "kenya": "KE", "kuwait": "KW", "jordan": "JO", "ethiopia": "ET",
    "myanmar": "MM", "bangladesh": "BD", "qatar": "QA", "tajikistan": "TJ", "bahamas": "BS", "mali": "ML",
    "togo": "TG", "zambia": "ZM", "gabon": "GA", "andorra": "AD", "guam": "GU", "haiti": "HT",
    "england": "GB", "scotland": "GB", "wales": "GB", "canaryislands": "ES",
}


def flag(country):
    """Принимает либо название страны (Demonlist), либо готовый ISO-код из 2 букв (Pointercrate)."""
    raw = str(country or "").strip()
    if len(raw) == 2 and raw.isalpha():
        code = raw.upper()
    else:
        code = COUNTRY.get("".join(ch for ch in raw.lower() if ch.isalpha()))
    if not code or len(code) != 2:
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)


def line_short(i, r, with_id=False):
    """5) 🇵🇱 dovvin (ORBIT, Thinking Space II 88%, ...) - 14.18 [id 12345]"""
    parts = []
    for rec in r["records"]:
        if rec["place"] is None:
            continue
        pct = "" if rec["percent"] >= 100 else f" {rec['percent']:g}%"
        parts.append(rec["level"] + pct)
    tail = f" [{r['id']}]" if with_id else ""
    return f"{i}) {flag(r['country'])} {r['name']} ({', '.join(parts)}) - {r['score']:.2f}{tail}"


def fmt_record(r):
    if r["place"] is None:
        return f"       {'—':<34} {'':>7}  {r['f']:8.2f}   (добор до {N} записей)"
    pct = f"{r['percent']:g}%"
    tag = " [x]" if r.get("special") else ""
    return f"    #{int(r['place']):<4} {(r['level'] + tag):<34} {pct:>7}  {r['f']:8.2f}"


def main():
    print("Источники: global" + "".join(" + " + s for s in sorted(SOURCES)))
    if args.add:
        print("Свои записи:", ", ".join(args.add))

    print("Загружаю список уровней...")
    pc_to_dl, canon_id, removals, accounts_of = load_links()
    # качаем только до --cap-place (по умолчанию топ-300), остальные уровни нигде не используются
    levels = load_paged("level/classic/list", "levels", 100, stop=levels_past_cap)
    extras = load_extras()
    tp = load_tp()

    meta, fallback = {}, 0
    for l in levels:
        k = str(l["id"])
        e = tp.get(k) or {}
        if e.get("T") and e.get("t"):
            T, t = float(e["T"]), float(e["t"])
        else:
            T, t = 100.0, float(l["list_percent"])
            fallback += 1
        # Особое название (если есть) берётся вместо названия из API
        meta[k] = {"name": SPECIAL_LEVEL_NAMES.get(k, l["name"]),
                   "place": float(l["placement"]), "T": T, "t": t}
    for k, e in tp.items():
        if k.startswith("x") and e.get("T") and e.get("t"):
            meta[k] = {"name": e["name"], "place": float(e["place"]), "T": float(e["T"]), "t": float(e["t"])}
    if fallback:
        print(f"Уровней без T/t в levels_tp.json (запасные T=100, list_percent): {fallback}")

    top_levels = sorted((l for l in levels if float(l["placement"]) <= args.cap_place),
                        key=lambda l: float(l["placement"]))
    if args.levels:
        top_levels = top_levels[: args.levels]
    print(f"Уровней для проверки: {len(top_levels)}\n")

    def fetch_dl(l):
        recs = level_records(l["id"])
        ver = verifier_of(l)
        if ver:
            recs = [r for r in recs if r[0] != ver[0]] + [[ver[0], ver[1], None, 100.0, ver[2]]]
        return recs

    print("Demonlist:")
    fetched = run_tasks(top_levels, fetch_dl, "уровней")

    best_by, info, pc_only = {}, {}, set()
    dl_pairs, pc_pairs = {}, {}  # (ID игрока, ID уровня) -> лучший процент, отдельно по каждому сайту
    removed_n = 0
    dl_seen = set()  # игроки, чей ник и страна уже пришли из записей глобала
    for l, recs in fetched:
        k = str(l["id"])
        for uid, name, country, pct, video in recs or []:
            country = clean_country(country)
            if is_removed(removals, uid, video):
                removed_n += 1
                continue
            m = best_by.setdefault(uid, {})
            m[k] = max(m.get(k, -1), pct)
            dl_pairs[(uid, k)] = max(dl_pairs.get((uid, k), -1), pct)
            if country:
                dl_seen.add(uid)
            if uid not in info or country:
                info[uid] = (name, country)

    if "pointercrate" in SOURCES:
        print("\nPointercrate:")
        by_ingame = {str(l["ingame_id"]): l for l in top_levels}
        try:
            demons = pc_demons()
        except Exception as e:
            print("Не удалось загрузить демонов Pointercrate:", e)
            demons = []
        targets = [(d, by_ingame[str(d["level_id"])]) for d in demons
                   if d.get("level_id") is not None and str(d["level_id"]) in by_ingame]
        print(f"Демонов Pointercrate, найденных в глобале (по игровому ID): {len(targets)}")
        pc_results = run_tasks([(f"{d['name']}", d, l) for d, l in targets],
                               lambda it: pc_records(it[1]["id"]), "демонов")
        for (_, d, l), recs in pc_results:
            k = str(l["id"])
            for pid, pname, pct, video in recs or []:
                dl_id = pc_to_dl(pid)
                if dl_id and is_removed(removals, dl_id, video):
                    removed_n += 1
                    continue
                if dl_id:
                    uid = dl_id
                    info.setdefault(uid, (pname, None))
                else:
                    uid = "pc" + pid
                    pc_only.add(uid)
                    info[uid] = ((pname or "?") + " [PC]", None)
                m = best_by.setdefault(uid, {})
                m[k] = max(m.get(k, -1), pct)
                pc_pairs[(uid, k)] = max(pc_pairs.get((uid, k), -1), pct)
        print(f"Игроков Pointercrate без линка: {len(pc_only)}")

        # Флаги для игроков, которых нет в глобале: берём страну из профиля Pointercrate
        if pc_only:
            print("Страны игроков Pointercrate:")
            pids = [u[2:] for u in pc_only]  # убираем префикс "pc"
            res = run_tasks([(pid,) for pid in pids],
                            lambda it: pc_player_country(it[0]), "игроков PC (страна)")
            got = 0
            for (pid,), code in res:
                if code:
                    nm, _ = info["pc" + pid]
                    info["pc" + pid] = (nm, code)
                    got += 1
            print(f"Страна найдена у {got} из {len(pids)}")

    for uid, m in extras.items():
        # my_extras мог записать игрока как "pc<ID Pointercrate>", когда линка ещё не было.
        # Если линк в links.json с тех пор появился, переводим на его главный ID глобала.
        if uid.startswith("pc") and pc_to_dl(uid[2:]):
            uid = pc_to_dl(uid[2:])
        b = best_by.setdefault(uid, {})
        for k, pct in m.items():
            b[k] = max(b.get(k, -1), pct)
        info.setdefault(uid, ("#" + uid, None))

    print(f"Убрано записей по removals (fix.json): {removed_n}")

    # Склеиваем аккаунты одного человека (links.json + fix.json) после сбора всех источников
    before = len(best_by)
    best_by, info = merge_accounts(best_by, info, canon_id)
    print(f"Склейка аккаунтов: {before} -> {len(best_by)}")

    # Рекорды (игрок + уровень), которые есть только на Pointercrate. Считаем после склейки
    # аккаунтов: если у человека есть этот уровень на глобале (хоть с другого аккаунта),
    # это уже не "только Pointercrate".
    only_pc_set = set()
    if "pointercrate" in SOURCES:
        def canon_pairs(d):
            out = {}
            for (u, k), pct in d.items():
                cu = canon_id(u)
                out[(cu, k)] = max(out.get((cu, k), -1), pct)
            return out

        dlp, pcp = canon_pairs(dl_pairs), canon_pairs(pc_pairs)
        only_pc_set = {key for key in pcp if key not in dlp}
        no_link = sum(1 for (u, _) in only_pc_set if u.startswith("pc"))
        better = sum(1 for key, pct in pcp.items() if key in dlp and pct > dlp[key])
        print(f"Рекордов только на Pointercrate (игрок + уровень, по всем игрокам): {len(only_pc_set)}")
        print(f"  у игроков без линка [PC]: {no_link}, у игроков, связанных с глобалом: {len(only_pc_set) - no_link}")
        print(f"  уровень есть на обоих сайтах, но процент на Pointercrate выше: {better}")

    # Правки из corrections.json: применяются всегда, при любом источнике
    apply_corrections(best_by, info, canon_id)

    # Свои записи из --add: самым последним шагом, после corrections.json
    apply_cli_adds(best_by, info, canon_id)

    # Вся информация об игроке (ник, страна) - с глобала, если он там есть.
    # Из Pointercrate остаются только те, кого на глобале нет (pc-only).
    need = [u for u in best_by if not u.startswith("pc") and u not in dl_seen]
    if need:
        print("Профили игроков на глобале:")
        res = run_tasks([(u,) for u in need], lambda it: dl_profile(it[0]), "профилей")
        got = 0
        for (u,), prof in res:
            if prof:
                info[u] = prof
                got += 1
        print(f"Профиль найден у {got} из {len(need)}")
        if need and got == 0:
            print("  ВНИМАНИЕ: ни один профиль не загрузился. Ники и флаги этих игроков "
                  "могут быть из Pointercrate или пустыми.")

    # Если у главного аккаунта страны нет - ищем её у остальных аккаунтов этого человека:
    # сначала по всем аккаунтам Demonlist (главный первым), затем по аккаунтам Pointercrate.
    # Белый флаг остаётся только если страны нет нигде.
    country_errors = []

    def find_country(uid):
        acc = accounts_of(uid)
        for d in acc["dl"]:
            if str(d).startswith("pc"):
                continue
            try:
                prof = dl_profile(d)
            except Exception as e:
                country_errors.append(f"DL {d} (игрок {uid}): {str(e)[:80]}")
                continue
            if prof and clean_country(prof[1]):
                return prof[1]
        for pid in acc["pc"]:
            try:
                code = pc_player_country(pid)
            except Exception as e:
                country_errors.append(f"PC {pid} (игрок {uid}): {str(e)[:80]}")
                continue
            if code:
                return code
        return None

    no_flag = [u for u in best_by if u not in pc_only
               and flag((info.get(u) or ("", None))[1]) == "🏳️"]
    if no_flag:
        print("Поиск страны по другим аккаунтам игроков без флага:")
        res = run_tasks([(u,) for u in no_flag], lambda it: find_country(it[0]), "игроков (страна)")
        got = 0
        for (u,), code in res:
            if code:
                info[u] = (info[u][0], code)
                got += 1
        print(f"Страна найдена у {got} из {len(no_flag)}")
        still = [u for u in no_flag if not info[u][1]]
        if still:
            print(f"  Без страны остались {len(still)}")
        if country_errors:
            print(f"  Ошибок запросов при поиске страны: {len(country_errors)} (первые 5):")
            for msg in country_errors[:5]:
                print(f"    {msg}")

    result = []
    for uid, best in best_by.items():
        recs = []
        for k, pct in best.items():
            lv = meta.get(k)
            if not lv:
                continue
            f = score(lv["place"], pct, lv["T"], lv["t"])
            if f is not None and f <= args.cap:
                recs.append({"level": lv["name"], "place": lv["place"], "percent": pct, "f": f,
                             "special": k.startswith("x"), "key": k})
        if not recs:
            continue
        recs.sort(key=lambda r: (r["f"], r["special"]))
        top = recs[:N]
        while len(top) < N:
            top.append({"level": "-", "place": None, "percent": None, "f": args.cap, "special": False})
        name, country = info[uid]
        result.append({"id": uid, "name": name, "country": country, "pc_only": uid in pc_only,
                       "score": geo_mean([r["f"] for r in top]), "records": top})

    result.sort(key=lambda r: r["score"])
    for i, r in enumerate(result, 1):
        r["rank"] = i  # место во всём рейтинге; фильтр по очкам его не меняет

    # Фильтр по очкам (меньше = лучше): оставляем игроков с очками от --min-score до --max-score
    suffix = ""
    if args.min_score is not None or args.max_score is not None:
        lo = args.min_score if args.min_score is not None else float("-inf")
        hi = args.max_score if args.max_score is not None else float("inf")
        total_players = len(result)
        result = [r for r in result if lo <= r["score"] <= hi]
        lo_txt = f"{args.min_score:g}" if args.min_score is not None else "0"
        hi_txt = f"{args.max_score:g}" if args.max_score is not None else "max"
        suffix = f"_score{lo_txt}-{hi_txt}"
        print(f"\nФильтр по очкам {lo_txt}-{hi_txt}: осталось игроков {len(result)} из {total_players}")

    # Сколько записей, которые есть только на Pointercrate, у оставшихся игроков
    for r in result:
        r["pc_only_in_top"] = sum(1 for rec in r["records"]
                                  if rec.get("key") and (r["id"], rec["key"]) in only_pc_set)

    print(f"\nТоп по {N} лучшим записям (меньше = лучше), игроков: {len(result)}\n")
    if "pointercrate" in SOURCES:
        ids = {r["id"] for r in result}
        total_pc = sum(1 for (u, _) in only_pc_set if u in ids)
        in_top = sum(r["pc_only_in_top"] for r in result)
        with_pc = sum(1 for r in result if r["pc_only_in_top"])
        # все записи этих игроков (игрок + уровень, уровни до --cap-place, после склейки и правок)
        total_all = sum(len(best_by.get(r["id"], {})) for r in result)
        share_all = f" ({total_pc * 100 / total_all:.1f}%)" if total_all else ""
        # записи, которые реально вошли в топ-N игроков (без пустых добивок до N)
        top_all = sum(1 for r in result for rec in r["records"] if rec.get("place") is not None)
        share_top = f" ({in_top * 100 / top_all:.1f}%)" if top_all else ""
        print(f"Записей в {N} лучших у этих игроков: {top_all}")
        print(f"  из них только на Pointercrate: {in_top}{share_top}, "
              f"игроков с такими записями: {with_pc} из {len(result)}")
        print(f"Для сравнения, вообще всех записей у этих игроков: {total_all}, "
              f"из них только на Pointercrate: {total_pc}{share_all}\n")

    # в консоль: первые --show игроков
    for r in result[: args.show]:
        i = r["rank"]
        if args.long:
            print(f"{i:3}. {str(r['name']):<28} {r['score']:8.2f}  {r['country'] or ''}")
            for rec in r["records"]:
                print(fmt_record(rec))
            print()
        else:
            print(line_short(i, r, args.ids))

    # в txt: весь топ целиком (или весь отфильтрованный)
    # Имя файла по набору источников: gdr_gl, gdr_gl_pc, gdr_gl_my, а когда подключено всё, просто gdr
    base = {
        frozenset(): "gdr_gl",
        frozenset({"pointercrate"}): "gdr_gl_pc",
        frozenset({"my_extras"}): "gdr_gl_my",
        frozenset({"pointercrate", "my_extras"}): "gdr",
    }.get(frozenset(SOURCES), "gdr_" + "_".join(["gl", *sorted(SOURCES)]))
    txt = HERE / f"{base}{suffix}.txt"
    txt.write_text("\n".join(line_short(r["rank"], r, args.ids) for r in result), "utf-8")
    print(f"\nТекст для копирования (весь топ, {len(result)} игроков): {txt.name}")

    out = HERE / f"{base}{suffix}.json"
    out.write_text(json.dumps(
        [{**r, "score": round(r["score"], 2)} for r in result],
        ensure_ascii=False, indent=1), "utf-8")
    print(f"Полный результат: {out.name}")


main()
