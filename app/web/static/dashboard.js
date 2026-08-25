/* Dashboard: trigger runs, and render live progress as it arrives.
 *
 * The success pulse is the one piece of deliberate delight here, and it obeys
 * one rule: it happens on the thing that changed. Not a toast in the corner —
 * the meter, the counter, and the panel that just advanced. A person watching
 * a catalogue fill up should be able to look away and still catch, in
 * peripheral vision, that something landed.
 */

const ASPECT_RU = {
  card: "карточки",
  media: "фото",
  price: "цены",
  stock: "остатки",
};

const UNIT_RU = {
  cards: (n) => `${nf.format(n)} карт.`,
  photos: (n) => `${nf.format(n)} фото`,
  prices: (n) => `${nf.format(n)} цен`,
  stocks: (n) => `${nf.format(n)} остатков`,
};

/* ------------------------------------------------------------ run triggers */

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-run]");
  if (!btn) return;

  const lineId = btn.dataset.run;
  const dry = btn.dataset.dry === "1";
  const out = document.getElementById(`plan-${lineId}`);
  const done = busy(btn, dry ? "Считаем…" : "Запускаем…");

  try {
    const r = await api(`/api/lines/${lineId}/run?dry_run=${dry}`, { method: "POST" });
    const p = r.plan;
    const rows = [
      `Карточек создать   ${nf.format(p.cards_to_create)}`,
      `Фото загрузить     ${nf.format(p.media_to_upload)}`,
      `Цен установить     ${nf.format(p.prices_to_set)}`,
      `Остатков записать  ${nf.format(p.stocks_to_set)}`,
    ];
    if (p.quota_remaining !== null && p.quota_remaining !== undefined) {
      rows.push(`Лимит создания     ${nf.format(p.quota_remaining)}`);
    }
    (p.notes || []).forEach((n) => rows.push(`\n⚠ ${n}`));
    if (!dry) rows.push(`\nЗапуск #${r.run_id} поставлен в очередь.`);
    out.textContent = rows.join("\n");
    out.removeAttribute("data-state");
    out.hidden = false;
  } catch (err) {
    out.textContent = `Ошибка: ${err.message}`;
    out.setAttribute("data-state", "bad");
    out.hidden = false;
  } finally {
    done();
  }
});

/* ------------------------------------------------------------- live stream */

const feed = document.getElementById("feed");
const feedEmpty = document.getElementById("feed-empty");
const liveDot = document.getElementById("live-dot");
const liveLabel = document.getElementById("live-label");

let landedThisSession = 0;

function setLive(state, label) {
  if (liveDot) liveDot.dataset.live = state ? "true" : "false";
  if (liveLabel) liveLabel.textContent = label;
}

function bumpCounter(card, kind, by) {
  const el = card?.querySelector(`[data-count="${kind}"]`);
  if (!el) return null;
  const next = (parseInt(el.textContent.replace(/\D/g, ""), 10) || 0) + by;
  el.textContent = nf.format(next);

  // Advance the meter to match, so the bar and the number never disagree.
  const fill = card.querySelector(`[data-fill="${kind}"]`);
  const label = el.closest(".meter-label");
  const totalEl = label?.querySelector(".num");
  const total = parseInt((totalEl?.textContent || "").replace(/\D/g, ""), 10);
  if (fill && total > 0) {
    const p = Math.min(1, next / total);
    fill.style.setProperty("--p", String(p));
    fill.dataset.complete = p >= 1 ? "true" : "false";
  }
  return el;
}

function pulse(card, kind) {
  replay(card, "pulse");
  replay(card.querySelector(`[data-spark="${kind}"]`), "is-on");
  // Border colour is set by .pulse; clear it once the animation is done so the
  // panel returns to its resting state rather than staying green forever.
  window.setTimeout(() => card.classList.remove("pulse"), 950);
}

function addToFeed(event) {
  if (!feed) return;
  if (feedEmpty) feedEmpty.hidden = true;

  const li = document.createElement("li");
  const failed = event.type === "task.failed";
  const time = new Date(event.ts || Date.now());

  const unit = UNIT_RU[event.unit];
  const what = failed
    ? `${ASPECT_RU[event.aspect] || event.aspect} — ошибка: ${event.detail || ""}`
    : `${ASPECT_RU[event.aspect] || event.aspect}${event.label ? ` · ${event.label}` : ""}`;

  li.innerHTML = `
    <span class="badge" data-tone="${failed ? "danger" : "ok"}">
      <span class="dot" aria-hidden="true"></span>${failed ? "сбой" : "готово"}
    </span>
    <span class="what">${escapeHtml(what)}</span>
    <time datetime="${time.toISOString()}">${time.toLocaleTimeString("ru-RU")}</time>`;

  if (!failed) li.classList.add("pulse-wash");
  feed.prepend(li);

  while (feed.children.length > 60) feed.lastElementChild.remove();

  if (!failed && unit && event.count) {
    landedThisSession += event.count;
    const rate = document.getElementById("feed-rate");
    if (rate) rate.textContent = `за эту сессию: ${nf.format(landedThisSession)}`;
  }
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = String(s ?? "");
  return d.innerHTML;
}

function handleEvent(event) {
  const card = event.line_id
    ? document.querySelector(`[data-line="${event.line_id}"]`)
    : null;

  if (event.type === "task.done") {
    const kind = event.unit === "cards" ? "cards" : event.unit === "photos" ? "photos" : null;
    if (card && kind && event.count > 0) {
      bumpCounter(card, kind, event.count);
      pulse(card, kind);
    } else if (card) {
      pulse(card, "cards");
    }
  }
  addToFeed(event);
}

function connect() {
  if (!("EventSource" in window)) {
    setLive(false, "поток недоступен");
    return;
  }
  const es = new EventSource("/events/stream");

  es.onopen = () => setLive(true, "в реальном времени");
  es.onmessage = (msg) => {
    try {
      handleEvent(JSON.parse(msg.data));
    } catch {
      /* a malformed frame must not kill the stream */
    }
  };
  es.onerror = () => {
    // EventSource reconnects on its own; just tell the truth meanwhile.
    setLive(false, "переподключение…");
  };
}

connect();
