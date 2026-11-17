import math
import os
import re
import sys
import collections
import json
import time
import argparse
import urllib.error
import urllib.request

# levels.py лежит рядом с этим файлом (скрипт могут запускать из любой папки)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# =========================
# Аргументы командной строки
# =========================
# Режим указывается явно, это обязательный аргумент:
#   python compare.py in-cbd-not-in-gdr
#       -> достижения, которые есть в cbd, а в gdr их нет (или в cbd процент выше)
#
#   python compare.py in-gdr-not-in-cbd
#       -> наоборот: достижения из gdr, которых нет в cbd (или в gdr процент выше)
#
# Файлы по умолчанию: cbd.txt (рейтинг CoreBoard, ники без ID) и gdr.txt (рейтинг
# gd-rating.py, запущенный с --ids: ID записан в [скобках] в конце строки).
# ID игроков всегда берутся из gdr, ники из cbd. Другие файлы: --cbd и --gdr.
# Соответствие ник -> ID (gdr-ids.json) всегда скачивается с GitHub, локальный файл не читается.
#
# Команды /fulladd и /try здесь больше не создаются:
# их делает add-video-links.py (он берёт missing_achievements.json отсюда).

MODES = ("in-cbd-not-in-gdr", "in-gdr-not-in-cbd")

parser = argparse.ArgumentParser(
    description="Находит достижения, которых не хватает в одном рейтинге по сравнению с другим."
)
parser.add_argument("mode", choices=MODES,
                    help="in-cbd-not-in-gdr: из cbd то, чего нет в gdr; "
                         "in-gdr-not-in-cbd: из gdr то, чего нет в cbd")
parser.add_argument("--cbd", default="cbd.txt",
                    help="файл рейтинга CoreBoard (ники, без ID). По умолчанию cbd.txt")
parser.add_argument("--gdr", default="gdr.txt",
                    help="файл gd-rating (ID в [скобках] в конце строки, делается с --ids). "
                         "По умолчанию gdr.txt")
parser.add_argument("--id-map", default="",
                    help="in-gdr-not-in-cbd: JSON с ID игроков CoreBoard (coreboard_id_map.json: ID -> {nick, gdr_id}). "
                         "По умолчанию coreboard_id_map.json в корне репозитория (или рядом со скриптом). Новые игроки получают ID по порядку "
                         "после самого большого в этом файле. Нет файла: ключи JSON наши")
parser.add_argument("--cbd-ids", default="",
                    help="JSON ник -> ID игрока на CoreBoard (делает coreboard-parse.py, когда в ссылках на игроков появятся ID). "
                         "По умолчанию cbd_ids.json рядом с файлом --cbd, если он есть. ID с сайта главнее coreboard_id_map.json")
parser.add_argument("--include-absent", action="store_true",
                    help="in-cbd-not-in-gdr: игроки CoreBoard, которых НЕТ в gdr совсем, но чей ID нашёлся "
                         "(gdr-ids.json или coreboard_id_map.json), сравниваются с пустым gdr: все их записи попадают в результат")
parser.add_argument("--out-dir", default="compare",
                    help="папка для всех создаваемых файлов (создаётся сама, рядом с тем местом, "
                         "откуда запущен скрипт). По умолчанию compare. Чтобы писать в текущую папку: --out-dir .")
parser.add_argument("--new-max-score", type=float, default=250.0,
                    help="in-gdr-not-in-cbd: в missing_players.json идут игроки gdr, которых нет в cbd, "
                         "с очками МЕНЬШЕ этого числа (по умолчанию 250, для всех стран)")
args = parser.parse_args()

# Внутри скрипта 1-й файл это ИСТОЧНИК (из него берём достижения),
# 2-й это ЭТАЛОН (с ним сравниваем). Режим только решает, кто из них cbd, а кто gdr.
if args.mode == "in-cbd-not-in-gdr":
    NAME1, FILE1 = "cbd", args.cbd
    NAME2, FILE2 = "gdr", args.gdr
else:
    NAME1, FILE1 = "gdr", args.gdr
    NAME2, FILE2 = "cbd", args.cbd

# True, если у файла ID записан прямо в строке [в скобках]: это всегда gdr
FILE1_HAS_ID = (NAME1 == "gdr")
FILE2_HAS_ID = (NAME2 == "gdr")

# Все создаваемые файлы кладём в одну папку (по умолчанию compare)
OUT_DIR = args.out_dir
os.makedirs(OUT_DIR, exist_ok=True)


def out_path(name):
    return os.path.join(OUT_DIR, name)


OUTPUT_FILE = out_path("missing_achievements.json")
OUTPUT_NICKS_FILE = out_path("missing_achievements_with_nicks.json")
INVALID_FILE1 = out_path(f"invalid_{NAME1}_lines.txt")
INVALID_FILE2 = out_path(f"invalid_{NAME2}_lines.txt")
NOT_IN_FILE2 = out_path(f"{NAME1}_not_in_{NAME2}.txt")
NOT_IN_FILE1 = out_path(f"{NAME2}_not_in_{NAME1}.txt")

# Префикс временного ключа для игроков, у которых ник есть, а ID не нашёлся
NICK_KEY_PREFIX = "nick:"

API_TRIES = 4  # сколько раз пробовать запрос к API, прежде чем сдаться

# gdr-ids.json (ник -> ID игрока) берётся только отсюда, локальный файл не используется
# МОБИЛЬНАЯ ВЕРСИЯ: ID игроков и уровни лежат в папке mobile/ (рядом с scripts/), с GitHub и из глобала ничего не берётся.
_DATA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVELS_FILE = os.path.join(_DATA, "levels_mobile.json")   # {"next_id": N, "levels": {"ID": {"name", "place", "T", "t"}}}
IDS_FILE = os.path.join(_DATA, "ids_mobile.json")         # ник -> ID игрока (ID = ID аккаунта на WML)

# Полный список уровней глобала (нужен, чтобы найти ID уровня по названию)
GLOBAL_LIST_URL = "https://api.demonlist.org/level/classic/list?limit={limit}&offset={offset}"

# Особые названия уровней (ID в глобале -> как он называется) и сравнение названий лежат в levels.py,
# общем для всех скриптов (gd-rating, sync-extras, add-video-links и т.д.). Дописали название там,
# и оно работает здесь.
import levels as levelsearch
import lists_core as L  # метки листов ([PC], [WML]) у ников игроков, которых нет в глобале

SPECIAL_LEVEL_NAMES = levelsearch.SPECIAL_LEVEL_NAMES
normalize = levelsearch.normalize

FLAG_RE = re.compile(r'^(?:[\U0001F1E6-\U0001F1FF]{2}|[\U0001F3F3\U0001F3F4]\uFE0F?)\s*')


def normalize_nick(nick):
    nick = FLAG_RE.sub('', nick.strip())
    return normalize(nick)


def normalize_id(player_id):
    return str(player_id).strip().casefold()


def read_lines(filename):
    with open(filename, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def split_line_parts(line):
    """
    Разбирает строку вида:
      17) 🇷🇺 ROZE (Sakupen circles (Nick24), Tidal Wave 87%) - 22.47 [12345]
    Ищет закрывающую ')' перед '- очки' и идёт влево по балансу скобок до её парной '('.
    Возвращает (nick, achievements_str, score) или (None, None, None).
    """
    tail_match = re.search(
        r'\)\s*-\s*(\d+(?:\.\d+)?)(?:\s+\[[^\]]+\])?\s*$',
        line
    )
    if not tail_match:
        return None, None, None

    score = float(tail_match.group(1))
    close_paren_idx = tail_match.start()  # индекс ')' прямо перед '- очки'

    depth = 0
    open_paren_idx = None
    for idx in range(close_paren_idx, -1, -1):
        ch = line[idx]
        if ch == ')':
            depth += 1
        elif ch == '(':
            depth -= 1
            if depth == 0:
                open_paren_idx = idx
                break

    if open_paren_idx is None:
        return None, None, score

    prefix = line[:open_paren_idx]
    achievements_str = line[open_paren_idx + 1:close_paren_idx]

    head_match = re.match(r'^\s*\d+\)\s*(.*?)\s*$', prefix)
    if not head_match:
        return None, achievements_str, score

    nick = FLAG_RE.sub('', head_match.group(1).strip())
    return (nick or None), achievements_str, score


def parse_nick(line):
    nick, _, _ = split_line_parts(line)
    return nick


LINE_FLAG_RE = re.compile(r'^\s*\d+\)\s*([\U0001F1E6-\U0001F1FF]{2})')


def parse_flag(line):
    """Код страны (US, RU...) из флага-эмодзи в начале строки рейтинга; нет флага - пустая строка."""
    match = LINE_FLAG_RE.match(line)
    if not match:
        return ""
    return "".join(chr(ord("A") + ord(ch) - 0x1F1E6) for ch in match.group(1))


def parse_id(line):
    match = re.search(r'\[([^\]]+)\]\s*$', line)
    return normalize_id(match.group(1)) if match else None


def parse_score(line):
    _, _, score = split_line_parts(line)
    return score


def parse_achievements(line):
    _, achievements_str, _ = split_line_parts(line)

    if achievements_str is None:
        return {}

    achievements = {}

    for item in achievements_str.split(","):
        item = item.strip()

        if not item or item == "No":  # No: пустой слот рекорда (только точно так)
            continue

        percent_match = re.search(r'\s+(\d+)%$', item)

        if percent_match:
            percent = int(percent_match.group(1))
            name = item[:percent_match.start()].strip()
        else:
            percent = 100
            name = item

        achievements[normalize(name)] = {
            "name": name,
            "percent": percent
        }

    return achievements


def player_id_sort_key(player_key):
    """Числовые ID по возрастанию, затем pc-ID по возрастанию."""
    player_id = str(player_key).split(" — ", 1)[0].strip().casefold()

    if player_id.startswith("pc"):
        suffix = player_id[2:]
        if suffix.isdigit():
            return (1, 0, int(suffix), suffix)
        return (1, 1, suffix)

    if player_id.isdigit():
        return (0, 0, int(player_id), player_id)

    # Прочие не-PC ID остаются перед PC-ID.
    return (0, 1, player_id)


def save_json(filename, result):
    with open(filename, "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')

        users = list(result.items())  # порядок задан при сборке: по очкам игрока (от меньшего к большему)

        for i, (player_id, records) in enumerate(users):
            f.write(
                f'    {json.dumps(player_id, ensure_ascii=False)}: [\n'
            )

            for j, record in enumerate(records):
                comma = "," if j < len(records) - 1 else ""

                f.write(
                    f'      {json.dumps(record, ensure_ascii=False)}{comma}\n'
                )

            comma = "," if i < len(users) - 1 else ""
            f.write(f'    ]{comma}\n')

        f.write('  }\n}\n')


# =========================
# Запросы к API и поиск ID уровня по названию
# =========================


def fetch_json(url):
    """
    GET-запрос с повторными попытками (до API_TRIES раз).
    Возвращает JSON или, если все попытки провалились, вызывает ошибку.
    """
    last_error = None

    for attempt in range(1, API_TRIES + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0"}
            )

            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)

            time.sleep(0.2)  # небольшая пауза, чтобы не заспамить сервер
            return payload
        except urllib.error.HTTPError as error:
            # 404, 403 и подобные - повторять бессмысленно
            if error.code < 500 and error.code != 429:
                raise
            last_error = error
        except Exception as error:
            last_error = error

        print(f"  Запрос не удался (попытка {attempt}/{API_TRIES}): {last_error}")

        if attempt < API_TRIES:
            time.sleep(2 * attempt)  # пауза растёт: 2, 4, 6... секунд

    raise last_error


# =========================
# Загружаем ID игроков (ник -> ID): gdr-ids.json ТОЛЬКО с GitHub
# =========================

print(f"ids_mobile.json: беру из {IDS_FILE}")

try:
    with open(IDS_FILE, encoding="utf-8") as _f:
        raw_ids = json.load(_f)
except Exception as error:
    sys.exit(f"ids_mobile.json: не удалось прочитать ({error}). Сначала запусти mobile-rating.py. Останавливаюсь.")

if not isinstance(raw_ids, dict) or not raw_ids:
    sys.exit("ids_mobile.json: не словарь или пустой файл. Останавливаюсь.")

nick_to_id = {
    normalize_nick(nick): normalize_id(player_id)
    for nick, player_id in raw_ids.items()
}


# =========================
# ID игроков на CoreBoard
# =========================
# Два источника, ID С САЙТА ГЛАВНЕЕ:
#   1. cbd_ids.json (ник -> ID): делает coreboard-parse.py, когда в ссылках на игроков стоят числовые ID.
#      Пока ссылки идут по нику, файла нет.
#   2. coreboard_id_map.json ({"<ID>": {"nick", "gdr_id"}}) в корне репозитория: ID -> наш ID.
# Если ID игрока на сайте есть в карте, игрок сопоставляется с gdr по ID (а не по нику). Без ID сайта работает
# сопоставление по нику через gdr-ids.json, как раньше. Новым игрокам ID выдаются после самого большого из обоих
# источников. Нет ни того, ни другого: ключи JSON наши.
cb_id_by_nick = {}   # нормализованный ник cbd -> ID CoreBoard (с сайта)
gdr_by_cb_id = {}    # ID CoreBoard -> наш ID (карта)
gdr_by_map_nick = {} # нормализованный ник из карты -> наш ID (запасной вариант, если ника нет в gdr-ids.json)
map_nicks = {}       # нормализованный ник из карты -> множество наших ID (для проверки уникальности ника новых игроков)
cb_id_by_gdr = {}    # наш ID (нормализованный) -> ID CoreBoard (карта)
max_cb_id = 0

cbd_dir = os.path.dirname(os.path.abspath(args.cbd))
cb_ids_file = args.cbd_ids or os.path.join(cbd_dir, "cbd_ids.json")
if os.path.exists(cb_ids_file):
    try:
        with open(cb_ids_file, encoding="utf-8") as f:
            cb_id_by_nick = {
                normalize_nick(nick): str(cb_id).strip()
                for nick, cb_id in json.load(f).items()
                if str(cb_id).strip().isdigit()
            }
    except (OSError, ValueError, AttributeError) as error:
        print(f"{cb_ids_file}: не удалось прочитать ({error}), ID с сайта не используются")
if cb_id_by_nick:
    max_cb_id = max(int(v) for v in cb_id_by_nick.values())
    print(f"ID CoreBoard с сайта: {len(cb_id_by_nick)} игроков ({cb_ids_file})")

# По умолчанию карта лежит в корне репозитория (папка выше scripts/); если там её нет, ищем рядом со скриптом
_here = os.path.dirname(os.path.abspath(__file__))
id_map_file = args.id_map or next(
    (p for p in (os.path.join(os.path.dirname(_here), "coreboard_id_map.json"),
                 os.path.join(_here, "coreboard_id_map.json")) if os.path.exists(p)),
    os.path.join(os.path.dirname(_here), "coreboard_id_map.json"))
if os.path.exists(id_map_file):
    try:
        with open(id_map_file, encoding="utf-8") as f:
            for cb_id, row in json.load(f).items():
                cb_id = str(cb_id).strip()
                if not cb_id.isdigit():
                    continue
                max_cb_id = max(max_cb_id, int(cb_id))
                gdr_id = row.get("gdr_id") if isinstance(row, dict) else None
                if gdr_id:
                    if row.get("nick"):
                        gdr_by_map_nick.setdefault(normalize_nick(str(row["nick"])), normalize_id(gdr_id))
                        map_nicks.setdefault(normalize_nick(str(row["nick"])), set()).add(normalize_id(gdr_id))
                    gdr_by_cb_id[cb_id] = normalize_id(gdr_id)
                    cb_id_by_gdr[normalize_id(gdr_id)] = cb_id
    except (OSError, ValueError, AttributeError) as error:
        sys.exit(f"{id_map_file}: не удалось прочитать ({error}). Останавливаюсь.")
    print(f"coreboard_id_map.json: {len(cb_id_by_gdr)} игроков, самый большой ID {max_cb_id}")
if not (cb_id_by_nick or cb_id_by_gdr):
    print("ID CoreBoard не найдены (нет ни cbd_ids.json, ни coreboard_id_map.json): ключи в missing_players.json остаются нашими")


# =========================
# Загружаем уровни: levels_tp.json ТОЛЬКО с GitHub
# =========================

print(f"levels_mobile.json: беру из {LEVELS_FILE}")

try:
    with open(LEVELS_FILE, encoding="utf-8") as _f:
        levels_data = json.load(_f)["levels"]
except Exception as error:
    sys.exit(f"levels_mobile.json: не удалось прочитать ({error}). Сначала запусти mobile-levels.py. Останавливаюсь.")

if not isinstance(levels_data, dict) or not levels_data:
    sys.exit("levels_mobile.json: пустой список уровней. Останавливаюсь.")

level_names = {}           # название (нормализованное) -> ID уровня

for level_id, data in levels_data.items():
    name = data.get("name")

    if not name:
        continue

    output_id = str(level_id)   # мобильная версия: ID уровня = название на CoreBoard ("x..." у алиасов)

    normalized_name = normalize(name)

    if normalized_name not in level_names:
        level_names[normalized_name] = output_id


# Список уровней глобала: {нормализованное название: ID}.
# None = ещё не загружали. Загружается один раз и только если понадобится.
global_levels_by_name = None

# Обратный словарь особых названий: {нормализованное название: ID}
special_name_to_id = {
    normalize(name): int(level_id)
    for level_id, name in SPECIAL_LEVEL_NAMES.items()
}


def load_global_levels():
    """Скачивает весь список уровней глобала (один раз за запуск)."""
    global global_levels_by_name

    global_levels_by_name = {}   # МОБИЛЬНАЯ ВЕРСИЯ: глобал не используется, уровни только из levels_mobile.json
    return global_levels_by_name

    result = {}
    step = 100
    offset = 0

    print("\nИщу недостающие уровни в глобале, загружаю их список...")

    try:
        while True:
            payload = fetch_json(
                GLOBAL_LIST_URL.format(limit=step, offset=offset)
            )
            levels = (payload.get("data") or {}).get("levels") or []

            if not levels:
                break

            added = 0

            for level in levels:
                name = level.get("name")
                level_id = level.get("id")

                if not name or level_id is None:
                    continue

                key = normalize(name)

                if key not in result:
                    result[key] = int(level_id)
                    added += 1

            # Защита от бесконечного цикла: если страница ничего нового не дала
            if added == 0:
                break

            offset += step

        print(f"  Уровней загружено из глобала: {len(result)}")
    except Exception as error:
        print(f"  Не удалось загрузить список уровней из глобала: {error}")

    # Запоминаем результат (даже неполный), чтобы не качать список заново
    global_levels_by_name = result
    return result


def find_level_id(normalized_name):
    """
    Ищет ID уровня по названию. Порядок поиска:
      1) levels_tp.json;
      2) особые названия (SPECIAL_LEVEL_NAMES);
      3) глобал (список уровней из API, с повторными попытками).

    Возвращает (ID, где найден) или (None, None).
    """
    level_id = level_names.get(normalized_name)

    if level_id is not None:
        return level_id, "levels_tp.json"

    level_id = special_name_to_id.get(normalized_name)

    if level_id is not None:
        return level_id, "особые названия"

    level_id = load_global_levels().get(normalized_name)

    if level_id is not None:
        return level_id, "глобал"

    return None, None


# =========================
# Универсальное чтение файла
# =========================

def read_players(filename, id_in_line, invalid_filename):
    """
    Читает файл и возвращает (игроки, статистика).

    id_in_line=True  -> ID берём из [скобок] в конце строки.
    id_in_line=False -> берём ник и ищем по нему ID в gdr-ids.json.
                        Если ника там нет, строка НЕ считается некорректной:
                        игрок сохраняется с временным ключом "nick:<ник>",
                        который ни с кем не совпадёт (по нику игроки не сопоставляются,
                        только через gdr-ids.json).

    Игроки возвращаются словарём, где ключ - ID игрока.
    В каждом игроке есть "order" - номер его строки в файле
    (по нему выводятся списки несопоставленных игроков).
    """
    players = {}
    stats = {
        "lines": 0,
        "invalid": 0,
        "missing_id": 0,
        "missing_score": 0,
        "duplicates": 0,
    }
    invalid_lines = []

    for order, line in enumerate(read_lines(filename)):
        stats["lines"] += 1

        nick = parse_nick(line)
        score = parse_score(line)
        reasons = []

        if id_in_line:
            player_id = parse_id(line)

            if not player_id:
                stats["missing_id"] += 1
                reasons.append("нет ID в [скобках]")
        else:
            player_id = None

            if not nick:
                reasons.append("не удалось найти ник")
            else:
                # ID с сайта главнее: если он есть в карте, игрок сопоставляется по нему, иначе по нику
                site_id = cb_id_by_nick.get(normalize_nick(nick))
                player_id = gdr_by_cb_id.get(site_id) if site_id else None
                if not player_id:
                    player_id = nick_to_id.get(normalize_nick(nick))
                if not player_id:  # ника нет в gdr-ids.json: пробуем coreboard_id_map.json
                    player_id = gdr_by_map_nick.get(normalize_nick(nick))

                if not player_id:
                    stats["missing_id"] += 1
                    # Не невалидная строка: просто игрок без пары в gdr-ids.json
                    player_id = NICK_KEY_PREFIX + normalize_nick(nick)

        if score is None:
            stats["missing_score"] += 1
            reasons.append("не удалось найти очки")

        if reasons:
            stats["invalid"] += 1
            invalid_lines.append(f"[{', '.join(reasons)}] {line}")
            continue

        player_id = normalize_id(player_id)

        if player_id in players:
            stats["duplicates"] += 1
            # при повторе остаётся позиция первой встреченной строки
            order = players[player_id]["order"]

        players[player_id] = {
            "nick": nick,  # может быть None, если ника в строке нет
            "score": round(score, 2),
            "achievements": parse_achievements(line),
            "order": order,
            "line": line  # исходная строка файла целиком
        }

    with open(invalid_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(invalid_lines))

    return players, stats


players1, stats1 = read_players(FILE1, FILE1_HAS_ID, INVALID_FILE1)
players2, stats2 = read_players(FILE2, FILE2_HAS_ID, INVALID_FILE2)


# Игроки cbd сопоставляются с gdr ТОЛЬКО через gdr-ids.json (ник -> ID).
# Если ника из cbd нет в gdr-ids.json, у игрока остаётся временный ключ "nick:<ник>",
# он ни с кем не совпадает и в сравнение не попадает (виден только в *_not_in_*.txt).


def get_nick(player_id):
    """
    Ник берём в первую очередь из файла БЕЗ ID в скобках
    (из того, где ники нужны для сопоставления по gdr-ids.json).
    Если там игрока нет, берём из другого файла.
    """
    # Если ID записан в 1-м файле, то файл с никами - 2-й, и наоборот
    if FILE1_HAS_ID:
        nick_file_players, id_file_players = players2, players1
    else:
        nick_file_players, id_file_players = players1, players2

    # Порядок важен: сначала файл с никами, потом запасной вариант
    for players in (nick_file_players, id_file_players):
        player = players.get(player_id)
        if player and player["nick"]:
            return player["nick"]
    return "?"


def show_id(player_id):
    """Как показывать ID в списках: временный ключ заменяем пометкой."""
    return "нет ID" if player_id.startswith(NICK_KEY_PREFIX) else player_id


# =========================
# Сопоставление игроков
# =========================

common_ids = set(players1) & set(players2)

ids_only_in_1 = set(players1) - set(players2)
ids_only_in_2 = set(players2) - set(players1)


def players_in_file_order(ids, players):
    """ID игроков в том порядке, в каком они идут в своём файле."""
    return sorted(ids, key=lambda player_id: players[player_id]["order"])


def write_not_found(filename, ids, players):
    with open(filename, "w", encoding="utf-8") as f:
        for player_id in players_in_file_order(ids, players):
            f.write(players[player_id]["line"] + "\n")


write_not_found(NOT_IN_FILE2, ids_only_in_1, players1)
write_not_found(NOT_IN_FILE1, ids_only_in_2, players2)


# =========================
# Сравниваем достижения
# =========================
# ЧТО ОТБИРАЕТСЯ:
#   Игрок есть в обоих файлах (совпал по ID):
#      1) очки в 1-м файле меньше, чем во 2-м;
#      2) из достижений 1-го файла берём те, которых нет во 2-м,
#         либо у которых процент в 1-м выше, чем во 2-м.
#   Игроков, которых нет в одном из файлов, не трогаем совсем: их достижения
#   в результат не попадают (список таких игроков лежит в *_not_in_*.txt).
#   Уровень должен найтись (levels_tp.json, особые названия или глобал).

result = {}
result_with_nicks = {}

unknown_levels = set()
levels_found_elsewhere = {}  # название -> (ID, где найден), если не в levels_tp.json

checked = 0
lower_score = 0
players_with_missing = 0

# Сравниваем только игроков, которые есть в обоих файлах.
# Игроки cbd, которых нет в gdr совсем, но чей ID нашёлся (gdr-ids.json или coreboard_id_map.json): сравниваем
# с пустым gdr, чтобы все их записи попали в результат (иначе они пропадали бы из my_extras при пересборке).
# Игроки без найденного ID (ключ "nick:...") по-прежнему не трогаются.
absent_ids = set()
if args.include_absent and args.mode == "in-cbd-not-in-gdr":
    absent_ids = {i for i in ids_only_in_1 if not i.startswith(NICK_KEY_PREFIX)}
    print(f"Игроков cbd с известным ID, но без записи в gdr: {len(absent_ids)}")

# В порядке очков (от меньшего к большему, при равных очках как в файле): в этом порядке игроки лягут в JSON.
for player_id in sorted(common_ids | absent_ids, key=lambda i: (players1[i]["score"], players1[i]["order"])):
    p1 = players1[player_id]
    # у отсутствующего в gdr игрока очки считаем бесконечными, чтобы проверка "очки меньше" его пропустила
    p2 = players2.get(player_id) or {"score": float("inf"), "achievements": {}}

    checked += 1

    if p1["score"] >= p2["score"]:
        continue

    lower_score += 1

    improvements = []

    for achievement_name, a1 in p1["achievements"].items():
        a2 = p2["achievements"].get(achievement_name)

        if a2 is None or a1["percent"] > a2["percent"]:
            improvements.append((achievement_name, a1))

    records_without_names = []
    records_with_names = []

    for achievement_name, achievement in sorted(improvements, key=lambda x: x[0]):
        level_id, found_in = find_level_id(achievement_name)

        if level_id is None:
            unknown_levels.add(achievement["name"])
            continue

        # Запоминаем уровни, которых не было в levels_tp.json
        if found_in != "levels_tp.json":
            levels_found_elsewhere[achievement["name"]] = (level_id, found_in)

        records_without_names.append([
            level_id,
            achievement["percent"],
            ""
        ])

        # название уровня второй раз не пишем: ID уровня и есть его название на CoreBoard
        records_with_names.append([
            level_id,
            achievement["percent"]
        ])

    if records_without_names:
        result[player_id] = records_without_names
        result_with_nicks[f"{player_id} — {get_nick(player_id)}"] = records_with_names
        players_with_missing += 1


# =========================
# Новые игроки: есть в gdr, нет в cbd
# =========================
# Только в режиме in-gdr-not-in-cbd. Берём игроков gdr, которых нет в cbd, с очками меньше
# --new-max-score (по умолчанию 250), для ВСЕХ стран. Результат: missing_players.json
#   {"users": {"<ID>": {"nick": "Ник", "country_code": "US",
#                       "gdr_id": "<наш ID>", "records": [[ID уровня, процент], ...]}}}
# ID-ключ: если есть ID CoreBoard (сайт или coreboard_id_map.json), это НОВЫЕ ID по порядку после самого большого;
# иначе наш ID (глобал, pcN или wmlN). Наш ID всегда лежит в gdr_id. Ссылки на видео дописывает add-video-links.py.
# Игрок НЕ попадает в файл (а пишется в missing_players_skipped.txt с причиной), если:
#   - его ник совпал с ником игрока cbd, которого мы не смогли сопоставить, или у них почти одни и те же
#     уровни (скорее всего это тот же человек под другим ником, дубль на CoreBoard не нужен);
#   - хотя бы один его уровень не нашёлся (иначе у него было бы меньше записей, чем есть на самом деле).
NEW_PLAYERS_FILE = out_path("missing_players.json")
SKIPPED_PLAYERS_FILE = out_path("missing_players_skipped.txt")
SAME_LEVELS_MIN = 4  # столько общих уровней с несопоставленным игроком cbd = подозрение на тот же человек

new_players = {}
skipped_players = []


def clean_nick(player_id, nick):
    """Ник без метки листа: 'Name [WML]' -> 'Name'."""
    nick = (nick or "").strip()
    tag = L.tag_of(player_id).strip()
    if tag and nick.endswith(tag):
        nick = nick[: -len(tag)].strip()
    return nick


CLAN_TAG_RE = re.compile(r"\[[^\]]*\]")


def strip_clan_tags(nick):
    """Клан-теги в квадратных скобках убираем: '[OX] zsakos' -> 'zsakos'. Если остался бы пустой ник, не трогаем."""
    stripped = re.sub(r"\s+", " ", CLAN_TAG_RE.sub("", nick)).strip()
    return stripped or nick


# Ники, которые уже заняты: все ники cbd и ники из coreboard_id_map.json (кроме самого игрока), плюс те, что новые
# игроки получили раньше в этом запуске.
taken_nicks = {normalize_nick(p["nick"]) for p in players2.values() if p["nick"]} if args.mode == "in-gdr-not-in-cbd" else set()


def unique_nick(player_id, nick):
    """Ник без клан-тегов; если он уже есть на CoreBoard, в карте (у другого игрока) или встречался раньше,
    в конец ставится число: Nejou -> Nejou2 -> Nejou3."""
    base = strip_clan_tags(nick)
    own = normalize_id(player_id)
    candidate, n = base, 1
    while True:
        key = normalize_nick(candidate)
        if key not in taken_nicks and not (map_nicks.get(key, set()) - {own}):
            break
        n += 1
        candidate = f"{base}{n}"
    taken_nicks.add(normalize_nick(candidate))
    return candidate


if args.mode == "in-gdr-not-in-cbd":
    # Новым игрокам CoreBoard-ID даём по порядку, начиная со следующего после числа игроков в cbd
    # (в порядке рейтинга gdr). Если игрок уже есть в карте (а на CoreBoard его нет), он сохраняет свой ID.
    # Мобильная доска: нумерация новых игроков начинается со следующего номера после числа игроков в cbd (в cbd 200 игроков:
    # новые получают 201, 202, ...). Занятые ID (уже выданные в карте или найденные на сайте) пропускаются.
    # Карта (mobile-id-map.py) нумерует игроков cbd подряд, без пропусков: новые идут сразу за её последним ID. Карты нет:
    # за числом игроков в cbd.
    next_cb_id = (max([int(k) for k in gdr_by_cb_id if k.isdigit()] or [0]) or len(players2)) + 1
    used_cb_ids = set(cb_id_by_gdr.values()) | set(cb_id_by_nick.values())
    unmapped_cbd = [p for pid, p in players2.items() if pid.startswith(NICK_KEY_PREFIX)]
    unmapped_cbd_nicks = {normalize_nick(p["nick"]) for p in unmapped_cbd if p["nick"]}

    for player_id in players_in_file_order(ids_only_in_1, players1):
        p1 = players1[player_id]
        if p1["score"] >= args.new_max_score:
            continue

        nick = clean_nick(player_id, p1["nick"])
        names = set(p1["achievements"])

        if normalize_nick(nick) in unmapped_cbd_nicks or normalize_nick(strip_clan_tags(nick)) in unmapped_cbd_nicks:
            skipped_players.append(f"{player_id} | {nick} | {p1['score']} | такой ник уже есть в cbd, но не сопоставился по gdr-ids.json")
            continue
        twin = next((p for p in unmapped_cbd if len(names & set(p["achievements"])) >= SAME_LEVELS_MIN), None)
        if twin is not None:
            skipped_players.append(f"{player_id} | {nick} | {p1['score']} | похож на игрока cbd {twin['nick']} (общих уровней: "
                                   f"{len(names & set(twin['achievements']))}), возможно тот же человек под другим ником")
            continue

        records, unresolved = [], []
        for achievement_name, achievement in p1["achievements"].items():
            level_id, found_in = find_level_id(achievement_name)
            if level_id is None:
                unresolved.append(achievement["name"])
                unknown_levels.add(achievement["name"])
                continue
            if found_in != "levels_tp.json":
                levels_found_elsewhere[achievement["name"]] = (level_id, found_in)
            records.append([level_id, achievement["percent"]])   # без видео: у новых игроков его в файле нет

        if unresolved or not records:
            skipped_players.append(f"{player_id} | {nick} | {p1['score']} | не найден уровень: {', '.join(unresolved) or 'нет записей'}")
            continue
        # Записи новых игроков: сначала с наименьшей стоимостью f (как в gd-rating, меньше = сложнее). Первая запись
        # получит ссылку на видео в add-video-links.py. Нет T/t у уровня в levels_tp.json или запись не даёт очков: в конец.
        def record_cost(record):
            lv = levels_data.get(str(record[0])) or {}
            try:
                place, T, t, c = float(lv["place"]), float(lv["T"]), float(lv["t"]), float(record[1])
                if c >= 100:
                    f = place
                elif not (t < T) or c < t or c <= 0:
                    return (float("inf"), place)
                else:
                    f = place * 2 * (T / c) ** (math.log(4 / 2) / math.log(T / t))
                return (f, place)
            except (KeyError, TypeError, ValueError):
                return (float("inf"), float("inf"))
        records.sort(key=record_cost)
        code = parse_flag(p1["line"])
        info = {
            "nick": unique_nick(player_id, nick),
            "country_code": code or None,
            "gdr_id": player_id,  # наш ID: по нему add-video-links ищет видео
            "records": records,
        }
        if cb_id_by_gdr or cb_id_by_nick:
            key = cb_id_by_gdr.get(normalize_id(player_id))
            if key is None or key in new_players:
                while str(next_cb_id) in used_cb_ids or str(next_cb_id) in new_players:
                    next_cb_id += 1
                key = str(next_cb_id)
                next_cb_id += 1
        else:
            key = player_id
        new_players[key] = info


def save_players_json(players, filename):
    with open(filename, "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(players.items())
        for i, (pid, info) in enumerate(items):
            f.write("    " + json.dumps(pid, ensure_ascii=False) + ": " + json.dumps(info, ensure_ascii=False))
            f.write(",\n" if i < len(items) - 1 else "\n")
        f.write("  }\n}\n")


GDR_TO_CB_FILE = out_path("gdr_to_cb_ids.json")

if args.mode == "in-gdr-not-in-cbd":
    save_players_json(new_players, NEW_PLAYERS_FILE)
    # Наш ID -> ID на CoreBoard для игроков, которые есть в обоих рейтингах (по нему cbd-update переводит
    # missing_achievements*.json на ID CoreBoard). Нет ID CoreBoard: файла нет, переводить нечего.
    gdr_to_cb = {}
    for pid in common_ids:
        cb_id = cb_id_by_nick.get(normalize_nick(players2[pid]["nick"] or "")) or cb_id_by_gdr.get(normalize_id(pid))
        if cb_id:
            gdr_to_cb[pid] = cb_id
    if os.path.exists(GDR_TO_CB_FILE):
        os.remove(GDR_TO_CB_FILE)
    if gdr_to_cb:
        with open(GDR_TO_CB_FILE, "w", encoding="utf-8") as f:
            json.dump(gdr_to_cb, f, ensure_ascii=False, indent=1)
    if cb_id_by_gdr or cb_id_by_nick:
        print(f"ID CoreBoard: сопоставлено игроков {len(gdr_to_cb)} из {len(common_ids)}; "
              f"новым игрокам выдано ID {len(new_players)} ")
    with open(SKIPPED_PLAYERS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(skipped_players) + ("\n" if skipped_players else ""))


# =========================
# Сохраняем оба JSON
# =========================

save_json(OUTPUT_FILE, result)
save_json(OUTPUT_NICKS_FILE, result_with_nicks)


# =========================
# Статистика
# =========================

def print_stats(title, filename, id_in_line, stats, players):
    source = "ID из [скобок]" if id_in_line else "ID по нику"
    print(f"\n=== {title}: {filename} ({source}) ===")
    print(f"Строк: {stats['lines']}")
    print(f"Некорректных строк: {stats['invalid']}")
    print(f"Строк без ID: {stats['missing_id']}")
    print(f"Строк без очков: {stats['missing_score']}")
    print(f"Повторяющихся ID: {stats['duplicates']}")
    print(f"Уникальных игроков: {len(players)}")


print_stats(f"{NAME1} (источник)", FILE1, FILE1_HAS_ID, stats1, players1)
print_stats(f"{NAME2} (эталон)", FILE2, FILE2_HAS_ID, stats2, players2)

print("\n=== Сопоставление ===")
print(f"Совпадений по ID: {len(common_ids)}")
print(f"Игроков {NAME1}, которых нет в {NAME2}: {len(ids_only_in_1)}")
print(f"Игроков {NAME2}, которых нет в {NAME1}: {len(ids_only_in_2)}")

if args.mode == "in-gdr-not-in-cbd":
    print(f"\n=== Новые игроки (очков меньше {args.new_max_score:g}) ===")
    print(f"В missing_players.json: {len(new_players)}")
    print(f"Пропущено (причины в missing_players_skipped.txt): {len(skipped_players)}")

print("\n=== Результат ===")
print(f"Игроков сопоставлено: {checked}")
print(f"У кого очков в {NAME1} меньше, чем в {NAME2}: {lower_score}")
print(f"Игроков с недостающими достижениями: {players_with_missing}")
print(f"Добавлено записей: {sum(map(len, result.values()))}")
print(f"Найдено не в levels_tp.json: {len(levels_found_elsewhere)}")
print(f"Не удалось найти уровни нигде: {len(unknown_levels)}")

if levels_found_elsewhere:
    print("\nУровни, которых нет в levels_tp.json, но ID найден (можно добавить в файл):")

    for name, (level_id, found_in) in sorted(levels_found_elsewhere.items()):
        print(f"  {name} -> {level_id} ({found_in})")

if unknown_levels:
    print("\nУровни, которых нет ни в levels_tp.json, ни в глобале:")

    for name in sorted(unknown_levels):
        print(" ", name)


print("\nСозданы файлы:")
for name in (OUTPUT_FILE, OUTPUT_NICKS_FILE, INVALID_FILE1,
             INVALID_FILE2, NOT_IN_FILE2, NOT_IN_FILE1,
             *((NEW_PLAYERS_FILE, SKIPPED_PLAYERS_FILE) if args.mode == "in-gdr-not-in-cbd" else ())):
    print(f"  {name}")
