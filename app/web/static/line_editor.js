// Product-line builder. Every field that could produce a bad run is validated
// server-side as you type: the grid is costed, the price formula is evaluated
// against a sample cell. Nothing here writes to the marketplace.

const axesEl = document.getElementById("axes");
const gridPreview = document.getElementById("grid-preview");
const pricePreview = document.getElementById("price-preview");

function axisRow(axis = {}) {
  const row = document.createElement("div");
  row.className = "axis-row";
  row.innerHTML = `
    <label>Имя <input class="ax-name" value="${axis.name ?? ""}" placeholder="w"></label>
    <label>Тип
      <select class="ax-type">
        <option value="range">диапазон</option>
        <option value="list">список</option>
      </select>
    </label>
    <label class="ax-range">От <input type="number" class="ax-start" value="${axis.start ?? 10}"></label>
    <label class="ax-range">До <input type="number" class="ax-stop" value="${axis.stop ?? 120}"></label>
    <label class="ax-range">Шаг <input type="number" class="ax-step" value="${axis.step ?? 1}"></label>
    <label class="ax-list" hidden>Значения <input class="ax-values" value="${(axis.values ?? []).join(", ")}"></label>
    <button type="button" class="secondary ax-del">✕</button>`;
  row.querySelector(".ax-type").value = axis.type ?? "range";
  toggleAxisType(row);
  row.querySelector(".ax-type").addEventListener("change", () => { toggleAxisType(row); refresh(); });
  row.querySelector(".ax-del").addEventListener("click", () => { row.remove(); refresh(); });
  row.addEventListener("input", refresh);
  return row;
}

function toggleAxisType(row) {
  const isList = row.querySelector(".ax-type").value === "list";
  row.querySelectorAll(".ax-range").forEach((el) => (el.hidden = isList));
  row.querySelector(".ax-list").hidden = !isList;
}

function readAxes() {
  return [...axesEl.querySelectorAll(".axis-row")].map((row) => {
    const type = row.querySelector(".ax-type").value;
    const base = { name: row.querySelector(".ax-name").value.trim(), type };
    if (type === "list") {
      const raw = row.querySelector(".ax-values").value;
      return { ...base, values: raw.split(",").map((s) => s.trim()).filter(Boolean) };
    }
    return {
      ...base,
      start: Number(row.querySelector(".ax-start").value),
      stop: Number(row.querySelector(".ax-stop").value),
      step: Number(row.querySelector(".ax-step").value) || 1,
    };
  }).filter((a) => a.name);
}

function readPriceRule() {
  let vars = {};
  try { vars = JSON.parse(document.getElementById("price-vars").value || "{}"); } catch { /* shown below */ }
  const rule = {
    type: "formula",
    expr: document.getElementById("price-expr").value,
    vars,
    round_to: Number(document.getElementById("round-to").value) || 1,
    discount: Number(document.getElementById("discount").value) || 0,
  };
  const min = document.getElementById("min-price").value;
  if (min !== "") rule.min_price = Number(min);
  return rule;
}

function sampleAxes() {
  const out = {};
  readAxes().forEach((a) => {
    out[a.name] = a.type === "list" ? (a.values[0] ?? "") : a.start;
  });
  return out;
}

const refresh = debounce(async () => {
  const grid_spec = { axes: readAxes() };
  const vendor_code_template = document.getElementById("vendor-template").value;

  if (!grid_spec.axes.length) {
    gridPreview.textContent = "Добавьте хотя бы одну ось.";
    return;
  }

  try {
    const r = await api("/api/validate/grid", {
      method: "POST",
      body: JSON.stringify({ grid_spec, vendor_code_template }),
    });
    gridPreview.classList.remove("bad");
    gridPreview.textContent =
      `${r.cells.toLocaleString("ru")} карточек · оси: ${r.axes.join(", ")}\n` +
      `пример артикула: ${r.sample_vendor_code}`;
  } catch (err) {
    gridPreview.classList.add("bad");
    gridPreview.textContent = err.message;
  }

  try {
    const r = await api("/api/validate/price", {
      method: "POST",
      body: JSON.stringify({ price_rule: readPriceRule(), axes: sampleAxes() }),
    });
    pricePreview.classList.remove("bad");
    const s = sampleAxes();
    pricePreview.textContent =
      `для ${JSON.stringify(s)} → ${r.price} ₽` + (r.discount ? ` (скидка ${r.discount}%)` : "");
  } catch (err) {
    pricePreview.classList.add("bad");
    pricePreview.textContent = err.message;
  }
}, 300);

async function loadSelects() {
  const accounts = await api("/api/accounts");
  const sel = document.getElementById("account-select");
  sel.innerHTML = accounts
    .map((a) => `<option value="${a.id}">${a.name}${a.sandbox ? " (песочница)" : ""}</option>`)
    .join("");

  const templates = await api("/api/templates");
  const tsel = document.getElementById("template-select");
  tsel.innerHTML =
    `<option value="">— без изображений —</option>` +
    templates
      .map((t) => `<option value="${t.id}">${t.name} (${t.expected_photo_count} фото)</option>`)
      .join("");

  if (window.LINE) {
    sel.value = window.LINE.account_id;
    if (window.LINE.image_template_id) tsel.value = window.LINE.image_template_id;
  }
}

document.getElementById("add-axis").addEventListener("click", () => {
  axesEl.append(axisRow());
  refresh();
});

document.getElementById("line-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = document.getElementById("save-status");
  const form = e.target;

  let card_template;
  try {
    card_template = JSON.parse(document.getElementById("card-template").value || "{}");
  } catch (err) {
    status.textContent = `Шаблон карточки — некорректный JSON: ${err.message}`;
    return;
  }

  const warehouse = document.getElementById("warehouse-id").value;
  const payload = {
    account_id: Number(document.getElementById("account-select").value),
    name: form.name.value,
    grid_spec: { axes: readAxes() },
    vendor_code_template: document.getElementById("vendor-template").value,
    card_template,
    image_template_id: Number(document.getElementById("template-select").value) || null,
    price_rule: readPriceRule(),
    stock_rule: {
      type: "constant",
      value: Number(document.getElementById("stock-value").value) || 0,
      ...(warehouse ? { warehouse_id: Number(warehouse) } : {}),
    },
    enabled_aspects: [...document.querySelectorAll('input[name="aspect"]:checked')].map((c) => c.value),
    schedule_cron: form.schedule_cron.value.trim() || null,
    enabled: true,
  };

  const id = form.dataset.lineId;
  status.textContent = "Сохраняем…";
  try {
    const r = await api(id ? `/api/lines/${id}` : "/api/lines", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    status.textContent = "Сохранено.";
    if (!id) window.location = `/lines/${r.id}`;
  } catch (err) {
    status.textContent = `Ошибка: ${err.message}`;
  }
});

// bootstrap
loadSelects();
const initial = window.LINE?.grid_spec?.axes ?? [
  { name: "w", type: "range", start: 10, stop: 120, step: 1 },
  { name: "l", type: "range", start: 10, stop: 380, step: 1 },
];
initial.forEach((a) => axesEl.append(axisRow(a)));
document.getElementById("vendor-template").addEventListener("input", refresh);
["price-expr", "price-vars", "min-price", "round-to", "discount"].forEach((id) =>
  document.getElementById(id).addEventListener("input", refresh)
);
refresh();
