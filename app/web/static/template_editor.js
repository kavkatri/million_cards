// Image-template editor. Layer coordinates are fractions of the canvas, so the
// preview here and the render in the worker agree regardless of the base image's
// pixel size.

let basePath = null;
const extras = [];
const layersEl = document.getElementById("layers");
const previewImg = document.getElementById("preview-img");
const previewMsg = document.getElementById("preview-msg");

function layerRow(layer = {}) {
  const row = document.createElement("div");
  row.className = "layer-row";
  row.innerHTML = `
    <label>Текст <input class="l-text" value="${layer.text ?? "{w}"}"></label>
    <label>Размер <input type="number" class="l-size" value="${layer.size ?? 200}"></label>
    <label>Цвет <input type="color" class="l-color" value="${layer.color ?? "#212123"}"></label>
    <label>X <input type="number" step="0.01" min="0" max="1" class="l-x" value="${layer.x ?? 0.5}"></label>
    <label>Y <input type="number" step="0.01" min="0" max="1" class="l-y" value="${layer.y ?? 0.11}"></label>
    <label>Шрифт <input class="l-font" value="${layer.font ?? ""}" placeholder="путь к .ttf"></label>
    <button type="button" class="l-del">✕</button>`;
  row.querySelector(".l-del").addEventListener("click", () => { row.remove(); refresh(); });
  row.addEventListener("input", refresh);
  return row;
}

function readLayers() {
  return [...layersEl.querySelectorAll(".layer-row")].map((r) => ({
    type: "text",
    text: r.querySelector(".l-text").value,
    size: Number(r.querySelector(".l-size").value) || 48,
    color: r.querySelector(".l-color").value,
    x: Number(r.querySelector(".l-x").value),
    y: Number(r.querySelector(".l-y").value),
    font: r.querySelector(".l-font").value || null,
    anchor: "mm",
  }));
}

async function upload(file) {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload/asset", { method: "POST", body: fd });
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()).path;
}

function updateCount() {
  document.getElementById("photo-count").textContent = String(1 + extras.length);
}

const refresh = debounce(async () => {
  if (!basePath) return;
  let values = {};
  try { values = JSON.parse(document.getElementById("sample-values").value || "{}"); }
  catch { previewMsg.textContent = "Пример значений — некорректный JSON."; return; }

  const res = await fetch("/api/validate/image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_image_path: basePath, layers: readLayers(), values }),
  });
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail; } catch { /* raw text */ }
    previewMsg.textContent = `Ошибка: ${msg}`;
    previewImg.removeAttribute("src");
    return;
  }
  previewMsg.textContent = "";
  previewImg.src = URL.createObjectURL(await res.blob());
}, 400);

document.getElementById("base-upload").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  previewMsg.textContent = "Загружаем подложку…";
  try {
    basePath = await upload(file);
    refresh();
  } catch (err) {
    previewMsg.textContent = `Ошибка загрузки: ${err.message}`;
  }
});

document.getElementById("extras-upload").addEventListener("change", async (e) => {
  const list = document.getElementById("extras");
  for (const file of e.target.files) {
    try {
      const path = await upload(file);
      extras.push(path);
      const li = document.createElement("li");
      li.textContent = file.name;
      list.append(li);
    } catch (err) {
      previewMsg.textContent = `Ошибка загрузки ${file.name}: ${err.message}`;
    }
  }
  updateCount();
});

document.getElementById("add-layer").addEventListener("click", () => {
  layersEl.append(layerRow());
  refresh();
});

document.getElementById("sample-values").addEventListener("input", refresh);

document.getElementById("save-tpl").addEventListener("click", async () => {
  const status = document.getElementById("tpl-status");
  if (!basePath) { status.textContent = "Сначала загрузите подложку."; return; }
  status.textContent = "Сохраняем…";
  try {
    const r = await api("/api/templates", {
      method: "POST",
      body: JSON.stringify({
        name: document.getElementById("tpl-name").value || "Без названия",
        base_image_path: basePath,
        layers: readLayers(),
        extra_photo_paths: extras,
      }),
    });
    status.textContent = `Сохранено (#${r.id}, ${r.expected_photo_count} фото на карточку).`;
  } catch (err) {
    status.textContent = `Ошибка: ${err.message}`;
  }
});

layersEl.append(layerRow());
updateCount();
