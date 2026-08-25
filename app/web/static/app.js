/* Shared helpers. Loaded on every page. */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let body;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const detail = (body && body.detail) || text || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function debounce(fn, ms = 300) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

const nf = new Intl.NumberFormat("ru-RU");

/* A spinner that appears instantly on a fast response is worse than no
   spinner — it flashes. Hold the busy state back until the wait is real. */
function busy(button, label) {
  const original = button.textContent;
  const timer = setTimeout(() => {
    button.textContent = label;
  }, 400);
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  return () => {
    clearTimeout(timer);
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = original;
  };
}

/* Restart a one-shot CSS animation. Without the reflow the class re-add is
   coalesced and the second pulse never plays. */
function replay(el, className) {
  if (!el) return;
  el.classList.remove(className);
  void el.offsetWidth;
  el.classList.add(className);
}
