(() => {
  "use strict";

  const DL_API = "https://api.demonlist.org/level/classic/record/list";
  const DL_LEVEL_API = "https://api.demonlist.org/level/classic/get";
  const DL_USER_API = "https://api.demonlist.org/user/record/list"; // записи игрока (страница профиля)
  const DL_LIST_API = "https://api.demonlist.org/level/classic/list"; // список уровней (нужен для самого низкого места)
  const DL_LB_API = "https://api.demonlist.org/leaderboard/user/list"; // таблица игроков (для места в рейтинге)
  const PC_API = "https://pointercrate.com/api/";
  const LINKS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/links.json";
  const PLAYERS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/players.json";
  const LB_RE = /^\/leaderboard\/players/; // страница рейтинга игроков
  const LB_TOGGLE_ID = "pc-lb-toggle";
  const POINTS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/points.json";
  const FIX_URL ="https://raw.githubusercontent.com/gl-ext/gl-ext/main/fix.json";
  const MERGED_ID = "pc-merged-list"; // список с рекордами Pointercrate
  const DL_ID = "pc-dl-list"; // список только с рекордами demonlist (когда экспорт выключен)
  const TOGGLE_ID = "pc-switch-toggle";
  const PROFILE_ID = "pc-profile-list"; // наш список записей на странице профиля
  const PROFILE_RE = /^\/profile\/(\d+)/; // адрес страницы профиля
  const EMPTY_ANCHOR_ID = "pc-empty-anchor"; // наш контейнер на уровнях без рекордов
  const CARD_SEL = ".record-card, [class*='Record_card'], [class*='record_card']";
  const PAGE_SIZE = 100; // сколько карточек показываем сразу и добавляем при прокрутке
  const DL_CONCURRENCY = 15; // сколько запросов к Demonlist выполняется одновременно
  const SENTINEL_ID = "pc-sentinel"; // невидимая метка в конце списка: когда её видно — докидываем карточки

  let running = false;
  let doneKey = null;
  let failedKey = null; // путь, на котором была ошибка (чтобы не повторять бесконечно)
  let lastPath = location.pathname;
  let retries = 0;
  let enabled = false;
  let stats = { merged: { total: 0, hundred: 0 }, dl: { total: 0, hundred: 0 } };
  let dataCache = null; // { key, dl, pc } — чтобы не качать всё заново при перерисовке
  let pcDemonsPromise = null; // полный список демонов Pointercrate (кеш на сессию страницы)
  let savedTemplate = null; // шаблон карточки, запомненный на уровне с рекордами
  let savedContainerClass = ""; // класс списка карточек, запомненный на уровне с рекордами
  let shown = PAGE_SIZE; // сколько карточек сейчас построено в каждом списке
  let listsState = null; // { dl, merged, template } — полные списки записей, чтобы дорисовывать карточки
  let sentinelIO = null; // IntersectionObserver, следящий за меткой
  let profileData = null; // { userId, items } — готовый список записей профиля
  let profileSkipKey = null; // профиль, на котором делать нечего (нет связанных аккаунтов Pointercrate)
  const dlLevelCache = new Map(); // ID уровня в игре -> уровень на Demonlist (или null, если его там нет)
  const countryCache = new Map();
  const suggestions = new Map(); // возможные пары аккаунтов (для ручной проверки)

  /* ---------- утилиты ---------- */

  const normalize = (s) =>
    String(s || "")
      .replace(/\[.*?\]|\(.*?\)|<.*?>|\{.*?\}/g, "")
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "");

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const isMine = (el) => !!el.closest("#" + MERGED_ID + ", #" + DL_ID + ", #" + PROFILE_ID);

  function getJson(url) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "fetchJson", url }, (res) => {
        if (chrome.runtime.lastError || !res) return resolve({ ok: false });
        resolve(res);
      });
    });
  }

  // Повторяет запрос при 429 / 5xx / сетевой ошибке. 404 и прочие 4xx не повторяет.
  async function getJsonRetry(url, tries = 4) {
    let res;
    for (let i = 0; i < tries; i++) {
      res = await getJson(url);
      if (res.ok) return res;
      if (res.status && res.status !== 429 && res.status < 500) return res;
      await sleep(800 * 2 ** i);
    }
    return res;
  }

  function storageGet() {
    return new Promise((resolve) => {
      try {
        chrome.storage.local.get("enabled", (r) => {
          resolve(r?.enabled === true);
        });
      } catch (_) {
        resolve(false);
      }
    });
  }
  function storageSet(v) {
    try { chrome.storage.local.set({ enabled: v }); } catch (_) {}
  }

  function extractVideoId(url) {
    if (!url) return null;
    const str = String(url).trim();
    const m = str.match(
      /(?:youtu\.be\/|youtube\.com\/(?:watch\?(?:.*&)?v=|embed\/|shorts\/|v\/))([a-zA-Z0-9_-]{11})/i
    );
    if (m) return m[1];
    try {
      const p = new URL(str.startsWith("http") ? str : `https://${str}`);
      return p.hostname.replace(/^www\./, "") + p.pathname + p.search;
    } catch (_) {
      return str.toLowerCase();
    }
  }

  /* ---------- чтение страницы ---------- */

  const findRecordCards = () =>
    [...document.querySelectorAll(CARD_SEL)].filter((el) => !isMine(el));

  function getNameFromCard(card) {
    const el = card.querySelector(".record-username, [class*='username'], [class*='name']");
    return el ? el.textContent.trim() : "";
  }

  function findLevelName() {
    const title = document.title || "";
    if (title.includes("|")) {
      const n = title.split("|")[0].trim();
      if (n && !/global demonlist/i.test(n)) return n;
    }
    for (const sel of ["main h1", "main h2", ".level-title", ".demon-title", "h1"]) {
      const t = document.querySelector(sel)?.textContent?.replace(/^#\d+\s*/, "").trim();
      if (t && t.length > 1 && !/global demonlist/i.test(t)) return t;
    }
    return "";
  }

  function findCardsContainer(cards) {
    if (!cards.length) return null;
    let node = cards[0].parentElement;
    while (node && !cards.every((c) => node.contains(c))) node = node.parentElement;
    return node;
  }

  // Сайт (Svelte) добавляет ко всем классам метку вида "svelte-1vjy86j", и стили привязаны к ней.
  // Метка меняется после обновлений сайта, поэтому берём её с самой страницы.
  function getSvelteClass() {
    const el = document.querySelector(".records-card, .records-content, .records-header");
    const m = el && /svelte-[a-z0-9]+/i.exec(el.className);
    return m ? m[0] : "svelte-1vjy86j";
  }

  // Запасная карточка на случай, если на странице нет ни одной настоящей
  // и мы ещё не видели ни одного уровня с рекордами. Повторяет вёрстку сайта.
  function makeFallbackTemplate() {
    const s = getSvelteClass();
    const card = document.createElement("div");
    card.className = `record-card ${s}`;
    card.innerHTML = `
      <div class="record-row ${s}">
        <a href="#" class="record-player ${s}" target="_blank" rel="noopener noreferrer">
          <img src="/img/flags/xx.svg" alt="Unknown" class="record-flag ${s}" title="Unknown">
          <span class="record-username ${s}"></span>
        </a>
        <div class="record-info ${s}">
          <span class="record-percent ${s}"></span>
          <a href="#" target="_blank" rel="noopener noreferrer" class="record-video ${s}" title="Video">
            <svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true" role="img" class="w-5 h-5 iconify iconify--mdi" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M10 16.5v-9l6 4.5M12 2A10 10 0 0 0 2 12a10 10 0 0 0 10 10a10 10 0 0 0 10-10A10 10 0 0 0 12 2"></path></svg>
          </a>
        </div>
      </div>`;
    return card;
  }

  // Пустой контейнер для уровней без рекордов:
  // кладём его прямо перед блоком "No records yet".
  function makeEmptyContainer() {
    document.getElementById(EMPTY_ANCHOR_ID)?.remove();
    const empty = document.querySelector(".records-content .empty-records");
    if (!empty) return null;
    const div = document.createElement("div");
    div.id = EMPTY_ANCHOR_ID;
    div.className = savedContainerClass || `records-table ${getSvelteClass()}`;
    empty.before(div);
    return div;
  }

  // Страница сама уже запросила records API со своим level_id (внутренний id demonlist).
  function getLevelId() {
    const urls = performance
      .getEntriesByType("resource")
      .map((e) => e.name)
      .filter((n) => n.includes("record/list") && n.includes("level_id="));
    if (!urls.length) return null;
    const m = urls[urls.length - 1].match(/level_id=(\d+)/);
    return m ? m[1] : null;
  }

  /* ---------- загрузка данных ---------- */

  // Внутриигровой ID уровня (тот, что в самой Geometry Dash) из API demonlist.
  async function getIngameId(levelId) {
    const res = await getJsonRetry(`${DL_LEVEL_API}?id=${encodeURIComponent(levelId)}`);
    const v = res.ok ? res.data?.data?.ingame_id ?? res.data?.ingame_id : null;
    return v ? String(v) : null;
  }

  // Полный список демонов Pointercrate (нужен, если фильтр level_id не сработал).
  function loadAllPcDemons() {
    if (pcDemonsPromise) return pcDemonsPromise;
    pcDemonsPromise = (async () => {
      const all = [];
      const seen = new Set();
      let after = 0;
      for (let i = 0; i < 40; i++) {
        const r = await getJsonRetry(`${PC_API}v2/demons/?limit=100&after=${after}`);
        if (!r.ok || !Array.isArray(r.data) || !r.data.length) break;
        let added = 0;
        for (const d of r.data) {
          if (!seen.has(d.id)) { seen.add(d.id); all.push(d); added++; }
        }
        // ВАЖНО: параметр after в API Pointercrate работает по ID демона, а не по позиции
        const last = Math.max(...r.data.map((d) => Number(d.id) || 0));
        if (!added || r.data.length < 100 || !last || last <= after) break;
        after = last;
      }
      console.info("[Demonlist + Pointercrate] демонов Pointercrate загружено:", all.length);
      return all;
    })();
    return pcDemonsPromise;
  }

  // Ищем демона Pointercrate по внутриигровому ID. По названию — только как запасной вариант
  // и только если совпадение однозначно (названия у разных уровней бывают одинаковые).


  async function findPointercrateDemon(ingameId, levelName) {
    if (!ingameId) return null;

    const id = String(ingameId);

    // Сначала ищем по ID через API.
    const r = await getJsonRetry(
      `${PC_API}v2/demons/?level_id=${encodeURIComponent(id)}&limit=10`
    );

    if (r.ok && Array.isArray(r.data)) {
      const demon = r.data.find(
        (d) => String(d.level_id ?? "") === id
      );

      if (demon) {
        return { demon, via: "точный level_id" };
      }
    }

    // Резервный вариант: полный список, но тоже только по ID.
    const all = await loadAllPcDemons();
    const demon = all.find(
      (d) => String(d.level_id ?? "") === id
    );

    if (demon) {
      return { demon, via: "точный level_id (полный список)" };
    }

    return null;
  }

  async function fetchAllPointercrate(demonId) {
    const out = [];
    let after = 0;
    for (let page = 0; page < 60; page++) {
      const res = await getJsonRetry(
        `${PC_API}v1/records/?demon_id=${demonId}&limit=100&after=${after}`
      );
      if (!res.ok || !Array.isArray(res.data) || !res.data.length) break;
      out.push(...res.data);
      const last = Math.max(...res.data.map((r) => Number(r.id) || 0));
      if (res.data.length < 100 || !last || last <= after) break;
      after = last;
    }
    return out;
  }

  async function fetchCountry(playerId) {
    if (countryCache.has(playerId)) return countryCache.get(playerId);
    const res = await getJsonRetry(`${PC_API}v1/players/${playerId}/`);
    // временную ошибку не кешируем, чтобы потом можно было повторить
    if (!res.ok && (!res.status || res.status === 429 || res.status >= 500)) return null;
    const code = res.ok ? res.data?.data?.nationality?.country_code || null : null;
    countryCache.set(playerId, code);
    return code;
  }

  /* ---------- связи аккаунтов (links.json) ---------- */

  // Формат links.json:
  // {
  //   "14476": { "pointercrate": [40436, 51234], "siteB": [1, 2], "siteC": [] },
  //   "20001": { "pointercrate": [777] }
  // }
  // Ключ — ID на demonlist, внутри — сайты и их ID.

  let LINKS = {}; // { pointercrate: { "40436": "14476" }, siteB: { "1": "14476" } }
  let linksPromise = null;
  let LINK_NAMES = new Map(); // id на demonlist -> "_name" из links.json

  function setLinks(data) {
    LINKS = {};
    LINK_NAMES = new Map();
    for (const [mainId, sites] of Object.entries(data || {})) {
      // старый формат ("14476": 40436 или [40436]) считаем данными Pointercrate
      const entry =
        sites && typeof sites === "object" && !Array.isArray(sites)
          ? sites
          : { pointercrate: sites };
      if (typeof entry._name === "string" && entry._name) LINK_NAMES.set(String(mainId), entry._name);
      for (const [site, ids] of Object.entries(entry)) {
        if (site.startsWith("_")) continue; // "_name" и т.п. — справочная информация для человека
        LINKS[site] ||= {};
        for (const id of [].concat(ids)) LINKS[site][String(id)] = String(mainId);
      }
    }
  }

  // Скачиваем links.json с GitHub; если не вышло — берём последнюю сохранённую копию
  async function loadLinksOnly() {
    const res = await getJsonRetry(LINKS_URL);
    if (res.ok && res.data) {
      setLinks(res.data);
      try { chrome.storage.local.set({ playerLinks: res.data }); } catch (_) {}
      return;
    }
    const saved = await new Promise((resolve) => {
      try { chrome.storage.local.get("playerLinks", (r) => resolve(r?.playerLinks)); }
      catch (_) { resolve(null); }
    });
    setLinks(saved);
  }

  /* ---------- fix.json: слияние дублей аккаунтов ---------- */
  // Работает всегда, в том числе при выключенном PC Import.
  // Второй аккаунт (absorb) считается основным (keep): общий ключ, ник и профиль основного.

  let ABSORB = new Map(); // "id второго аккаунта" -> { keep: "id основного", keepName }
  let KEEP_NAMES = new Map(); // "id основного аккаунта" -> его ник (keep_name)
  const DL_NAMES = new Map(); // id на demonlist -> настоящий ник (собираем из записей demonlist по ходу работы)

  function setFix(data) {
    ABSORB = new Map();
    KEEP_NAMES = new Map();
    for (const m of data?.merges || []) {
      if (m.keep_name) KEEP_NAMES.set(String(m.keep), m.keep_name);
      for (const a of m.absorb || []) {
        ABSORB.set(String(a.id), { keep: String(m.keep), keepName: m.keep_name || "" });
      }
    }
  }

  async function loadFix() {
    const res = await getJsonRetry(FIX_URL);
    if (res.ok && res.data) {
      setFix(res.data);
      try { chrome.storage.local.set({ fixData: { merges: res.data.merges } }); } catch (_) {}
      return;
    }
    const saved = await new Promise((resolve) => {
      try { chrome.storage.local.get("fixData", (r) => resolve(r?.fixData)); }
      catch (_) { resolve(null); }
    });
    setFix(saved);
  }

  // links.json и fix.json грузим вместе, один раз
  async function loadPlayerLinks() {
    await Promise.all([loadFix(), loadLinksOnly()]);
  }

  const canonDlId = (id) => ABSORB.get(String(id))?.keep ?? String(id);

  // Ник аккаунта demonlist: fix.json -> ник из записей demonlist -> "_name" из links.json -> запасной
  const dlNameFor = (dlId, fallback) =>
    KEEP_NAMES.get(String(dlId)) ||
    DL_NAMES.get(String(dlId)) ||
    LINK_NAMES.get(String(dlId)) ||
    fallback;

  // ID на demonlist для аккаунта с данного сайта (или undefined)
  const dlIdFor = (site, id) => {
    const m = LINKS[site]?.[String(id)];
    return m ? canonDlId(m) : undefined;
  };

  // Ключ личности: все аккаунты одного человека получают один и тот же ключ
  function getPlayerKey(site, playerId, name, fallback) {
    if (playerId == null) return site + "-name:" + (normalize(name) || fallback);
    const mainId = dlIdFor(site, playerId);
    return mainId ? "dl:" + mainId : site + ":" + playerId;
  }

  const getDemonlistPlayerKey = (id, name, fallback) =>
    id == null
      ? "dl-name:" + (normalize(name) || fallback)
      : "dl:" + String(id);

  const getPointercratePlayerKey = (id, name, fallback) =>
    getPlayerKey("pointercrate", id, name, fallback);

  /* ---------- слияние ---------- */

  function mergeRecords(dlRecords, pcRecords) {
    const items = [];
    const videoIds = new Set();
    const byPlayer = new Map(); // по id игрока
    const byNorm = new Map(); // по нику (без [тегов], регистра и спецсимволов)
    const dlByVid = new Map(); // видео -> запись demonlist (для подсказок пар)
    let order = 0;

    // Ищем уже добавленную запись этого игрока: сначала по id, потом по нику
    const findPrev = (identityKey, norm) =>
      byPlayer.get(identityKey) || (norm ? byNorm.get(norm) : undefined);

    const removeItem = (it) => {
      items.splice(items.indexOf(it), 1);
      if (byPlayer.get(it.identityKey) === it) byPlayer.delete(it.identityKey);
      if (it.norm && byNorm.get(it.norm) === it) byNorm.delete(it.norm);
    };

    const add = (it) => {
      it.order = order++;
      items.push(it);
      if (it.vid) videoIds.add(it.vid);
      byPlayer.set(it.identityKey, it);
      if (it.norm) byNorm.set(it.norm, it);
    };

    for (const r of dlRecords) {
      const rawName = r.user?.username || "";
      // второй аккаунт из fix.json выдаём за основной (ник, id, профиль)
      const fixed = r.user?.id != null ? ABSORB.get(String(r.user.id)) : undefined;
      if (r.user?.id != null && rawName && !fixed) DL_NAMES.set(String(r.user.id), rawName);
      const name = fixed?.keepName || rawName;
      const norm = normalize(name);
      const dlId = fixed ? fixed.keep : r.user?.id;
      const identityKey = getDemonlistPlayerKey(dlId, name, "record:" + order);
      const prev = findPrev(identityKey, norm);
      const percent = Number(r.percent ?? 100);
      if (prev && prev.percent >= percent) continue;
      if (prev) removeItem(prev);
      const item = {
        source: "global",
        name,
        norm,
        origNorm: normalize(rawName), // ник настоящего аккаунта записи (для поиска флага)
        identityKey,
        percent,
        video: r.video_url || "",
        vid: extractVideoId(r.video_url),
        dlId,
        country: r.user?.country ?? r.user?.country_code ?? r.country ?? null,
        href: dlId ? `/profile/${dlId}` : "#",
      };
      add(item);
      if (item.vid) dlByVid.set(item.vid, item);
    }

    for (const r of pcRecords) {
      const pcName = r.player?.name || r.player_name || "Unknown";
      // если игрок есть в links.json — показываем его ник и профиль с demonlist
      const mainId = r.player?.id != null ? dlIdFor("pointercrate", r.player.id) : undefined;
      const name = mainId ? dlNameFor(mainId, pcName) : pcName;
      const norm = normalize(name);
      const identityKey = getPointercratePlayerKey(
        r.player?.id,
        name,
        "record:" + order
      );
      const percent = Number(r.progress ?? r.percent);
      if (!Number.isFinite(percent)) continue;
      const vid = extractVideoId(r.video);
      if (vid && videoIds.has(vid)) {
        // То же видео уже есть у игрока demonlist, а ники разные — возможно, это один человек
        const twin = dlByVid.get(vid);
        const pcId = r.player?.id;
        if (
          twin && twin.dlId != null && pcId != null &&
          twin.norm !== norm &&
          dlIdFor("pointercrate", pcId) !== String(twin.dlId)
        ) {
          suggestions.set(twin.dlId + ":" + pcId, {
            dl: twin.dlId, pc: pcId, dlName: twin.name, pcName: name,
          });
        }
        continue;
      }
      const prev = findPrev(identityKey, norm);
      if (prev && prev.percent >= percent) continue;
      if (prev) removeItem(prev);
      const pid = r.player?.id;
      add({
        source: "pointercrate",
        name,
        norm,
        identityKey,
        percent,
        video: r.video || "",
        vid,
        playerId: pid,
        country: pid ? countryCache.get(pid) || null : null,
        href: mainId
          ? `/profile/${mainId}`
          : pid
            ? `https://pointercrate.com/demonlist/statsviewer/?player=${encodeURIComponent(pid)}`
            : "https://pointercrate.com/demonlist/statsviewer/",
      });
    }

    items.sort(
      (a, b) =>
        b.percent - a.percent ||
        (a.source === "pointercrate") - (b.source === "pointercrate") ||
        a.order - b.order
    );
    return items;
  }

  /* ---------- страны и флаги ---------- */

  const FLAG_SEL = "img.player-avatar-flag, img[src*='/flags/'], img[class*='flag' i]";
  const FLAG_DIR = "/img/flags/";
  const PLACEHOLDER = { src: FLAG_DIR + "xx.svg", alt: "Unknown", title: "Unknown" };

  // Названия, которые Intl отдаёт не так, как привыкли на сайтах
  const NAME_OVERRIDES = {
    HK: "Hong Kong",
    MO: "Macao",
    PS: "Palestine",
    MM: "Myanmar",
    CD: "DR Congo",
    CG: "Republic of the Congo",
    TR: "Turkey",
    CZ: "Czech Republic",
    SZ: "Eswatini",
    CV: "Cape Verde",
    TL: "East Timor",
    MK: "North Macedonia",
  };

  const ALIASES = [
    ["UAE", "AE"], ["UK", "GB"], ["USA", "US"], ["America", "US"],
    ["Czechia", "CZ"], ["Turkiye", "TR"], ["Swaziland", "SZ"], ["Macedonia", "MK"],
    ["Cabo Verde", "CV"], ["Cote dIvoire", "CI"], ["Ivory Coast", "CI"],
    ["Timor-Leste", "TL"], ["Democratic Republic of the Congo", "CD"], ["Congo", "CG"],
    ["Korea", "KR"], ["Burma", "MM"], ["Macau", "MO"], ["Russian Federation", "RU"],
    ["Holy See", "VA"], ["Vatican City", "VA"], ["Palestinian Territories", "PS"],
    ["Kosovo", "XK"], ["Vietnam", "VN"], ["Laos", "LA"], ["Iran", "IR"], ["Syria", "SY"],
    ["Bolivia", "BO"], ["Venezuela", "VE"], ["Moldova", "MD"], ["Brunei", "BN"],
    ["Tanzania", "TZ"], ["Micronesia", "FM"], ["Russia", "RU"], ["South Korea", "KR"],
    ["North Korea", "KP"], ["Taiwan", "TW"], ["Hong Kong", "HK"], ["Macao", "MO"],
    ["DR Congo", "CD"], ["Republic of the Congo", "CG"], ["Czech Republic", "CZ"],
    ["Turkey", "TR"], ["Cape Verde", "CV"], ["East Timor", "TL"],
  ];

  const regionNames = (() => {
    try { return new Intl.DisplayNames(["en"], { type: "region" }); } catch (_) { return null; }
  })();

  // Полное название страны по двухбуквенному коду
  function countryName(code) {
    const c = String(code || "").toUpperCase();
    if (!/^[A-Z]{2}$/.test(c)) return "";
    if (NAME_OVERRIDES[c]) return NAME_OVERRIDES[c];
    try {
      const n = regionNames?.of(c);
      if (n && n !== c) return n.replace(/\s*&\s*/g, " and ");
    } catch (_) {}
    return c;
  }

  // Название страны (нормализованное) -> код
  const NAME_TO_CODE = (() => {
    const map = new Map();
    for (let a = 65; a <= 90; a++) {
      for (let b = 65; b <= 90; b++) {
        const code = String.fromCharCode(a, b);
        const n = countryName(code);
        if (n && n !== code) map.set(normalize(n), code);
      }
    }
    for (const [alias, code] of ALIASES) map.set(normalize(alias), code);
    return map;
  })();

  const unresolvedCountries = new Set();
  let flagCtx = { byName: new Map(), byKey: new Map() };

  const findFlag = (card) => card.querySelector(FLAG_SEL) || card.querySelector("img");

  // Флаги с оригинальных карточек: ник -> флаг (самый надёжный источник)
  function collectFlags(cards) {
    flagCtx = { byName: new Map(), byKey: new Map() };
    for (const c of cards) {
      const img = findFlag(c);
      if (!img || !img.src) continue;
      const info = { src: img.src, alt: img.alt || "", title: img.title || "" };
      const nm = normalize(getNameFromCard(c));
      if (nm) flagCtx.byName.set(nm, info);
      for (const k of [img.alt, img.title]) {
        if (k && !/^unknown$/i.test(k) && !/^[a-z]{2}$/i.test(k.trim())) {
          flagCtx.byKey.set(normalize(k), info);
        }
      }
    }
  }

  const flagFromCode = (code) => {
    const c = String(code).toLowerCase();
    const name = countryName(c) || c.toUpperCase();
    return { src: `${FLAG_DIR}${c}.svg`, alt: name, title: name };
  };

  // value: название страны ("UNITED STATES") или код ("US")
  function resolveFlag(value) {
    if (!value) return null;
    const v = typeof value === "object" ? value.code || value.country_code || value.name : value;
    if (!v) return null;
    const k = normalize(v);
    if (!k || k === "unknown" || k === "xx") return null;
    if (flagCtx.byKey.has(k)) return flagCtx.byKey.get(k);
    if (NAME_TO_CODE.has(k)) return flagFromCode(NAME_TO_CODE.get(k));
    if (/^[a-z]{2}$/i.test(String(v).trim())) return flagFromCode(String(v).trim());
    unresolvedCountries.add(String(v));
    return null;
  }

  const flagFor = (it) =>
    flagCtx.byName.get(it.norm) ||
    (it.origNorm && flagCtx.byName.get(it.origNorm)) ||
    resolveFlag(it.country);

  // Полное название страны для подсказки: по коду из имени файла флага
  function fullNameOf(f) {
    const m = /\/([a-z]{2})\.svg/i.exec(f.src || "");
    if (m) {
      const n = countryName(m[1]);
      if (n && n.toUpperCase() !== m[1].toUpperCase()) return n;
    }
    const t = (f.title || f.alt || "").trim();
    if (t && !/^[a-z]{2}$/i.test(t)) return t;
    return m && m[1].toLowerCase() !== "xx" ? m[1].toUpperCase() : "Unknown";
  }

  function applyFlag(card, info) {
    const img = findFlag(card);
    if (!img) return;
    const f = info || PLACEHOLDER;
    const name = fullNameOf(f);
    img.removeAttribute("srcset");
    img.onerror = () => {
      img.onerror = null;
      img.src = PLACEHOLDER.src;
      img.alt = img.title = "Unknown";
    };
    img.style.visibility = "";
    img.src = f.src;
    img.alt = name;
    img.title = name;
    const p = img.parentElement;
    if (p && p !== card && p.hasAttribute("title")) p.title = name;
  }

  /* ---------- отрисовка ---------- */

  function buildCard(template, it) {
    const card = template.cloneNode(true);
    card.removeAttribute("id");
    card.dataset.source = it.source;

    const nameEl = card.querySelector(".record-username, [class*='username'], [class*='name']");
    if (nameEl) nameEl.textContent = it.name;

    const link = card.querySelector(".record-player") || card.querySelector("a:not([class*='video'])");
    if (link) {
      link.href = it.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }

    const pEl = card.querySelector(".record-percent, [class*='percent']");
    if (pEl) {
      pEl.textContent = `${it.percent}%`;
      pEl.classList.toggle("percent-100", it.percent === 100);
    }

    const video = card.querySelector(".record-video, a[href*='youtu'], [class*='video']");
    if (video) {
      if (it.video) {
        video.href = it.video;
        video.target = "_blank";
        video.rel = "noopener noreferrer";
        video.title = it.source === "pointercrate" ? "Pointercrate" : "Video";
      } else {
        video.remove();
      }
    }

    applyFlag(card, flagFor(it));
    it.card = card;
    return card;
  }

  function renderList(id, container, items, template) {
    const list = container.cloneNode(false);
    list.id = id;
    list.className = container.className;
    list.replaceChildren(...items.map((it) => buildCard(template, it)));
    return list;
  }

  // Числа в счётчике ("21 всего записей, 21 из которых 100%") — элементы без вложенных тегов.
  // Меняем только сами числа, остальной текст остаётся на языке сайта.
  const countNumbers = (el) =>
    [...el.querySelectorAll("*")].filter(
      (n) => !n.children.length && /^\d+…?$/.test(n.textContent.trim())
    );

  function setNumber(node, value) {
    const s = String(value);
    const t = node.firstChild && node.firstChild.nodeType === 3 ? node.firstChild : null;
    if (t) {
      if (t.nodeValue !== s) t.nodeValue = s; // правим текстовый узел на месте, чтобы не ломать сайт
    } else if (node.textContent !== s) {
      node.textContent = s;
    }
  }

  // Счётчик показывает числа нашего списка в обоих режимах (дубли из fix.json уже убраны)
  function updateCount() {
    const el = document.querySelector(".records-count");
    if (!el) return;
    const nums = countNumbers(el);
    if (nums.length < 2) return;
    if (!el.dataset.origTotal) {
      el.dataset.origTotal = nums[0].textContent.trim();
      el.dataset.origHundred = nums[1].textContent.trim();
    }
    const s = enabled ? stats.merged : stats.dl;
    // Пока данные догружаются, числа ориентировочные — помечаем их многоточием
    const mark = dataCache && !dataCache.complete ? "…" : "";
    setNumber(nums[0], s.total + mark);
    setNumber(nums[1], s.hundred + mark);
  }

  // Надпись "No records yet" показываем, только если нам самим нечего показать
  function updateEmptyNotice() {
    const e = document.querySelector(".empty-records");
    if (!e) return;
    const s = enabled ? stats.merged : stats.dl;
    e.style.display = s.total === 0 ? "" : "none";
  }

  function applyMode() {
    document.documentElement.classList.toggle("pc-on", enabled);
    updateCount();
    updateEmptyNotice();
    observeSentinel(); // при смене режима длина видимого списка другая — перепроверяем
  }

  // Переключатель кладём поверх карточки "Records" абсолютным позиционированием,
  // ничего не меняя в вёрстке заголовка — счётчик остаётся на своём месте.
  function ensureSwitch(container) {
    if (document.getElementById(TOGGLE_ID)) return;
    const countEl = document.querySelector(".records-count");
    if (!countEl) return;

    let card = countEl;
    while (card && !card.contains(container)) card = card.parentElement;
    if (!card || card === document.body || card === document.documentElement) return;

    const parent = countEl.parentElement;
    const ref = parent && !parent.contains(container) ? parent : countEl;
    const refRect = ref.getBoundingClientRect();
    if (getComputedStyle(card).position === "static") card.style.position = "relative"; // сам по себе ничего не сдвигает
    const cardRect = card.getBoundingClientRect();

    const wrap = document.createElement("div");
    wrap.id = TOGGLE_ID;
    wrap.className = "pc-import-switch-container";
    wrap.style.top = `${refRect.top - cardRect.top + refRect.height / 2}px`;
    wrap.innerHTML = `
      <span class="pc-switch-title">PC Import</span>
      <button type="button" class="pc-native-toggle" role="switch">
        <div class="pc-switch"><div class="pc-switch-thumb"></div></div>
      </button>`;
    const btn = wrap.querySelector("button");
    const sw = wrap.querySelector(".pc-switch");
    const sync = () => {
      sw.classList.toggle("active", enabled);
      btn.setAttribute("aria-checked", String(enabled));
    };
    sync();
    btn.addEventListener("click", () => {
      enabled = !enabled;
      sync();
      storageSet(enabled);
      applyMode();
    });
    card.appendChild(wrap);
  }

  function markPagination(container) {
    const parent = container.parentElement;
    if (!parent) return;

    parent.querySelectorAll("button, a").forEach((b) => {
      if (isMine(b) || container.contains(b)) return;

      const t = b.textContent.trim().toLowerCase();

      if (
        t.includes("previous") ||
        t.includes("next") ||
        t.includes("назад") ||
        t.includes("вперёд") ||
        t.includes("вперед")
      ) {
        const row = b.closest("div");
        (row && row !== container && row !== parent ? row : b)
          .setAttribute("data-pc-pag", "1");
      }
    });
  }

  /* ---------- основной цикл ---------- */

  function reset() {
    document.getElementById(MERGED_ID)?.remove();
    document.getElementById(DL_ID)?.remove();
    document.getElementById(EMPTY_ANCHOR_ID)?.remove();
    document.getElementById(TOGGLE_ID)?.remove();
    document.getElementById(PROFILE_ID)?.remove();
    document.getElementById(LB_TOGGLE_ID)?.remove();
    restoreLeaderboard();
    clearProfileChips();
    document.querySelectorAll(".pc-hidden-original").forEach((e) => e.classList.remove("pc-hidden-original"));
    document.querySelectorAll("[data-pc-pag]").forEach((e) => e.removeAttribute("data-pc-pag"));
    document.querySelector(".empty-records")?.style.removeProperty("display");
    const cnt = document.querySelector(".records-count");
    if (cnt) {
      const nums = countNumbers(cnt);
      if (nums.length >= 2 && cnt.dataset.origTotal) {
        setNumber(nums[0], cnt.dataset.origTotal);
        setNumber(nums[1], cnt.dataset.origHundred);
      }
      delete cnt.dataset.origTotal;
      delete cnt.dataset.origHundred;
    }
    document.documentElement.classList.remove("pc-on", "pc-active", "pc-profile");
    profileSkipKey = null;
    doneKey = null;
    failedKey = null;
    retries = 0;
    clearTimeout(renderTimer);
    renderTimer = null;
    shown = PAGE_SIZE;
    listsState = null;
    sentinelIO?.disconnect();
    document.getElementById(SENTINEL_ID)?.remove();
  }

  // Таймер отложенной отрисовки и время последней отрисовки (см. scheduleRender)
  let renderTimer = null;
  let lastRenderMs = 0;

  const countStats = (items) => ({
    total: items.length,
    hundred: items.filter((i) => i.percent === 100).length,
  });

  // Числа из счётчика сайта ("5000 всего, 120 из них 100%") — до того как мы их изменили
  function originalCounts() {
    const el = document.querySelector(".records-count");
    if (!el) return null;
    if (el.dataset.origTotal) {
      return { total: parseInt(el.dataset.origTotal, 10), hundred: parseInt(el.dataset.origHundred, 10) };
    }
    const nums = countNumbers(el);
    if (nums.length < 2) return null;
    return { total: parseInt(nums[0].textContent, 10), hundred: parseInt(nums[1].textContent, 10) };
  }

  // Пока записи ещё грузятся, показываем ОЖИДАЕМЫЕ числа (они известны сразу),
  // а когда всё загружено — точные, уже с учётом удалённых дублей.
  function computeStats(dlItems, mergedItems) {
    if (dataCache.complete) return { merged: countStats(mergedItems), dl: countStats(dlItems) };
    const orig = originalCounts();
    const total = dataCache.dlTotal || (orig ? orig.total : dlItems.length);
    const hundred = orig ? orig.hundred : countStats(dlItems).hundred;
    const pcHundred = dataCache.pc.filter((r) => Number(r.progress ?? r.percent) === 100).length;
    return {
      dl: { total, hundred },
      merged: { total: total + dataCache.pc.length, hundred: hundred + pcHundred },
    };
  }

  // Строит (или перестраивает) наши списки и вставляет их на страницу.
  // Возвращает список записей или null, если страница ещё не готова.
  function render() {
    if (!dataCache) return null;
    const cards = findRecordCards();

    let container, template;
    if (cards.length) {
      collectFlags(cards);
      container = findCardsContainer(cards);
      template = cards[0];
      savedTemplate = template.cloneNode(true);
      savedContainerClass = container.className.replace(/\bpc-hidden-original\b/g, "").trim();
    } else {
      flagCtx = { byName: new Map(), byKey: new Map() };
      container = makeEmptyContainer();
      template = savedTemplate || makeFallbackTemplate();
    }
    if (!container) return null;

    const dlItems = mergeRecords(dataCache.dl, []);
    const mergedItems = mergeRecords(dataCache.dl, dataCache.pc);
    stats = computeStats(dlItems, mergedItems);

    listsState = { dl: dlItems, merged: mergedItems, template: savedTemplate || template };

    // Строим не все карточки, а только первые `shown` (по умолчанию 100)
    const dlList = renderList(DL_ID, container, dlItems.slice(0, shown), template);
    const mergedList = renderList(MERGED_ID, container, mergedItems.slice(0, shown), template);
    // renderList копирует класс контейнера, а он мог уже быть помечен как скрытый
    dlList.classList.remove("pc-hidden-original");
    mergedList.classList.remove("pc-hidden-original");

    // Если наши списки уже есть — подменяем их на месте (страница не "прыгает"),
    // если нет — вставляем рядом с оригиналом.
    const oldDl = document.getElementById(DL_ID);
    const oldMerged = document.getElementById(MERGED_ID);
    if (oldDl) oldDl.replaceWith(dlList); else container.after(dlList);
    if (oldMerged) oldMerged.replaceWith(mergedList); else dlList.after(mergedList);

    // Невидимая метка под списком: когда пользователь долистал почти до неё, добавляем карточки
    let sentinel = document.getElementById(SENTINEL_ID);
    if (!sentinel) {
      sentinel = document.createElement("div");
      sentinel.id = SENTINEL_ID;
      sentinel.style.cssText = "height:1px;width:100%;pointer-events:none;";
    }
    mergedList.after(sentinel);

    container.classList.add("pc-hidden-original"); // оригинал прячем
    document.documentElement.classList.add("pc-active");

    markPagination(container);
    ensureSwitch(container);
    applyMode();
    return mergedItems;
  }

  /* ---------- постепенный показ карточек ---------- */

  // Есть ли ещё карточки, которые не показаны (в том режиме, который сейчас включён)
  function hasMoreToShow() {
    if (!listsState) return false;
    const items = enabled ? listsState.merged : listsState.dl;
    return shown < items.length;
  }

  // Дорисовывает следующие PAGE_SIZE карточек в оба списка (без перестройки уже готовых)
  function appendMore() {
    if (!listsState) return;
    const from = shown;
    shown += PAGE_SIZE;
    for (const [id, items] of [[DL_ID, listsState.dl], [MERGED_ID, listsState.merged]]) {
      const list = document.getElementById(id);
      if (!list) continue;
      list.append(...items.slice(from, shown).map((it) => buildCard(listsState.template, it)));
    }
  }

  // IntersectionObserver сообщает, когда метка попала в зону видимости
  // (с запасом 800px, чтобы карточки появлялись до того, как пользователь долистает до конца).
  function observeSentinel() {
    const s = document.getElementById(SENTINEL_ID);
    if (!s || typeof IntersectionObserver === "undefined") return;
    if (!sentinelIO) {
      sentinelIO = new IntersectionObserver(
        (entries) => {
          if (!entries.some((e) => e.isIntersecting)) return;
          if (hasMoreToShow()) appendMore();
          observeSentinel(); // метка уехала вниз — проверяем заново, вдруг она всё ещё видна
        },
        { rootMargin: "800px 0px" }
      );
    }
    sentinelIO.disconnect();
    if (hasMoreToShow()) sentinelIO.observe(s);
  }

  // Мгновенно обновляет только счётчик (без карточек): как только узнали общее число записей
  function updateCounterEarly() {
    if (!dataCache || dataCache.complete) return;
    stats = computeStats([], []);
    applyMode();
  }

  function doRender() {
    const t0 = performance.now();
    const items = render();
    lastRenderMs = performance.now() - t0;
    if (items) doneKey = location.pathname;
    return items;
  }

  // Перерисовывать список после КАЖДОЙ порции данных слишком тяжело (карточек тысячи),
  // поэтому перерисовка откладывается: не чаще раза в 300 мс и не чаще, чем
  // "время прошлой отрисовки × 3" — так страница всегда успевает реагировать на мышь.
  function scheduleRender(isCancelled) {
    if (renderTimer) return;
    renderTimer = setTimeout(() => {
      renderTimer = null;
      if (!isCancelled()) doRender();
    }, Math.max(300, lastRenderMs * 3));
  }

  // Страны игроков Pointercrate грузим в фоне (по одному запросу на игрока),
  // а потом перерисовываем списки, чтобы появились флаги.
  async function loadCountriesAndRerender(items, key) {
    const ids = [
      ...new Set(
        items.filter((i) => i.source === "pointercrate" && i.playerId != null).map((i) => i.playerId)
      ),
    ].filter((id) => !countryCache.has(id));
    if (!ids.length) return;

    let i = 0;
    await Promise.all(
      Array.from({ length: 5 }, async () => {
        while (i < ids.length) await fetchCountry(ids[i++]);
      })
    );
    if (location.pathname === key && document.getElementById(MERGED_ID)) doRender();
  }

  // Загружает данные уровня по частям. Первая порция показывается сразу,
  // остальные дорисовываются по мере прихода.
  async function loadLevelData(data, key) {
    const levelId = data.key;
    const cancelled = () => dataCache !== data || location.pathname !== key || data.failed;
    const limit = 50;
    const url = (offset) =>
      `${DL_API}?level_id=${encodeURIComponent(levelId)}&limit=${limit}&offset=${offset}`;
    const getRecords = (res) => res.data?.data?.records || res.data?.records || [];
    const getTotal = (res, records) =>
      Number(res.data?.data?.total_count ?? res.data?.total_count ?? records.length);

    // links.json и fix.json нужны до первой отрисовки (они влияют на ники и слияние)
    const linksReady = ensurePlayerLinks();

    // Pointercrate идёт отдельной цепочкой, параллельно с Demonlist
    const pcTask = (async () => {
      try {
        const ingameId = await getIngameId(levelId);
        if (!ingameId) return;
        const found = await findPointercrateDemon(ingameId);
        if (!found?.demon?.id) return;
        data.pc = await fetchAllPointercrate(found.demon.id);
        if (!cancelled()) updateCounterEarly(); // счётчик учитывает Pointercrate сразу
        await linksReady; // для самих карточек нужны links.json / fix.json
        if (!cancelled()) scheduleRender(cancelled);
      } catch (e) {
        console.error("[PC Import] Pointercrate:", e); // Demonlist при этом всё равно покажется
      }
    })();

    // Первая порция — по ней сразу узнаём общее число записей
    const first = await getJsonRetry(url(0));
    if (!first.ok) throw new Error("Не удалось загрузить записи Demonlist");
    const firstRecords = getRecords(first);
    data.dlTotal = Math.min(getTotal(first, firstRecords), 20000);
    data.dl = firstRecords;
    updateCounterEarly(); // счётчик обновляется сразу, не дожидаясь links.json и карточек
    console.log(`[PC Import] Всего записей Demonlist: ${data.dlTotal}, грузим по частям...`);

    await linksReady;
    if (cancelled()) { if (dataCache === data) dataCache = null; return; }
    doRender(); // счётчик и первые карточки появляются сразу

    // Остальные порции качаем в несколько потоков (DL_CONCURRENCY)
    const offsets = [];
    for (let o = firstRecords.length; o < data.dlTotal; o += limit) offsets.push(o);
    const pages = new Array(offsets.length);
    let next = 0;
    let prefix = 0; // сколько страниц подряд (без дыр) уже получено

    async function worker() {
      while (!cancelled()) {
        const idx = next++;
        if (idx >= offsets.length) return;
        const res = await getJsonRetry(url(offsets[idx]));
        if (!res.ok) {
          data.failed = true;
          throw new Error(`Ошибка загрузки Demonlist: offset=${offsets[idx]}`);
        }
        pages[idx] = getRecords(res);

        // Показываем только "цепочку без дыр" — так порядок карточек не прыгает
        const before = prefix;
        while (prefix < pages.length && pages[prefix]) prefix++;
        if (prefix > before) {
          data.dl = firstRecords.concat(...pages.slice(0, prefix));
          scheduleRender(cancelled);
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(DL_CONCURRENCY, offsets.length) }, worker));
    await pcTask;

    if (cancelled()) { if (dataCache === data) dataCache = null; return; }

    // Всё загружено — финальная точная отрисовка и точный счётчик
    data.dl = firstRecords.concat(...pages);
    data.complete = true;
    clearTimeout(renderTimer);
    renderTimer = null;
    const items = doRender();
    console.log(`[PC Import] Готово: Demonlist ${data.dl.length}, Pointercrate ${data.pc.length}`);
    if (items) loadCountriesAndRerender(items, key); // в фоне, без await
  }

  async function run() {
    if (running) return;

    const key = location.pathname;
    if (failedKey === key) return; // на этой странице уже была ошибка

    const levelId = getLevelId();
    if (!levelId) return; // сайт ещё не запросил записи

    // ждём, пока сайт нарисует свой список (или надпись "No records yet")
    if (!findRecordCards().length && !document.querySelector(".empty-records")) return;

    running = true;
    try {
      enabled = await storageGet(); // читаем сохранённое состояние переключателя

      if (!dataCache || dataCache.key !== levelId) {
        // новый уровень — грузим с нуля
        const data = { key: levelId, dl: [], pc: [], dlTotal: 0, complete: false, failed: false };
        dataCache = data;
        await loadLevelData(data, key);
      } else if (dataCache.complete) {
        // данные уже есть (сайт пересоздал страницу) — просто рисуем заново
        doRender();
      }
    } catch (error) {
      failedKey = key;
      if (dataCache && !dataCache.complete) dataCache = null; // чтобы при следующем заходе загрузить заново
      console.error("[PC Import] Ошибка загрузки уровня:", error);
    } finally {
      running = false;
    }
  }

  /* ---------- страница профиля игрока ---------- */

  // links.json и fix.json качаем один раз, дальше берём из памяти
  const ensurePlayerLinks = () => (linksPromise ||= loadPlayerLinks());

  // Выполняет fn для каждого элемента, но не больше `limit` вызовов одновременно.
  // Результаты возвращаются в том же порядке, что и элементы.
  async function mapPool(items, limit, fn) {
    const out = new Array(items.length);
    let next = 0;
    await Promise.all(
      Array.from({ length: Math.min(limit, items.length) }, async () => {
        while (next < items.length) {
          const i = next++;
          out[i] = await fn(items[i], i);
        }
      })
    );
    return out;
  }

  // ID аккаунтов Pointercrate, привязанных к этому профилю Demonlist (из links.json)
  function pcIdsForUser(userId) {
    const target = canonDlId(userId);
    const out = [];
    for (const [pcId, mainId] of Object.entries(LINKS.pointercrate || {})) {
      if (mainId === String(userId) || canonDlId(mainId) === target) out.push(pcId);
    }
    return out;
  }

  // Строки записей, которые сайт нарисовал на профиле (у них есть .record-level)
  const findProfileRows = () =>
    [...document.querySelectorAll(".record-row")].filter((el) => el.querySelector(".record-level") && !isMine(el));

  // container — общий родитель строк; templateEl — то, что клонируем для новых строк
  function profileAnchor(rows) {
    let container = rows[0].parentElement;
    while (container && !rows.every((r) => container.contains(r))) container = container.parentElement;
    let templateEl = rows[0];
    while (templateEl.parentElement && templateEl.parentElement !== container) templateEl = templateEl.parentElement;
    return { container, templateEl };
  }

  // Записи игрока на Demonlist (все страницы)
  async function fetchUserRecords(userId) {
    const limit = 50;
    const url = (o) => `${DL_USER_API}?user_id=${encodeURIComponent(userId)}&limit=${limit}&offset=${o}`;
    const getRecords = (res) => res.data?.data?.records || [];
    const first = await getJsonRetry(url(0));
    if (!first.ok) throw new Error("Не удалось загрузить записи профиля Demonlist");
    const head = getRecords(first);
    const total = Number(first.data?.data?.total_count ?? head.length);
    const offsets = [];
    for (let o = head.length; o < total; o += limit) offsets.push(o);
    const pages = await mapPool(offsets, DL_CONCURRENCY, async (o) => {
      const res = await getJsonRetry(url(o));
      if (!res.ok) throw new Error(`Ошибка загрузки записей профиля: offset=${o}`);
      return getRecords(res);
    });
    return head.concat(...pages);
  }

  // Записи одного игрока Pointercrate (в ответе игрока есть список его записей)
  async function fetchPcPlayerRecords(pcId) {
    const res = await getJsonRetry(`${PC_API}v1/players/${encodeURIComponent(pcId)}/`);
    if (!res.ok) {
      console.warn(`[PC Import] Не удалось загрузить игрока Pointercrate ${pcId}`);
      return [];
    }
    const list = res.data?.data?.records || [];
    // берём только принятые записи
    return list.filter((r) => !r.status || String(r.status).toUpperCase() === "APPROVED");
  }

  // Уровень Demonlist по ID уровня в игре. null — если такого уровня (или его места в списке) нет.
  async function getDlLevelByIngame(ingameId) {
    const id = String(ingameId);
    if (dlLevelCache.has(id)) return dlLevelCache.get(id);
    const res = await getJsonRetry(`${DL_LEVEL_API}?ingame_id=${encodeURIComponent(id)}`, 6);
    const d = res.ok ? res.data?.data : null;
    const level =
      d && d.id != null && d.placement != null
        ? { id: d.id, placement: d.placement, name: d.name, listPercent: d.list_percent ?? null }
        : null;
    // временную ошибку (сеть, 429, 5xx) не запоминаем, чтобы потом можно было повторить
    if (res.ok || (res.status && res.status !== 429 && res.status < 500)) dlLevelCache.set(id, level);
    return level;
  }

  // Ищет уровни по ID в игре: быстрый проход, затем медленный повтор для тех, на кого
  // Demonlist ответил ошибкой/лимитом (иначе такие уровни ошибочно считались бы "только на Pointercrate").
  async function resolveDlLevels(ingameIds) {
    await mapPool(ingameIds, 6, getDlLevelByIngame);
    for (let pass = 0; pass < 3; pass++) {
      const missing = ingameIds.filter((id) => !dlLevelCache.has(String(id)));
      if (!missing.length) break;
      await sleep(1500 * (pass + 1));
      await mapPool(missing, 2, getDlLevelByIngame);
    }
  }

  // Превращает записи Pointercrate в "элементы списка", находя для каждой уровень на Demonlist
  async function buildPcItems(pcRecords) {
    if (!pcRecords.length) return [];
    const demons = await loadAllPcDemons();
    const byId = new Map(demons.map((d) => [d.id, d]));

    // Демоны, которых нет в общем списке (или у них нет level_id): докачиваем по одному.
    // В записи есть demon.id; параметр after у API работает по ID демона,
    // поэтому after=<id-1> возвращает демонов, начиная ровно с нужного.
    const lacking = new Map();
    for (const r of pcRecords) {
      const dm = r.demon;
      if (!dm || dm.id == null || byId.get(dm.id)?.level_id) continue;
      lacking.set(dm.id, dm);
    }
    // По названию не ищем (названия бывают одинаковые), совпадение — только по id демона.
    await mapPool([...lacking.values()], 5, async (dm) => {
      const res = await getJsonRetry(`${PC_API}v2/demons/?limit=5&after=${Math.max(0, Number(dm.id) - 1)}`);
      const found = res.ok && Array.isArray(res.data) ? res.data.find((d) => d.id === dm.id && d.level_id) : null;
      if (found) byId.set(found.id, found);
    });

    const ingameOf = (r) => {
      const v = (byId.get(r.demon?.id) || r.demon || {}).level_id;
      return v != null && v !== "" ? String(v) : null;
    };

    const ingameIds = [...new Set(pcRecords.map(ingameOf).filter(Boolean))];
    await resolveDlLevels(ingameIds);

    return pcRecords
      .map((r) => {
        const demon = byId.get(r.demon?.id) || r.demon || {};
        const ingame = ingameOf(r);
        const lvl = ingame ? dlLevelCache.get(ingame) || null : null;
        const progress = Number(r.progress);
        if (!Number.isFinite(progress)) return null;
        return {
          source: "pointercrate",
          key: lvl ? "lvl:" + lvl.id : "pc:" + (demon.id ?? r.demon?.id ?? r.id),
          placement: lvl ? lvl.placement : null, // место на Demonlist (null — уровня там нет)
          listPercent: lvl ? lvl.listPercent ?? null : null, // минимальный процент уровня на Demonlist
          pcPosition: demon.position ?? r.demon?.position ?? null,
          name: lvl ? lvl.name : demon.name || r.demon?.name || "Unknown",
          percent: progress,
          video: r.video || "",
          status: "accepted",
          href: lvl
            ? `/classic/${lvl.placement}`
            : demon.id != null
              ? `https://pointercrate.com/demonlist/permalink/${demon.id}/`
              : "https://pointercrate.com/demonlist/",
          external: !lvl,
        };
      })
      .filter(Boolean);
  }

  // Объединяет записи Demonlist и Pointercrate.
  // Записи Demonlist остаются в порядке сайта; записи Pointercrate добавляются после них
  // (от сложных уровней к лёгким). Если уровень есть на обоих сайтах — остаётся запись
  // с большим процентом, при равных — запись Demonlist.
  function mergeProfile(dlRecords, pcItems) {
    const items = [];
    const byKey = new Map();
    for (const r of dlRecords) {
      const lvl = r.level || {};
      const it = {
        source: "global",
        key: lvl.id != null ? "lvl:" + lvl.id : "dl:" + r.id,
        placement: lvl.placement ?? null,
        listPercent: lvl.list_percent ?? null, // минимальный процент уровня (нужен для очков за прогресс)
        pcPosition: null,
        name: lvl.name || "",
        percent: Number(r.percent),
        video: r.video_url || "",
        status: r.status || "accepted",
        href: lvl.placement != null ? `/classic/${lvl.placement}` : "#",
        external: false,
      };
      items.push(it);
      byKey.set(it.key, it);
    }

    const extras = [];
    for (const it of pcItems) {
      const prev = byKey.get(it.key);
      if (!prev) {
        byKey.set(it.key, it);
        extras.push(it);
      } else if (it.percent > prev.percent) {
        const oldPercent = prev.percent;
        const oldListPercent = prev.listPercent;
        Object.assign(prev, it); // заменяем на месте: запись Pointercrate лучше
        prev.replaced = true; // у этого уровня в блоках сверху надо спрятать старую плашку Demonlist
        prev.oldPercent = oldPercent; // нужно для пересчёта очков: старая запись заменяется новой
        prev.listPercent = it.listPercent ?? oldListPercent ?? null;
      }
    }
    extras.sort(
      (a, b) =>
        (a.placement ?? Infinity) - (b.placement ?? Infinity) ||
        (a.pcPosition ?? Infinity) - (b.pcPosition ?? Infinity)
    );
    return items.concat(extras);
  }

  async function loadProfileItems(userId, pcIds) {
    const [dlRecords, pcLists] = await Promise.all([
      fetchUserRecords(userId),
      Promise.all(pcIds.map(fetchPcPlayerRecords)),
    ]);
    const pcItems = await buildPcItems(pcLists.flat());
    console.log(`[PC Import] Профиль ${userId}: Demonlist ${dlRecords.length}, Pointercrate ${pcItems.length}`);
    return mergeProfile(dlRecords, pcItems);
  }

  // Одна строка записи: клонируем настоящую строку сайта и подставляем свои данные
  // Иконки статусов (mdi: check-circle и clock-outline), как на сайте
  const svgIcon = (d) =>
    `<svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true" role="img" class="w-4 h-4 iconify iconify--mdi" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="${d}"></path></svg>`;
  const STATUS_SVG = {
    accepted: svgIcon("M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10s10-4.5 10-10S17.5 2 12 2m-2 15l-5-5l1.41-1.41L10 14.17l7.59-7.59L19 8z"),
    waiting: svgIcon("M12 20a8 8 0 0 0 8-8a8 8 0 0 0-8-8a8 8 0 0 0-8 8a8 8 0 0 0 8 8m0-18a10 10 0 0 1 10 10a10 10 0 0 1-10 10C6.47 22 2 17.5 2 12A10 10 0 0 1 12 2m.5 5v5.25l4.5 2.67l-.75 1.23L11 13V7z"),
  };

  function buildProfileRow(template, it) {
    const row = template.cloneNode(true);
    row.removeAttribute("id");
    row.dataset.source = it.source;

    const link = row.querySelector(".record-level");
    if (link) {
      link.href = it.href;
      if (it.external) {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = "Pointercrate";
      } else {
        link.removeAttribute("target");
        link.removeAttribute("rel");
      }
    }

    const place = row.querySelector(".record-level-placement");
    if (place) {
      place.textContent =
        it.placement != null ? `#${it.placement}` : it.pcPosition ? `PC #${it.pcPosition}` : "PC";
    }
    const nameEl = row.querySelector(".record-level-name");
    if (nameEl) nameEl.textContent = it.name;

    const pct = row.querySelector(".record-percent");
    if (pct) pct.textContent = `${it.percent}%`;

    // Статус (галочка / часы). Шаблон — случайная строка профиля, у неё может быть любой статус,
    // поэтому класс и иконку ставим сами для каждой строки, а не оставляем от шаблона.
    const status = row.querySelector(".record-status");
    if (status) {
      const st = String(it.status || "accepted").toLowerCase();
      [...status.classList].filter((c) => c.startsWith("status-")).forEach((c) => status.classList.remove(c));
      status.classList.add("status-" + st);
      if (STATUS_SVG[st]) status.innerHTML = STATUS_SVG[st];
    }

    const video = row.querySelector(".record-video");
    if (video) {
      if (it.video) {
        video.href = it.video;
        video.target = "_blank";
        video.rel = "noopener noreferrer";
        if (it.source === "pointercrate") video.title = "Pointercrate";
      } else {
        video.remove();
      }
    }
    return row;
  }

  /* ---------- плашки сверху профиля (Main / Extended / Advanced / Unbounded / прогресс) ---------- */

  const CAT_ORDER = ["main", "extended", "advanced", "unbounded", "verified", "progress"];
  const CAT_WORD = { main: "Main", extended: "Extended", advanced: "Advanced", unbounded: "Unbounded" };
  const CAT_ICON = {
    main: ['#ef4444', '<path fill="currentColor" d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/>'],
    extended: ['#eab308', '<circle cx="12" cy="12" r="8" fill="currentColor"/>'],
    advanced: ['#22c55e', '<circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" stroke-width="2.4"/>'],
    unbounded: ['#3b82f6', '<path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M12 12c-2-2.5-3.5-4-5.5-4a4 4 0 0 0 0 8c2 0 3.5-1.5 5.5-4zm0 0c2 2.5 3.5 4 5.5 4a4 4 0 0 0 0-8c-2 0-3.5 1.5-5.5 4z"/>'],
    progress: ['#94a3b8', '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2.2" stroke-dasharray="3 3"/>'],
  };

  // Какой блок по заголовку ("Main уровни", "Уровни с прогрессом" и т.д.)
  function catKeyOf(text) {
    const t = String(text || "");
    if (/проверен|verif/i.test(t)) return "verified";
    if (/прогресс|progress/i.test(t)) return "progress";
    if (/main/i.test(t)) return "main";
    if (/extended/i.test(t)) return "extended";
    if (/advanced/i.test(t)) return "advanced";
    if (/unbounded/i.test(t)) return "unbounded";
    return null;
  }

  // Границы блоков по месту на глобале: Main 1-75, Extended 76-150, Advanced 151-300, Unbounded 301+
  const catForPlacement = (p) => (p <= 75 ? "main" : p <= 150 ? "extended" : p <= 300 ? "advanced" : "unbounded");

  const profileCategories = () =>
    [...document.querySelectorAll(".records-category")]
      .map((el) => {
        const header = el.querySelector(".records-header");
        const list = el.querySelector(".records-list");
        const key = header && list ? catKeyOf(header.textContent) : null;
        return key ? { el, header, list, key } : null;
      })
      .filter(Boolean);

  // Текстовые узлы заголовка: название и число
  function headerTextNodes(header) {
    const w = document.createTreeWalker(header, NodeFilter.SHOW_TEXT);
    let title = null, count = null, n;
    while ((n = w.nextNode())) {
      const t = n.nodeValue.trim();
      if (!t) continue;
      if (/^\d+$/.test(t)) count = n;
      else if (!title) title = n;
    }
    return { title, count };
  }
  const readCount = (header) => Number(headerTextNodes(header).count?.nodeValue.trim() || 0);
  function setCount(header, v) {
    const { count } = headerTextNodes(header);
    if (count) count.nodeValue = String(v);
  }

  // Название уровня на плашке (у плашек с прогрессом там ещё процент)
  function chipName(chip) {
    const first = chip.childNodes[0];
    if (first && first.nodeType === 3 && first.nodeValue.trim()) return first.nodeValue.trim();
    const leaf = [...chip.querySelectorAll("*")].find((n) => !n.children.length && n.textContent.trim());
    return (leaf || chip).textContent.trim();
  }

  function setChip(chip, name, pct) {
    const leaves = [...chip.querySelectorAll("*")].filter((n) => !n.children.length);
    if (pct != null && leaves.length >= 2) {
      leaves[0].textContent = name;
      leaves[leaves.length - 1].textContent = pct + "%";
    } else if (pct != null && leaves.length === 1 && chip.firstChild?.nodeType === 3) {
      chip.firstChild.nodeValue = name + " ";
      leaves[0].textContent = pct + "%";
    } else {
      chip.textContent = pct != null ? `${name} ${pct}%` : name;
    }
  }

  function makeChip(tpl, cat, it) {
    let chip;
    if (tpl) {
      chip = tpl.cloneNode(true);
    } else {
      chip = document.createElement("a");
      const s = /svelte-[a-z0-9]+/i.exec(cat.el.className)?.[0] || "";
      chip.className = `record-chip ${s}`.trim();
    }
    chip.removeAttribute("data-pc-chip-hide");
    chip.setAttribute("data-pc-chip", "1");
    setChip(chip, it.name, it.percent >= 100 ? null : it.percent);
    if (it.video) {
      chip.href = it.video;
      chip.target = "_blank";
      chip.rel = "noopener noreferrer";
    } else {
      chip.href = `/classic/${it.placement}`;
      chip.removeAttribute("target");
    }
    chip.title = "Pointercrate";
    return chip;
  }

  // Нет такого блока на странице — делаем его копией другого блока и ставим по порядку
  function createCategory(key, tplCat) {
    const el = tplCat.el.cloneNode(true);
    el.removeAttribute("id");
    delete el.dataset.pcTouched;
    delete el.dataset.pcOrigCount;
    el.setAttribute("data-pc-section", "1");
    const list = el.querySelector(".records-list");
    list.replaceChildren();
    const header = el.querySelector(".records-header");

    const { title } = headerTextNodes(header);
    if (title) {
      const tpl = title.nodeValue.trim();
      const ru = /[а-яё]/i.test(tpl);
      title.nodeValue = CAT_WORD[key] ? tpl.replace(/^\S+/, CAT_WORD[key]) : ru ? "Уровни с прогрессом" : "Levels in progress";
    }
    setCount(header, 0);

    const [color, svgInner] = CAT_ICON[key];
    const oldIcon = header.querySelector("svg");
    if (oldIcon) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", oldIcon.getAttribute("width") || "1em");
      svg.setAttribute("height", oldIcon.getAttribute("height") || "1em");
      if (oldIcon.getAttribute("class")) svg.setAttribute("class", oldIcon.getAttribute("class"));
      svg.style.color = color;
      svg.innerHTML = svgInner;
      oldIcon.replaceWith(svg);
    }

    const idx = CAT_ORDER.indexOf(key);
    const cats = profileCategories();
    const before = cats.find((c) => CAT_ORDER.indexOf(c.key) > idx);
    if (before) before.el.before(el);
    else cats[cats.length - 1].el.after(el);

    el.dataset.pcTouched = "1";
    el.dataset.pcOrigCount = "0";
    return { el, header, list, key };
  }

  // "Сложнейший уровень": с включённым PC Import — самый сложный уровень, пройденный на 100%
  // (по месту на глобале) среди записей Demonlist и Pointercrate. Выключили — возвращаем как было.
  // Сайт разбивает "#61" на несколько текстовых узлов ("#" и "61"), поэтому
  // меняем весь текст элемента целиком, а не один узел.
  function setElText(el, v) {
    if (el.textContent !== v) el.textContent = v;
  }

  function applyHardest() {
    const a = document.querySelector(".hardest-level");
    const pl = a?.querySelector(".hardest-placement");
    const nm = a?.querySelector(".hardest-name");
    if (!a || !pl || !nm) return;
    if (!a.dataset.pcOrig) {
      a.dataset.pcOrig = JSON.stringify({ href: a.getAttribute("href"), p: pl.textContent, n: nm.textContent });
    }
    const o = JSON.parse(a.dataset.pcOrig);

    let best = null;
    if (enabled) {
      for (const it of profileData?.items || []) {
        if (it.percent >= 100 && it.placement != null && (it.status || "accepted") === "accepted") {
          if (!best || it.placement < best.placement) best = it;
        }
      }
    }
    const want = best
      ? { href: `/classic/${best.placement}`, p: `#${best.placement}`, n: best.name }
      : o;
    if (a.getAttribute("href") !== want.href) a.setAttribute("href", want.href);
    setElText(pl, want.p);
    setElText(nm, want.n);
  }

  /* ---------- очки и место в рейтинге ---------- */

  // Очки за уровень по месту h (формулы из правил Demonlist); H — самое низкое место в списке
  function levelPoints(h, H) {
    if (h <= 8) return 1040 - 40 * h;
    if (h <= 10) return 1200 - 60 * h;
    if (h <= 20) return 800 - 20 * h;
    if (h <= 50) return 480 - 4 * h;
    if (h <= 75) return 680 - 8 * h;
    if (h <= 150) return 155 - h;
    if (h <= 250) return 5 - 0.4 * Math.ceil((h - 150) / 10);
    return 1 - 0.01 * Math.ceil((95 * (h - 250)) / (H - 250));
  }

  // Очки за запись: c — процент, tau — минимальный процент уровня. null — если не хватает данных.
  function recordPoints(h, H, c, tau) {
    const P = levelPoints(h, H);
    if (c >= 100) return P;
    if (tau == null || !Number.isFinite(Number(tau)) || Number(tau) >= 99) return null;
    return 0.25 * P * ((c - Number(tau)) / (99 - Number(tau)) + 1);
  }

  // Самое низкое место в списке H. Оно постоянно меняется, поэтому ищем на месте:
  // список уровней листается по offset, находим последнюю непустую страницу.
  let lowestPromise = null;
  function getLowestPlacement() {
    return (lowestPromise ||= (async () => {
      const page = async (offset) => {
        const r = await getJsonRetry(`${DL_LIST_API}?limit=50&offset=${offset}`);
        if (!r.ok) throw new Error("Не удалось загрузить список уровней Demonlist");
        return r.data?.data?.levels || [];
      };
      const nonEmpty = async (o) => (await page(o)).length > 0;
      // ниже 1800 места список никогда не опускается
      let lo = 0, hi;
      if (await nonEmpty(1800)) {
        lo = 1800;
        let step = 256;
        hi = lo + step;
        while (await nonEmpty(hi)) { lo = hi; step *= 2; hi = lo + step; }
      } else {
        hi = 1800;
      }
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (await nonEmpty(mid)) lo = mid; else hi = mid;
      }
      // lo — смещение последнего уровня; берём последнюю страницу и самое большое место на ней
      const last = await page(Math.max(0, lo - 49));
      const H = Math.max(lo + 1, ...last.map((l) => Number(l.placement) || 0));
      return H;
    })().catch((e) => { lowestPromise = null; throw e; }));
  }

  // points.json из репозитория: игроки уже отсортированы по очкам с учётом Pointercrate,
  // при равных очках выше стоит тот, кто был выше на Demonlist. Порядок в массиве = место.
  let pointsTablePromise = null;
  function loadPointsTable() {
    return (pointsTablePromise ||= (async () => {
      const res = await getJsonRetry(POINTS_URL);
      if (!res.ok || !Array.isArray(res.data?.users)) throw new Error("points.json недоступен");
      const map = new Map();
      res.data.users.forEach((u, i) => map.set(String(u[0]), i + 1));
      return map;
    })().catch((e) => { pointsTablePromise = null; throw e; }));
  }
  async function rankFromPointsTable(userId) {
    try {
      const map = await loadPointsTable();
      return map.get(canonDlId(userId)) ?? map.get(String(userId)) ?? null;
    } catch (_) {
      return null;
    }
  }

  // Место в рейтинге при новой сумме очков. Новые очки не меньше старых, поэтому место
  // может только подняться: ищем двоичным поиском среди мест выше текущего.
  async function findRank(points, curRank) {
    const pointsAt = async (i) => {
      const r = await getJsonRetry(`${DL_LB_API}?limit=1&offset=${i}`);
      const u = r.ok ? r.data?.data?.users?.[0] : null;
      if (!u) throw new Error("Не удалось загрузить таблицу игроков Demonlist");
      return Math.round(Number(u.points) * 100) / 100;
    };
    let lo = 0, hi = curRank - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if ((await pointsAt(mid)) <= points) hi = mid; else lo = mid + 1;
    }
    return lo + 1;
  }

  const statEls = () => {
    const score = document.querySelector(".stat-score .stat-value");
    if (!score) return null;
    const rank =
      document.querySelector(".stat-placement .stat-value") ||
      [...document.querySelectorAll(".stat-card .stat-value")].find(
        (v) => v !== score && /^#\s*\d+…?$/.test(v.textContent.trim())
      );
    return { score, rank };
  };

  // Считает новые очки и место один раз для загруженных данных профиля
  function ensureProfileStats() {
    const pd = profileData;
    if (!pd?.items || pd.statsPromise) return;
    pd.statsPromise = (async () => {
      const changed = pd.items.filter((it) => it.source === "pointercrate" && it.placement != null);
      if (!changed.length) { pd.stats = null; return; }

      const els = statEls();
      if (!els) { pd.statsPromise = null; return; } // страница ещё не нарисована — попробуем позже
      const origText = els.score.dataset.pcOrig ?? els.score.textContent;
      const origPoints = parseFloat(String(origText).replace(/\s/g, "").replace(",", "."));
      if (!Number.isFinite(origPoints)) { pd.stats = null; return; }
      const origRank = els.rank ? parseInt(String(els.rank.dataset.pcOrig ?? els.rank.textContent).replace(/\D/g, ""), 10) : NaN;

      const H = await getLowestPlacement();

      // Проверка формулы: очки по записям Demonlist должны совпасть с очками на сайте
      let check = 0, checkOk = true;
      for (const it of pd.items) {
        if (it.placement == null) continue;
        if (it.source === "global" && (it.status || "accepted") === "accepted") {
          const p = recordPoints(it.placement, H, it.percent, it.listPercent);
          if (p == null) checkOk = false; else check += p;
        } else if (it.replaced) {
          const p = recordPoints(it.placement, H, it.oldPercent, it.listPercent);
          if (p == null) checkOk = false; else check += p;
        }
      }
      console.info(
        `[PC Import] проверка формулы очков: H=${H}, ` +
        (checkOk ? `по записям Demonlist ${check.toFixed(2)}, на сайте ${origPoints.toFixed(2)}` : "не хватает list_percent у части записей, проверка невозможна")
      );

      let delta = 0;
      for (const it of changed) {
        const np = recordPoints(it.placement, H, it.percent, it.listPercent);
        const op = it.replaced ? recordPoints(it.placement, H, it.oldPercent, it.listPercent) : 0;
        if (np != null && op != null) delta += np - op;
      }
      const points = Math.round((origPoints + delta) * 100) / 100;
      // Место: сначала из points.json (там у всех игроков перенесены записи Pointercrate),
      // если файла или игрока в нём нет — запасной вариант, сравнение только с текущей таблицей сайта.
      let rank = await rankFromPointsTable(pd.userId);
      if (rank == null && Number.isFinite(origRank) && origRank > 1) {
        try { rank = await findRank(points, origRank); }
        catch (e) { console.error("[PC Import] Место в рейтинге:", e); }
      }
      pd.stats = { points, rank };
      applyStats();
    })().catch((e) => {
      console.error("[PC Import] Очки:", e);
      pd.stats = null; // не вышло — оставляем очки сайта и убираем индикатор "…"
      applyStats();
    });
  }

  // Подставляет очки и место на страницу (только при включённом PC Import).
  // Пока новые числа ещё считаются, после старых показывается "…".
  function applyStats() {
    const els = statEls();
    if (!els) return;
    const pd = profileData && String(profileData.userId) === (PROFILE_RE.exec(location.pathname)?.[1] ?? "") ? profileData : null;
    // данные ещё грузятся или считаются (если загрузка профиля упала — индикатор не нужен)
    const pending = enabled && failedKey !== location.pathname && !(pd && pd.stats !== undefined);
    const st = enabled && pd ? pd.stats : null;
    for (const [el, value] of [[els.score, st ? st.points.toFixed(2) : null], [els.rank, st && st.rank != null ? "#" + st.rank : null]]) {
      if (!el) continue;
      if (value == null && !pending) {
        if (el.dataset.pcOrig != null) setElText(el, el.dataset.pcOrig); // показываем числа сайта
        continue;
      }
      if (el.dataset.pcOrig == null) el.dataset.pcOrig = el.textContent;
      setElText(el, pending ? el.dataset.pcOrig + "…" : value);
    }
  }

  function restoreStats() {
    document.querySelectorAll(".stat-value[data-pc-orig]").forEach((el) => {
      setElText(el, el.dataset.pcOrig);
      delete el.dataset.pcOrig;
    });
  }

  // Убирает всё, что мы добавили сверху профиля, и возвращает исходные числа
  function clearProfileChips() {
    restoreStats();
    const hard = document.querySelector(".hardest-level[data-pc-orig]");
    if (hard) {
      const o = JSON.parse(hard.dataset.pcOrig);
      hard.setAttribute("href", o.href);
      const pl = hard.querySelector(".hardest-placement");
      const nm = hard.querySelector(".hardest-name");
      if (pl) setElText(pl, o.p);
      if (nm) setElText(nm, o.n);
      delete hard.dataset.pcOrig;
    }
    document.querySelectorAll("[data-pc-section]").forEach((e) => e.remove());
    document.querySelectorAll("[data-pc-chip]").forEach((e) => e.remove());
    document.querySelectorAll("[data-pc-chip-hide]").forEach((e) => e.removeAttribute("data-pc-chip-hide"));
    document.querySelectorAll("[data-pc-touched]").forEach((el) => {
      const h = el.querySelector(".records-header");
      if (h && el.dataset.pcOrigCount != null) setCount(h, el.dataset.pcOrigCount);
      delete el.dataset.pcTouched;
      delete el.dataset.pcOrigCount;
    });
  }

  // Числа в заголовках блоков: с включённым PC Import — с учётом наших плашек
  function applyChipCounts() {
    for (const c of profileCategories()) {
      if (!c.el.dataset.pcTouched) continue;
      const orig = Number(c.el.dataset.pcOrigCount) || 0;
      const added = c.list.querySelectorAll("[data-pc-chip]").length;
      const hidden = c.list.querySelectorAll("[data-pc-chip-hide]").length;
      setCount(c.header, enabled ? orig + added - hidden : orig);
    }
  }

  const profileExtras = () =>
    (profileData?.items || []).filter(
      (it) => it.source === "pointercrate" && it.placement != null && Number.isFinite(it.percent)
    );

  // Добавляет записи Pointercrate плашками в блоки сверху профиля.
  // Видны они только при включённом PC Import (см. styles.css).
  function renderProfileChips() {
    clearProfileChips();
    const extras = profileExtras().sort((a, b) => a.placement - b.placement);
    if (!extras.length) return;
    if (!profileCategories().length) return;

    const placeByName = new Map();
    for (const it of profileData.items) if (it.placement != null) placeByName.set(normalize(it.name), it.placement);

    for (const it of extras) {
      const key = it.percent >= 100 ? catForPlacement(it.placement) : "progress";
      let cats = profileCategories();
      let cat = cats.find((c) => c.key === key);
      if (!cat) {
        const tplCat = cats.find((c) => ["main", "extended", "advanced", "unbounded"].includes(c.key)) || cats[0];
        cat = createCategory(key, tplCat);
        cats = profileCategories();
      }
      if (!cat.el.dataset.pcTouched) {
        cat.el.dataset.pcTouched = "1";
        cat.el.dataset.pcOrigCount = String(readCount(cat.header));
      }

      // запись Pointercrate лучше записи Demonlist — старую плашку этого уровня прячем
      if (it.replaced) {
        for (const c of cats) {
          for (const ch of c.list.querySelectorAll(".record-chip:not([data-pc-chip])")) {
            if (normalize(chipName(ch)) === normalize(it.name)) {
              ch.setAttribute("data-pc-chip-hide", "1");
              c.el.dataset.pcTouched ||= "1";
              c.el.dataset.pcOrigCount ??= String(readCount(c.header));
            }
          }
        }
      }

      const tplCat = key === "progress" ? cat : cats.find((c) => c.key !== "progress" && c.key !== "verified") || cat;
      const tpl =
        tplCat.list.querySelector(".record-chip:not([data-pc-chip])") ||
        document.querySelector(".records-category .record-chip:not([data-pc-chip])");
      const chip = makeChip(tpl, cat, it);

      // ставим по месту в списке: плашки идут от сложных уровней к лёгким
      const next = [...cat.list.querySelectorAll(".record-chip")]
        .filter((ch) => !ch.hasAttribute("data-pc-chip-hide"))
        .find((ch) => {
          const p = placeByName.get(normalize(chipName(ch)));
          return p != null && p > it.placement;
        });
      if (next) next.before(chip);
      else cat.list.append(chip);
    }
    applyChipCounts();
  }

  // Строит наш список и кладёт его рядом с оригинальным. Оригинал и пагинацию
  // прячет CSS, но только пока переключатель включён (см. styles.css).
  function renderProfile() {
    const rows = findProfileRows();
    if (!rows.length || !profileData?.items) return false;
    const { container, templateEl } = profileAnchor(rows);
    if (!container) return false;

    const template = templateEl.cloneNode(true);
    const list = container.cloneNode(false);
    list.id = PROFILE_ID;
    list.className = container.className.replace(/\bpc-hidden-original\b/g, "").trim();
    list.replaceChildren(...profileData.items.map((it) => buildProfileRow(template, it)));

    const old = document.getElementById(PROFILE_ID);
    if (old) old.replaceWith(list); else container.after(list);

    container.classList.add("pc-hidden-original");
    document.documentElement.classList.add("pc-profile");
    markPagination(container);
    renderProfileChips();
    applyHardest();
    ensureProfileStats();
    applyStats();
    return true;
  }

  function applyProfileMode() {
    document.documentElement.classList.toggle("pc-on", enabled);
    applyChipCounts();
    applyHardest();
    applyStats();
  }

  function setProfileLoading(on) {
    const t = document.querySelector("#" + TOGGLE_ID + " .pc-switch-title");
    if (t) t.textContent = on ? "PC Import…" : "PC Import";
  }

  // Переключатель в заголовке карточки "Рекорды" (ищем заголовок по тексту)
  function ensureProfileSwitch(container) {
    if (document.getElementById(TOGGLE_ID)) return;

    const isLabel = (el) => /^(рекорды|records)$/i.test(el.textContent.trim());
    let card = container.parentElement;
    let head = null;
    while (card && card !== document.body && card !== document.documentElement) {
      const found = [...card.querySelectorAll("*")].filter(
        (el) => !container.contains(el) && !isMine(el) && isLabel(el)
      );
      if (found.length) { head = found[found.length - 1]; break; } // самый вложенный элемент
      card = card.parentElement;
    }
    if (!head) card = container.parentElement; // запасной вариант: над списком
    if (!card) return;

    if (getComputedStyle(card).position === "static") card.style.position = "relative";
    const cardRect = card.getBoundingClientRect();
    let top;
    if (head) {
      const r = head.getBoundingClientRect();
      top = r.top - cardRect.top + r.height / 2;
    } else {
      top = Math.max(16, container.getBoundingClientRect().top - cardRect.top - 20);
    }

    const wrap = document.createElement("div");
    wrap.id = TOGGLE_ID;
    wrap.className = "pc-import-switch-container";
    wrap.style.top = `${top}px`;
    wrap.innerHTML = `
      <span class="pc-switch-title">PC Import</span>
      <button type="button" class="pc-native-toggle" role="switch">
        <div class="pc-switch"><div class="pc-switch-thumb"></div></div>
      </button>`;
    const btn = wrap.querySelector("button");
    const sw = wrap.querySelector(".pc-switch");
    const sync = () => {
      sw.classList.toggle("active", enabled);
      btn.setAttribute("aria-checked", String(enabled));
    };
    sync();
    btn.addEventListener("click", () => {
      enabled = !enabled;
      sync();
      storageSet(enabled);
      applyProfileMode();
      if (enabled && !profileData?.items) doneKey = null; // данных ещё нет — пусть цикл их загрузит
    });
    card.appendChild(wrap);
  }

  async function runProfile(userId) {
    if (running) return;
    const key = location.pathname;
    if (failedKey === key || profileSkipKey === key) return;

    const rows = findProfileRows();
    if (!rows.length) return; // сайт ещё не нарисовал записи

    running = true;
    try {
      enabled = await storageGet();
      await ensurePlayerLinks();

      const pcIds = pcIdsForUser(userId);
      if (!pcIds.length) {
        profileSkipKey = key; // у игрока нет связанных аккаунтов — ничего не трогаем
        return;
      }

      const { container } = profileAnchor(rows);
      ensureProfileSwitch(container); // переключатель появляется сразу
      applyProfileMode();

      if (!profileData || profileData.userId !== userId) {
        if (!enabled) { doneKey = key; return; } // пока выключено — ничего не качаем
        setProfileLoading(true);
        const items = await loadProfileItems(userId, pcIds);
        if (location.pathname !== key) return; // пользователь ушёл, пока грузили
        profileData = { userId, items };
      }

      if (renderProfile()) doneKey = key;
    } catch (error) {
      failedKey = key;
      console.error("[PC Import] Ошибка загрузки профиля:", error);
    } finally {
      setProfileLoading(false);
      running = false;
    }
  }

  /* ---------- страница рейтинга игроков (/leaderboard/players) ---------- */

  // Рейтинг с учётом Pointercrate: players.json лежит в порядке итогового места,
  // игроки в нём — [id, ник, очки]. Сопоставляем с карточками по нику (только если ник уникален).
  let lbPromise = null;
  let lbMap = null; // нижний регистр ника -> { rank, points }
  function loadLeaderboardData() {
    return (lbPromise ||= (async () => {
      const res = await getJsonRetry(PLAYERS_URL);
      if (!res.ok || !Array.isArray(res.data?.users)) throw new Error("players.json недоступен");
      const map = new Map();
      const dup = new Set();
      res.data.users.forEach((u, i) => {
        const name = u[1] != null ? String(u[1]).trim().toLowerCase() : "";
        if (!name) return;
        if (map.has(name)) { dup.add(name); return; }
        map.set(name, { rank: i + 1, points: Number(u[2]) });
      });
      for (const n of dup) map.delete(n); // одинаковые ники не трогаем: не понять, кто есть кто
      lbMap = map;
    })().catch((e) => { lbPromise = null; console.error("[PC Import] Рейтинг:", e); }));
  }

  // Подставляем значение в элемент и помним исходное. Если Svelte сам перерисовал текст — берём новый исходный.
  function lbSet(el, want) {
    if (!el) return;
    const cur = el.textContent;
    const hasShown = el.dataset.pcShown !== undefined;
    const orig = hasShown && cur === el.dataset.pcShown ? el.dataset.pcOrig : cur;
    el.dataset.pcOrig = orig;
    const target = want == null ? orig : want;
    if (cur !== target) el.textContent = target;
    if (want == null) delete el.dataset.pcShown; else el.dataset.pcShown = target;
  }

  function applyLeaderboard() {
    const cards = document.querySelectorAll(".players-list .player-card");
    for (const card of cards) {
      const name = card.querySelector(".player-name")?.textContent.trim().toLowerCase();
      const e = enabled && lbMap && name ? lbMap.get(name) : null;
      const placeEl = card.querySelector(".player-placement");
      lbSet(placeEl, e ? "#" + e.rank : null);
      lbSet(card.querySelector(".player-points"), e ? e.points.toFixed(2) : null);

      // Порядок карточек меняем через CSS order (список — flex), сами узлы не трогаем: ими управляет сайт.
      // Игрок без пересчёта (одинаковый ник и т.п.) остаётся на своём прежнем месте.
      let order = "";
      if (enabled && lbMap) {
        const o = e ? e.rank : parseInt(String(placeEl?.dataset.pcOrig ?? "").replace(/\D/g, ""), 10);
        if (Number.isFinite(o)) order = String(o);
      }
      if (card.style.order !== order) card.style.order = order;
    }
    const t = document.querySelector("#" + LB_TOGGLE_ID + " .pc-switch-title");
    if (t) t.textContent = enabled && !lbMap ? "PC Import: рейтинг…" : "PC Import: рейтинг";
  }

  function restoreLeaderboard() {
    document.querySelectorAll(".players-list .player-card").forEach((c) => { if (c.style.order) c.style.order = ""; });
    document.querySelectorAll("[data-pc-shown]").forEach((el) => {
      el.textContent = el.dataset.pcOrig;
      delete el.dataset.pcShown;
      delete el.dataset.pcOrig;
    });
  }

  // Переключатель — в самом верху панели со списком игроков (над поиском): так видно, что он
  // относится ко всему рейтингу, а не к игроку справа.
  function ensureLeaderboardSwitch() {
    if (document.getElementById(LB_TOGGLE_ID)) return true;
    const panel = document.querySelector(".players-list-panel");
    if (!panel) return false;
    const wrap = document.createElement("div");
    wrap.id = LB_TOGGLE_ID;
    wrap.className = "pc-lb-switch";
    wrap.title = "Пересчитать весь рейтинг, как будто у всех игроков перенесены записи с Pointercrate";
    wrap.innerHTML = `
      <span class="pc-switch-title">PC Import: рейтинг</span>
      <button type="button" class="pc-native-toggle" role="switch">
        <div class="pc-switch"><div class="pc-switch-thumb"></div></div>
      </button>`;
    const btn = wrap.querySelector("button");
    const sw = wrap.querySelector(".pc-switch");
    const sync = () => {
      sw.classList.toggle("active", enabled);
      btn.setAttribute("aria-checked", String(enabled));
    };
    sync();
    btn.addEventListener("click", () => {
      enabled = !enabled;
      sync();
      storageSet(enabled);
      if (enabled) loadLeaderboardData().then(applyLeaderboard);
      applyLeaderboard();
    });
    panel.prepend(wrap);
    return true;
  }

  let lbInit = null;
  function leaderboardTick() {
    // настройку (включён ли PC Import) читаем один раз при заходе на страницу
    if (!lbInit) {
      lbInit = storageGet().then((v) => { enabled = v; });
      return;
    }
    if (!ensureLeaderboardSwitch()) return;
    document.querySelector("#" + LB_TOGGLE_ID + " .pc-switch")?.classList.toggle("active", enabled);
    if (enabled && !lbMap) loadLeaderboardData();
    applyLeaderboard();
  }

  // Вызывается раз в 800 мс, пока открыт профиль
  function profileTick(userId) {
    const key = location.pathname;
    if (doneKey === key) {
      const rows = findProfileRows();
      if (rows.length) {
        if (!document.getElementById(TOGGLE_ID)) {
          doneKey = null; // сайт пересоздал страницу и снёс переключатель
        } else if (profileData?.items) {
          if (!document.getElementById(PROFILE_ID)) {
            doneKey = null; // снесло наш список
          } else {
            // сайт мог пересоздать плашки сверху — добавляем наши снова
            if (!document.querySelector("[data-pc-chip], [data-pc-section]") && profileExtras().length) {
              renderProfileChips();
            }
            applyHardest();
            ensureProfileStats();
            applyStats();
            const { container } = profileAnchor(rows);
            if (container && !container.classList.contains("pc-hidden-original")) {
              container.classList.add("pc-hidden-original"); // сайт пересоздал оригинал — прячем снова
              markPagination(container);
            }
          }
        }
      }
    }
    if (doneKey !== key) runProfile(userId);
  }

  setInterval(() => {
    if (location.pathname !== lastPath) {
      lastPath = location.pathname;
      lbInit = null;
      reset();
      return;
    }
    if (LB_RE.test(location.pathname)) {
      leaderboardTick(); // рейтинг игроков — отдельная страница
      return;
    }
    const profile = PROFILE_RE.exec(location.pathname);
    if (profile) {
      profileTick(profile[1]); // страница профиля обрабатывается отдельно от страниц уровней
      return;
    }
    if (doneKey === location.pathname) {
      if (!document.getElementById(MERGED_ID)) {
        // Svelte мог пересоздать страницу и снести наши списки
        const hasPage = findRecordCards().length || document.querySelector(".empty-records");
        if (hasPage && failedKey !== location.pathname) doneKey = null;
      } else {
        updateCount(); // сайт мог перерисовать счётчик (например, при смене языка)
        // Если сайт пересоздал оригинальный список — снова прячем его и пагинацию
        const cards = findRecordCards();
        const c = cards.length
          ? findCardsContainer(cards)
          : document.getElementById(EMPTY_ANCHOR_ID);
        if (c && !c.classList.contains("pc-hidden-original")) {
          c.classList.add("pc-hidden-original");
          markPagination(c);
        }
      }
    }
    if (doneKey !== location.pathname) run();
  }, 800);
})();
