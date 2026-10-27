(function () {
  "use strict";

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy") ? resolve() : reject(new Error("copy failed"));
      } catch (err) {
        reject(err);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  document.addEventListener("click", function (event) {
    var copyButton = event.target.closest("[data-copy]");
    if (copyButton) {
      var target = document.getElementById(copyButton.getAttribute("data-copy"));
      if (!target) return;
      copyText(target.textContent || "").then(
        function () {
          var label = copyButton.textContent;
          copyButton.textContent = "已复制 ✓";
          copyButton.classList.add("copied");
          window.setTimeout(function () {
            copyButton.textContent = label;
            copyButton.classList.remove("copied");
          }, 1600);
        },
        function () {
          copyButton.textContent = "复制失败";
        }
      );
      return;
    }

    var tab = event.target.closest("[data-tab]");
    if (tab) {
      var scope = tab.closest(".config-card") || document;
      scope.querySelectorAll("[data-tab]").forEach(function (item) {
        var selected = item === tab;
        item.classList.toggle("active", selected);
        item.setAttribute("aria-selected", selected ? "true" : "false");
      });
      scope.querySelectorAll(".tab-panel").forEach(function (panel) {
        panel.classList.toggle("active", panel.id === tab.getAttribute("data-tab"));
      });
    }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.getAttribute("action") !== "/provision") return;
    var button = form.querySelector("button[type=submit]");
    if (!button) return;
    button.classList.add("is-loading");
    var label = button.querySelector(".btn-label");
    if (label) label.textContent = "正在验证并启动…";
    window.setTimeout(function () {
      button.classList.remove("is-loading");
      if (label) label.textContent = "验证并创建实例";
    }, 180000);
  });

  // ---------------------------------------------------------------------
  // Folder picker: fetch the mailbox's folders once, then let the user pick.
  // The list only needs ~0.2s on Aliyun/QQ/Gmail, so the spinner is brief; it
  // exists mainly so a slow server never looks like a frozen page.
  // ---------------------------------------------------------------------
  var picker = document.querySelector("[data-folder-picker]");
  if (picker) {
    initPicker(picker);
  }

  function initPicker(root) {
    var form = root.closest("form");
    if (!form) return;
    var fetchBtn = root.querySelector("[data-folder-fetch]");
    var listEl = root.querySelector("[data-folder-list]");
    var toolbar = root.querySelector("[data-folder-toolbar]");
    var statusEl = root.querySelector("[data-folder-status]");
    var countEl = root.querySelector("[data-folder-count]");
    var filterEl = root.querySelector("[data-folder-filter]");
    var usedEl = root.querySelector("[data-folder-used]");
    var allEl = root.querySelector("[data-folder-mode-all]");
    var state = { loaded: false, busy: false };

    function field(name) {
      return form.querySelector('[name="' + name + '"]');
    }

    function setStatus(text, kind) {
      statusEl.textContent = text || "";
      statusEl.className = "picker-status" + (kind ? " picker-status-" + kind : "");
    }

    function boxes() {
      return Array.prototype.slice.call(
        listEl.querySelectorAll('input[type="checkbox"]')
      );
    }

    function updateCount() {
      if (allEl && allEl.checked) {
        countEl.textContent = "全部文件夹";
        return;
      }
      var n = boxes().filter(function (box) { return box.checked; }).length;
      countEl.textContent = n ? "已选 " + n + " 个" : "未选择";
    }

    function syncDisabled() {
      var all = !!(allEl && allEl.checked);
      boxes().forEach(function (box) {
        box.disabled = all;
        if (all) box.checked = false;
      });
      if (filterEl) filterEl.disabled = all;
      updateCount();
    }

    function render(folders, current) {
      listEl.textContent = "";
      folders.forEach(function (name) {
        var label = document.createElement("label");
        label.className = "picker-item";
        var box = document.createElement("input");
        box.type = "checkbox";
        box.name = "folders";
        box.value = name;
        box.addEventListener("change", updateCount);
        var text = document.createElement("span");
        text.textContent = name;
        label.appendChild(box);
        label.appendChild(text);
        listEl.appendChild(label);
      });
      // Server-rendered presets (after a failed submit) are replaced by the
      // real checkboxes; drop them so the field is not submitted twice.
      form.querySelectorAll("[data-folder-preset]").forEach(function (el) {
        el.parentNode.removeChild(el);
      });
      listEl.hidden = false;
      if (toolbar) toolbar.hidden = false;
      usedEl.value = "1";
      state.loaded = true;

      if (current && current.length === 0) {
        if (allEl) allEl.checked = true;
        syncDisabled();
        return;
      }
      var pre = current && current.length ? current : ["INBOX", "已发送", "Sent", "Sent Items"];
      var wanted = {};
      pre.forEach(function (name) { wanted[name] = true; });
      boxes().forEach(function (box) {
        box.checked = !!wanted[box.value];
      });
      if (allEl) allEl.checked = false;
      syncDisabled();
    }

    function markInvalid(name) {
      var el = name ? field(name) : null;
      if (!el) return;
      el.classList.add("input-invalid");
      el.focus();
      window.setTimeout(function () { el.classList.remove("input-invalid"); }, 4000);
    }

    function fetchFolders() {
      if (state.busy) return;
      var email = field("email");
      var password = field("password");
      if (email && !email.value.trim()) {
        setStatus("先填写邮箱地址。", "warn");
        email.focus();
        return;
      }
      if (password && !password.value) {
        setStatus("先填写 IMAP 授权码。", "warn");
        password.focus();
        return;
      }

      var body = new URLSearchParams();
      var csrf = field("csrf");
      body.set("csrf", csrf ? csrf.value : "");
      ["email", "password", "host", "port", "tls_mode", "provider"].forEach(function (name) {
        var el = field(name);
        if (el) body.set(name, el.value);
      });

      state.busy = true;
      fetchBtn.classList.add("is-loading");
      setStatus("正在连接邮箱并读取文件夹…", "info");

      fetch("/folders", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        body: body.toString()
      })
        .then(function (response) {
          return response.json().catch(function () {
            return { ok: false, error: "服务器返回了无法解析的响应（HTTP " + response.status + "）。" };
          });
        })
        .then(function (data) {
          state.busy = false;
          fetchBtn.classList.remove("is-loading");
          if (!data || !data.ok) {
            setStatus((data && data.error) || "获取文件夹失败，请稍后重试。", "error");
            markInvalid(data && data.field);
            return;
          }
          render(data.folders || [], data.current);
          setStatus(
            "已读取 " + (data.count || 0) + " 个文件夹（" + (data.elapsed_ms || 0) + " ms）。取消勾选不需要的，然后提交。",
            "ok"
          );
        })
        .catch(function () {
          state.busy = false;
          fetchBtn.classList.remove("is-loading");
          setStatus("网络错误，请重试。", "error");
        });
    }

    if (fetchBtn) fetchBtn.addEventListener("click", fetchFolders);

    if (allEl) allEl.addEventListener("change", syncDisabled);

    var selectAll = root.querySelector("[data-folder-all]");
    if (selectAll) {
      selectAll.addEventListener("click", function () {
        if (allEl && allEl.checked) return;
        boxes().forEach(function (box) {
          if (!box.closest(".picker-item").hidden) box.checked = true;
        });
        updateCount();
      });
    }

    var selectNone = root.querySelector("[data-folder-none]");
    if (selectNone) {
      selectNone.addEventListener("click", function () {
        boxes().forEach(function (box) { box.checked = false; });
        updateCount();
      });
    }

    if (filterEl) {
      filterEl.addEventListener("input", function () {
        var needle = filterEl.value.trim().toLowerCase();
        boxes().forEach(function (box) {
          var item = box.closest(".picker-item");
          var haystack = (box.value || "").toLowerCase();
          item.hidden = needle !== "" && haystack.indexOf(needle) === -1;
        });
        updateCount();
      });
    }

    // A failed submit re-rendered the page with the previous selection: show
    // it as a hint until the user fetches the fresh list.
    var presets = form.querySelectorAll("[data-folder-preset]");
    if (presets.length) {
      var names = Array.prototype.map.call(presets, function (el) { return el.value; });
      usedEl.value = "1";
      setStatus("当前选择：" + names.join("、") + "（点击「获取文件夹列表」可以重新勾选）", "info");
    }
  }
})();
