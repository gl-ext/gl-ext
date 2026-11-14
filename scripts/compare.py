import os
import re
import sys
import json
import time
import argparse
import urllib.error
import urllib.request

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
parser.add_argument("--out-dir", default="compare",
                    help="папка для всех создаваемых файлов (создаётся сама, рядом с тем местом, "
                         "откуда запущен скрипт). По умолчанию compare. Чтобы писать в текущую папку: --out-dir .")
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
LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"
IDS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/gdr-ids.json"

# Полный список уровней глобала (нужен, чтобы найти ID уровня по названию)
GLOBAL_LIST_URL = "https://api.demonlist.org/level/classic/list?limit={limit}&offset={offset}"

# Особые названия: ID уровня в глобале -> как он называется.
# Нужны, чтобы найти ID уровня по названию из файла.
# ВАЖНО: такой же список лежит в add-video-links.py (там он нужен для команд).
# Если дописываете сюда новый уровень - допишите и туда.
SPECIAL_LEVEL_NAMES = {
    "3299": "Sakupen circles (Nick24)",
    "3351": "Thinking Space II (Legacy)",
    "2234": "Deimos (Endlevel)"
}


def normalize(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def normalize_nick(nick):
    nick = re.sub(
        r'^[\U0001F1E6-\U0001F1FF]{2}\s*',
        '',
        nick.strip()
    )
    return normalize(nick)


def normalize_id(player_id):
    return str(player_id).strip().casefold()


def read_lines(filename):
    with open(filename, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def parse_nick(line):
    match = re.match(r'^\s*\d+\)\s*(.*?)\s*\(', line)

    if not match:
        return None

    nick = match.group(1).strip()

    nick = re.sub(
        r'^[\U0001F1E6-\U0001F1FF]{2}\s*',
        '',
        nick
    )

    return nick


def parse_id(line):
    match = re.search(r'\[([^\]]+)\]\s*$', line)
    return normalize_id(match.group(1)) if match else None


def parse_score(line):
    match = re.search(
        r'\)\s*.*?\s-\s*(\d+(?:\.\d+)?)'
        r'(?:\s+\[[^\]]+\])?\s*$',
        line
    )

    return float(match.group(1)) if match else None


def parse_achievements(line):
    match = re.search(
        r'\((.*)\)\s*-\s*\d+(?:\.\d+)?'
        r'(?:\s+\[[^\]]+\])?\s*$',
        line
    )

    if not match:
        return {}

    achievements = {}

    for item in match.group(1).split(","):
        item = item.strip()

        if not item:
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

        users = sorted(result.items(), key=lambda item: player_id_sort_key(item[0]))

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

print("gdr-ids.json: беру с GitHub (raw кешируется до ~5 минут после правки)")

try:
    raw_ids = fetch_json(IDS_URL)
except Exception as error:
    sys.exit(f"gdr-ids.json: не удалось загрузить с GitHub ({error}). Останавливаюсь.")

if not isinstance(raw_ids, dict) or not raw_ids:
    sys.exit("gdr-ids.json: с GitHub пришёл не словарь или пустой файл. Останавливаюсь.")

nick_to_id = {
    normalize_nick(nick): normalize_id(player_id)
    for nick, player_id in raw_ids.items()
}


# =========================
# Загружаем уровни: levels_tp.json ТОЛЬКО с GitHub
# =========================

print("levels_tp.json: беру с GitHub (raw кешируется до ~5 минут после правки)")

try:
    levels_data = fetch_json(LEVELS_URL)["levels"]
except Exception as error:
    sys.exit(f"levels_tp.json: не удалось загрузить с GitHub ({error}). Останавливаюсь.")

if not isinstance(levels_data, dict) or not levels_data:
    sys.exit("levels_tp.json: на GitHub пустой список уровней. Останавливаюсь.")

level_names = {}           # название (нормализованное) -> ID уровня

for level_id, data in levels_data.items():
    name = data.get("name")

    if not name:
        continue

    if str(level_id).startswith("x"):
        output_id = str(level_id)
    else:
        output_id = int(level_id)

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

    if global_levels_by_name is not None:
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
                player_id = nick_to_id.get(normalize_nick(nick))

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
for player_id in common_ids:
    p1 = players1[player_id]
    p2 = players2[player_id]

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

        records_with_names.append([
            level_id,
            achievement["percent"],
            achievement["name"]
        ])

    if records_without_names:
        result[player_id] = records_without_names
        result_with_nicks[f"{player_id} — {get_nick(player_id)}"] = records_with_names
        players_with_missing += 1


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
             INVALID_FILE2, NOT_IN_FILE2, NOT_IN_FILE1):
    print(f"  {name}")
