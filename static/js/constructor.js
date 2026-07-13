// Конструктор блоков: интерактивность, живой JSON-предпросмотр, запуск
// симуляции и отрисовка результатов графиками Chart.js.
(function () {
  "use strict";

  const root = document.querySelector(".constructor");
  if (!root) return;
  const saveUrl = root.dataset.saveUrl;
  const simUrl = root.dataset.simUrl;
  const num = (v) => { const n = parseFloat(v); return isNaN(n) ? 0 : n; };

  // ---------- интерактивность блоков ----------
  function syncBlock(fs) {
    const toggle = fs.querySelector(".block-toggle");
    fs.querySelector(".block-body").hidden = !toggle.checked;
    fs.classList.toggle("block-on", toggle.checked);
  }
  root.querySelectorAll("fieldset[data-block]").forEach((fs) => {
    fs.querySelector(".block-toggle").addEventListener("change", () => { syncBlock(fs); refreshPreview(); });
    syncBlock(fs);
  });

  function applyDepends(scope) {
    scope.querySelectorAll("[data-depends-field]").forEach((el) => {
      const ctrl = scope.querySelector('[data-field="' + el.dataset.dependsField + '"] .js-val');
      if (!ctrl) return;
      const update = () => { el.hidden = String(ctrl.value) !== String(el.dataset.dependsValue); };
      ctrl.addEventListener("change", update);
      update();
    });
  }
  root.querySelectorAll("fieldset[data-block]").forEach(applyDepends);
  applyDepends(document.getElementById("targets-grid") || root);

  root.querySelectorAll('[data-type="grid"]').forEach((el) => {
    const known = el.querySelector(".js-grid-known");
    const sync = () => {
      el.querySelector(".js-grid-value").hidden = !known.checked;
      el.querySelector(".js-grid-scan").hidden = known.checked;
    };
    known.addEventListener("change", () => { sync(); refreshPreview(); });
    sync();
  });

  function wireRows(el) {
    const body = el.querySelector(".rows-body");
    const tpl = el.querySelector(".row-tpl");
    const wire = (row) => {
      const del = row.querySelector(".row-del");
      if (del) del.addEventListener("click", () => { row.remove(); refreshPreview(); });
    };
    body.querySelectorAll(".row").forEach(wire);
    el.querySelector(".row-add").addEventListener("click", () => {
      const frag = tpl.content.cloneNode(true);
      const row = frag.querySelector(".row");
      wire(row);
      body.appendChild(frag);
      refreshPreview();
    });
  }
  root.querySelectorAll('[data-type="rows"]').forEach(wireRows);

  // ---------- сериализация ----------
  function readField(el, target) {
    const key = el.dataset.field, type = el.dataset.type;
    if (type === "range") target[key] = [num(el.querySelector(".js-min").value), num(el.querySelector(".js-max").value)];
    else if (type === "bool") target[key] = el.querySelector(".js-val").checked;
    else if (type === "int" || type === "float") target[key] = num(el.querySelector(".js-val").value);
    else if (type === "select" || type === "text") target[key] = el.querySelector(".js-val").value;
    else if (type === "grid") {
      if (el.querySelector(".js-grid-known").checked) target[key] = { mode: "known", value: num(el.querySelector(".js-grid-value").value) };
      else target[key] = { mode: "scan", values: el.querySelector(".js-grid-scan").value.split(",").map((s) => parseFloat(s.trim())).filter((x) => !isNaN(x)) };
    } else if (type === "rows") {
      const rows = [];
      el.querySelectorAll(".rows-body .row").forEach((r) => {
        const obj = {};
        r.querySelectorAll("[data-col]").forEach((c) => {
          const ct = c.dataset.coltype;
          if (ct === "bool") obj[c.dataset.col] = c.checked;
          else if (ct === "int" || ct === "float") obj[c.dataset.col] = num(c.value);
          else obj[c.dataset.col] = c.value;
        });
        rows.push(obj);
      });
      target[key] = rows;
    }
  }

  function serialize() {
    const cfg = { players: {}, win: {}, targets: {}, blocks: {} };
    cfg.players = { min: num(document.getElementById("players-min").value), max: num(document.getElementById("players-max").value) };
    // условие победы
    const winPanel = document.getElementById("win-panel");
    if (winPanel) {
      const flat = {};
      winPanel.querySelectorAll("[data-field]").forEach((el) => readField(el, flat));
      cfg.win = { type: flat.win_type, threshold: flat.win_threshold };
    }
    const targets = document.getElementById("targets-grid");
    if (targets) targets.querySelectorAll(":scope > [data-field]").forEach((el) => readField(el, cfg.targets));
    root.querySelectorAll("fieldset[data-block]").forEach((fs) => {
      if (!fs.querySelector(".block-toggle").checked) return;
      const obj = {};
      fs.querySelectorAll(".block-body [data-field]").forEach((el) => readField(el, obj));
      cfg.blocks[fs.dataset.block] = obj;
    });
    return cfg;
  }

  // ---------- живой предпросмотр ----------
  const BLOCK_LABELS = { track: "🏁 Трек", resources: "💰 Ресурсы", decks: "🃏 Колоды",
    rounds: "🔄 Раунды", judge_vote: "⚖️ Судья", timer: "⏳ Таймер", characters: "🎭 Персонажи" };
  const jsonEl = document.getElementById("json-preview");
  const chipsEl = document.getElementById("summary-chips");

  function refreshPreview() {
    const cfg = serialize();
    jsonEl.textContent = JSON.stringify(cfg, null, 2);
    const chips = [];
    chips.push(`<span class="chip chip-key">игроки ${cfg.players.min}–${cfg.players.max}</span>`);
    const blocks = Object.keys(cfg.blocks);
    if (blocks.length === 0) chips.push('<span class="chip chip-empty">блоки не выбраны</span>');
    blocks.forEach((b) => chips.push(`<span class="chip">${BLOCK_LABELS[b] || b}</span>`));
    chipsEl.innerHTML = chips.join("");
  }

  root.addEventListener("input", refreshPreview);
  root.addEventListener("change", refreshPreview);
  refreshPreview();

  document.getElementById("json-toggle").addEventListener("click", (e) => {
    const collapsed = jsonEl.classList.toggle("collapsed");
    e.target.textContent = collapsed ? "развернуть" : "свернуть";
  });

  // ---------- сохранение ----------
  const status = document.getElementById("save-status");
  document.getElementById("save-btn").addEventListener("click", async () => {
    status.textContent = "Сохранение…"; status.className = "save-status";
    try {
      const resp = await fetch(saveUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(serialize()) });
      const data = await resp.json();
      status.textContent = resp.ok && data.ok ? "✓ Сохранено" : "Ошибка";
      status.className = "save-status " + (resp.ok && data.ok ? "save-ok" : "save-err");
    } catch (e) { status.textContent = "Сеть недоступна"; status.className = "save-status save-err"; }
  });

  // ---------- симуляция ----------
  let lastResult = null;
  const charts = {};
  const COLORS = { accent: "#46c2ff", blue: "#4d86e6", warn: "#ffc450", bad: "#ff6f6f", good: "#5adc96", dim: "#aac1e6" };

  const simBtn = document.getElementById("sim-btn");
  simBtn.addEventListener("click", async () => {
    const cfg = serialize();
    if (Object.keys(cfg.blocks).length === 0) {
      alert("Отметьте хотя бы один блок — иначе симулировать нечего.");
      return;
    }
    simBtn.disabled = true; simBtn.textContent = "⏳ Считаем партии…";
    try {
      const resp = await fetch(simUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: cfg }) });
      const data = await resp.json();
      if (data.ok && data.result.runnable) { lastResult = data.result; renderResults(data.result); }
      else alert((data.result && data.result.reason) || "Симуляция не запустилась.");
    } catch (e) { alert("Ошибка сети при запуске симуляции."); }
    simBtn.disabled = false; simBtn.textContent = "▶ Запустить симуляцию";
  });

  function configLabel(c) {
    return `${c.n_players} игр.` + (c.p_correct != null ? `, p=${c.p_correct}` : "");
  }

  function renderResults(res) {
    const section = document.getElementById("sim-results");
    section.hidden = false;
    document.getElementById("results-summary").textContent =
      `${res.games_per_config.toLocaleString("ru")} партий × ${res.configs.length} конфиг. · блоки: ${res.blocks_used.map((b) => BLOCK_LABELS[b] || b).join(", ")}`;

    const sel = document.getElementById("config-select");
    sel.innerHTML = res.configs.map((c, i) => `<option value="${i}">${configLabel(c)}</option>`).join("");
    sel.onchange = () => renderConfig(res.configs[+sel.value]);

    renderConfig(res.configs[0]);
    renderSensitivity(res);
    renderFindings(res.summary_findings);
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function metricCard(label, value, cls) {
    return `<div class="metric-card ${cls || ''}"><span class="metric-value">${value}</span><span class="metric-label">${label}</span></div>`;
  }

  function renderConfig(c) {
    const fair = (c.fair_share * 100).toFixed(1);
    const cards = [
      metricCard("ничьи за 1-е место", (c.tie_rate * 100).toFixed(1) + "%", c.tie_rate > 0.1 ? "m-bad" : "m-good"),
      metricCard("без победителя (deadlock)", (c.no_winner_rate * 100).toFixed(1) + "%", c.no_winner_rate > 0.01 ? "m-bad" : "m-good"),
      metricCard("перекос 1-го игрока", (c.first_player_edge >= 0 ? "+" : "") + (c.first_player_edge * 100).toFixed(1) + " п.п.", Math.abs(c.first_player_edge) > 0.05 ? "m-warn" : "m-good"),
      metricCard("медиана ходов на игрока", c.rounds.median, ""),
      metricCard("действий за партию", c.actions_per_game ? c.actions_per_game.median : "—", ""),
      metricCard("вылетов/партия", c.avg_eliminated, ""),
    ];
    document.getElementById("metric-cards").innerHTML = cards.join("");

    // win rate по местам
    const seats = Object.keys(c.win_rate_by_seat);
    drawChart("chart-winrate", "bar", {
      labels: seats.map((s) => "место " + s),
      datasets: [{
        label: "win rate",
        data: seats.map((s) => +(c.win_rate_by_seat[s] * 100).toFixed(2)),
        backgroundColor: seats.map((s) => Math.abs(c.win_rate_by_seat[s] - c.fair_share) > 0.05 ? COLORS.warn : COLORS.accent),
        borderRadius: 6,
      }],
    }, fairLineOptions(c.fair_share * 100, "%"));

    // гистограмма раундов
    const h = c.rounds_hist;
    drawChart("chart-rounds", "bar", {
      labels: h.edges.slice(0, -1).map((e, i) => `${e}–${h.edges[i + 1]}`),
      datasets: [{ label: "партий", data: h.counts, backgroundColor: COLORS.blue, borderRadius: 4 }],
    }, baseOptions());

    // причины завершения
    const reasons = c.end_reason_share;
    const rLabels = { finish: "финиш", max_score: "по очкам", round_cap: "лимит раундов",
      last_standing: "остался один", all_eliminated: "все выбыли", timer: "таймер", deck_empty: "колода кончилась" };
    drawChart("chart-reasons", "doughnut", {
      labels: Object.keys(reasons).map((k) => rLabels[k] || k),
      datasets: [{ data: Object.values(reasons).map((v) => +(v * 100).toFixed(1)),
        backgroundColor: [COLORS.accent, COLORS.blue, COLORS.warn, COLORS.bad, COLORS.good, COLORS.dim, "#b07cff"] }],
    }, { responsive: true, plugins: { legend: { position: "right", labels: { color: COLORS.dim } } } });
  }

  function renderSensitivity(res) {
    const card = document.getElementById("sensitivity-card");
    if (!res.scan_p || res.p_grid.length < 2) { card.hidden = true; return; }
    card.hidden = false;
    // линия перекоса первого игрока по p, для каждого числа игроков
    const byPlayers = {};
    res.configs.forEach((c) => {
      (byPlayers[c.n_players] = byPlayers[c.n_players] || []).push(c);
    });
    const datasets = Object.keys(byPlayers).map((np, i) => {
      const arr = byPlayers[np].sort((a, b) => a.p_correct - b.p_correct);
      return { label: np + " игроков", data: arr.map((c) => +(c.first_player_edge * 100).toFixed(2)),
        borderColor: [COLORS.accent, COLORS.warn, COLORS.good][i % 3], tension: 0.3, fill: false };
    });
    drawChart("chart-sensitivity", "line", { labels: res.p_grid.map((p) => "p=" + p), datasets }, baseOptions("перекос, п.п."));
  }

  function renderFindings(findings) {
    const el = document.getElementById("findings-list");
    if (!findings.length) { el.innerHTML = '<p class="dash-note">💎 Серьёзных аномалий не обнаружено.</p>'; return; }
    const sev = { critical: "🔴", warn: "🟡", info: "🔵" };
    el.innerHTML = findings.map((f) => `
      <div class="finding finding-${f.severity}">
        <div class="finding-head">${sev[f.severity] || "•"} <b>${f.anomaly}</b></div>
        <div class="finding-detail">${f.detail}</div>
        <div class="finding-recipe">→ ${f.recipe}</div>
      </div>`).join("");
  }

  // ---------- Chart.js хелперы ----------
  function baseOptions(yTitle) {
    return {
      responsive: true,
      plugins: { legend: { display: !!yTitle, labels: { color: COLORS.dim } } },
      scales: {
        x: { ticks: { color: COLORS.dim }, grid: { color: "rgba(255,255,255,0.06)" } },
        y: { ticks: { color: COLORS.dim }, grid: { color: "rgba(255,255,255,0.06)" },
             title: { display: !!yTitle, text: yTitle || "", color: COLORS.dim } },
      },
    };
  }
  function fairLineOptions(fair, unit) {
    const o = baseOptions(unit);
    o.plugins.annotationFair = fair; // храним для подписи в легенде заголовка
    o.scales.y.suggestedMin = 0;
    return o;
  }
  function drawChart(id, type, data, options) {
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(document.getElementById(id), { type, data, options });
  }
})();
