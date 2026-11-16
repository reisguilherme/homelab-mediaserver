// Jellyfin 12.2 device defaults for the layout shown in Abyss previews.
// Runs before Jellyfin bundles; explicit device preferences remain authoritative.
(function () {
  "use strict";
  try {
    var layout = localStorage.getItem("layout");
    if (layout === null) {
      var agent = navigator.userAgent || "";
      if (!/SmartTV|Smart-TV|HbbTV|Tizen|Web0S|WebOS|NetCast/i.test(agent)) {
        localStorage.setItem("layout", /Android|iPhone|iPad|iPod|Mobile/i.test(agent)
          ? "mobile-legacy" : "desktop-legacy");
      }
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
