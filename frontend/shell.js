/* Shared app shell for the Character Editor.
   - Injects the top navigation bar + project chip on every page.
   - Exposes window.Project: a persistent, server-backed workspace that carries
     a model between tools (retopo -> wrap -> rig -> face -> cloth). Tools save
     their result as the project's new "current" model and can pull that current
     model as their input via a [data-project-use] button + `project:use-model`.
   - Exposes window.toast() and keeps custom range sliders' gradient fill in sync.
   Plain (non-module) script so it runs identically on all tool pages. */
(function () {
  "use strict";

  var TOOLS = [
    { href: "/topology.html", ic: "▦", lbl: "Topology" },
    { href: "/wrap.html",     ic: "‹›", lbl: "Shape Match" },
    { href: "/rig.html",      ic: "⚹", lbl: "Rigger" },
    { href: "/face.html",     ic: "☺", lbl: "Face" },
    { href: "/cloth.html",    ic: "〜", lbl: "Cloth" },
  ];
  var LS_KEY = "ce.activeProject";

  function currentFile() {
    var p = location.pathname.replace(/\/+$/, "");
    if (p === "" || /\/index\.html$/i.test(p)) return "/";
    var m = p.match(/[^/]+$/);
    return m ? "/" + m[0] : "/";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* ------------------------------------------------------------ app bar ---- */
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

    var chip = document.createElement("div");
    chip.className = "proj";
    chip.id = "projChip";
    chip.innerHTML =
      '<button class="proj-btn" type="button" aria-haspopup="true">' +
      '<span class="proj-ic">◆</span><span class="proj-txt"></span><span class="proj-caret">▾</span>' +
      "</button><div class=\"proj-menu hidden\"></div>";
    bar.appendChild(chip);
    chip.querySelector(".proj-btn").addEventListener("click", toggleMenu);

    document.body.insertBefore(bar, document.body.firstChild);
  }

  /* ---------------------------------------------------------- Project ------ */
  var state = { id: null, manifest: null };

  function dispatchChange() {
    window.dispatchEvent(new CustomEvent("project:change",
      { detail: { id: Project.id(), manifest: state.manifest } }));
  }

  var Project = {
    id: function () { return localStorage.getItem(LS_KEY); },
    _set: function (pid) { pid ? localStorage.setItem(LS_KEY, pid) : localStorage.removeItem(LS_KEY); },
    manifest: function () { return state.manifest; },

    current: function () {
      var m = state.manifest;
      if (!m || !m.current) return null;
      for (var i = 0; i < m.assets.length; i++) if (m.assets[i].id === m.current) return m.assets[i];
      return null;
    },

    list: function () {
      return fetch("/api/project").then(function (r) { return r.json(); }).catch(function () { return []; });
    },

    create: function (name) {
      return fetch("/api/project", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name || "Untitled project" }),
      }).then(function (r) { return r.json(); }).then(function (m) {
        Project._set(m.id); state = { id: m.id, manifest: m }; sync(); dispatchChange(); return m;
      });
    },

    ensure: function () {
      if (Project.id() && state.manifest) return Promise.resolve(Project.id());
      if (Project.id()) return Project.refresh().then(function (m) { return m ? Project.id() : Project.create("Project 1").then(function (n) { return n.id; }); });
      return Project.list().then(function (l) { return Project.create("Project " + ((l ? l.length : 0) + 1)); }).then(function (m) { return m.id; });
    },

    setActive: function (pid) { Project._set(pid); return Project.refresh().then(dispatchChange); },
    clear: function () { Project._set(null); state = { id: null, manifest: null }; sync(); dispatchChange(); return Promise.resolve(); },

    rename: function (name) {
      var id = Project.id(); if (!id) return Promise.resolve();
      return fetch("/api/project/" + id, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name }),
      }).then(function (r) { return r.json(); }).then(function (m) { state.manifest = m; sync(); dispatchChange(); return m; });
    },

    del: function (pid) {
      return fetch("/api/project/" + pid, { method: "DELETE" }).then(function () {
        if (pid === Project.id()) Project._set(null);
        return Project.refresh().then(dispatchChange);
      });
    },

    refresh: function () {
      var id = Project.id();
      if (!id) { state = { id: null, manifest: null }; sync(); return Promise.resolve(null); }
      return fetch("/api/project/" + id).then(function (r) { if (!r.ok) throw 0; return r.json(); })
        .then(function (m) { state = { id: id, manifest: m }; sync(); return m; })
        .catch(function () { Project._set(null); state = { id: null, manifest: null }; sync(); return null; });
    },

    // Save a tool result as the project's new current model (auto-creates a
    // project if none is active). opts: { url? | blob?, name, tool }
    saveResult: function (opts) {
      return Project.ensure().then(function (id) {
        var blobP = opts.blob ? Promise.resolve(opts.blob) : fetch(opts.url).then(function (r) { return r.blob(); });
        return blobP.then(function (blob) {
          var fd = new FormData();
          fd.append("file", new File([blob], opts.name || "model.glb"));
          fd.append("tool", opts.tool || "Tool");
          fd.append("name", opts.name || "model.glb");
          return fetch("/api/project/" + id + "/assets", { method: "POST", body: fd }).then(function (r) { return r.json(); });
        }).then(function (res) {
          state = { id: id, manifest: res.project }; sync(); dispatchChange();
          if (window.toast) toast("Saved “" + (opts.name || "model") + "” to project “" + res.project.name + "”", "ok");
          return res;
        });
      }).catch(function (e) { if (window.toast) toast("Couldn't save to project: " + (e.message || e), "err"); });
    },

    // Fetch the project's current model as a File, for use as a tool input.
    getCurrentFile: function () {
      var id = Project.id(); if (!id) return Promise.resolve(null);
      var cur = Project.current();
      return fetch("/api/project/" + id + "/current").then(function (r) { if (!r.ok) throw 0; return r.blob(); })
        .then(function (blob) { return new File([blob], (cur && cur.name) || "model.glb"); })
        .catch(function () { return null; });
    },

    // Upload a user-chosen model file straight into a project (defaults to the
    // active one) and make it the current model. Rejects on unsupported format.
    uploadTo: function (pid, file) {
      pid = pid || Project.id();
      if (!pid) return Promise.reject(new Error("no project"));
      var fd = new FormData();
      fd.append("file", file);
      fd.append("tool", "Upload");
      fd.append("name", file.name || "model.glb");
      return fetch("/api/project/" + pid + "/assets", { method: "POST", body: fd })
        .then(function (r) {
          if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || ("HTTP " + r.status)); },
                                          function () { throw new Error("HTTP " + r.status); });
          return r.json();
        })
        .then(function (res) {
          if (pid === Project.id()) { state = { id: pid, manifest: res.project }; sync(); }
          dispatchChange();
          if (window.toast) toast("Added “" + res.asset.name + "” to “" + res.project.name + "”", "ok");
          return res;
        });
    },

    // Open a file picker and upload the chosen model into the given project.
    pickAndUpload: function (pid) {
      return new Promise(function (resolve) {
        pickModelFile(function (f) {
          Project.uploadTo(pid, f).then(resolve, function (e) {
            if (window.toast) toast("Upload failed: " + (e.message || e), "err");
            resolve(null);
          });
        });
      });
    },
  };
  window.Project = Project;

  // Hidden file input helper for "upload a model" affordances.
  function pickModelFile(cb) {
    var inp = document.createElement("input");
    inp.type = "file";
    inp.accept = ".obj,.glb,.gltf,.fbx,.ply,.stl";
    inp.style.display = "none";
    document.body.appendChild(inp);
    inp.addEventListener("change", function () {
      var f = inp.files && inp.files[0];
      inp.remove();
      if (f) cb(f);
    });
    inp.click();
  }

  /* --------------------------------------------------------- chip + menu --- */
  function sync() { renderChip(); toggleUseButtons(); }

  function renderChip() {
    var chip = document.getElementById("projChip");
    if (!chip) return;
    var txt = chip.querySelector(".proj-txt");
    var m = state.manifest, cur = Project.current();
    if (m) {
      txt.innerHTML = "<b>" + esc(m.name) + "</b>" +
        (cur ? '<span class="proj-cur"> · ' + esc(cur.name) + "</span>"
             : '<span class="proj-cur faint"> · empty</span>');
      chip.classList.add("has");
    } else {
      txt.innerHTML = '<span class="faint">No project</span>';
      chip.classList.remove("has");
    }
  }

  function toggleMenu(e) {
    e.stopPropagation();
    var menu = document.querySelector(".proj-menu");
    if (!menu.classList.contains("hidden")) { menu.classList.add("hidden"); return; }
    buildMenu(menu).then(function () { menu.classList.remove("hidden"); });
  }

  function buildMenu(menu) {
    return Project.list().then(function (list) {
      var id = Project.id();
      var html = '<div class="proj-menu-hd">Projects</div>';
      if (!list.length) html += '<div class="proj-empty">No saved projects yet. Create one, then upload or generate a model.</div>';
      list.forEach(function (p) {
        html += '<div class="proj-item' + (p.id === id ? " active" : "") + '" data-open="' + p.id + '">' +
          '<span class="proj-item-main"><span class="proj-item-name">' + esc(p.name) + "</span>" +
          '<span class="proj-item-sub">' + (p.current ? esc(p.current) : p.assetCount + " asset(s)") + "</span></span>" +
          '<button class="proj-del" data-del="' + p.id + '" title="Delete project" type="button">✕</button></div>';
      });
      html += '<div class="proj-menu-actions">' +
        '<button class="seg" type="button" data-act="new">＋ New</button>' +
        (id ? '<button class="seg" type="button" data-act="upload">⬆ Upload model</button>' +
              '<button class="seg" type="button" data-act="rename">Rename</button>' +
              '<button class="seg" type="button" data-act="clear">Close</button>' : "") +
        "</div>";
      menu.innerHTML = html;

      menu.querySelectorAll("[data-open]").forEach(function (row) {
        row.addEventListener("click", function (ev) {
          if (ev.target.hasAttribute("data-del")) return;
          Project.setActive(row.getAttribute("data-open")).then(function () { menu.classList.add("hidden"); });
        });
      });
      menu.querySelectorAll("[data-del]").forEach(function (b) {
        b.addEventListener("click", function (ev) {
          ev.stopPropagation();
          if (confirm("Delete this project and its saved models?")) Project.del(b.getAttribute("data-del")).then(function () { buildMenu(menu); });
        });
      });
      var actNew = menu.querySelector('[data-act="new"]');
      if (actNew) actNew.addEventListener("click", function () {
        var name = prompt("New project name:", "Project");
        if (name !== null) Project.create(name || "Untitled project").then(function () { menu.classList.add("hidden"); });
      });
      var actUp = menu.querySelector('[data-act="upload"]');
      if (actUp) actUp.addEventListener("click", function () {
        Project.pickAndUpload(id).then(function () { menu.classList.add("hidden"); });
      });
      var actRen = menu.querySelector('[data-act="rename"]');
      if (actRen) actRen.addEventListener("click", function () {
        var name = prompt("Rename project:", state.manifest ? state.manifest.name : "");
        if (name) Project.rename(name).then(function () { buildMenu(menu); });
      });
      var actClr = menu.querySelector('[data-act="clear"]');
      if (actClr) actClr.addEventListener("click", function () { Project.clear().then(function () { menu.classList.add("hidden"); }); });
    });
  }

  document.addEventListener("click", function () {
    var mn = document.querySelector(".proj-menu");
    if (mn) mn.classList.add("hidden");
  });

  /* --------------------------------------------- "use current model" ------- */
  function toggleUseButtons() {
    var cur = Project.current();
    document.querySelectorAll("[data-project-use]").forEach(function (b) {
      b.classList.toggle("hidden", !cur);
      if (cur) b.title = "Load “" + cur.name + "” from " + (state.manifest ? "“" + state.manifest.name + "”" : "the project");
    });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-project-use]");
    if (!b) return;
    e.preventDefault();
    Project.getCurrentFile().then(function (f) {
      if (f) window.dispatchEvent(new CustomEvent("project:use-model", { detail: { file: f } }));
      else if (window.toast) toast("No current model in this project yet.", "err");
    });
  });

  /* -------------------------------------------------------------- toasts --- */
  function ensureToastHost() {
    var host = document.getElementById("toasts");
    if (!host) { host = document.createElement("div"); host.id = "toasts"; document.body.appendChild(host); }
    return host;
  }
  window.toast = function (msg, type, ms) {
    var host = ensureToastHost();
    var el = document.createElement("div");
    el.className = "toast" + (type ? " " + type : "");
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(function () {
      el.classList.add("leaving");
      setTimeout(function () { el.remove(); }, 220);
    }, ms || 3200);
    return el;
  };

  /* ------------------------------------------------ range slider fill ------ */
  function paintRange(r) {
    if (r.type !== "range") return;
    var min = parseFloat(r.min || 0), max = parseFloat(r.max || 100), val = parseFloat(r.value);
    var pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
    r.style.setProperty("--range-fill", pct + "%");
  }
  function paintAll(root) { (root || document).querySelectorAll('input[type="range"]').forEach(paintRange); }
  document.addEventListener("input", function (e) { if (e.target && e.target.type === "range") paintRange(e.target); });

  /* ---------------------------------------------------------------- init --- */
  function init() {
    buildAppbar();
    paintAll(document);
    Project.refresh();
    var mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        muts[i].addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          if (n.matches && n.matches('input[type="range"]')) paintRange(n);
          else if (n.querySelectorAll) { paintAll(n); if (n.querySelector("[data-project-use]")) toggleUseButtons(); }
        });
      }
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
