async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const detail = (body && body.detail) || text || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

// Dashboard: preview (dry run) and real run.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-run]");
  if (!btn) return;

  const lineId = btn.dataset.run;
  const dry = btn.dataset.dry === "1";
  const out = document.getElementById(`plan-${lineId}`);
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = dry ? "Считаем…" : "Запускаем…";

  try {
    const r = await api(`/api/lines/${lineId}/run?dry_run=${dry}`, { method: "POST" });
    const p = r.plan;
    const lines = [
      `Карточек создать:  ${p.cards_to_create.toLocaleString("ru")}`,
      `Фото загрузить:    ${p.media_to_upload.toLocaleString("ru")}`,
      `Цен установить:    ${p.prices_to_set.toLocaleString("ru")}`,
      `Остатков записать: ${p.stocks_to_set.toLocaleString("ru")}`,
    ];
    if (p.quota_remaining !== null && p.quota_remaining !== undefined) {
      lines.push(`Лимит создания:    ${p.quota_remaining.toLocaleString("ru")}`);
    }
    (p.notes || []).forEach((n) => lines.push(`⚠ ${n}`));
    if (!dry) lines.push(`\nЗапуск #${r.run_id} поставлен в очередь.`);
    out.textContent = lines.join("\n");
    out.hidden = false;
    out.classList.remove("bad");
  } catch (err) {
    out.textContent = `Ошибка: ${err.message}`;
    out.hidden = false;
    out.classList.add("bad");
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

function debounce(fn, ms = 350) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
