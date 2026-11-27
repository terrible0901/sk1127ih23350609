"use strict";

document.querySelectorAll("[data-reply]").forEach((button) => {
  button.addEventListener("click", () => {
    const body = document.querySelector("#body");
    if (!body) return;
    const prefix = `>>${button.dataset.reply}\n`;
    body.value = body.value ? body.value + "\n" + prefix : prefix;
    body.dispatchEvent(new Event("input"));
    body.focus();
    body.scrollIntoView({ behavior: "smooth", block: "center" });
  });
});

document.querySelectorAll("[data-copy-link]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(location.origin + location.pathname);
      button.textContent = "コピーしました ✓";
      setTimeout(() => { button.textContent = "リンクをコピー ↗"; }, 2500);
    } catch (_) { button.textContent = "アドレスバーのURLをコピーしてください"; }
  });
});

document.querySelectorAll("[data-go-back]").forEach((button) => {
  button.addEventListener("click", () => { if (history.length > 1) history.back(); else location.href = "/"; });
});

// Drafts stay in this tab's sessionStorage, never passwords or login fields.
// After a successful POST-redirect, clear the submitted draft. Failed submissions
// keep it so a 429/503 doesn't silently discard a student's long explanation.
const submittedKey = "houkago:submitted";
try {
  const pending = sessionStorage.getItem(submittedKey);
  if (pending && !document.querySelector(".error-page")) {
    sessionStorage.removeItem(pending);
    sessionStorage.removeItem(submittedKey);
  }
} catch (_) { /* Storage can be disabled by browser policy. */ }

document.querySelectorAll("[data-post-form]").forEach((form) => {
  const field = form.querySelector("[data-counted]");
  const count = form.querySelector("[data-char-count]");
  const status = form.querySelector("[data-draft-status]");
  const key = "houkago:draft:" + location.pathname;
  const fields = [...form.querySelectorAll("textarea, input[name=title], select[name=subject]")];
  try {
    const draft = JSON.parse(sessionStorage.getItem(key) || "null");
    if (draft) {
      fields.forEach((input) => { if (!input.value || input.tagName === "SELECT") input.value = draft[input.name] || input.value; });
      if (status) status.textContent = "このタブに保存した下書きを復元しました。";
    }
  } catch (_) { /* Ignore unavailable or malformed browser storage. */ }
  function update() {
    if (count && field) count.textContent = `${[...field.value].length.toLocaleString("ja-JP")} / 10,000`;
    try { sessionStorage.setItem(key, JSON.stringify(Object.fromEntries(fields.map((input) => [input.name, input.value])))); }
    catch (_) { /* Optional convenience; never prevents submission. */ }
  }
  fields.forEach((input) => input.addEventListener("input", update));
  if (count && field) count.textContent = `${[...field.value].length.toLocaleString("ja-JP")} / 10,000`;
  form.addEventListener("submit", () => {
    update();
    try { sessionStorage.setItem(submittedKey, key); } catch (_) { /* Optional. */ }
    const button = form.querySelector("button[type=submit]");
    if (button) { button.disabled = true; button.textContent = "送信しています…"; }
  });
});

// Back/forward cache may restore a submit button in its disabled state.
window.addEventListener("pageshow", () => {
  document.querySelectorAll("[data-post-form] button[type=submit]").forEach((button) => {
    if (button.disabled) {
      button.disabled = false;
      button.textContent = location.pathname === "/new" ? "質問を公開する ↗" : "返信を公開する ↗";
    }
  });
});

