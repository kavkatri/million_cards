/* Credential capture.
 *
 * The token field is write-only by design: it posts once and is cleared. The
 * page never receives a stored token back from the server, so there is nothing
 * here to leak into a screenshot, a bug report, or a browser autofill store.
 */

const form = document.getElementById("account-form");
const status = document.getElementById("acct-status");

form?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const tokenField = document.getElementById("acct-token");
  const submit = form.querySelector('button[type="submit"]');
  const done = busy(submit, "Сохраняем…");

  try {
    const r = await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({
        name: document.getElementById("acct-name").value,
        token: tokenField.value,
        tier: document.getElementById("acct-tier").value,
        sandbox: document.getElementById("acct-sandbox").checked,
      }),
    });
    tokenField.value = "";
    status.textContent = r.external_id
      ? `Сохранено. Продавец ${r.external_id}.`
      : "Сохранено.";
    // Reload so the table shows the new row with its fingerprint.
    setTimeout(() => window.location.reload(), 700);
  } catch (err) {
    status.textContent = `Ошибка: ${err.message}`;
  } finally {
    done();
  }
});
