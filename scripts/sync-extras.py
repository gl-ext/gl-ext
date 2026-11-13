#!/usr/bin/env python3
"""
Автоматически добавляет в my_extras.json то, что compare.py находит командой in-cbd-not-in-gdr:
достижения, которые есть в CoreBoard (cbd), а в нашем рейтинге (gdr) их нет или процент ниже.

Что делает по шагам:
  1. coreboard_parse.py all   -> cbd.txt (оба топа CoreBoard вместе);
  2. gd-rating.py --src all --ids -> gdr.txt (наш рейтинг, ID игроков в [скобках]);
  3. compare.py in-cbd-not-in-gdr -> missing_achievements.json;
  4. дописывает найденное в my_extras.json (по каждому игроку и уровню остаётся лучший процент,
     старое не удаляется и не понижается);
  5. с флагом --commit коммитит и пушит my_extras.json.

Запуск (из любой папки, пути считаются от места скрипта):
  python scripts/sync_extras.py --dry-run      только показать, что добавилось бы, ничего не писать
  python scripts/sync_extras.py                записать в my_extras.json
  python scripts/sync_extras.py --commit       записать, закоммитить и запушить

Параметры:
  --max N         защита: если новых записей больше N, ничего не пишем (по умолчанию 300).
                  Огромный список почти всегда значит сбой парсинга, а не реальные рекорды.
  --force         игнорировать --max
  --skip-fetch    не запускать coreboard_parse и gd-rating, взять готовые cbd.txt и gdr.txt
                  (удобно, чтобы быстро перепроверить сравнение)
  --ids PATH      файл ник -> ID (по умолчанию gdr-ids.json в корне репозитория)

Скрипты coreboard_parse.py, gd-rating.py и compare.py должны лежать рядом с этим файлом
(допустимы и имена через дефис: coreboard-parse.py и т.д.).
Нужен только Python, ничего устанавливать не надо.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # scripts/
ROOT = HERE.parent                              # корень репозитория
EXTRAS = ROOT / "my_extras.json"
LEVELS = ROOT / "levels_tp.json"

ap = argparse.ArgumentParser(description="Дописывает находки compare.py в my_extras.json")
ap.add_argument("--dry-run", action="store_true", help="ничего не писать, только показать")
ap.add_argument("--commit", action="store_true", help="закоммитить и запушить my_extras.json")
ap.add_argument("--max", type=int, default=300, help="максимум новых записей за раз (защита)")
ap.add_argument("--force", action="store_true", help="игнорировать --max")
ap.add_argument("--skip-fetch", action="store_true", help="не качать cbd и gdr заново")
ap.add_argument("--ids", default=str(ROOT / "gdr-ids.json"), help="файл ник -> ID игрока")
args = ap.parse_args()


def find_script(*names):
    for n in names:
        if (HERE / n).exists():
            return HERE / n
    sys.exit(f"Не найден скрипт {names[0]} в папке {HERE}. Положи его рядом с sync_extras.py.")


def run(cmd, what):
    print(f"\n=== {what} ===\n$ {' '.join(map(str, cmd))}", flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=HERE)
    if r.returncode != 0:
        sys.exit(f"Шаг «{what}» завершился с ошибкой (код {r.returncode}). my_extras.json не тронут.")


def load_extras():
    if not EXTRAS.exists():
        return {"users": {}}
    try:
        data = json.loads(EXTRAS.read_text("utf-8"))
    except Exception as e:
        sys.exit(f"Не могу прочитать {EXTRAS.name} ({e}). Чтобы не затереть его, останавливаюсь.")
    if not isinstance(data, dict) or not isinstance(data.get("users", {}), dict):
        sys.exit(f"В {EXTRAS.name} ожидался объект вида {{\"users\": {{...}}}}. Останавливаюсь.")
    data.setdefault("users", {})
    return data


def merge_found(extras, found):
    """Добавляет найденное в extras (на месте). Возвращает (новых записей, улучшенных, подробности)."""
    users = extras["users"]
    added = improved = 0
    details = []
    for uid, rows in found.items():
        uid = str(uid)
        cur = users.setdefault(uid, [])
        index = {}
        for i, r in enumerate(cur):
            if isinstance(r, list) and len(r) >= 2:
                index[str(r[0])] = i
        # длина строки как у уже существующих записей, иначе как в compare: [уровень, процент, ""]
        width = len(cur[0]) if cur and isinstance(cur[0], list) else 3
        for r in rows:
            lvl, pct = r[0], float(r[1])
            pct = int(pct) if pct == int(pct) else pct
            k = str(lvl)
            if k in index:
                old = cur[index[k]]
                if pct > float(old[1]):
                    details.append(f"  {uid}: уровень {lvl}: {old[1]}% -> {pct}%")
                    old[1] = pct
                    improved += 1
            else:
                new = [lvl, pct, ""][:max(width, 2)]
                new += [""] * (max(width, 2) - len(new))
                cur.append(new)
                index[k] = len(cur) - 1
                details.append(f"  {uid}: уровень {lvl}: новая запись {pct}%")
                added += 1
        if not cur:
            del users[uid]
    return added, improved, details


def save_extras(extras):
    """Формат как у compare.py: игрок -> записи по одной на строку (маленькие диффы в git)."""
    users = extras["users"]
    items = sorted(users.items(), key=lambda kv: (not kv[0].isdigit(),
                                                  int(kv[0]) if kv[0].isdigit() else 0, kv[0]))
    out = ['{', '  "users": {']
    for i, (uid, rows) in enumerate(items):
        out.append(f'    {json.dumps(uid, ensure_ascii=False)}: [')
        for j, r in enumerate(rows):
            out.append(f'      {json.dumps(r, ensure_ascii=False)}' + ("," if j < len(rows) - 1 else ""))
        out.append('    ]' + ("," if i < len(items) - 1 else ""))
    out += ['  }', '}', '']
    tmp = EXTRAS.with_suffix(".json.tmp")
    tmp.write_text("\n".join(out), "utf-8")
    os.replace(tmp, EXTRAS)


def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=ROOT, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(a)} не удался:\n{r.stdout}{r.stderr}")
    return r


def commit_and_push(message):
    git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]"))
    git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL",
                                               "41898282+github-actions[bot]@users.noreply.github.com"))
    git("add", EXTRAS.name)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Нечего коммитить: my_extras.json не изменился.")
        return
    git("commit", "-m", message)
    for attempt in range(1, 4):  # параллельно может пушить другой workflow (make_links)
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
    cbd_txt, gdr_txt = HERE / "cbd.txt", HERE / "gdr.txt"

    if not args.skip_fetch:
        run([sys.executable, cb, "all", "-r"], "CoreBoard: оба топа -> cbd.txt")
        run([sys.executable, gdr_script, "--src", "all", "--ids"], "Наш рейтинг -> gdr.txt")
    for f in (cbd_txt, gdr_txt):
        if not f.exists() or f.stat().st_size == 0:
            sys.exit(f"Нет файла {f.name} (или он пустой). Останавливаюсь.")

    ids = Path(args.ids)
    work = Path(tempfile.mkdtemp(prefix="extras_sync_"))
    if not ids.exists():
        # запасной вариант: ник -> ID берём из самого gdr.json (сработают только совпадающие ники)
        gj = HERE / "gdr.json"
        print(f"ВНИМАНИЕ: {ids} не найден, строю ник -> ID из gdr.json (совпадут только одинаковые ники).")
        if not gj.exists():
            sys.exit("Нет ни gdr-ids.json, ни gdr.json: сопоставлять игроков нечем.")
        ids = work / "gdr-ids.json"
        ids.write_text(json.dumps({p["name"]: p["id"] for p in json.loads(gj.read_text("utf-8"))
                                   if p.get("name") and not p.get("pc_only")}, ensure_ascii=False), "utf-8")

    run([sys.executable, cmp_script, "in-cbd-not-in-gdr", "--cbd", cbd_txt, "--gdr", gdr_txt,
         "--ids", ids, "--levels", LEVELS, "--out-dir", work], "compare: in-cbd-not-in-gdr")

    found_file = work / "missing_achievements.json"
    if not found_file.exists():
        sys.exit("compare.py не создал missing_achievements.json.")
    found = json.loads(found_file.read_text("utf-8")).get("users", {})
    total = sum(len(v) for v in found.values())
    print(f"\ncompare нашёл: игроков {len(found)}, записей {total}")

    extras = load_extras()
    added, improved, details = merge_found(extras, found)
    print(f"Из них новых для {EXTRAS.name}: {added}, улучшений процента: {improved}")
    for line in details[:50]:
        print(line)
    if len(details) > 50:
        print(f"  ... и ещё {len(details) - 50}")

    if not (added or improved):
        print("Менять нечего.")
        return
    if added > args.max and not args.force:
        sys.exit(f"Новых записей {added}, больше лимита --max {args.max}. Ничего не записано. "
                 f"Если это нормально, запусти с --force (или подними --max).")
    if args.dry_run:
        print("\n--dry-run: файл не записан.")
        return

    save_extras(extras)
    print(f"\n{EXTRAS.name} обновлён.")
    if args.commit:
        commit_and_push(f"my_extras: +{added} записей, {improved} улучшений (авто, CoreBoard)")


main()
