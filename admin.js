/* Admin dashboard: orders, status tracking, products, banners, live new-order alerts. */
(() => {
  "use strict";

  const STATUSES = ["pending", "confirmed", "shipped", "delivered", "cancelled"];
  const LABELS = { pending: "Pending", confirmed: "Confirmed", shipped: "Shipped", delivered: "Delivered", cancelled: "Cancelled" };
  const state = { products: [], orders: [], statusFilter: "", lastSeq: null, unseen: 0, currentTab: "overview" };

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inr = (n) => "₹" + Number(n || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  const modal = document.getElementById("modal");
  const modalBody = document.getElementById("modalBody");
  const toastEl = document.getElementById("toast");

  // ---------- plumbing
  class AuthError extends Error {}

  async function api(path, options = {}) {
    const init = { method: options.method || (options.body !== undefined ? "POST" : "GET"), headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const res = await fetch(path, init);
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) throw new AuthError(data.error || "Please log in");
    if (!res.ok) throw new Error(data.error || "Request failed");
    return data;
  }

  // Shrinks a phone photo in the browser and returns a data URL small enough to store in Firestore.
  function imageToDataUrl(file, maxSide = 1000) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Could not read that image. Use a JPG, PNG or WebP photo.")); };
      img.onload = () => {
        URL.revokeObjectURL(url);
        const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#ffffff";  // transparent PNGs would otherwise turn black as JPEG
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        for (const quality of [0.85, 0.7, 0.55, 0.4]) {
          const dataUrl = canvas.toDataURL("image/jpeg", quality);
          if (dataUrl.length <= 700000) return resolve(dataUrl);
        }
        reject(new Error("That photo is still too large. Please choose a smaller image."));
      };
      img.src = url;
    });
  }

  function toast(message, isError = false) {
    toastEl.textContent = message;
    toastEl.className = "toast" + (isError ? " err" : "");
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (toastEl.hidden = true), 3600);
  }

  function openModal(html) {
    modalBody.innerHTML = html;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
  }
  function closeModal() {
    modal.hidden = true;
    modalBody.innerHTML = "";
    document.body.style.overflow = "";
  }
  document.getElementById("modalClose").onclick = closeModal;
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

  function handleError(err) {
    if (err instanceof AuthError) return showLogin();
    toast(err.message, true);
  }

  // ---------- theme
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("theme", t); } catch (_) { /* ignore */ }
  }
  applyTheme((() => { try { return localStorage.getItem("theme") || "dark"; } catch (_) { return "dark"; } })());
  document.getElementById("themeBtn").onclick = () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

  // ---------- login / logout
  function showLogin() {
    stopPolling();
    document.getElementById("appView").hidden = true;
    document.getElementById("loginView").hidden = false;
    $("#a_pass").value = "";
  }

  function showApp() {
    document.getElementById("loginView").hidden = true;
    document.getElementById("appView").hidden = false;
    switchTab("overview");
    startPolling();
  }

  document.getElementById("loginForm").onsubmit = async (e) => {
    e.preventDefault();
    const err = $("#loginError");
    err.textContent = "";
    try {
      await api("/api/admin/login", { body: { username: $("#a_user").value.trim(), password: $("#a_pass").value } });
      showApp();
    } catch (ex) {
      err.textContent = ex.message;
    }
  };

  document.getElementById("logoutBtn").onclick = async () => {
    await api("/api/admin/logout", { body: {} }).catch(() => {});
    showLogin();
  };

  // ---------- tabs
  function switchTab(name) {
    state.currentTab = name;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.tab === name));
    ["overview", "orders", "products", "banners"].forEach((n) => {
      document.getElementById("tab-" + n).hidden = n !== name;
    });
    if (name === "overview") loadStats();
    if (name === "orders") { state.unseen = 0; updateBadges(); loadOrders(); }
    if (name === "products") loadProducts();
    if (name === "banners") loadBanners();
  }
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));

  // ---------- overview
  async function loadStats() {
    try {
      const s = await api("/api/admin/stats");
      const b = s.orders_by_status;
      const card = (label, value, sub = "", hl = false) =>
        `<div class="stat${hl ? " hl" : ""}"><div class="label">${esc(label)}</div><div class="value">${value}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`;
      document.getElementById("stats").innerHTML = [
        card("Total orders", s.total_orders, "All time"),
        card("Pending", b.pending, "Needs action", b.pending > 0),
        card("Confirmed", b.confirmed),
        card("Shipped", b.shipped),
        card("Delivered", b.delivered),
        card("Revenue", inr(s.revenue), "Excludes cancelled"),
        card("Products", s.products),
        card("Low stock", s.low_stock, "5 or fewer units", s.low_stock > 0),
      ].join("");
    } catch (err) { handleError(err); }
  }

  // ---------- orders
  function renderFilters() {
    const chips = [{ v: "", l: "All" }, ...STATUSES.map((s) => ({ v: s, l: LABELS[s] }))];
    document.getElementById("statusFilter").innerHTML = chips
      .map((c) => `<button class="chip${state.statusFilter === c.v ? " on" : ""}" data-s="${c.v}" type="button">${c.l}</button>`).join("");
  }

  document.getElementById("statusFilter").onclick = (e) => {
    const b = e.target.closest("[data-s]");
    if (!b) return;
    state.statusFilter = b.dataset.s;
    renderFilters();
    loadOrders();
  };
  document.getElementById("refreshOrders").onclick = () => loadOrders();

  async function loadOrders() {
    renderFilters();
    const qs = state.statusFilter ? `?status=${encodeURIComponent(state.statusFilter)}` : "";
    const tbody = document.querySelector("#ordersTable tbody");
    try {
      const data = await api("/api/admin/orders" + qs);
      state.orders = data.orders;
      tbody.innerHTML = data.orders.length
        ? data.orders.map(orderRow).join("")
        : `<tr class="empty-row"><td colspan="9">No orders ${state.statusFilter ? "with this status" : "yet"}.</td></tr>`;
    } catch (err) { handleError(err); }
  }

  function orderRow(o) {
    const options = STATUSES.map((s) => `<option value="${s}"${s === o.status ? " selected" : ""}>${LABELS[s]}</option>`).join("");
    return `<tr>
      <td class="num"><b>${esc(o.id)}</b></td>
      <td class="small-text num">${esc(o.created_at)}</td>
      <td><b>${esc(o.customer_name)}</b></td>
      <td class="num"><a href="tel:+91${esc(o.mobile)}">+91 ${esc(o.mobile)}</a></td>
      <td style="min-width:240px">${esc(o.address)}<div class="small-text">${esc(o.city)}, ${esc(o.state)} · ${esc(o.pin)}</div></td>
      <td>${esc(o.product_name)}<div class="small-text">${inr(o.unit_price)} each</div></td>
      <td class="num">${o.quantity}</td>
      <td class="num"><b>${inr(o.total)}</b></td>
      <td><div style="display:flex;flex-direction:column;gap:6px"><span class="pill ${esc(o.status)}">${LABELS[o.status]}</span>
        <select class="status-select" data-order="${esc(o.id)}" data-prev="${esc(o.status)}" aria-label="Status for ${esc(o.id)}">${options}</select></div></td>
    </tr>`;
  }

  document.querySelector("#ordersTable").addEventListener("change", async (e) => {
    const sel = e.target.closest("select[data-order]");
    if (!sel) return;
    const next = sel.value;
    if (next === "cancelled" && !confirm("Cancel this order? Its items will be returned to stock.")) {
      sel.value = sel.dataset.prev;
      return;
    }
    try {
      await api(`/api/admin/orders/${encodeURIComponent(sel.dataset.order)}`, { method: "PATCH", body: { status: next } });
      toast(`Order marked ${LABELS[next].toLowerCase()}`);
      loadOrders();
      loadStats();
    } catch (err) {
      sel.value = sel.dataset.prev;
      handleError(err);
    }
  });

  // ---------- products
  async function loadProducts() {
    const tbody = document.querySelector("#productsTable tbody");
    try {
      state.products = (await api("/api/admin/products")).products;
      tbody.innerHTML = state.products.length
        ? state.products.map((p) => `<tr>
            <td><div class="thumb-mini">${p.image ? `<img src="${esc(p.image)}" alt="">` : "—"}</div></td>
            <td><b>${esc(p.name)}</b><div class="small-text">${esc((p.description || "").slice(0, 70))}</div></td>
            <td class="num">${inr(p.price)}</td>
            <td class="num">${p.discount_price != null ? inr(p.discount_price) : "—"}</td>
            <td class="num">${p.stock}</td>
            <td>${p.featured ? "Yes" : "No"}</td>
            <td>${p.active ? "Visible" : "Hidden"}</td>
            <td><div class="actions">
              <button class="btn ghost small" type="button" data-edit="${p.id}">Edit</button>
              <button class="btn danger small" type="button" data-del="${p.id}">Delete</button>
            </div></td></tr>`).join("")
        : `<tr class="empty-row"><td colspan="8">No products yet. Add your first product.</td></tr>`;
    } catch (err) { handleError(err); }
  }

  document.getElementById("productsTable").addEventListener("click", async (e) => {
    const edit = e.target.closest("[data-edit]");
    const del = e.target.closest("[data-del]");
    if (edit) return productForm(state.products.find((p) => p.id === edit.dataset.edit));
    if (del) {
      const p = state.products.find((x) => x.id === del.dataset.del);
      if (!confirm(`Delete "${p?.name}"? Past orders keep their records.`)) return;
      try { await api(`/api/admin/products/${del.dataset.del}`, { method: "DELETE" }); toast("Product deleted"); loadProducts(); }
      catch (err) { handleError(err); }
    }
  });

  document.getElementById("newProduct").onclick = () => productForm(null);

  function imageField(url, label) {
    return `<div class="field"><label>${label}</label>
      <img class="upload-preview" id="imgPreview" alt="" ${url ? `src="${esc(url)}"` : "hidden"}>
      <input type="file" id="imgFile" accept="image/jpeg,image/png,image/webp">
      <div class="small-text" style="margin-top:6px">JPG, PNG or WebP, up to 5 MB.</div>
      <div style="margin-top:8px"><button class="btn ghost small" id="imgRemove" type="button" ${url ? "" : "hidden"}>Remove image</button></div>
    </div>`;
  }

  function wireImageField(formState) {
    const file = $("#imgFile");
    const preview = $("#imgPreview");
    const removeBtn = $("#imgRemove");
    file.onchange = () => {
      const f = file.files[0];
      if (!f) return;
      preview.src = URL.createObjectURL(f);
      preview.hidden = false;
      removeBtn.hidden = false;
      formState.pending = imageToDataUrl(f).then((d) => { formState.image = d; formState.pending = null; });
      formState.pending.catch(() => {});  // the save step re-throws it with a message
    };
    removeBtn.onclick = () => {
      formState.image = null; formState.pending = null; file.value = "";
      preview.hidden = true; preview.removeAttribute("src"); removeBtn.hidden = true;
    };
  }

  function productForm(p) {
    const formState = { image: p?.image || null };
    openModal(`
      <h2 class="form-title" id="formTitle">${p ? "Edit product" : "New product"}</h2>
      <form id="pForm" novalidate>
        <div class="field"><label for="p_name">Product name</label><input id="p_name" name="name" required maxlength="120" value="${esc(p?.name)}"></div>
        <div class="field"><label for="p_desc">Description</label><textarea id="p_desc" name="description" maxlength="2000">${esc(p?.description)}</textarea></div>
        <div class="row2">
          <div class="field"><label for="p_price">Price (₹)</label><input id="p_price" name="price" type="number" min="0" step="0.01" required value="${p ? p.price : ""}"></div>
          <div class="field"><label for="p_disc">Discounted price (₹, optional)</label><input id="p_disc" name="discount_price" type="number" min="0" step="0.01" value="${p?.discount_price ?? ""}"></div>
          <div class="field"><label for="p_stock">Stock</label><input id="p_stock" name="stock" type="number" min="0" step="1" required value="${p ? p.stock : 0}"></div>
        </div>
        ${imageField(formState.image, "Product image")}
        <div class="check-row">
          <label><input type="checkbox" name="featured" ${p?.featured ? "checked" : ""}> Featured on homepage</label>
          <label><input type="checkbox" name="active" ${!p || p.active ? "checked" : ""}> Visible in store</label>
        </div>
        <div class="error" id="pError" role="alert"></div>
        <button class="btn block" id="pSave" type="submit">${p ? "Save changes" : "Create product"}</button>
      </form>`);
    wireImageField(formState);

    $("#pForm").onsubmit = async (e) => {
      e.preventDefault();
      const form = e.target;
      const fd = new FormData(form);
      const save = $("#pSave");
      const errEl = $("#pError");
      errEl.textContent = "";
      save.disabled = true;
      try {
        if (formState.pending) await formState.pending;
        const body = {
          name: fd.get("name"),
          description: fd.get("description"),
          price: fd.get("price"),
          discount_price: fd.get("discount_price") || null,
          stock: Number(fd.get("stock")),
          image: formState.image,
          featured: fd.get("featured") === "on",
          active: fd.get("active") === "on",
        };
        if (p) await api(`/api/admin/products/${p.id}`, { method: "PUT", body });
        else await api("/api/admin/products", { body });
        closeModal();
        toast(p ? "Product updated" : "Product added");
        loadProducts();
      } catch (err) {
        if (err instanceof AuthError) return handleError(err);
        errEl.textContent = err.message;
      } finally {
        save.disabled = false;
      }
    };
  }

  // ---------- banners
  async function loadBanners() {
    const list = document.getElementById("bannerList");
    try {
      const [{ banners }, { products }] = await Promise.all([api("/api/admin/banners"), api("/api/admin/products")]);
      state.products = products;
      state.banners = banners;
      list.innerHTML = banners.length
        ? banners.map((b) => {
            const linked = products.find((p) => p.id === b.link_product_id);
            return `<div class="banner-item">
              <div class="img" style="${b.image ? `background-image:url('${esc(b.image)}')` : ""}">${b.image ? "" : "No image"}</div>
              <div>
                <h3>${esc(b.title)} ${b.active ? "" : '<span class="pill cancelled">Hidden</span>'}</h3>
                <div class="small-text">${esc(b.subtitle)}</div>
                <div class="small-text">Links to: ${linked ? esc(linked.name) : "Shop page"} · Order ${b.sort}</div>
              </div>
              <div class="actions">
                <button class="btn ghost small" type="button" data-bedit="${b.id}">Edit</button>
                <button class="btn danger small" type="button" data-bdel="${b.id}">Delete</button>
              </div></div>`;
          }).join("")
        : `<div class="card-box empty-row"><p class="small-text">No banners yet. Banners appear as the rotating hero on the storefront.</p></div>`;
    } catch (err) { handleError(err); }
  }

  document.getElementById("bannerList").addEventListener("click", async (e) => {
    const edit = e.target.closest("[data-bedit]");
    const del = e.target.closest("[data-bdel]");
    if (edit) return bannerForm(state.banners.find((b) => b.id === edit.dataset.bedit));
    if (del) {
      if (!confirm("Delete this banner?")) return;
      try { await api(`/api/admin/banners/${del.dataset.bdel}`, { method: "DELETE" }); toast("Banner deleted"); loadBanners(); }
      catch (err) { handleError(err); }
    }
  });

  document.getElementById("newBanner").onclick = () => bannerForm(null);

  function bannerForm(b) {
    const formState = { image: b?.image || null };
    const options = ['<option value="">Shop page (no product)</option>']
      .concat(state.products.map((p) => `<option value="${p.id}"${b?.link_product_id === p.id ? " selected" : ""}>${esc(p.name)}</option>`))
      .join("");
    openModal(`
      <h2 class="form-title" id="formTitle">${b ? "Edit banner" : "New banner"}</h2>
      <form id="bForm" novalidate>
        <div class="field"><label for="b_title">Headline</label><input id="b_title" name="title" required maxlength="120" value="${esc(b?.title)}"></div>
        <div class="field"><label for="b_sub">Subtitle</label><input id="b_sub" name="subtitle" maxlength="200" value="${esc(b?.subtitle)}"></div>
        <div class="row2">
          <div class="field"><label for="b_link">Button links to</label><select id="b_link" name="link_product_id">${options}</select></div>
          <div class="field"><label for="b_sort">Display order</label><input id="b_sort" name="sort" type="number" min="0" max="999" value="${b?.sort ?? 0}"></div>
        </div>
        ${imageField(formState.image, "Banner image (wide photo works best)")}
        <div class="check-row"><label><input type="checkbox" name="active" ${!b || b.active ? "checked" : ""}> Show on storefront</label></div>
        <div class="error" id="bError" role="alert"></div>
        <button class="btn block" id="bSave" type="submit">${b ? "Save banner" : "Create banner"}</button>
      </form>`);
    wireImageField(formState);

    $("#bForm").onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const save = $("#bSave");
      const errEl = $("#bError");
      errEl.textContent = "";
      save.disabled = true;
      try {
        if (formState.pending) await formState.pending;
        const body = {
          title: fd.get("title"),
          subtitle: fd.get("subtitle"),
          image: formState.image,
          link_product_id: fd.get("link_product_id") || null,
          sort: Number(fd.get("sort")) || 0,
          active: fd.get("active") === "on",
        };
        if (b) await api(`/api/admin/banners/${b.id}`, { method: "PUT", body });
        else await api("/api/admin/banners", { body });
        closeModal();
        toast(b ? "Banner updated" : "Banner added");
        loadBanners();
      } catch (err) {
        if (err instanceof AuthError) return handleError(err);
        errEl.textContent = err.message;
      } finally {
        save.disabled = false;
      }
    };
  }

  // ---------- live new-order alerts
  let pollTimer = null;

  function updateBadges() {
    const dot = document.getElementById("bellDot");
    dot.hidden = state.unseen === 0;
    dot.textContent = state.unseen;
    const tabCount = document.getElementById("tabOrdersCount");
    tabCount.hidden = state.unseen === 0;
    tabCount.textContent = state.unseen;
  }

  function chime() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      [660, 880].forEach((freq, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.frequency.value = freq;
        osc.connect(gain);
        gain.connect(ctx.destination);
        const t = ctx.currentTime + i * 0.18;
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.35);
        osc.start(t);
        osc.stop(t + 0.36);
      });
    } catch (_) { /* audio not available */ }
  }

  async function pollNotifications() {
    try {
      const qs = state.lastSeq === null ? "" : `?after=${state.lastSeq}`;
      const data = await api("/api/admin/notifications" + qs);
      if (state.lastSeq !== null && data.new.length) {
        const first = data.new[0];
        state.unseen += data.new.length;
        updateBadges();
        chime();
        toast(`New order ${first.id}: ${first.customer_name} · ${first.product_name}${data.new.length > 1 ? ` (+${data.new.length - 1} more)` : ""}`);
        if ("Notification" in window && Notification.permission === "granted") {
          new Notification("New order received", { body: `${first.product_name} · ${inr(first.total)}` });
        }
        if (state.currentTab === "orders") loadOrders();
        if (state.currentTab === "overview") loadStats();
      }
      state.lastSeq = data.latest;
    } catch (err) {
      if (err instanceof AuthError) showLogin();
    }
  }

  function startPolling() {
    stopPolling();
    state.lastSeq = null;
    pollNotifications();
    pollTimer = setInterval(pollNotifications, 15000);
  }
  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  document.getElementById("bell").onclick = () => {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    switchTab("orders");
  };

  // ---------- boot: restore an existing session if there is one
  (async () => {
    try {
      await api("/api/admin/me");
      showApp();
    } catch (_) {
      showLogin();
    }
  })();
})();
