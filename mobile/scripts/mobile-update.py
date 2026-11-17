#!/usr/bin/env python3
"""
Автообновление МОБИЛЬНОГО рейтинга (аналог sync-extras.py + cbd-update.py основной доски, в одном скрипте).
Записи только с WML, топ с https://coreboard.pythonanywhere.com/mobile/world, ID и уровни свои (см. mobile-rating.py).

Шаги:
  1. coreboard-parse.py world  -> cbd.txt, cbd_ids.json        (топ мобильной доски CoreBoard)
  2. mobile-levels.py          -> mobile/levels_mobile.json    (уровни, T, t и свои ID уровней)
  3. mobile-rating.py --ids    -> gdr.txt, gdr_videos.json     (наш рейтинг по WML; ник -> ID дописывается в ids_mobile.json)
  4. compare in-cbd-not-in-gdr -> mobile/my_extras.json        (что есть на CoreBoard, а у нас нет; пересобирается целиком)
  5. compare in-gdr-not-in-cbd -> mobile/cbd/                  (что есть у нас, а на CoreBoard нет, с видео из WML):
       missing_achievements.json, missing_achievements_with_nicks.json, missing_players.json, commands.txt
  6. с --commit коммитит и пушит всё из mobile/ (только эти файлы, промежуточные в репозиторий не попадают).

Запуск (из любой папки):
  python mobile/scripts/mobile-update.py                 записать файлы локально, без коммита
  python mobile/scripts/mobile-update.py --commit        то же + коммит и пуш
  python mobile/scripts/mobile-update.py --skip-fetch    взять готовые cbd.txt и gdr.txt (быстро перепроверить сравнение)
  python mobile/scripts/mobile-update.py --max 300       защита: если в my_extras изменений больше N, файл не пишем
  python mobile/scripts/mobile-update.py --force         игнорировать --max
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent      # mobile/scripts
DATA = HERE.parent                          # mobile
REPO = DATA.parent                          # корень репозитория
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import wml as site  # noqa: E402  (normalize_link)

CBD_DIR = DATA / "cbd"
EXTRAS = DATA / "my_extras.json"
LEVELS = DATA / "levels_mobile.json"
IDS = DATA / "ids_mobile.json"
ID_MAP = DATA / "coreboard_id_map.json"
PUBLISHED = ("missing_achievements.json", "missing_achievements_with_nicks.json", "commands.txt", "missing_players.json")
RECORDS_FILES = ("missing_achievements.json", "missing_achievements_with_nicks.json")

ap = argparse.ArgumentParser(description="Автообновление мобильного рейтинга")
ap.add_argument("--commit", action="store_true", help="закоммитить и запушить файлы из mobile/")
ap.add_argument("--skip-fetch", action="store_true", help="не качать cbd и не пересчитывать gdr, взять готовые")
ap.add_argument("--max", type=int, default=300, help="максимум изменений в my_extras.json за раз (защита)")
ap.add_argument("--force", action="store_true", help="игнорировать --max")
ap.add_argument("--out-dir", default=str(HERE / "cbd-update"), help="рабочая папка с полными результатами")
args = ap.parse_args()


def run(cmd, what):
    print(f"\n=== {what} ===\n$ {' '.join(map(str, cmd))}", flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=HERE)
    if r.returncode != 0:
        sys.exit(f"Шаг «{what}» завершился с ошибкой (код {r.returncode}).")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def write_users(path, users, sort=False):
    """{"users": {ключ: [запись, ...]}}: игрок на блок, запись на строку (маленькие диффы в git)."""
    items = list(users.items())
    if sort:
        items.sort(key=lambda kv: (not kv[0].isdigit(), int(kv[0]) if kv[0].isdigit() else 0, kv[0]))
    out = ["{", '  "users": {']
    for i, (uid, rows) in enumerate(items):
        out.append(f"    {json.dumps(uid, ensure_ascii=False)}: [")
        for j, r in enumerate(rows):
            out.append(f"      {json.dumps(r, ensure_ascii=False)}" + ("," if j < len(rows) - 1 else ""))
        out.append("    ]" + ("," if i < len(items) - 1 else ""))
    out += ["  }", "}", ""]
    tmp = Path(str(path) + ".tmp")
    tmp.write_text("\n".join(out), "utf-8")
    os.replace(tmp, path)


# ---------- my_extras.json ----------
def replace_found(old_users, found):
    """my_extras.json всегда равен тому, что compare нашёл сейчас. Из старых записей переносится только ссылка на видео.
    Возвращает (новая карта, новых, повышено, понижено, убрано, подробности)."""
    new_users, added, improved, lowered, removed, details = {}, 0, 0, 0, 0, []
    for uid, rows in found.items():
        uid = str(uid)
        old_index = {str(r[0]): r for r in old_users.get(uid, []) if isinstance(r, list) and len(r) >= 2}
        cur = []
        for r in rows:
            lvl, pct = r[0], float(r[1])
            pct = int(pct) if pct == int(pct) else pct
            old = old_index.pop(str(lvl), None)
            if old is None:
                cur.append([lvl, pct, ""])
                details.append(f"  {uid}: уровень {lvl}: новая запись {pct}%")
                added += 1
            else:
                if float(old[1]) < pct:
                    details.append(f"  {uid}: уровень {lvl}: {old[1]}% -> {pct}%")
                    improved += 1
                elif float(old[1]) > pct:
                    details.append(f"  {uid}: уровень {lvl}: ПОНИЖЕН {old[1]}% -> {pct}%")
                    lowered += 1
                cur.append([lvl, pct, old[2] if len(old) > 2 and isinstance(old[2], str) else ""])
        for old in old_index.values():
            details.append(f"  {uid}: уровень {old[0]}: запись {old[1]}% убрана")
            removed += 1
        if cur:
            new_users[uid] = cur
    found_ids = {str(u) for u in found}
    for uid, rows in old_users.items():
        if str(uid) not in found_ids:
            for r in rows:
                if isinstance(r, list) and len(r) >= 2:
                    details.append(f"  {uid}: уровень {r[0]}: запись {r[1]}% убрана")
                    removed += 1
    return new_users, added, improved, lowered, removed, details


# ---------- видео и команды (видео берутся из WML, ничего искать не надо) ----------
def attach_videos_and_commands(out):
    levels = (read_json(LEVELS, {}) or {}).get("levels", {})
    videos = read_json(HERE / "gdr_videos.json", {}) or {}

    def video_of(gdr_id, lid, percent):
        rec = (videos.get(str(gdr_id)) or {}).get(str(lid))
        # ссылка нужна только если процент рекорда на WML тот же, что в записи (иначе это другой рекорд)
        return site.normalize_link(rec[1]) if rec and float(rec[0]) == float(percent) and rec[1] else ""

    plain = read_json(out / "missing_achievements.json", {}).get("users", {})
    nicks = read_json(out / "missing_achievements_with_nicks.json", {}).get("users", {})

    blocks, no_video = [], []
    for key, records in nicks.items():
        gdr_id, _, nick = key.partition(" — ")
        for rec in records:  # [ID уровня, процент] -> [ID уровня, процент, ссылка]; название не дублируем: ID и есть название
            link = video_of(gdr_id.strip(), rec[0], rec[1])
            rec[:] = rec[:2] + [link]
            lv_name = (levels.get(str(rec[0])) or {}).get("name") or str(rec[0])
            shown = lv_name if float(rec[1]) == 100 else f"{lv_name} {rec[1]}%"
            lines = ([f"Видео: WML"] if link else []) + \
                    [f"/fulladd {nick.strip()}, {shown}, {link}".rstrip(), f"/try {nick.strip()}, {shown}"]
            blocks.append("\n".join(lines))
            if not link:
                no_video.append(f"{key} | {lv_name} | level_id={rec[0]} | {rec[1]}% | на WML нет видео")
    for key, records in plain.items():
        for rec in records:
            rec[:] = rec[:2] + [video_of(key.partition(" — ")[0].strip(), rec[0], rec[1])]
    write_users(out / "missing_achievements_with_nicks.json", nicks)
    write_users(out / "missing_achievements.json", plain)
    (out / "commands.txt").write_text("\n\n".join(blocks) + ("\n" if blocks else ""), "utf-8")
    (out / "missing_video_links.txt").write_text("\n".join(no_video) + ("\n" if no_video else ""), "utf-8")

    # новые игроки: видео в missing_players.json нет совсем, записи [ID уровня, процент]
    players = read_json(out / "missing_players.json", {"users": {}}).get("users", {})
    for info in players.values():
        for rec in info.get("records", []):
            rec[:] = rec[:2]
    with open(out / "missing_players.json", "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(players.items())
        for i, (pid, info) in enumerate(items):
            f.write("    " + json.dumps(pid, ensure_ascii=False) + ": " + json.dumps(info, ensure_ascii=False))
            f.write(",\n" if i < len(items) - 1 else "\n")
        f.write("  }\n}\n")
    n = sum(len(v) for v in nicks.values())
    print(f"Видео из WML: записей {n}, без видео {len(no_video)}")


# ---------- публикация ----------
def convert_records(src, dst, mapping):
    """Наши ID игроков -> ID CoreBoard (ключ 'ID' или 'ID — ник'). Нет в карте: оставляем наш ID."""
    users = read_json(src, {}).get("users", {})
    conv, kept = {}, []
    for key, records in users.items():
        our_id, sep, rest = key.partition(" — ")
        cb_id = mapping.get(our_id.strip().casefold())
        if cb_id is None:
            kept.append(key)
            conv[key] = records
        else:
            conv[cb_id + sep + rest] = records
    write_users(dst, conv)
    if kept:
        print(f"{src.name}: нет ID CoreBoard у {len(kept)} игроков, оставил наши ID: {', '.join(kept[:5])}"
              + (" ..." if len(kept) > 5 else ""))


def publish(out):
    CBD_DIR.mkdir(exist_ok=True)
    mapping = read_json(out / "gdr_to_cb_ids.json", {}) or {}
    rels = []
    for name in PUBLISHED:
        src, dst = out / name, CBD_DIR / name
        if not src.exists():
            sys.exit(f"Нет {src}, нечего публиковать.")
        if mapping and name in RECORDS_FILES:
            convert_records(src, dst, mapping)
        else:
            shutil.copyfile(src, dst)
        rels.append(dst)
    return rels


# ---------- git ----------
def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=REPO, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(a)} не удался:\n{r.stdout}{r.stderr}")
    return r


def commit_and_push(message, files):
    git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]"))
    git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com"))
    for f in files:
        if Path(f).exists():
            git("add", str(Path(f).resolve().relative_to(REPO)))
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Нечего коммитить: файлы не изменились.")
        return
    git("commit", "-m", message)
    for attempt in range(1, 4):  # параллельно может пушить основной workflow
        git("pull", "--rebase", "--autostash", check=False)
        r = git("push", check=False)
        if r.returncode == 0:
            print("Запушено.")
            return
        print(f"push не прошёл (попытка {attempt}/3): {r.stderr.strip()}")
    sys.exit("Не удалось запушить за 3 попытки. Коммит остался локально.")


def main():
    py = sys.executable
    cbd_txt, gdr_txt = HERE / "cbd.txt", HERE / "gdr.txt"

    # 1-3. данные
    if not args.skip_fetch:
        run([py, HERE / "coreboard-parse.py", "world", "-r", "-o", "cbd.json"], "CoreBoard: мобильный топ -> cbd.txt")
        run([py, HERE / "mobile-levels.py"], "CoreBoard: мобильные уровни -> levels_mobile.json")
        run([py, HERE / "mobile-rating.py", "--ids", "--check-cbd", cbd_txt], "Наш рейтинг по WML -> gdr.txt")
    for f in (cbd_txt, gdr_txt):
        if not f.exists() or f.stat().st_size == 0:
            sys.exit(f"Нет файла {f.name} (или он пустой). Останавливаюсь.")

    cbd_ids = HERE / "cbd_ids.json"
    extra = []
    if cbd_ids.exists():
        extra += ["--cbd-ids", cbd_ids]
    if ID_MAP.exists():
        extra += ["--id-map", ID_MAP]

    # 4. CoreBoard -> my_extras.json
    work = HERE / "cbd-update-extras"
    shutil.rmtree(work, ignore_errors=True)
    run([py, HERE / "compare.py", "in-cbd-not-in-gdr", "--cbd", cbd_txt, "--gdr", gdr_txt,
         "--out-dir", work, "--include-absent", *extra], "compare: in-cbd-not-in-gdr")
    found = read_json(work / "missing_achievements.json", None)
    if found is None:
        sys.exit("compare.py не создал missing_achievements.json.")
    found = found.get("users", {})
    old = (read_json(EXTRAS, {}) or {}).get("users", {})
    new_users, added, improved, lowered, removed, details = replace_found(old, found)
    changes = added + improved + lowered + removed
    print(f"\nmy_extras.json: новых {added}, повышено {improved}, понижено {lowered}, убрано {removed}")
    for line in details[:50]:
        print(line)
    if len(details) > 50:
        print(f"  ... и ещё {len(details) - 50}")
    if changes > args.max and not args.force:
        sys.exit(f"Изменений {changes}, больше лимита --max {args.max}. my_extras.json не записан. "
                 f"Если это нормально, запусти с --force (почти всегда большое число = сбой разбора).")
    if changes or not EXTRAS.exists():
        write_users(EXTRAS, new_users, sort=True)
        print("my_extras.json обновлён.")

    # 5. наш рейтинг -> CoreBoard (cbd/)
    out = Path(args.out_dir).resolve()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    run([py, HERE / "compare.py", "in-gdr-not-in-cbd", "--cbd", cbd_txt, "--gdr", gdr_txt,
         "--out-dir", out, *extra], "compare: in-gdr-not-in-cbd")
    attach_videos_and_commands(out)
    published = publish(out)

    total = sum(len(v) for v in read_json(out / "missing_achievements.json", {}).get("users", {}).values())
    players = len(read_json(out / "missing_players.json", {}).get("users", {}))
    print(f"\nДля CoreBoard: записей {total}, новых игроков {players}. Файлы: {CBD_DIR}")

    if args.commit:
        msg = (f"mobile: my_extras +{added}/{improved}↑/{lowered}↓/{removed}−, cbd {total} записей, "
               f"новых игроков {players} (авто)")
        commit_and_push(msg, [EXTRAS, LEVELS, IDS, *published])


main()
