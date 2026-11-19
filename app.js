/* Storefront: banners, product catalogue, Buy Now checkout, order tracking. */
(() => {
  "use strict";

  const app = document.getElementById("app");
  const modal = document.getElementById("modal");
  const modalBody = document.getElementById("modalBody");
  const toastEl = document.getElementById("toast");
  let slideTimer = null;
  let products = [];

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inr = (n) => "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  const $ = (sel, root = document) => root.querySelector(sel);

  const STATES = ["Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chandigarh",
    "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh",
    "Jammu and Kashmir", "Jharkhand", "Karnataka", "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh", "Maharashtra",
    "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal"];

  // ---------- helpers
  async function api(path, options = {}) {
    const init = { method: options.method || (options.body ? "POST" : "GET"), headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const res = await fetch(path, init);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
    return data;
  }

  function toast(message, isError = false) {
    toastEl.textContent = message;
    toastEl.className = "toast" + (isError ? " err" : "");
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (toastEl.hidden = true), 3200);
  }

  function openModal(html) {
    modalBody.innerHTML = html;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    const first = modalBody.querySelector("input, select, button.btn");
    if (first) first.focus({ preventScroll: true });
  }
  function closeModal() {
    modal.hidden = true;
    modalBody.innerHTML = "";
    document.body.style.overflow = "";
  }
  document.getElementById("modalClose").onclick = closeModal;
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) closeModal(); });

  // Theme: dark by default, black-and-white light option, remembered per browser.
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("theme", t); } catch (_) { /* private mode */ }
  }
  applyTheme(getSavedTheme());
  function getSavedTheme() {
    try { return localStorage.getItem("theme") || "dark"; } catch (_) { return "dark"; }
  }
  document.getElementById("themeBtn").onclick = () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  document.getElementById("year").textContent = new Date().getFullYear();

  // ---------- product cards
  function priceHtml(p) {
    const onSale = p.discount_price != null && p.discount_price < p.price;
    return onSale
      ? `<s>${inr(p.price)}</s> <b>${inr(p.discount_price)}</b>`
      : `<b>${inr(p.price)}</b>`;
  }

  function stockHtml(stock) {
    if (stock <= 0) return `<span class="stock out">Out of stock</span>`;
    if (stock <= 5) return `<span class="stock low">Only ${stock} left</span>`;
    return `<span class="stock">In stock (${stock} available)</span>`;
  }

  function cardHtml(p) {
    const onSale = p.discount_price != null && p.discount_price < p.price;
    const off = onSale ? Math.round((1 - p.discount_price / p.price) * 100) : 0;
    const img = p.image
      ? `<img loading="lazy" src="${esc(p.image)}" alt="${esc(p.name)}">`
      : `<span class="noimg">No image</span>`;
    return `
      <article class="card" data-id="${p.id}" tabindex="0" aria-label="${esc(p.name)}">
        <div class="thumb">${img}${onSale ? `<span class="badge">-${off}%</span>` : ""}</div>
        <div class="card-body">
          <h3>${esc(p.name)}</h3>
          <div class="price">${priceHtml(p)}</div>
          ${stockHtml(p.stock)}
        </div>
      </article>`;
  }

  function bindCards(container) {
    container.addEventListener("click", (e) => {
      const card = e.target.closest(".card");
      if (card) openProduct(card.dataset.id);
    });
    container.addEventListener("keydown", (e) => {
      const card = e.target.closest?.(".card");
      if (card && e.key === "Enter") openProduct(card.dataset.id);
    });
  }

  // ---------- home page
  function renderHero(banners) {
    const hero = $("#hero");
    clearInterval(slideTimer);
    if (!banners.length) {
      hero.innerHTML = `<div class="slide on"><div><h1>Premium essentials, delivered.</h1>
        <p>Curated pieces with transparent pricing and fast delivery across India.</p>
        <button class="btn" type="button" data-scroll>Shop now</button></div></div>`;
      return;
    }
    hero.innerHTML = banners.map((b, i) => `
      <div class="slide${i === 0 ? " on" : ""}" style="${b.image ? `background-image:url('${esc(b.image)}')` : ""}" data-product="${b.link_product_id ?? ""}">
        <div>
          <h1>${esc(b.title)}</h1>
          ${b.subtitle ? `<p>${esc(b.subtitle)}</p>` : ""}
          ${b.link_product_id ? `<button class="btn" type="button" data-open="${b.link_product_id}">View offer</button>` : `<button class="btn" type="button" data-scroll>Shop now</button>`}
        </div>
      </div>`).join("") +
      (banners.length > 1 ? `<div class="dots">${banners.map((_, i) => `<button type="button" aria-label="Slide ${i + 1}" class="${i === 0 ? "on" : ""}"></button>`).join("")}</div>` : "");

    const slides = [...hero.querySelectorAll(".slide")];
    const dots = [...hero.querySelectorAll(".dots button")];
    let index = 0;
    const show = (i) => {
      index = i;
      slides.forEach((s, j) => s.classList.toggle("on", j === i));
      dots.forEach((d, j) => d.classList.toggle("on", j === i));
    };
    dots.forEach((d, i) => d.onclick = () => { show(i); restart(); });
    hero.addEventListener("click", (e) => {
      const id = e.target.dataset?.open;
      if (id) openProduct(id);
      if (e.target.dataset?.scroll !== undefined) $("#products").scrollIntoView({ behavior: "smooth" });
    });
    const restart = () => { clearInterval(slideTimer); if (slides.length > 1) slideTimer = setInterval(() => show((index + 1) % slides.length), 5500); };
    restart();
  }

  let activeFilter = "all";
  function renderGrid() {
    const featured = products.filter((p) => p.featured);
    const filtered = products.filter((p) => {
      if (activeFilter === "sale") return p.discount_price != null && p.discount_price < p.price;
      if (activeFilter === "featured") return p.featured;
      return true;
    });
    $("#featured").innerHTML = featured.map(cardHtml).join("") || `<p class="empty">New arrivals coming soon.</p>`;
    $("#grid").innerHTML = filtered.map(cardHtml).join("") || `<p class="empty">No products match this filter.</p>`;
  }

  async function home() {
    app.innerHTML = `
      <section class="hero" id="hero" aria-label="Promotions"></section>
      <section class="section" id="featuredSection">
        <div class="section-head"><h2>Featured</h2></div>
        <div class="grid" id="featured"></div>
      </section>
      <section class="section" id="products">
        <div class="section-head">
          <h2>All products</h2>
          <div class="chips" id="filters">
            <button class="chip on" data-f="all" type="button">All</button>
            <button class="chip" data-f="featured" type="button">Featured</button>
            <button class="chip" data-f="sale" type="button">On sale</button>
          </div>
        </div>
        <div class="grid" id="grid"><p class="empty">Loading…</p></div>
      </section>`;

    bindCards($("#featured"));
    bindCards($("#grid"));
    $("#filters").onclick = (e) => {
      const b = e.target.closest("[data-f]");
      if (!b) return;
      activeFilter = b.dataset.f;
      document.querySelectorAll("#filters .chip").forEach((c) => c.classList.toggle("on", c === b));
      renderGrid();
    };

    try {
      const [{ banners }, data] = await Promise.all([api("/api/banners"), api("/api/products")]);
      products = data.products;
      renderHero(banners);
      renderGrid();
    } catch (err) {
      $("#grid").innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    }
  }

  // ---------- product details and Buy Now
  async function openProduct(id) {
    let p = products.find((x) => x.id === id);
    if (!p) {
      try { p = (await api(`/api/products/${id}`)).product; } catch (err) { return toast(err.message, true); }
    }
    const onSale = p.discount_price != null && p.discount_price < p.price;
    const img = p.image ? `<img src="${esc(p.image)}" alt="${esc(p.name)}">` : `<span class="noimg">No image</span>`;
    const canBuy = p.stock > 0;
    openModal(`
      <div class="detail">
        <div class="thumb">${img}${onSale ? `<span class="badge">Sale</span>` : ""}</div>
        <div>
          <h2 id="modalTitle">${esc(p.name)}</h2>
          <div class="price">${priceHtml(p)}</div>
          ${stockHtml(p.stock)}
          <p class="desc">${esc(p.description) || "Details coming soon."}</p>
          <button class="btn block" id="buyNow" type="button" ${canBuy ? "" : "disabled"}>${canBuy ? "Buy now" : "Out of stock"}</button>
        </div>
      </div>`);
    const buy = $("#buyNow");
    if (buy) buy.onclick = () => orderForm(p);
  }

  function orderForm(p) {
    const unit = p.discount_price ?? p.price;
    const maxQty = Math.min(p.stock, 10);
    const options = Array.from({ length: maxQty }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join("");
    openModal(`
      <h2 id="modalTitle">Place your order</h2>
      <p class="desc" style="margin-top:6px">${esc(p.name)}</p>
      <form id="orderForm" novalidate>
        <input type="hidden" name="product_id" value="${p.id}">
        <div class="row2">
          <div class="field"><label for="f_name">Full name</label><input id="f_name" name="customer_name" autocomplete="name" required maxlength="100"></div>
          <div class="field"><label for="f_mobile">Mobile number</label><input id="f_mobile" name="mobile" type="tel" inputmode="tel" autocomplete="tel" placeholder="10-digit mobile" required maxlength="16"></div>
        </div>
        <div class="field"><label for="f_address">Complete delivery address</label><textarea id="f_address" name="address" autocomplete="street-address" required maxlength="300" placeholder="House no., street, landmark"></textarea></div>
        <div class="row2">
          <div class="field"><label for="f_city">City</label><input id="f_city" name="city" autocomplete="address-level2" required maxlength="80"></div>
          <div class="field"><label for="f_state">State</label><input id="f_state" name="state" list="stateList" autocomplete="address-level1" required maxlength="80"></div>
          <div class="field"><label for="f_pin">PIN code</label><input id="f_pin" name="pin" inputmode="numeric" autocomplete="postal-code" placeholder="6 digits" required maxlength="6"></div>
          <div class="field"><label for="f_qty">Quantity</label><select id="f_qty" name="quantity">${options}</select></div>
        </div>
        <datalist id="stateList">${STATES.map((s) => `<option value="${esc(s)}">`).join("")}</datalist>
        <div class="summary">
          <div><div class="stock">Unit price</div><div>${inr(unit)}</div></div>
          <div style="text-align:right"><div class="stock">Total</div><div class="total" id="orderTotal">${inr(unit)}</div></div>
        </div>
        <div class="error" id="formError" role="alert"></div>
        <button class="btn block" id="submitOrder" type="submit">Place order</button>
        <p class="stock" style="text-align:center;margin-top:12px">Your details are used only to deliver this order.</p>
      </form>`);

    const form = $("#orderForm");
    const qty = $("#f_qty");
    qty.onchange = () => ($("#orderTotal").textContent = inr(unit * Number(qty.value)));

    form.onsubmit = async (e) => {
      e.preventDefault();
      const err = $("#formError");
      const body = Object.fromEntries(new FormData(form).entries());
      body.quantity = Number(body.quantity);
      const btn = $("#submitOrder");
      btn.disabled = true;
      btn.textContent = "Placing order…";
      err.textContent = "";
      try {
        const order = await api("/api/orders", { body });
        confirmation(order);
        loadProductsQuietly();
      } catch (ex) {
        err.textContent = ex.message;
        btn.disabled = false;
        btn.textContent = "Place order";
      }
    };
  }

  function confirmation(order) {
    openModal(`
      <div style="text-align:center;padding:12px 0">
        <div class="pill confirmed" style="margin-bottom:14px">Order received</div>
        <h2 id="modalTitle">Thank you!</h2>
        <p class="desc">Your order has been placed. Please keep your order ID to track it.</p>
        <div class="card-box" style="margin:18px 0;text-align:left">
          <div class="kv">
            <span>Order ID</span><b style="letter-spacing:.06em">${esc(order.order_id)}</b>
            <span>Product</span><span>${esc(order.product_name)} × ${order.quantity}</span>
            <span>Total</span><span>${inr(order.total)}</span>
            <span>Status</span><span><span class="pill pending">Pending</span></span>
          </div>
        </div>
        <div style="display:grid;gap:10px">
          <button class="btn" type="button" id="goTrack">Track this order</button>
          <button class="btn ghost" type="button" id="keepShopping">Continue shopping</button>
        </div>
      </div>`);
    $("#goTrack").onclick = () => { closeModal(); location.hash = "#/track"; };
    $("#keepShopping").onclick = closeModal;
    toast("Order placed successfully");
  }

  async function loadProductsQuietly() {
    try { products = (await api("/api/products")).products; renderGrid(); } catch (_) { /* stock refresh is best-effort */ }
  }

  // ---------- track order
  const STEPS = ["pending", "confirmed", "shipped", "delivered"];
  const LABELS = { pending: "Pending", confirmed: "Confirmed", shipped: "Shipped", delivered: "Delivered", cancelled: "Cancelled" };

  function track() {
    clearInterval(slideTimer);
    app.innerHTML = `
      <section class="narrow">
        <h2>Track your order</h2>
        <p class="desc" style="color:var(--muted);margin:8px 0 22px">Enter the Order ID from your confirmation and the mobile number you used at checkout.</p>
        <div class="card-box">
          <form id="trackForm" novalidate>
            <div class="field"><label for="t_id">Order ID</label><input id="t_id" name="order_id" placeholder="ORD-XXXXXXXXXX" required maxlength="30" autocapitalize="characters"></div>
            <div class="field"><label for="t_mobile">Mobile number</label><input id="t_mobile" name="mobile" type="tel" inputmode="tel" required maxlength="16"></div>
            <div class="error" id="trackError" role="alert"></div>
            <button class="btn block" type="submit">Check status</button>
          </form>
          <div id="trackResult"></div>
        </div>
      </section>`;

    $("#trackForm").onsubmit = async (e) => {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(e.target).entries());
      const errEl = $("#trackError");
      errEl.textContent = "";
      try {
        const o = await api("/api/orders/track", { body });
        const idx = STEPS.indexOf(o.status);
        const cancelled = o.status === "cancelled";
        $("#trackResult").innerHTML = `
          <div class="kv" style="margin-top:22px">
            <span>Order ID</span><b>${esc(o.order_id)}</b>
            <span>Product</span><span>${esc(o.product_name)} × ${o.quantity}</span>
            <span>Total</span><span>${inr(o.total)}</span>
            <span>Placed on</span><span>${esc(o.placed_on)}</span>
            <span>Status</span><span><span class="pill ${esc(o.status)}">${LABELS[o.status] || esc(o.status)}</span></span>
          </div>
          ${cancelled ? "" : `<div class="steps">${STEPS.map((s, i) => `<div class="${i <= idx ? "done" : ""}">${LABELS[s]}</div>`).join("")}</div>`}`;
      } catch (ex) {
        errEl.textContent = ex.message;
      }
    };
  }

  // ---------- router
  function route() {
    clearInterval(slideTimer);
    closeModal();
    const h = location.hash;
    if (h.startsWith("#/track")) return track();
    return home();
  }
  window.addEventListener("hashchange", route);
  route();
})();
