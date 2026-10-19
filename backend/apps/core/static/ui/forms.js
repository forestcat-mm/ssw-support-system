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
const initializers = [];
const initKeepalive = (root) => root.querySelectorAll("form[data-keepalive]").forEach((form) => {
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
initializers.push(initKeepalive);

// 行を足せる入力（Django の formset。[data-formset]）: 空の行の雛形を複製し、__prefix__ を行の番号に
// 置き換えて、行の数（TOTAL_FORMS）を 1 増やす。JS が無いときは表示した行だけを送る。
initializers.push((root) => root.querySelectorAll("[data-formset]").forEach((container) => {
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
    container.dispatchEvent(new Event("change", { bubbles: true }));
  });
}));

// 条件つきの入力欄（[data-enabled-when="名前=値 名前=値"]。値が * なら「空でない」。StyledFormMixin.enable_when）: 条件をすべて
// 満たすときだけ入力でき、満たさないときは無効にして値を空にする（無効の欄は送信されない）。
// 欄どうしが連なる（要否が「要」のときだけ通報先）ので、変化が無くなるまで繰り返す。
// JS が無いときは欄は有効のままで、保存時の検査で弾く。
initializers.push((root) => root.querySelectorAll("form").forEach((form) => {
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
}));

// まとめて選ぶボタン（[data-set-radios="値"]。[data-radio-scope] の中の、その値のラジオボタンをすべて
// 選ぶ）。定期面談の「すべて問題無にする」。JS が無いときはボタンを出さない。
initializers.push((root) => root.querySelectorAll("[data-set-radios]").forEach((button) => {
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
}));

// 選択肢の絞り込み（修正 1 回目 B1・B2）: data-filter-source の欄で選んだ値を data-filter-values に
// 含むチェックボックスだけを見せ、隠したものは外す（保存の検査はサーバーで行う）。
initializers.push((root) => {
  const groups = new Map();
  root.querySelectorAll("input[data-filter-source]").forEach((input) => {
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
});

// 定番の選択肢と「その他（自由入力）」（修正 1 回目 B4）: その他のときだけ文字の欄を見せる。
initializers.push((root) => root.querySelectorAll("[data-preset-or-other]").forEach((group) => {
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
}));

// 委託料の内訳（修正 1 回目 B11）: 支援委託契約の画面の委託料の欄の「内訳」で、内訳の行をモーダルに開く。
// 行はフォームの中に置いたまま（保存は契約と一緒に 1 回）。合計が委託料と合わないときは入力中に知らせ、
// 合わないまま閉じると「委託料を内訳の合計に直す」「戻って内訳を直す」を選ぶ。委託料が空なら合計を入れる。
// JS が無いときは内訳の行は欄の下に並ぶ（保存の検査はサーバーで行う）。
const toNumber = (value) => {
  const text = String(value || "")
    .replace(/[０-９]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0))
    .replace(/[,，\s]/g, "");
  if (text === "") return null;
  const number = Number(text);
  return Number.isFinite(number) ? number : null;
};
initializers.push((root) => root.querySelectorAll("[data-fee-breakdown]").forEach((box) => {
  const fee = document.getElementById(box.dataset.feeInput);
  const panel = box.querySelector("[data-fee-panel]");
  const opener = box.querySelector("[data-fee-open]");
  const summary = box.querySelector("[data-fee-summary]");
  if (!fee || !panel || !opener) return;
  const part = (selector) => panel.querySelector(selector);
  const dialog = document.createElement("dialog");
  dialog.className = "modal";
  const title = part("[data-fee-title]");
  title.id = `${box.id}-title`;
  dialog.setAttribute("aria-labelledby", title.id);
  box.appendChild(dialog);
  dialog.appendChild(panel);
  panel.classList.add("modal__body");
  opener.hidden = false;
  part("[data-fee-actions]").hidden = false;
  const rows = () =>
    Array.from(panel.querySelectorAll("[data-formset-rows] > li")).filter(
      (row) => !row.querySelector('input[name$="-DELETE"]')?.checked,
    );
  const total = () => {
    let sum = null;
    rows().forEach((row) => {
      const amount = toNumber(row.querySelector('input[name$="-amount"]')?.value);
      if (amount !== null) sum = (sum || 0) + amount;
    });
    return sum;
  };
  const shown = (number) => (number === null ? "—" : number.toLocaleString("ja-JP"));
  const mismatch = () => {
    const sum = total();
    const amount = toNumber(fee.value);
    return sum !== null && amount !== null && sum !== amount;
  };
  const update = () => {
    part("[data-fee-amount]").textContent = shown(toNumber(fee.value));
    part("[data-fee-total]").textContent = shown(total());
    part("[data-fee-mismatch]").hidden = !mismatch();
    const sum = total();
    if (summary) {
      summary.textContent =
        sum === null
          ? summary.dataset.empty
          : summary.dataset.template.replace("{count}", String(rows().length)).replace("{total}", shown(sum));
    }
  };
  const setFee = (value) => {
    fee.value = String(value);
    fee.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const close = () => {
    const sum = total();
    if (sum !== null && toNumber(fee.value) === null) setFee(sum);
    else if (mismatch()) {
      part("[data-fee-choice]").hidden = false;
      part("[data-fee-apply]").focus();
      return;
    }
    part("[data-fee-choice]").hidden = true;
    dialog.close();
    update();
    opener.focus();
  };
  opener.addEventListener("click", () => {
    update();
    dialog.showModal();
  });
  part("[data-fee-close]").addEventListener("click", close);
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
  part("[data-fee-apply]").addEventListener("click", () => {
    setFee(total());
    close();
  });
  part("[data-fee-back]").addEventListener("click", () => {
    part("[data-fee-choice]").hidden = true;
    panel.querySelector('input[name$="-amount"]')?.focus();
  });
  panel.addEventListener("input", update);
  panel.addEventListener("change", update);
  fee.addEventListener("input", update);
  update();
  if (box.hasAttribute("data-open")) dialog.showModal();
}));

// モーダル（修正 1 回目 B8・B10）: a[data-modal] は、リンク先の画面の [data-modal-content] を <dialog id="modal">
// に読み込む。中のフォームは fetch で送り、検査の結果・入力の誤りはモーダルの中に出し直す。保存して移るとき
// （リダイレクト。サーバーは X-Modal の要求には 204 と X-Modal-Location で返す。ModalRedirectMiddleware）は、
// リンクに data-modal-return があれば今の画面を読み直し、無ければ移り先を開く。JS が無いときはふつうのリンク。
const initModal = () => {
  const dialog = document.getElementById("modal");
  if (!dialog) return;
  const title = dialog.querySelector("[data-modal-title]");
  const body = dialog.querySelector("[data-modal-body]");
  const headers = { "X-Modal": "1" };
  let current = null;
  const show = (html, url) => {
    const page = new DOMParser().parseFromString(html, "text/html");
    const content = page.querySelector("[data-modal-content]");
    if (!content) {
      window.location.assign(url);
      return;
    }
    title.textContent = page.querySelector(".page-header h1")?.textContent?.trim() || "";
    body.replaceChildren(document.adoptNode(content));
    initAll(body);
    if (!dialog.open) dialog.showModal();
    body.querySelector(".messages, .form-errors, input:not([type=hidden]), select, textarea")?.focus?.();
  };
  const handle = async (response, url) => {
    const location = response.headers.get("X-Modal-Location");
    if (response.status === 204 && location) {
      if (current.trigger.hasAttribute("data-modal-return")) window.location.reload();
      else window.location.assign(location);
      return;
    }
    if (!response.ok) {
      window.location.assign(url);
      return;
    }
    show(await response.text(), url);
  };
  document.addEventListener("click", async (event) => {
    const link = event.target instanceof Element ? event.target.closest("a[data-modal]") : null;
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    current = { url: link.href, trigger: link };
    try {
      await handle(await fetch(link.href, { headers, credentials: "same-origin" }), link.href);
    } catch {
      window.location.assign(link.href);
    }
  });
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !body.contains(form) || !current) return;
    if (event.defaultPrevented) return; // 二重送信（上の送信の検査が止めた）
    event.preventDefault();
    const url = form.getAttribute("action") ? form.action : current.url;
    const data = new FormData(form, event.submitter);
    try {
      await handle(
        await fetch(url, { method: "POST", body: data, headers, credentials: "same-origin" }),
        url,
      );
    } catch {
      delete form.dataset.submitted;
      form.querySelectorAll('button[type="submit"]').forEach((button) => {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      });
    }
  });
  dialog.addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest("[data-modal-close]")) {
      event.preventDefault();
      dialog.close();
    }
  });
  dialog.addEventListener("close", () => {
    body.replaceChildren();
    current = null;
  });
};

const initAll = (root) => initializers.forEach((init) => init(root));
initAll(document);
initModal();
