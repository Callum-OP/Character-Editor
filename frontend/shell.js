/* Shared app shell for the Character Editor.
   Injects the top navigation bar on every page, exposes a global toast()
   helper, and keeps custom range sliders' gradient fill in sync. Plain
   (non-module) script so it runs identically on all tool pages. */
(function () {
  "use strict";

  var TOOLS = [
    { href: "/",              ic: "⌂", lbl: "Home" },
    { href: "/topology.html", ic: "▦", lbl: "Topology" },
    { href: "/wrap.html",     ic: "‹›", lbl: "Shape Match" },
    { href: "/rig.html",      ic: "⚹", lbl: "Rigger" },
    { href: "/face.html",     ic: "☺", lbl: "Face" },
    { href: "/cloth.html",    ic: "〜", lbl: "Cloth" },
  ];

  function currentFile() {
    var p = location.pathname.replace(/\/+$/, "");
    if (p === "" || /\/index\.html$/i.test(p)) return "/";
    var m = p.match(/[^/]+$/);
    return m ? "/" + m[0] : "/";
  }

  function buildAppbar() {
    if (document.querySelector(".appbar")) return;
    var here = currentFile();
    var bar = document.createElement("header");
    bar.className = "appbar";

    var brand = document.createElement("a");
    brand.className = "brand";
    brand.href = "/";
    brand.innerHTML =
      '<span class="mark">◈</span>' +
      '<span><b>Character</b> Editor</span>' +
      '<span class="brand-sub">3D toolkit</span>';
    bar.appendChild(brand);

    var nav = document.createElement("nav");
    TOOLS.forEach(function (t) {
      if (t.href === "/") return; // brand already links home
      var a = document.createElement("a");
      a.href = t.href;
      if (t.href === here) a.className = "active";
      a.innerHTML = '<span class="ic">' + t.ic + '</span><span class="lbl">' + t.lbl + "</span>";
      nav.appendChild(a);
    });
    bar.appendChild(nav);

    var spacer = document.createElement("span");
    spacer.className = "spacer";
    bar.appendChild(spacer);

    document.body.insertBefore(bar, document.body.firstChild);
  }

  /* -------------------------------------------------------- toasts --------- */
  function ensureToastHost() {
    var host = document.getElementById("toasts");
    if (!host) {
      host = document.createElement("div");
      host.id = "toasts";
      document.body.appendChild(host);
    }
    return host;
  }

  window.toast = function (msg, type, ms) {
    var host = ensureToastHost();
    var el = document.createElement("div");
    el.className = "toast" + (type ? " " + type : "");
    el.textContent = msg;
    host.appendChild(el);
    var life = ms || 3200;
    setTimeout(function () {
      el.classList.add("leaving");
      setTimeout(function () { el.remove(); }, 220);
    }, life);
    return el;
  };

  /* ------------------------------------------------ range slider fill ------ */
  function paintRange(r) {
    if (r.type !== "range") return;
    var min = parseFloat(r.min || 0), max = parseFloat(r.max || 100);
    var val = parseFloat(r.value);
    var pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
    r.style.setProperty("--range-fill", pct + "%");
  }
  function paintAll(root) {
    (root || document).querySelectorAll('input[type="range"]').forEach(paintRange);
  }

  document.addEventListener("input", function (e) {
    if (e.target && e.target.type === "range") paintRange(e.target);
  });

  function init() {
    buildAppbar();
    paintAll(document);
    // sliders added later (e.g. face shape keys) get painted on appearance
    var mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        muts[i].addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          if (n.matches && n.matches('input[type="range"]')) paintRange(n);
          else if (n.querySelectorAll) paintAll(n);
        });
      }
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
