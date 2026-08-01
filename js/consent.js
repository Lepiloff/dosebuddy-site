/* ============================================================================
   Cookie consent + Google Analytics 4 (Consent Mode v2)
   ----------------------------------------------------------------------------
   The page ships with no analytics on it. gtag.js is injected only after the
   visitor accepts, so a visitor who declines (or never answers) causes exactly
   zero requests to Google and zero cookies.

   Consent Mode v2 signals are queued into dataLayer before the library loads,
   so whenever GA does start it already knows what it is allowed to do.

   OWNER: replace GA_MEASUREMENT_ID with the real ID from
   Google Analytics → Admin → Data streams → Web. While it is still the
   placeholder the banner stays hidden, because nothing is being collected and
   asking for consent to nothing would be misleading. To preview the banner
   anyway, load any page with ?consent-preview=1
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

  function decide(value) {
    write(value);
    hide();
    if (value === "granted") loadAnalytics();
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
