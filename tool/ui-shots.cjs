/* Renders the landing in a real browser and reports what a screenshot cannot:
   console errors, page errors, and horizontal overflow. Driven by ui-shots.sh,
   which supplies the base URL and resolves Playwright; run that, not this. */
"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const base = process.env.SHOTS_BASE || "http://127.0.0.1:8765";
const outDir = process.env.SHOTS_OUT || "build/ui-shots";

const PAGES = { en: "/", es: "/es/" };
const VIEWPORTS = {
  mobile: { width: 360, height: 800 },
  tablet: { width: 768, height: 1024 },
  desktop: { width: 1440, height: 900 },
};

function pick(env, all) {
  const raw = (process.env[env] || "").trim();
  if (!raw) return all;
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

/* The landing is served under a CSP that `python3 -m http.server` does not
   send: `style-src 'self'` drops every inline <style> and style="" attribute,
   and `script-src` allows exactly one inline script by hash. Without this, a
   redesign looks correct locally and loses its styling in production. Read the
   real header out of the nginx snippet and put it back on the local response,
   so a violation shows up here as a console error like any other. */
function productionCsp() {
  try {
    const conf = fs.readFileSync(
      "deploy/nginx/snippets/landing.conf",
      "utf8"
    );
    const m = conf.match(/add_header Content-Security-Policy "([^"]+)"/);
    return m ? m[1] : null;
  } catch (e) {
    return null;
  }
}

/* The same staleness check `.github/workflows/deploy.yml` runs — that workflow
   is the authority; this only moves the failure from after the push to before
   it. Edit the one inline script (the anti-CLS language hint) without
   regenerating the hash in landing.conf and the browser silently drops it. */
function staleCspHashes() {
  const conf = productionCsp();
  if (!conf) return [];
  const declared = new Set(conf.match(/'sha256-[A-Za-z0-9+/=]+'/g) || []);
  const stale = [];
  for (const f of ["index.html", "es/index.html", "404.html"]) {
    let html;
    try {
      html = fs.readFileSync(f, "utf8");
    } catch (e) {
      continue;
    }
    for (const m of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
      const hash =
        "'sha256-" +
        require("crypto").createHash("sha256").update(m[1]).digest("base64") +
        "'";
      if (!declared.has(hash)) stale.push(`${f}: ${hash}`);
    }
  }
  return stale;
}

async function launch() {
  const channel = process.env.SHOTS_CHANNEL || "chrome";
  try {
    return await chromium.launch({ channel });
  } catch (e) {
    console.log(`  note  channel "${channel}" unavailable, using bundled chromium`);
    return await chromium.launch();
  }
}

(async () => {
  const locales = pick("SHOTS_LOCALES", Object.keys(PAGES));
  const viewports = pick("SHOTS_VIEWPORTS", Object.keys(VIEWPORTS));
  const schemes = pick("SHOTS_SCHEMES", ["light"]);
  // Viewport frames by default: a full-page mobile shot is 360×8000 and
  // unreadable once scaled. SHOTS_FULL_PAGE=1 for composition, SHOTS_SELECTOR
  // for one component at a time.
  const fullPage = process.env.SHOTS_FULL_PAGE === "1";
  const selector = (process.env.SHOTS_SELECTOR || "").trim();

  fs.mkdirSync(outDir, { recursive: true });

  const local = /^https?:\/\/(127\.0\.0\.1|localhost)/.test(base);
  const csp = process.env.SHOTS_CSP === "0" || !local ? null : productionCsp();
  if (csp) {
    const stale = staleCspHashes();
    for (const s of stale) {
      console.log(`  WARN  inline script not allowed by the CSP — ${s}`);
      console.log("          add it to deploy/nginx/snippets/landing.conf");
    }
  }
  // The system Chrome first: the npx-cached Playwright is whatever version was
  // last pulled, and its bundled Chromium is often not downloaded. Chrome is
  // installed on this machine and is the browser the page is judged in anyway.
  const browser = await launch();
  let problems = 0;

  for (const locale of locales) {
    const route = PAGES[locale];
    if (!route) {
      console.log(`  SKIP  unknown locale "${locale}"`);
      continue;
    }
    for (const vp of viewports) {
      const size = VIEWPORTS[vp];
      if (!size) {
        console.log(`  SKIP  unknown viewport "${vp}"`);
        continue;
      }
      for (const scheme of schemes) {
        const context = await browser.newContext({
          viewport: size,
          colorScheme: scheme,
          deviceScaleFactor: 2,
          locale: locale === "es" ? "es-ES" : "en-US",
        });
        const page = await context.newPage();
        if (csp) {
          await page.route("**/*", async (route) => {
            const res = await route.fetch();
            const headers = { ...res.headers() };
            if ((headers["content-type"] || "").includes("text/html")) {
              headers["content-security-policy"] = csp;
            }
            await route.fulfill({ response: res, headers });
          });
        }
        const noise = [];
        page.on("console", (m) => {
          if (m.type() === "error" || m.type() === "warning") {
            noise.push(`${m.type()}: ${m.text()}`);
          }
        });
        page.on("pageerror", (e) => noise.push(`pageerror: ${e.message}`));
        page.on("requestfailed", (r) =>
          noise.push(`requestfailed: ${r.url()} ${r.failure()?.errorText || ""}`)
        );

        const url = base + route;
        const response = await page.goto(url, { waitUntil: "networkidle" });
        const overflow = await page.evaluate(() => ({
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
          title: document.title,
          h1: document.querySelector("h1")?.textContent?.trim() || null,
        }));

        const name = `${locale}-${vp}-${scheme}`;
        const file = path.join(outDir, `${name}.png`);
        if (selector) {
          await page.locator(selector).first().screenshot({ path: file });
        } else {
          await page.screenshot({ path: file, fullPage });
        }

        const flags = [];
        if (!response || !response.ok()) flags.push(`http=${response?.status()}`);
        if (overflow.scrollWidth > overflow.innerWidth + 1) {
          flags.push(`h-overflow ${overflow.scrollWidth}>${overflow.innerWidth}`);
        }
        if (noise.length) flags.push(`console=${noise.length}`);
        if (flags.length) problems++;

        console.log(
          `  ${flags.length ? "WARN" : "ok  "}  ${name.padEnd(22)} ${file}` +
            (flags.length ? `  [${flags.join(", ")}]` : "")
        );
        for (const n of noise.slice(0, 8)) console.log(`          ${n}`);

        await context.close();
      }
    }
  }

  await browser.close();
  console.log(
    problems ? `\n${problems} render(s) need a look.` : "\nno console errors, no horizontal overflow."
  );
  process.exit(problems ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(2);
});
