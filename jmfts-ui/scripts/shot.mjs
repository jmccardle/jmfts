// Screenshot the console against a running appliance, for looking at the design.
//
//   node scripts/shot.mjs <token> <out-dir> [route ...]
//
// Development only: it needs a Chrome on PATH and the dev server up. It seeds the token into
// `sessionStorage` before the first script runs, which is the same place the page itself keeps
// it — no dev-only door is opened in the application for this.

import { mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import puppeteer from "puppeteer-core";

const [token, outDir, ...routes] = process.argv.slice(2);
const base = process.env.UI_URL || "http://localhost:5183";
const shots = routes.length > 0 ? routes : ["/"];

const chrome = ["/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/chromium-browser"].find(
  (path) => existsSync(path)
);
if (!chrome) throw new Error("No Chrome on this machine; looked in /usr/bin.");

await mkdir(outDir, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: chrome,
  headless: "new",
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--force-color-profile=srgb"],
});

for (const spec of shots) {
  const [route, width = "1440", height = "980", theme = "light"] = spec.split("@");
  const page = await browser.newPage();
  await page.setViewport({ width: Number(width), height: Number(height), deviceScaleFactor: 2 });
  await page.emulateMediaFeatures([
    { name: "prefers-color-scheme", value: theme === "dark" ? "dark" : "light" },
  ]);
  await page.evaluateOnNewDocument((value) => {
    window.sessionStorage.setItem("jmfts.token", value);
  }, token);

  const problems = [];
  page.on("pageerror", (error) => problems.push(String(error)));
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));

  await page.goto(`${base}/#${route}`, { waitUntil: "networkidle0", timeout: 30000 });
  // The router renders on the hash, and a second visit to the same page needs the hash change
  // to settle before anything is on screen.
  await new Promise((resolve) => setTimeout(resolve, 700));

  const name = `${route.replace(/[^\w]+/g, "-").replace(/^-|-$/g, "") || "corpus"}-${theme}`;
  await page.screenshot({ path: `${outDir}/${name}.png`, fullPage: false });
  console.log(`${name}.png`, problems.length ? `PROBLEMS: ${problems.join(" | ")}` : "clean");
  await page.close();
}

await browser.close();
