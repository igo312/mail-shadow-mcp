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
})();
