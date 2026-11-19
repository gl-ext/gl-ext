#!/usr/bin/env python3
"""
Находит то, что есть в нашем рейтинге (gdr), а в CoreBoard (cbd) нет или там процент ниже,
ищет на это видео и собирает команды /fulladd и /try для CoreBoard.
Обратная версия sync-extras.py: тот берёт из cbd то, чего нет в gdr, и пишет в my_extras.json.
my_extras.json этот скрипт не трогает.

Что делает по шагам:
  1. coreboard_parse.py all   -> cbd.txt (оба топа CoreBoard вместе);
  2. gd-rating.py --src all --ids -> gdr.txt (наш рейтинг, ID игроков в [скобках]);
  3. compare.py in-gdr-not-in-cbd -> missing_achievements.json и missing_achievements_with_nicks.json;
  4. add-video-links.py -> ищет видео (GlobalList, Pointercrate) и собирает команды (commands.txt);
  5. с флагом --commit копирует три файла в папку cbd/ репозитория, коммитит и пушит:
       cbd/missing_achievements.json
       cbd/missing_achievements_with_nicks.json
       cbd/commands.txt
       cbd/missing_players.json   (новые игроки: есть у нас с очками меньше 250, на CoreBoard их нет;
                                   для них команд нет, только JSON с никами, записями и ссылками на видео)
       cbd/missing_players_with_waiting.json  (игроки, у которых меньше 250 очков только если принять их waiting-записи;
                                   ID продолжаются после последнего в missing_players.json)
     Если добавлять в CoreBoard больше нечего, файлы в cbd/ обновятся на пустые.

levels_tp.json всегда скачивается с GitHub (локальная копия не читается).
gdr-ids.json больше не нужен: ники и ID игроков берутся только из coreboard_id_map.json.

ID CoreBoard. Пока в ссылках на игроков сайта только ники, ID берутся из coreboard_id_map.json в корне репозитория
({"<ID>": {"nick": ..., "gdr_id": ...}}). Когда в ссылках появятся числовые ID, coreboard_parse.py
сам сохранит их в cbd_ids.json (ник -> ID), и они главнее карты: игрок сопоставляется по ID сайта через карту,
новые ID идут после самого большого из обоих источников. JSON для CoreBoard выходят с ЕГО ID:
в missing_achievements*.json вместо наших ID стоят ID CoreBoard, а новые игроки в missing_players.json получают ID по
порядку после самого большого в coreboard_id_map.json (игрок, уже записанный в карте, сохраняет свой ID). В
missing_players.json только код страны (country_code), без названия; наш ID остаётся в поле gdr_id. Файла карты нет:
всё работает как раньше, ключи наши.

Запуск (из любой папки, пути считаются от места скрипта):
  python scripts/cbd-update.py                  полный прогон, результат в scripts/cbd-update/
  python scripts/cbd-update.py --commit         то же + копия в cbd/, коммит и пуш
  python scripts/cbd-update.py --no-videos      только compare, без поиска видео и команд
  python scripts/cbd-update.py --skip-fetch     взять готовые cbd.txt и gdr.txt

Параметры:
  --commit        скопировать три файла в cbd/, закоммитить и запушить
  --out-dir PATH  рабочая папка с полными результатами (по умолчанию cbd-update рядом со скриптом)
  --skip-fetch    не запускать coreboard_parse и gd-rating, взять готовые cbd.txt и gdr.txt
  --no-videos     остановиться после compare, видео и команды не делать (с --commit нельзя)

В рабочей папке кроме трёх файлов лежат missing_achievements_with_videos.json,
missing_video_links.txt (где видео не нашлось), video_link_sources.txt и служебные файлы compare.

Скрипты coreboard_parse.py, gd-rating.py, compare.py и add-video-links.py должны лежать
рядом с этим файлом (допустимы имена и через дефис, и через подчёркивание).
Для поиска видео нужен модуль requests (pip install requests); для --no-videos он не нужен.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # scripts/
ROOT = HERE.parent                              # корень репозитория
CBD_DIR = ROOT / "cbd"                          # сюда копируются результаты при --commit
PUBLISHED = ("missing_achievements.json", "missing_achievements_with_nicks.json", "commands.txt",
             "missing_players.json")
# Публикуется, только если файл появился в этом запуске (его делает gd-rating --waiting через sync-extras.py)
PUBLISHED_OPTIONAL = ("missing_players_with_waiting.json",)
WAITING_NEW = HERE / "waiting_new_players.json"   # игроки, у которых меньше 250 очков только с waiting (наши ID)

ap = argparse.ArgumentParser(
    description="cbd-update: достижения из нашего рейтинга, которых нет в CoreBoard, "
                "с видео и командами /fulladd и /try")
ap.add_argument("--commit", action="store_true", help="скопировать результаты в cbd/, закоммитить и запушить")
ap.add_argument("--out-dir", default=str(HERE / "cbd-update"), help="рабочая папка с результатами")
ap.add_argument("--skip-fetch", action="store_true", help="не качать cbd и gdr заново")
ap.add_argument("--no-videos", action="store_true", help="только compare, без видео и команд")
args = ap.parse_args()
if args.no_videos and args.commit:
    ap.error("--no-videos нельзя вместе с --commit: в cbd/ попал бы устаревший commands.txt")


def find_script(*names):
    for n in names:
        if (HERE / n).exists():
            return HERE / n
    sys.exit(f"Не найден скрипт {names[0]} в папке {HERE}. Положи его рядом с этим файлом.")


def run(cmd, what):
    print(f"\n=== {what} ===\n$ {' '.join(map(str, cmd))}", flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=HERE)
    if r.returncode != 0:
        sys.exit(f"Шаг «{what}» завершился с ошибкой (код {r.returncode}).")


def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=ROOT, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(a)} не удался:\n{r.stdout}{r.stderr}")
    return r


RECORDS_FILES = ("missing_achievements.json", "missing_achievements_with_nicks.json")


def convert_records(src, dst, mapping):
    """missing_achievements*.json: наши ID игроков -> ID CoreBoard (ключ 'ID' или 'ID — ник').
    Игрока, которого в карте нет, оставляем под нашим ID, чтобы не потерять записи (и предупреждаем)."""
    users = json.loads(src.read_text("utf-8")).get("users", {})
    converted, kept = {}, []
    for key, records in users.items():
        our_id, sep, rest = key.partition(" — ")
        cb_id = mapping.get(our_id.strip().casefold())
        if cb_id is None:
            kept.append(key)
            converted[key] = records
        else:
            converted[cb_id + sep + rest] = records
    with open(dst, "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(converted.items())  # порядок как в compare: по очкам игрока
        for i, (key, records) in enumerate(items):
            f.write(f'    {json.dumps(key, ensure_ascii=False)}: [\n')
            for j, record in enumerate(records):
                f.write(f'      {json.dumps(record, ensure_ascii=False)}{"," if j < len(records) - 1 else ""}\n')
            f.write(f'    ]{"," if i < len(items) - 1 else ""}\n')
        f.write('  }\n}\n')
    if kept:
        print(f"{src.name}: нет ID CoreBoard у {len(kept)} игроков, оставил наши ID: {', '.join(kept[:5])}"
              + (" ..." if len(kept) > 5 else ""))


def publish(out, message):
    """Копирует файлы из рабочей папки в cbd/ (записи переводит на ID CoreBoard), коммитит и пушит."""
    CBD_DIR.mkdir(exist_ok=True)
    mapping_file = out / "gdr_to_cb_ids.json"
    mapping = json.loads(mapping_file.read_text("utf-8")) if mapping_file.exists() else {}
    rels = []
    for name in PUBLISHED:
        src, dst = out / name, CBD_DIR / name
        if not src.exists():
            sys.exit(f"Нет {src}, нечего публиковать.")
        if mapping and name in RECORDS_FILES:
            convert_records(src, dst, mapping)
        else:
            shutil.copyfile(src, dst)
        rels.append(str(dst.relative_to(ROOT)))
    for name in PUBLISHED_OPTIONAL:
        src, dst = out / name, CBD_DIR / name
        if src.exists():
            shutil.copyfile(src, dst)
            rels.append(str(dst.relative_to(ROOT)))

    git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]"))
    git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL",
                                               "41898282+github-actions[bot]@users.noreply.github.com"))
    git("add", *rels)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Нечего коммитить: файлы в cbd/ не изменились.")
        return
    git("commit", "-m", message)
    for attempt in range(1, 4):  # параллельно может пушить другой шаг или workflow
        git("pull", "--rebase", "--autostash", check=False)
        r = git("push", check=False)
        if r.returncode == 0:
            print("Запушено.")
            return
        print(f"push не прошёл (попытка {attempt}/3): {r.stderr.strip()}")
    sys.exit("Не удалось запушить за 3 попытки. Коммит остался локально.")


CLAN_TAG_RE = re.compile(r"\[[^\]]*\]")


def strip_clan_tags(nick):
    """'[OX] zsakos' -> 'zsakos' (как в compare.py). Если остался бы пустой ник, не трогаем."""
    return re.sub(r"\s+", " ", CLAN_TAG_RE.sub("", nick)).strip() or nick


def make_players_with_waiting(out, cbd_ids, id_map):
    """missing_players_with_waiting.json: игроки, которые набирают меньше 250 очков ТОЛЬКО с waiting-записями.
    Берёт scripts/waiting_new_players.json (его пишет gd-rating --waiting), выбрасывает тех, кто уже есть на
    CoreBoard, выдаёт ID, продолжая после последнего ID в missing_players.json, и делает ники уникальными."""
    if not WAITING_NEW.exists():
        print("\nwaiting_new_players.json нет (gd-rating --waiting в этом запуске не работал): "
              "missing_players_with_waiting.json не обновляю.")
        return
    waiting = json.loads(WAITING_NEW.read_text("utf-8")).get("users", {})
    regular = json.loads((out / "missing_players.json").read_text("utf-8")).get("users", {})
    map_data = json.loads(id_map.read_text("utf-8")) if id_map.exists() else {}
    site_ids = json.loads(cbd_ids.read_text("utf-8")) if cbd_ids.exists() else {}
    mapping_file = out / "gdr_to_cb_ids.json"   # наш ID -> ID CoreBoard у тех, кто уже есть на CoreBoard
    on_cbd = {k.casefold() for k in json.loads(mapping_file.read_text("utf-8"))} if mapping_file.exists() else set()

    # Продолжаем нумерацию после самого большого ID: он стоит последним в missing_players.json (новые ID идут
    # по порядку после максимума из карты и сайта). Нет ни карты, ни ID с сайта: compare.py оставил наши ID,
    # и здесь тоже остаются наши.
    use_cb_ids = bool(map_data or site_ids)
    all_ids = [k for k in (*regular, *map_data, *site_ids.values()) if str(k).isdigit()] if use_cb_ids else []
    next_id = max(map(int, all_ids), default=0) + 1

    taken = {strip_clan_tags(i["nick"]).casefold() for i in regular.values() if i.get("nick")}
    taken |= {str(n).casefold() for n in site_ids}
    taken |= {str(v.get("nick", "")).casefold() for v in map_data.values()}

    result, already = {}, 0
    for gdr_id, info in waiting.items():
        if gdr_id.casefold() in on_cbd:   # игрок уже на CoreBoard: ему нужны не новый ID, а обычные добавления
            already += 1
            continue
        base = strip_clan_tags(info["nick"])
        nick, n = base, 1
        while nick.casefold() in taken:
            n += 1
            nick = f"{base}{n}"
        taken.add(nick.casefold())
        if use_cb_ids:
            key, next_id = str(next_id), next_id + 1
        else:
            key = gdr_id
        result[key] = {**info, "nick": nick}

    with open(out / "missing_players_with_waiting.json", "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(result.items())
        for i, (key, info) in enumerate(items):
            f.write(f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps(info, ensure_ascii=False)}"
                    + (",\n" if i < len(items) - 1 else "\n"))
        f.write("  }\n}\n")
    print(f"\nmissing_players_with_waiting.json: игроков {len(result)} (уже на CoreBoard, пропущено: {already})"
          + (f", ID с {next_id - len(result)} по {next_id - 1}" if result and use_cb_ids else ""))


def main():
    cb = find_script("coreboard_parse.py", "coreboard-parse.py")
    gdr_script = find_script("gd-rating.py", "gd_rating.py")
    cmp_script = find_script("compare.py")
    avl_script = None
    if not args.no_videos:
        avl_script = find_script("add-video-links.py", "add_video_links.py")
        if importlib.util.find_spec("requests") is None:
            sys.exit("Для поиска видео нужен модуль requests: pip install requests "
                     "(или запусти с --no-videos).")

    cbd_txt, gdr_txt = HERE / "cbd.txt", HERE / "gdr.txt"
    if not args.skip_fetch:
        run([sys.executable, cb, "all", "-r"], "CoreBoard: оба топа -> cbd.txt")
        run([sys.executable, gdr_script, "--src", "all", "--ids"], "Наш рейтинг -> gdr.txt")
    for f in (cbd_txt, gdr_txt):
        if not f.exists() or f.stat().st_size == 0:
            sys.exit(f"Нет файла {f.name} (или он пустой). Останавливаюсь.")

    cbd_ids = HERE / "cbd_ids.json"   # ник -> ID с сайта; делает coreboard_parse, когда в ссылках появятся ID
    id_map = next((p for p in (ROOT / "coreboard_id_map.json", HERE / "coreboard_id_map.json") if p.exists()),
                  ROOT / "coreboard_id_map.json")   # корень репозитория, запасной вариант рядом со скриптом
    print(f"\nID CoreBoard: с сайта {'есть' if cbd_ids.exists() else 'нет'}, "
          f"{id_map.name} {'есть' if id_map.exists() else 'нет'}")

    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # результаты прошлого запуска убираем, чтобы старые команды не приняли за новые
    for name in ("missing_achievements.json", "missing_achievements_with_nicks.json",
                 "missing_achievements_with_videos.json", "missing_video_links.txt",
                 "video_link_sources.txt", "commands.txt", "missing_players.json",
                 "missing_players_skipped.txt", "missing_players_video_links.txt", "gdr_to_cb_ids.json",
                 "missing_players_with_waiting.json"):
        (out / name).unlink(missing_ok=True)

    cmp_cmd = [sys.executable, cmp_script, "in-gdr-not-in-cbd", "--cbd", cbd_txt, "--gdr", gdr_txt,
               "--out-dir", out]
    if cbd_ids.exists():
        cmp_cmd += ["--cbd-ids", cbd_ids]
    if id_map.exists():
        cmp_cmd += ["--id-map", id_map]
    run(cmp_cmd, "compare: in-gdr-not-in-cbd")

    found_file = out / "missing_achievements.json"
    if not found_file.exists() or not (out / "missing_achievements_with_nicks.json").exists():
        sys.exit("compare.py не создал missing_achievements.json / missing_achievements_with_nicks.json.")
    found = json.loads(found_file.read_text("utf-8")).get("users", {})
    total = sum(len(v) for v in found.values())
    players_file = out / "missing_players.json"
    if not players_file.exists():
        sys.exit("compare.py не создал missing_players.json.")
    new_players = len(json.loads(players_file.read_text("utf-8")).get("users", {}))
    print(f"\ncompare нашёл: игроков {len(found)}, записей {total}; новых игроков (нет на CoreBoard) {new_players}")

    if args.no_videos:
        print(f"--no-videos: видео и команды не делаю. Результат: {found_file}")
        make_players_with_waiting(out, cbd_ids, id_map)
        return

    if total or new_players:
        # --prev-players: ссылки, уже найденные в опубликованном cbd/missing_players.json, заново не ищутся
        run([sys.executable, avl_script, "--out-dir", out, "--id-map", id_map,
             "--prev-players", CBD_DIR / "missing_players.json"],
            "Видео и команды (add-video-links)")
        if not (out / "commands.txt").exists():
            sys.exit("add-video-links.py не создал commands.txt.")
    else:
        print("В CoreBoard нечего добавлять.")
        (out / "commands.txt").write_text("", "utf-8")

    make_players_with_waiting(out, cbd_ids, id_map)

    print(f"\nКоманды: {out / 'commands.txt'}")
    if total:
        print(f"Не нашлось видео: {out / 'missing_video_links.txt'}")
    if args.commit:
        publish(out, f"cbd: {len(found)} игроков, {total} записей, новых игроков {new_players} (авто, gdr-not-in-cbd)")


main()
