// Migrate existing Jellyfin 12.2 browsers to the layout shown in Abyss previews.
// Runs before Jellyfin bundles, once per browser origin; later choices are respected.
(function () {
  "use strict";
  try {
    var marker = "homeserver.abyss.legacy-layout-v1";
    if (localStorage.getItem(marker) !== "1") {
      var layout = localStorage.getItem("layout");
      var agent = navigator.userAgent || "";
      var tv = /SmartTV|Smart-TV|HbbTV|Tizen|Web0S|WebOS|NetCast|Android TV|Google ?TV|Fire ?TV|AFT[A-Z0-9]+/i.test(agent);
      if (!tv && [null, "", "auto", "modern", "desktop", "mobile"].indexOf(layout) !== -1) {
        var mobile = layout === "mobile" || (layout !== "desktop" && /Android|iPhone|iPad|iPod|Mobile/i.test(agent));
        localStorage.setItem("layout", mobile
          ? "mobile-legacy" : "desktop-legacy");
      }
      // A failed layout write must remain retryable on the next page load.
      localStorage.setItem(marker, "1");
    }
  } catch (error) { /* Restricted browser storage must not prevent login. */ }

  function seedBackdrops() {
    try {
      var credentials = JSON.parse(localStorage.getItem("jellyfin_credentials") || "{}");
      (credentials.Servers || []).forEach(function (server) {
        var id = server.UserId;
        if (typeof id !== "string" || !/^[a-f0-9-]{32,36}$/i.test(id)) return;
        var key = id + "-enableBackdrops";
        if (localStorage.getItem(key) === null) localStorage.setItem(key, "true");
      });
    } catch (error) { /* Missing credentials are normal before first login. */ }
  }
  seedBackdrops();
  window.addEventListener("hashchange", seedBackdrops);
  window.addEventListener("storage", seedBackdrops);
})();
