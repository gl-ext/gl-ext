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
     Если добавлять в CoreBoard больше нечего, файлы в cbd/ обновятся на пустые.

levels_tp.json и gdr-ids.json всегда скачиваются с GitHub (локальные копии не читаются).

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
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # scripts/
ROOT = HERE.parent                              # корень репозитория
CBD_DIR = ROOT / "cbd"                          # сюда копируются результаты при --commit
RAW = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/"
UA = "gl-ext-top6"
PUBLISHED = ("missing_achievements.json", "missing_achievements_with_nicks.json", "commands.txt")

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


def download_github(name, dest, tries=4):
    """Скачивает файл репозитория с GitHub и проверяет, что это непустой JSON-объект."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(RAW + name, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8")
            data = json.loads(text)
            if not isinstance(data, dict) or not data:
                raise ValueError("пустой файл или не объект")
            dest.write_text(text, "utf-8")
            return
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    sys.exit(f"{name}: не удалось загрузить с GitHub ({last}). Останавливаюсь.")


def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=ROOT, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(a)} не удался:\n{r.stdout}{r.stderr}")
    return r


def publish(out, message):
    """Копирует три файла из рабочей папки в cbd/, коммитит и пушит."""
    CBD_DIR.mkdir(exist_ok=True)
    rels = []
    for name in PUBLISHED:
        src, dst = out / name, CBD_DIR / name
        if not src.exists():
            sys.exit(f"Нет {src}, нечего публиковать.")
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

    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # результаты прошлого запуска убираем, чтобы старые команды не приняли за новые
    for name in ("missing_achievements.json", "missing_achievements_with_nicks.json",
                 "missing_achievements_with_videos.json", "missing_video_links.txt",
                 "video_link_sources.txt", "commands.txt"):
        (out / name).unlink(missing_ok=True)

    work = Path(tempfile.mkdtemp(prefix="cbd_update_"))
    ids = work / "gdr-ids.json"
    print("\ngdr-ids.json: беру с GitHub (levels_tp.json compare и add-video-links берут с GitHub сами; raw кешируется до ~5 минут)")
    download_github("gdr-ids.json", ids)

    run([sys.executable, cmp_script, "in-gdr-not-in-cbd", "--cbd", cbd_txt, "--gdr", gdr_txt,
         "--out-dir", out], "compare: in-gdr-not-in-cbd")

    found_file = out / "missing_achievements.json"
    if not found_file.exists() or not (out / "missing_achievements_with_nicks.json").exists():
        sys.exit("compare.py не создал missing_achievements.json / missing_achievements_with_nicks.json.")
    found = json.loads(found_file.read_text("utf-8")).get("users", {})
    total = sum(len(v) for v in found.values())
    print(f"\ncompare нашёл: игроков {len(found)}, записей {total}")

    if args.no_videos:
        print(f"--no-videos: видео и команды не делаю. Результат: {found_file}")
        return

    if total:
        run([sys.executable, avl_script, "--out-dir", out, "--ids", ids],
            "Видео и команды (add-video-links)")
        if not (out / "commands.txt").exists():
            sys.exit("add-video-links.py не создал commands.txt.")
    else:
        print("В CoreBoard нечего добавлять.")
        (out / "commands.txt").write_text("", "utf-8")

    print(f"\nКоманды: {out / 'commands.txt'}")
    if total:
        print(f"Не нашлось видео: {out / 'missing_video_links.txt'}")
    if args.commit:
        publish(out, f"cbd: {len(found)} игроков, {total} записей (авто, gdr-not-in-cbd)")


main()
