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

// 条件つきの入力欄（[data-enabled-when="名前=値 名前=値"]。値が * なら「空でない」。StyledFormMixin.enable_when）: 条件をすべて
// 満たすときだけ入力でき、満たさないときは無効にして値を空にする（無効の欄は送信されない）。
// 欄どうしが連なる（要否が「要」のときだけ通報先）ので、変化が無くなるまで繰り返す。
// JS が無いときは欄は有効のままで、保存時の検査で弾く。
document.querySelectorAll("form").forEach((form) => {
  const targets = Array.from(form.querySelectorAll("[data-enabled-when]"));
  if (targets.length === 0) return;
  const valueOf = (name) => {
    for (const item of form.querySelectorAll(`[name="${name}"]`)) {
      if (item.disabled) continue;
      if (item.type === "radio" || item.type === "checkbox") {
        if (item.checked) return item.value;
      } else {
        return item.value;
      }
    }
    return "";
  };
  const clear = (item) => {
    if (item.type === "radio" || item.type === "checkbox") item.checked = false;
    else if (item instanceof HTMLSelectElement) item.selectedIndex = 0;
    else item.value = "";
  };
  const update = () => {
    for (let pass = 0; pass < targets.length; pass += 1) {
      let changed = false;
      targets.forEach((item) => {
        const enabled = item.dataset.enabledWhen.split(" ").every((rule) => {
          const [name, value] = rule.split("=");
          return value === "*" ? valueOf(name) !== "" : valueOf(name) === value; // * は「空でない」
        });
        if (item.disabled === !enabled) return;
        item.disabled = !enabled;
        if (!enabled) clear(item);
        changed = true;
      });
      if (!changed) return;
    }
  };
  form.addEventListener("change", update);
  update();
});

// まとめて選ぶボタン（[data-set-radios="値"]。[data-radio-scope] の中の、その値のラジオボタンをすべて
// 選ぶ）。定期面談の「すべて問題無にする」。JS が無いときはボタンを出さない。
document.querySelectorAll("[data-set-radios]").forEach((button) => {
  const scope = button.closest("[data-radio-scope]");
  if (!scope) return;
  button.hidden = false;
  button.addEventListener("click", () => {
    scope
      .querySelectorAll(`input[type="radio"][value="${button.dataset.setRadios}"]`)
      .forEach((radio) => {
        if (!radio.disabled) radio.checked = true;
      });
    scope.closest("form")?.dispatchEvent(new Event("change"));
  });
});

// 選択肢の絞り込み（修正 1 回目 B1・B2）: data-filter-source の欄で選んだ値を data-filter-values に
// 含むチェックボックスだけを見せ、隠したものは外す（保存の検査はサーバーで行う）。
(() => {
  const groups = new Map();
  document.querySelectorAll("input[data-filter-source]").forEach((input) => {
    const id = input.dataset.filterSource;
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(input);
  });
  groups.forEach((inputs, id) => {
    const source = document.getElementById(id);
    if (!source) return;
    const apply = () => {
      const value = source.value;
      inputs.forEach((input) => {
        const values = (input.dataset.filterValues || "").split(" ").filter(Boolean);
        const show = !value || values.includes(value);
        const row = input.closest("div") || input.parentElement;
        row.hidden = !show;
        if (!show) input.checked = false;
      });
    };
    source.addEventListener("change", apply);
    apply();
  });
})();

// 定番の選択肢と「その他（自由入力）」（修正 1 回目 B4）: その他のときだけ文字の欄を見せる。
document.querySelectorAll("[data-preset-or-other]").forEach((group) => {
  const select = group.querySelector("select");
  const text = group.querySelector("input");
  if (!select || !text) return;
  const apply = () => {
    const other = select.value === "__other__";
    text.hidden = !other;
    if (!other) text.value = "";
  };
  select.addEventListener("change", apply);
  apply();
});
