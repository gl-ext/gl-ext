# Мобильный рейтинг (WML + CoreBoard mobile)

Отдельная копия системы auto-update для мобильной доски. Основные скрипты и файлы не затрагиваются.
Workflow: `.github/workflows/mobile-auto-update.yml` (запуск каждые 30 минут со сдвигом, либо вручную).

## Что где лежит
| Файл | Что это |
|---|---|
| `scripts/mobile-update.py` | всё одним запуском (его вызывает workflow) |
| `scripts/mobile-levels.py` | уровни мобильной доски -> `levels_mobile.json` |
| `scripts/mobile-rating.py` | рейтинг по WML -> `gdr.txt` (аналог gd-rating.py) |
| `scripts/compare.py`, `coreboard-parse.py` | копии основных, переключены на мобильные данные и `mobile/world` |
| `scripts/levels.py` | мобильный поиск уровней по названию (без глобала, подменяет общий) |
| `scripts/lists_core.py`, `wml.py`, `list_wml.py` | WML как в основной системе |
| `levels_mobile.json` | уровни: свой ID, место, T, t (сам обновляется) |
| `ids_mobile.json` | ник -> ID игрока (сам дополняется, можно править руками) |
| `level_aliases.json` | `{"название на WML": "название на CoreBoard"}` (руками) |
| `coreboard_id_map.json` | необязательно: ID игроков CoreBoard mobile (формат как в основной) |
| `my_extras.json` | записи, которые есть на CoreBoard, а на WML нет (пересобирается целиком) |
| `cbd/` | что есть у нас, а на CoreBoard нет: missing_achievements*.json, missing_players.json, commands.txt |

## Система ID
- Игрок: ID его аккаунта на WML. Ник на сайте может меняться, ID нет. Если ник на CoreBoard пишется иначе, чем на WML,
  допиши в `ids_mobile.json` строку `"ник на CoreBoard": WML-ID`, и он сопоставится.
- Уровень: свой номер 1, 2, 3... Ищется по названию, при смене места ID сохраняется, новому уровню выдаётся следующий
  свободный (`next_id`), ID пропавших уровней не переиспользуются. Одиночная замена названия на том же месте считается
  переименованием и ID сохраняет.
- Видео берутся из самих записей WML, ничего искать не нужно.

## Первый запуск: что проверить
1. В логе `mobile-levels.py` должно быть число уровней. Если пишет «разобрано 0», разметка `/mobile/levels` отличается:
   пришли кусок HTML вокруг первого уровня.
2. Блок «Сверка с CoreBoard» в логе `mobile-rating.py`: очки должны сходиться с CoreBoard. Если нет, формула мобильной
   доски отличается (проверь T, t и место уровней).
3. `mobile_levels_not_found.txt`: уровни WML без пары. Если это уровень под другим названием, допиши в `level_aliases.json`.
4. Ники из `cbd_not_in_gdr.txt` (в артефакте debug-files) без пары: допиши в `ids_mobile.json`.
