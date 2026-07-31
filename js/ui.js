/* ============================================================================
   Progressive enhancement only. Everything below is optional polish — with
   JavaScript off the page keeps every link, both language versions, and the
   whole FAQ (native <details>) working.
   ========================================================================= */
(function () {
  "use strict";

  /* --- Language hint ------------------------------------------------------
     A soft offer, never a redirect. Auto-redirecting by Accept-Language hides
     one version from crawlers and traps anyone who deliberately picked the
     other one; the header switch stays the real control.

     Whether to show it is decided by the inline script next to the element so
     it costs no layout shift. All that is left here is dismissing it. */
  var hint = document.getElementById("lang-hint");
  if (hint) {
    var DISMISSED = "dosebuddy-lang-hint";
    var close = hint.querySelector("[data-hint-close]");
    if (close) {
      close.addEventListener("click", function () {
        hint.hidden = true;
        try {
          sessionStorage.setItem(DISMISSED, "1");
        } catch (e) {
          /* ignore */
        }
      });
    }
  }

  /* --- Lazy YouTube facade ------------------------------------------------
     Dormant until a video is added. Drop this into any section and nothing
     else has to change:

       <div class="video" data-yt="VIDEO_ID" data-poster="/img/video-poster.webp"
            data-label="Play the DoseBuddy overview"></div>

     Nothing is requested from Google until the visitor clicks, and the embed
     then uses youtube-nocookie.com. */
  var videos = document.querySelectorAll(".video[data-yt]");
  for (var i = 0; i < videos.length; i++) {
    (function (box) {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = box.dataset.label || "Play video";

      if (box.dataset.poster) {
        var poster = document.createElement("img");
        poster.src = box.dataset.poster;
        poster.alt = "";
        poster.loading = "lazy";
        poster.decoding = "async";
        box.appendChild(poster);
      }

      button.addEventListener("click", function () {
        var frame = document.createElement("iframe");
        frame.src =
          "https://www.youtube-nocookie.com/embed/" +
          encodeURIComponent(box.dataset.yt) +
          "?autoplay=1&rel=0";
        frame.title = box.dataset.label || "Video";
        frame.allow = "accelerometer; autoplay; encrypted-media; picture-in-picture";
        frame.allowFullscreen = true;
        box.textContent = "";
        box.appendChild(frame);
      });

      box.appendChild(button);
    })(videos[i]);
  }
})();
