import argparse
import json
import re
import time
from pathlib import Path

import requests

# Папку по умолчанию выбираем такую же, как у compare.py: он кладёт туда
# missing_achievements.json, а этот скрипт читает его оттуда и складывает рядом
# свои файлы (с видео, не найденные, источники, команды).
parser = argparse.ArgumentParser(
    description="Ищет видео для достижений из compare/missing_achievements.json "
                "и собирает команды /fulladd и /try."
)
parser.add_argument("--out-dir", default="compare",
                    help="папка compare.py: оттуда берётся missing_achievements.json, "
                         "туда же пишутся все результаты (создаётся сама). По умолчанию compare. "
                         "Чтобы работать в текущей папке: --out-dir .")
parser.add_argument("--ids", default="gdr-ids.json",
                    help="файл с ID игроков (ник -> ID), нужен для команд. По умолчанию gdr-ids.json")
args = parser.parse_args()

OUT_DIR = Path(args.out_dir)
OUT_DIR.mkdir(parents=True, exist_ok=True)

INPUT_FILE = str(OUT_DIR / "missing_achievements.json")
OUTPUT_FILE = str(OUT_DIR / "missing_achievements_with_videos.json")
NOT_FOUND_FILE = str(OUT_DIR / "missing_video_links.txt")
SOURCES_FILE = str(OUT_DIR / "video_link_sources.txt")  # откуда взято каждое найденное видео
COMMANDS_FILE = str(OUT_DIR / "commands.txt")           # команды /fulladd и /try
IDS_FILE = args.ids        # ник -> ID игрока (нужен для команд), лежит в текущей папке
LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"  # названия уровней (нужны для команд), только GitHub

# Особые названия: ID уровня в глобале -> как его надо писать в командах.
# Для этих уровней API не спрашивается, берётся название отсюда.
# ВАЖНО: такой же список лежит в compare.py. Если дописываете сюда новый
# уровень - допишите и туда.
SPECIAL_LEVEL_NAMES = {
    "3299": "Sakupen circles (Nick24)",
    "3351": "Thinking Space II (Legacy)",
    "2234": "Deimos (Endlevel)"
}

GLOBAL_API = "https://api.demonlist.org"
POINTERCRATE_API = "https://pointercrate.com/api/v1"
POINTERCRATE_DEMONS_API = "https://pointercrate.com/api/v2/demons"
LINKS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/refs/heads/main/links.json"

LIMIT = 100
DELAY = 0.15
TIMEOUT = 30
TRIES = 5        # сколько раз пробовать запрос, если сервер не отвечает
RETRY_PAUSE = 2  # базовая пауза между попытками (секунд), растёт с каждой попыткой

session = requests.Session()
session.headers.update({
    "User-Agent": "GD-Video-Link-Finder/1.0",
    "Accept": "application/json"
})

global_records_cache = {}
global_level_cache = {}
pointer_demon_cache = {}
pointer_records_cache = {}
level_name_cache = {}       # ID уровня -> название (чтобы не искать дважды)
name_failed_levels = []     # уровни, название которых не нашлось нигде

# Сколько названий уровней откуда получили (для итоговой статистики)
name_stats = {
    "особые названия": 0,
    "levels_tp.json": 0,
    "API": 0,
    "не найдено": 0,
}


def get_json(url, params=None):
    """
    Делает запрос и возвращает (json, response).
    Если сервер не отвечает или вернул временную ошибку, пробует ещё раз,
    до TRIES попыток, с растущей паузой между ними.
    """
    last_error = None

    for attempt in range(1, TRIES + 1):
        try:
            response = session.get(url, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            return response.json(), response
        except (requests.RequestException, ValueError) as error:
            # Код ответа сервера, если он вообще был (иначе None).
            # Не проверяем response через "if response", потому что
            # у ответов с ошибкой такая проверка даёт False.
            status = getattr(getattr(error, "response", None), "status_code", None)

            # 404, 403 и подобные - повторять бессмысленно, ответ не изменится.
            # Повторяем только обрыв связи, таймаут, 429 и ошибки сервера (5xx).
            if status is not None and status < 500 and status != 429:
                raise

            last_error = error

            if attempt < TRIES:
                pause = RETRY_PAUSE * attempt  # 2, 4, 6, 8 секунд
                print(f"    Повтор {attempt}/{TRIES - 1} через {pause} с: {error}")
                time.sleep(pause)

    raise last_error


def get_video(record):
    for field in ("video_url", "video", "video_link"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_percent(value):
    """
    Приводит процент к int. Пустое значение = 100
    (у 100% прохождений процент в файле не пишется).
    Возвращает None, если значение не удалось разобрать.
    """
    text = str(value if value is not None else "").strip().replace("%", "")
    if text == "":
        return 100
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def record_percent(record, *fields):
    """Достаёт процент из записи API (в GlobalList - percent, в Pointercrate - progress)."""
    for field in fields:
        value = record.get(field)
        if value is not None:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
    return None


def player_id_from_key(key):
    return str(key).split(" — ", 1)[0].strip()


def is_pc(player_id):
    return player_id.casefold().startswith("pc")


def get_links():
    data, _ = get_json(LINKS_URL)
    links = {}

    for global_id, entry in data.items():
        if isinstance(entry, dict):
            links[str(global_id)] = [
                str(value).strip()
                for value in entry.get("pointercrate", [])
            ]
    return links


# ---------------- GLOBAL DEMONLIST ----------------

def get_global_records(user_id):
    user_id = int(user_id)

    if user_id in global_records_cache:
        return global_records_cache[user_id]

    records = []
    offset = 0

    while True:
        data, _ = get_json(
            f"{GLOBAL_API}/user/record/list",
            {
                "user_id": user_id,
                "limit": 50,
                "offset": offset
            }
        )

        if data.get("message") != "success":
            raise RuntimeError(f"Global API error: {data}")

        payload = data.get("data", {})
        page = payload.get("records", [])
        records.extend(page)

        total = payload.get("total_count", len(records))
        if not page or len(records) >= total:
            break

        offset += len(page)
        time.sleep(DELAY)

    global_records_cache[user_id] = records
    return records


def get_global_level(level_id):
    level_id = int(level_id)

    if level_id not in global_level_cache:
        data, _ = get_json(
            f"{GLOBAL_API}/level/classic/get",
            {"id": level_id}
        )
        if data.get("message") != "success":
            raise RuntimeError(f"Global level API error: {data}")

        global_level_cache[level_id] = data.get("data", {})

    return global_level_cache[level_id]


def find_global_video(user_id, level_id, percent):
    """
    Ищет видео рекорда на этот уровень И с этим процентом.
    Возвращает (video, status). Принятый рекорд (accepted) предпочтительнее
    ожидающего (waiting); отклонённые (rejected) пропускаются.
    """
    waiting = None

    for record in get_global_records(user_id):
        level = record.get("level") or {}
        if str(level.get("id")) != str(level_id):
            continue

        if record_percent(record, "percent") != percent:
            continue

        status = str(record.get("status") or "").strip().lower()
        if status == "rejected":
            continue

        video = get_video(record)
        if not video:
            continue

        if status == "waiting":
            if waiting is None:
                waiting = (video, "waiting")
            continue

        return video, status or "accepted"

    return waiting if waiting else ("", "")


# ---------------- POINTERCRATE ----------------

def get_pointer_demon(ingame_id):
    ingame_id = int(ingame_id)

    if ingame_id in pointer_demon_cache:
        return pointer_demon_cache[ingame_id]

    data, _ = get_json(
        f"{POINTERCRATE_DEMONS_API}/",
        {"level_id": ingame_id, "limit": LIMIT}
    )

    if isinstance(data, dict):
        demons = data.get("data", data.get("demons", []))
    else:
        demons = data

    if not isinstance(demons, list):
        demons = []

    found = None
    for demon in demons:
        level_id = demon.get("level_id")
        if level_id is None and isinstance(demon.get("level"), dict):
            level_id = demon["level"].get("id")
        if level_id is None:
            level_id = demon.get("ingame_id")

        if str(level_id) == str(ingame_id):
            found = demon
            break

    # Фильтр level_id уже ограничивает результат этим уровнем.
    if found is None and len(demons) == 1:
        found = demons[0]

    pointer_demon_cache[ingame_id] = found
    return found


def get_pointer_demon_records(demon_id):
    demon_id = int(demon_id)

    if demon_id in pointer_records_cache:
        return pointer_records_cache[demon_id]

    records = []
    after = None

    while True:
        params = {"demon_id": demon_id, "limit": LIMIT}
        if after is not None:
            params["after"] = after

        data, _ = get_json(
            f"{POINTERCRATE_API}/records/",
            params
        )

        if isinstance(data, dict):
            page = data.get("data", data.get("records", []))
        else:
            page = data

        if not isinstance(page, list) or not page:
            break

        records.extend(page)

        if len(page) < LIMIT:
            break

        last_id = page[-1].get("id")
        if last_id is None or last_id == after:
            break

        after = last_id
        time.sleep(DELAY)

    pointer_records_cache[demon_id] = records
    return records


def find_pointer_video(pointer_player_id, ingame_id, percent):
    demon = get_pointer_demon(ingame_id)
    if not demon or demon.get("id") is None:
        return ""

    records = get_pointer_demon_records(demon["id"])

    for record in records:
        player = record.get("player") or {}
        record_player_id = (
            player.get("id") if isinstance(player, dict) else player
        )

        if str(record_player_id) != str(pointer_player_id):
            continue

        if record_percent(record, "progress", "percent") != percent:
            continue

        video = get_video(record)
        if video:
            return video

    return ""


def find_video(player_id, level_id, percent, links):
    # Сюда складываем ошибки запросов, чтобы отличить
    # "видео нет" от "не смогли проверить из-за сбоя".
    request_errors = []

    # Для pc-ID сразу используем Pointercrate.
    if is_pc(player_id):
        pointer_ids = [player_id[2:]]
    else:
        pointer_ids = links.get(str(player_id), [])

        # Сначала проверяем Global Demonlist.
        try:
            video, status = find_global_video(player_id, level_id, percent)
            if video:
                if status == "waiting":
                    return video, "GlobalList (статус: waiting)"
                return video, "GlobalList"
        except Exception as error:
            print(f"    GlobalList API error: {error}")
            request_errors.append("GlobalList")

    # Для Pointercrate нужен ingame_id уровня из Global API.
    try:
        level = get_global_level(level_id)
        ingame_id = level.get("ingame_id")
        if ingame_id is None:
            return "", "у уровня отсутствует ingame_id"
    except Exception as error:
        return "", f"ошибка получения ingame_id: {error}"

    for pointer_id in pointer_ids:
        try:
            video = find_pointer_video(pointer_id, ingame_id, percent)
            if video:
                return video, f"Pointercrate {pointer_id}"
        except Exception as error:
            print(f"    Pointercrate {pointer_id}: {error}")
            request_errors.append(f"Pointercrate {pointer_id}")

        time.sleep(DELAY)

    if request_errors:
        # Видео могло существовать, но проверить не удалось - стоит запустить ещё раз.
        return "", (
            "НЕ ПРОВЕРЕНО из-за ошибок запросов "
            f"({', '.join(request_errors)}), запустите ещё раз"
        )

    return "", f"видео с {percent}% не найдено"


# ---------------- OUTPUT ----------------

def save_json(users, filename):
    with open(filename, "w", encoding="utf-8") as file:
        file.write('{\n  "users": {\n')
        items = list(users.items())

        for user_index, (player, achievements) in enumerate(items):
            file.write(
                "    " + json.dumps(player, ensure_ascii=False) + ": [\n"
            )

            for index, achievement in enumerate(achievements):
                file.write(
                    "      " + json.dumps(achievement, ensure_ascii=False)
                )
                if index < len(achievements) - 1:
                    file.write(",")
                file.write("\n")

            file.write("    ]")
            if user_index < len(items) - 1:
                file.write(",")
            file.write("\n")

        file.write("  }\n}\n")


def format_found_line(player_key, level_id, level_name, percent, video, source_name):
    """Одна строка для файла с источниками."""
    name = level_name if level_name else "-"
    return (
        f"{player_key} | level_id={level_id} | {name} | {percent}% | "
        f"{video} | {source_name}"
    )


def save_sources(global_lines, pointer_lines, filename):
    """Пишет найденные видео двумя блоками: из GlobalList и из Pointercrate."""
    with open(filename, "w", encoding="utf-8") as file:
        file.write(f"=== GlobalList ({len(global_lines)}) ===\n")
        for line in global_lines:
            file.write(line + "\n")

        file.write(f"\n=== Pointercrate ({len(pointer_lines)}) ===\n")
        for line in pointer_lines:
            file.write(line + "\n")


# ---------------- КОМАНДЫ /fulladd И /try ----------------

def normalize_id(player_id):
    return str(player_id).strip().casefold()


def load_id_to_nick():
    """Читает gdr-ids.json (ник -> ID) и переворачивает в {ID: ник}."""
    try:
        with open(IDS_FILE, encoding="utf-8") as file:
            raw_ids = json.load(file)
    except FileNotFoundError:
        print(f"ВНИМАНИЕ: не найден {IDS_FILE}, в командах вместо ников будут ID")
        return {}

    id_to_nick = {}
    for nick, player_id in raw_ids.items():
        # Если у одного ID несколько ников, берём первый.
        id_to_nick.setdefault(normalize_id(player_id), nick)
    return id_to_nick


def load_level_names():
    """Скачивает levels_tp.json с GitHub и возвращает {ID уровня: название}."""
    print("levels_tp.json: беру с GitHub (raw кешируется до ~5 минут после правки)")
    try:
        levels_data = get_json(LEVELS_URL)[0]["levels"]
    except Exception as error:
        print(f"ВНИМАНИЕ: не удалось загрузить levels_tp.json с GitHub ({error}), "
              f"названия уровней возьмутся из API")
        return {}

    names = {}
    for level_id, data in levels_data.items():
        name = data.get("name")
        if not name:
            continue

        # Обычные ID храним числами, а "x..." - строками.
        key = str(level_id) if str(level_id).startswith("x") else int(level_id)
        names.setdefault(key, name)
    return names


def get_level_name(level_id, level_names):
    """
    Порядок поиска названия уровня по ID:
      1) особые названия (SPECIAL_LEVEL_NAMES);
      2) levels_tp.json (быстро, без сети);
      3) API (медленно, с повторными попытками).
    """
    key = str(level_id)

    if key in level_name_cache:
        return level_name_cache[key]

    name = SPECIAL_LEVEL_NAMES.get(key)
    found_in = "особые названия" if name else None

    # levels_tp.json: ID там могут быть и числом, и строкой "x..."
    if not name:
        name = level_names.get(level_id) or level_names.get(key)
        if not name and key.isdigit():
            name = level_names.get(int(key))
        if name:
            found_in = "levels_tp.json"

    # API. В нём есть только обычные числовые ID (у "x..." уровней его нет).
    if not name and key.isdigit():
        try:
            name = get_global_level(level_id).get("name")
            if name:
                found_in = "API"
        except Exception as error:
            print(f"    Не удалось получить название уровня {level_id}: {error}")

    if not name:
        name_failed_levels.append(key)
        name = f"УРОВЕНЬ {key}"
        found_in = "не найдено"

    name_stats[found_in] += 1
    level_name_cache[key] = name
    return name


# Ровно 11 символов (буквы, цифры, "_" и "-") после одного из вариантов
# начала ссылки: youtu.be/ID, youtube.com/watch?v=ID, /shorts/, /live/, /embed/.
# Всё, что идёт после ID (?si=..., &t=... и т.д.), отбрасывается.
YOUTUBE_ID_PATTERN = re.compile(
    r'(?:youtu\.be/'
    r'|youtube\.com/(?:watch\?(?:[^\s]*?&)?v=|shorts/|live/|embed/|v/))'
    r'([A-Za-z0-9_-]{11})'
)


def normalize_link(link):
    """
    Приводит YouTube-ссылку к виду https://www.youtube.com/watch?v=ID
    Ссылки не с YouTube и пустые остаются как есть.
    """
    link = str(link).strip()
    match = YOUTUBE_ID_PATTERN.search(link)

    if not match:
        return link

    return f"https://www.youtube.com/watch?v={match.group(1)}"


def build_commands(command_records, id_to_nick, level_names):
    """
    command_records: список (ключ игрока, ID игрока, ID уровня, процент, ссылка, источник).

    На каждый рекорд - 2 команды, между рекордами пустая строка.
    Если видео найдено, перед командами отдельной строкой пишется источник:
        Видео: GlobalList
        /fulladd Ник, Уровень, ссылка
        /try Ник, Уровень

    Если рекорд в GlobalList ещё ожидает проверки, это пишется явно:
        Видео: GlobalList (статус: waiting)
    """
    blocks = []

    for player_key, player_id, level_id, percent, link, site in command_records:
        # Ник: сначала из gdr-ids.json, потом из ключа "ID — Ник", потом сам ID.
        nick = id_to_nick.get(normalize_id(player_id))
        if not nick and " — " in str(player_key):
            nick = str(player_key).split(" — ", 1)[1].strip()
        if not nick:
            nick = player_id

        level_name = get_level_name(level_id, level_names)
        link = normalize_link(link)

        # Если рекорд не на 100%, добавляем процент к названию
        if percent != 100:
            level_name = f"{level_name} {percent}%"

        lines = []
        if link and site:
            lines.append(f"Видео: {site}")

        lines.append(f"/fulladd {nick}, {level_name}, {link}".rstrip())
        lines.append(f"/try {nick}, {level_name}")

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_commands(command_records, filename):
    print("\nСобираю команды (названия уровней: особые -> levels_tp.json -> API)...")

    id_to_nick = load_id_to_nick()
    level_names = load_level_names()
    text = build_commands(command_records, id_to_nick, level_names)

    with open(filename, "w", encoding="utf-8") as file:
        file.write(text)

    print(f"Команды сохранены в {filename}")

    print("Названия уровней получены:")
    for source_name, count in name_stats.items():
        if count:
            print(f"  {source_name}: {count}")

    if name_failed_levels:
        print("Название не нашлось ни в levels_tp.json, ни в API для уровней:")
        for level_id in sorted(set(name_failed_levels)):
            print(" ", level_id)


def main():
    input_path = Path(INPUT_FILE)
    if not input_path.exists():
        raise SystemExit(
            f"Не найден файл {INPUT_FILE}. Сначала запустите compare.py "
            f"(или укажите папку через --out-dir)"
        )

    with input_path.open("r", encoding="utf-8") as file:
        source = json.load(file)

    users = source.get("users", source)

    print("Загружаю links.json...")
    links = get_links()
    print(f"Привязок загружено: {len(links)}")

    output_users = {}
    not_found = []
    global_lines = []   # видео, найденные в Global
    pointer_lines = []  # видео, найденные в Pointercrate
    command_records = []  # всё, из чего потом сделаем команды /fulladd и /try

    total = sum(len(records) for records in users.values())
    processed = found_count = global_count = pointer_count = 0

    for player_key, achievements in users.items():
        player_id = player_id_from_key(player_key)
        output_achievements = []

        print(f"\nИгрок: {player_key}")

        for achievement in achievements:
            processed += 1
            updated = list(achievement)

            while len(updated) < 3:
                updated.append("")

            level_id, raw_percent, level_name = updated[:3]
            percent = parse_percent(raw_percent)
            shown_percent = percent if percent is not None else raw_percent

            if percent is None:
                not_found.append(
                    f"{player_key} | {level_name} | level_id={level_id} "
                    f"| {raw_percent} | не удалось разобрать процент"
                )
                output_achievements.append(updated)
                print(f"  [{processed}/{total}] {level_name}: непонятный процент")
                continue

            if not str(level_id).strip().isdigit():
                not_found.append(
                    f"{player_key} | {level_name} | level_id={level_id} "
                    f"| {percent}% | нечисловой ID уровня"
                )
                output_achievements.append(updated)
                command_records.append(
                    (player_key, player_id, level_id, percent, "", "")
                )
                print(f"  [{processed}/{total}] {level_name}: нечисловой ID")
                continue

            link, site = "", ""  # если видео не найдём, в команде ссылка будет пустой

            try:
                video, source_name = find_video(player_id, level_id, percent, links)

                if video:
                    updated[2] = video
                    link = video
                    site = (
                        "Pointercrate"
                        if source_name.startswith("Pointercrate")
                        else source_name
                    )
                    found_count += 1

                    line = format_found_line(
                        player_key, level_id, level_name,
                        shown_percent, video, source_name
                    )

                    if source_name.startswith("GlobalList"):
                        global_count += 1
                        global_lines.append(line)
                    else:
                        pointer_count += 1
                        pointer_lines.append(line)

                    print(
                        f"  [{processed}/{total}] {level_name}: "
                        f"найдено ({source_name})"
                    )
                else:
                    not_found.append(
                        f"{player_key} | {level_name} | level_id={level_id} "
                        f"| {percent}% | {source_name}"
                    )
                    print(f"  [{processed}/{total}] {level_name}: не найдено")

            except Exception as error:
                not_found.append(
                    f"{player_key} | {level_name} | level_id={level_id} "
                    f"| {percent}% | ОШИБКА: {error}"
                )
                print(f"  [{processed}/{total}] {level_name}: ОШИБКА {error}")

            output_achievements.append(updated)
            command_records.append(
                (player_key, player_id, level_id, percent, link, site)
            )
            time.sleep(DELAY)

        output_users[player_key] = output_achievements

    save_json(output_users, OUTPUT_FILE)
    save_sources(global_lines, pointer_lines, SOURCES_FILE)
    write_commands(command_records, COMMANDS_FILE)
    Path(NOT_FOUND_FILE).write_text(
        "\n".join(not_found) + ("\n" if not_found else ""),
        encoding="utf-8"
    )

    print("\n========== ИТОГ ==========")
    print(f"Всего прогрессов: {total}")
    print(f"Обработано: {processed}")
    print(f"Найдено видео: {found_count}")
    print(f"  GlobalList: {global_count}")
    print(f"  Pointercrate: {pointer_count}")
    print(f"Не найдено / ошибки: {len(not_found)}")
    print(f"\nJSON: {OUTPUT_FILE}")
    print(f"TXT (не найдено): {NOT_FOUND_FILE}")
    print(f"TXT (источники видео): {SOURCES_FILE}")
    print(f"TXT (команды): {COMMANDS_FILE}")


if __name__ == "__main__":
    main()
