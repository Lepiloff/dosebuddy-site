/* ============================================================================
   Cookie consent + Google Analytics 4 (Consent Mode v2)
   ----------------------------------------------------------------------------
   The page ships with no analytics on it. gtag.js is injected only after the
   visitor accepts, so a visitor who declines (or never answers) causes exactly
   zero requests to Google and zero cookies.

   Consent Mode v2 signals are queued into dataLayer before the library loads,
   so whenever GA does start it already knows what it is allowed to do.

   Withdrawing is not the mirror image of granting. Once gtag.js is on the
   page, a consent update stops it storing cookies but does not unload it, so
   declining after having accepted also clears the GA cookies and reloads into
   a state where the script is never injected again.

   GA_MEASUREMENT_ID below is live. If it is ever put back to a placeholder the
   banner hides itself, because nothing would be collected and asking consent
   for nothing is misleading; ?consent-preview=1 forces it visible.
   ========================================================================= */
(function () {
  "use strict";

  var GA_MEASUREMENT_ID = "G-LE5K9MMGRG";

  var STORAGE_KEY = "dosebuddy-consent";
  var banner = document.getElementById("consent");
  if (!banner) return;

  var configured = /^G-[A-Z0-9]{6,}$/.test(GA_MEASUREMENT_ID) && GA_MEASUREMENT_ID !== "G-XXXXXXXXXX";
  var preview = location.search.indexOf("consent-preview=1") !== -1;

  window.dataLayer = window.dataLayer || [];
  function gtag() {
    window.dataLayer.push(arguments);
  }

  // Everything denied until told otherwise, including on repeat visits, since
  // the update below only ever relaxes what the visitor explicitly allowed.
  gtag("consent", "default", {
    ad_storage: "denied",
    ad_user_data: "denied",
    ad_personalization: "denied",
    analytics_storage: "denied",
    functionality_storage: "granted",
    security_storage: "granted"
  });

  function read() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null; // private mode / storage blocked, treat as "not answered"
    }
  }

  function write(value) {
    try {
      localStorage.setItem(STORAGE_KEY, value);
    } catch (e) {
      /* nothing to do: the choice simply is not remembered */
    }
  }

  var loaded = false;
  function loadAnalytics() {
    if (loaded || !configured) return;
    loaded = true;

    gtag("consent", "update", { analytics_storage: "granted" });

    var s = document.createElement("script");
    s.async = true;
    s.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(GA_MEASUREMENT_ID);
    document.head.appendChild(s);

    gtag("js", new Date());
    gtag("config", GA_MEASUREMENT_ID);
  }

  function show() {
    banner.hidden = false;
  }

  function hide() {
    banner.hidden = true;
  }

  /* Google's own cookies. Names are documented and stable: _ga plus one
     _ga_<container> per measurement id. */
  function clearAnalyticsCookies() {
    var names = document.cookie.split(";").map(function (c) {
      return c.split("=")[0].trim();
    });
    var host = location.hostname;
    for (var i = 0; i < names.length; i++) {
      if (names[i] !== "_ga" && names[i].indexOf("_ga_") !== 0) continue;
      // Delete on the exact host and on the registrable domain, since GA sets
      // its cookie on the latter and the two are different jars.
      var scopes = ["", "; domain=" + host, "; domain=." + host];
      var parent = host.split(".").slice(-2).join(".");
      if (parent !== host) scopes.push("; domain=." + parent);
      for (var s = 0; s < scopes.length; s++) {
        document.cookie =
          names[i] + "=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT" + scopes[s];
      }
    }
  }

  function decide(value) {
    write(value);
    hide();
    if (value === "granted") {
      loadAnalytics();
      return;
    }
    if (loaded) {
      // gtag.js is already on the page. Updating consent stops new storage but
      // leaves the library running, so signal the withdrawal, drop the cookies
      // it set, and reload into the state where it is never injected.
      gtag("consent", "update", {
        ad_storage: "denied",
        ad_user_data: "denied",
        ad_personalization: "denied",
        analytics_storage: "denied"
      });
      clearAnalyticsCookies();
      location.reload();
    }
  }

  banner.addEventListener("click", function (event) {
    var button = event.target.closest("[data-consent]");
    if (button) decide(button.dataset.consent);
  });

  // Withdrawing consent has to be as easy as giving it (GDPR art. 7(3)), so the
  // footer link reopens this banner on every page.
  var reopen = document.querySelectorAll("[data-consent-reopen]");
  for (var i = 0; i < reopen.length; i++) {
    reopen[i].addEventListener("click", function (event) {
      event.preventDefault();
      show();
      var first = banner.querySelector("[data-consent]");
      if (first) first.focus();
    });
  }

  var stored = read();
  if (stored === "granted") {
    loadAnalytics();
  } else if (stored !== "denied" && (configured || preview)) {
    show();
  }
})();
