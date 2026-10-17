// 二重送信の防止: 送信したら、そのフォームの送信ボタンを押せなくする（サーバー側でも 1 回だけ受け付ける）。
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
  if (form.dataset.submitted === "true") {
    event.preventDefault();
    return;
  }
  form.dataset.submitted = "true";
  window.setTimeout(() => {
    form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach((button) => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    });
  }, 0);
});

// 長い入力の画面（form[data-keepalive]）: 入力している間は 5 分おきにセッションを延ばす。
// 無操作 30 分のセッション失効で、入力途中の内容が消えるのを防ぐ（入力していなければ延ばさない）。
const KEEPALIVE_INTERVAL_MS = 5 * 60 * 1000;
document.querySelectorAll("form[data-keepalive]").forEach((form) => {
  let lastPing = Date.now();
  form.addEventListener("input", () => {
    if (Date.now() - lastPing < KEEPALIVE_INTERVAL_MS) return;
    lastPing = Date.now();
    const token = form.querySelector('input[name="csrfmiddlewaretoken"]');
    fetch(form.dataset.keepalive, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": token ? token.value : "" },
    });
  });
});

// 行を足せる入力（Django の formset。[data-formset]）: 空の行の雛形を複製し、__prefix__ を行の番号に
// 置き換えて、行の数（TOTAL_FORMS）を 1 増やす。JS が無いときは表示した行だけを送る。
document.querySelectorAll("[data-formset]").forEach((container) => {
  const prefix = container.dataset.formset;
  const total = container.querySelector(`input[name="${prefix}-TOTAL_FORMS"]`);
  const template = container.querySelector("template[data-formset-template]");
  const rows = container.querySelector("[data-formset-rows]");
  const button = container.querySelector("[data-formset-add]");
  if (!total || !template || !rows || !button) return;
  button.hidden = false;
  button.addEventListener("click", () => {
    const index = Number(total.value);
    rows.insertAdjacentHTML("beforeend", template.innerHTML.replaceAll("__prefix__", String(index)));
    total.value = String(index + 1);
    const first = rows.lastElementChild?.querySelector("input, select, textarea");
    if (first instanceof HTMLElement) first.focus();
  });
});
