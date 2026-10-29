(() => {
  "use strict";

  const DL_API = "https://api.demonlist.org/level/classic/record/list";
  const DL_LEVEL_API = "https://api.demonlist.org/level/classic/get";
  const PC_API = "https://pointercrate.com/api/";
  const LINKS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/links.json";
  const MERGED_ID = "pc-merged-list"; // список с рекордами Pointercrate
  const DL_ID = "pc-dl-list"; // список только с рекордами demonlist (когда экспорт выключен)
  const TOGGLE_ID = "pc-switch-toggle";
  const EMPTY_ANCHOR_ID = "pc-empty-anchor"; // наш контейнер на уровнях без рекордов
  const CARD_SEL = ".record-card, [class*='Record_card'], [class*='record_card']";

  let running = false;
  let doneKey = null;
  let failedKey = null; // путь, на котором была ошибка (чтобы не повторять бесконечно)
  let lastPath = location.pathname;
  let retries = 0;
  let enabled = false;
  let stats = { total: 0, hundred: 0 };
  let dataCache = null; // { key, dl, pc } — чтобы не качать всё заново при перерисовке
  let pcDemonsPromise = null; // полный список демонов Pointercrate (кеш на сессию страницы)
  let savedTemplate = null; // шаблон карточки, запомненный на уровне с рекордами
  let savedContainerClass = ""; // класс списка карточек, запомненный на уровне с рекордами
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

  const isMine = (el) => !!el.closest("#" + MERGED_ID + ", #" + DL_ID);

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

  async function fetchAllDemonlist(levelId) {
    const out = [];
    const limit = 50;
    let offset = 0;
    let total = Infinity;
    while (offset < total && offset < 20000) {
      const res = await getJson(
        `${DL_API}?level_id=${encodeURIComponent(levelId)}&limit=${limit}&offset=${offset}`
      );
      if (!res.ok) break;
      const recs = res.data?.data?.records || res.data?.records || [];
      total = res.data?.data?.total_count ?? res.data?.total_count ?? recs.length;
      if (!recs.length) break;
      out.push(...recs);
      offset += recs.length;
      if (recs.length < limit) break;
    }
    return out;
  }

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
        const last = Math.max(...r.data.map((d) => Number(d.position) || 0));
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
    const sameLevel = (d) => String(d.level_id ?? "") === ingameId;

    if (ingameId) {
      const r = await getJsonRetry(`${PC_API}v2/demons/?level_id=${encodeURIComponent(ingameId)}&limit=10`);
      if (r.ok && Array.isArray(r.data)) {
        const d = r.data.find(sameLevel);
        if (d) return { demon: d, via: "level_id (фильтр)" };
      }
      const all = await loadAllPcDemons();
      const d = all.find(sameLevel);
      if (d) return { demon: d, via: "level_id (полный список)" };
    }

    if (levelName) {
      const r = await getJsonRetry(
        `${PC_API}v2/demons/?name_contains=${encodeURIComponent(levelName)}&limit=100`
      );
      if (r.ok && Array.isArray(r.data)) {
        const wanted = normalize(levelName);
        const cands = r.data.filter(
          (d) =>
            normalize(d.name) === wanted &&
            // если у демона известен level_id и он другой — это другой уровень
            !(ingameId && d.level_id != null && String(d.level_id) !== ingameId)
        );
        if (cands.length === 1) return { demon: cands[0], via: "название (однозначно)" };
      }
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

  function setLinks(data) {
    LINKS = {};
    for (const [mainId, sites] of Object.entries(data || {})) {
      // старый формат ("14476": 40436 или [40436]) считаем данными Pointercrate
      const entry =
        sites && typeof sites === "object" && !Array.isArray(sites)
          ? sites
          : { pointercrate: sites };
      for (const [site, ids] of Object.entries(entry)) {
        if (site.startsWith("_")) continue; // "_name" и т.п. — справочная информация для человека
        LINKS[site] ||= {};
        for (const id of [].concat(ids)) LINKS[site][String(id)] = String(mainId);
      }
    }
  }

  // Скачиваем links.json с GitHub; если не вышло — берём последнюю сохранённую копию
  async function loadPlayerLinks() {
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

  // ID на demonlist для аккаунта с данного сайта (или undefined)
  const dlIdFor = (site, id) => LINKS[site]?.[String(id)];

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
      const name = r.user?.username || "";
      const norm = normalize(name);
      const identityKey = getDemonlistPlayerKey(
        r.user?.id,
        name,
        "record:" + order
      );
      const prev = findPrev(identityKey, norm);
      const percent = Number(r.percent ?? 100);
      if (prev && prev.percent >= percent) continue;
      if (prev) removeItem(prev);
      const item = {
        source: "global",
        name,
        norm,
        identityKey,
        percent,
        video: r.video_url || "",
        vid: extractVideoId(r.video_url),
        dlId: r.user?.id,
        country: r.user?.country ?? r.user?.country_code ?? r.country ?? null,
        href: r.user?.id ? `/profile/${r.user.id}` : "#",
      };
      add(item);
      if (item.vid) dlByVid.set(item.vid, item);
    }

    for (const r of pcRecords) {
      const name = r.player?.name || r.player_name || "Unknown";
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
        href: pid
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

  const flagFor = (it) => flagCtx.byName.get(it.norm) || resolveFlag(it.country);

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

  function updateCount() {
    const el = document.querySelector(".records-count");
    if (!el) return;
    if (!el.dataset.originalHtml) el.dataset.originalHtml = el.innerHTML;
    if (!enabled) {
      el.innerHTML = el.dataset.originalHtml;
      return;
    }
    el.innerHTML = `
      <span class="text-blue-500 dark:text-blue-400 font-semibold">${stats.total}</span> records in total,
      <span class="text-green-500 dark:text-green-400 font-semibold">${stats.hundred}</span> of which are 100%`;
  }

  // Надпись "No records yet" показываем, только если нам самим нечего показать
  function updateEmptyNotice() {
    const e = document.querySelector(".empty-records");
    if (!e) return;
    const show = !enabled || stats.total === 0;
    e.style.display = show ? "" : "none";
  }

  function applyMode() {
    document.documentElement.classList.toggle("pc-on", enabled);
    updateCount();
    updateEmptyNotice();
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

  // Прячем пагинацию оригинального списка (в обоих режимах)
  function markPagination(container) {
    const parent = container.parentElement;
    if (!parent) return;
    parent.querySelectorAll("button, a").forEach((b) => {
      if (isMine(b) || container.contains(b)) return;
      const t = b.textContent.trim().toLowerCase();
      if (t.includes("previous") || t.includes("next")) {
        const row = b.closest("div");
        (row && row !== container && row !== parent ? row : b).setAttribute("data-pc-pag", "1");
      }
    });
  }

  /* ---------- основной цикл ---------- */

  function reset() {
    document.getElementById(MERGED_ID)?.remove();
    document.getElementById(DL_ID)?.remove();
    document.getElementById(EMPTY_ANCHOR_ID)?.remove();
    document.getElementById(TOGGLE_ID)?.remove();
    document.querySelectorAll(".pc-hidden-original").forEach((e) => e.classList.remove("pc-hidden-original"));
    document.querySelectorAll("[data-pc-pag]").forEach((e) => e.removeAttribute("data-pc-pag"));
    document.querySelector(".empty-records")?.style.removeProperty("display");
    const cnt = document.querySelector(".records-count");
    if (cnt) { cnt.innerHTML = cnt.dataset.originalHtml || cnt.innerHTML; delete cnt.dataset.originalHtml; }
    document.documentElement.classList.remove("pc-on", "pc-active");
    doneKey = null;
    failedKey = null;
    retries = 0;
  }

  async function run() {
    if (running || !/demonlist\.org$/.test(location.hostname)) return;

    const cards = findRecordCards();
    const levelId = getLevelId();
    if (!levelId) return;

    // Контейнер: либо настоящий список карточек, либо наша заглушка на пустом уровне
    let container = cards.length ? findCardsContainer(cards) : null;
    if (container && cards.length) {
      savedContainerClass = container.className.replace("pc-hidden-original", "").trim();
    }
    if (!container) container = makeEmptyContainer();
    if (!container) return; // страница ещё не отрисована

    running = true;
    const key = location.pathname;
    try {
      if (cards.length) savedTemplate = cards[0].cloneNode(true);
      const template = savedTemplate || makeFallbackTemplate();
      collectFlags(cards);
      enabled = await storageGet();
      await (linksPromise ||= loadPlayerLinks()); // связи аккаунтов грузим один раз

      let data = dataCache && dataCache.key === key ? dataCache : null;
      if (!data) {
        const dl = await fetchAllDemonlist(levelId);

        // Проверка: данные API должны соответствовать тому, что сейчас на странице.
        // На пустом уровне карточек нет, поэтому проверять нечего.
        const firstOnPage = cards.length ? normalize(getNameFromCard(cards[0])) : "";
        const inApi = dl.slice(0, 50).some((r) => normalize(r.user?.username) === firstOnPage);
        if (firstOnPage && !inApi) {
          if (++retries > 6) throw new Error("level_id не совпадает со страницей");
          return; // подождём следующий тик
        }
        if (location.pathname !== key) return;

        const ingameId = await getIngameId(levelId);
        const found = await findPointercrateDemon(ingameId, findLevelName());
        const pc = found?.demon?.id ? await fetchAllPointercrate(found.demon.id) : [];
        console.info(
          "[Demonlist + Pointercrate] ingame_id:", ingameId,
          "| демон Pointercrate:", found ? `${found.demon.name} (id ${found.demon.id}), найден через ${found.via}` : "не найден",
          "| рекордов DL:", dl.length,
          "| рекордов PC:", pc.length
        );
        if (location.pathname !== key) return;
        data = dataCache = { key, dl, pc };
      }

      const mergedItems = mergeRecords(data.dl, data.pc);
      const dlItems = mergeRecords(data.dl, []);

      // Подсказки: возможные пары аккаунтов, которых ещё нет в links.json
      if (suggestions.size) {
        console.info(
          "[Demonlist + Pointercrate] возможные пары, проверь вручную:\n" +
          [...suggestions.values()]
            .map((s) => `"${s.dl}": { "pointercrate": [${s.pc}] }, // ${s.dlName} = ${s.pcName}`)
            .join("\n")
        );
      }

      document.getElementById(MERGED_ID)?.remove();
      document.getElementById(DL_ID)?.remove();
      const mergedList = renderList(MERGED_ID, container, mergedItems, template);
      const dlList = renderList(DL_ID, container, dlItems, template);
      container.after(mergedList, dlList);
      container.classList.add("pc-hidden-original");
      markPagination(container);
      document.documentElement.classList.add("pc-active");

      if (unresolvedCountries.size) {
        console.warn("[Demonlist + Pointercrate] нераспознанные страны:", [...unresolvedCountries]);
      }
      stats = { total: mergedItems.length, hundred: mergedItems.filter((i) => i.percent === 100).length };
      ensureSwitch(container);
      applyMode();
      doneKey = key;

      // Флаги игроков Pointercrate подгружаем лениво, 3 запроса параллельно
      const queue = mergedItems.filter((i) => i.source === "pointercrate" && i.playerId && !i.country);
      const worker = async () => {
        while (queue.length) {
          const it = queue.shift();
          const code = await fetchCountry(it.playerId);
          if (code && it.card?.isConnected) applyFlag(it.card, resolveFlag(code));
        }
      };
      await Promise.all(Array.from({ length: 3 }, worker));
    } catch (e) {
      console.warn("[Demonlist + Pointercrate]", e);
      failedKey = key;
      doneKey = key; // не зацикливаемся на ошибке
    } finally {
      running = false;
    }
  }

  setInterval(() => {
    if (location.pathname !== lastPath) {
      lastPath = location.pathname;
      reset();
      return;
    }
    if (doneKey === location.pathname) {
      if (!document.getElementById(MERGED_ID)) {
        // Svelte мог пересоздать страницу и снести наши списки
        const hasPage = findRecordCards().length || document.querySelector(".empty-records");
        if (hasPage && failedKey !== location.pathname) doneKey = null;
      } else {
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
