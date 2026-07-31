import { mkdir, writeFile } from "node:fs/promises";
import { chromium } from "playwright";

const baseUrl = (process.env.GAIA_BASE_URL || "https://gaiajob.vercel.app").replace(/\/$/, "");
const outputDir = process.env.GAIA_MOBILE_SMOKE_OUTPUT || "mobile-smoke";
await mkdir(outputDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 1,
  isMobile: true,
  hasTouch: true,
});
const consoleErrors = [];
page.on("console", message => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("pageerror", error => consoleErrors.push(error.message));

async function captureLayout() {
  return page.evaluate(() => {
    const details = document.querySelector("#advanced-filters");
    const summary = details?.querySelector("summary");
    const filterGrid = details?.querySelector(".filter-grid");
    const quickActions = document.querySelector(".quick-actions");
    const firstJob = document.querySelector("#job-grid .job-row");
    const results = document.querySelector("#results");
    const topbar = document.querySelector(".topbar");
    const hero = document.querySelector(".page-intro h1");
    const toast = document.querySelector("#toast");
    const toastStyle = toast ? getComputedStyle(toast) : null;
    const enhancementScript = [...document.scripts].find(script =>
      script.src.includes("/assets/app-improvements.js")
    );

    return {
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      documentWidth: document.documentElement.scrollWidth,
      filtersExist: Boolean(details),
      filtersOpen: Boolean(details?.open),
      filterSummaryVisible: Boolean(summary && getComputedStyle(summary).display !== "none"),
      filterGridHidden: Boolean(filterGrid && getComputedStyle(filterGrid).display === "none"),
      quickActionsHorizontal: Boolean(
        quickActions &&
        ["auto", "scroll"].includes(getComputedStyle(quickActions).overflowX) &&
        quickActions.scrollWidth > quickActions.clientWidth
      ),
      resultsTop: results?.getBoundingClientRect().top ?? null,
      firstJobTop: firstJob?.getBoundingClientRect().top ?? null,
      topbarHeight: topbar?.getBoundingClientRect().height ?? null,
      heroFontSize: hero ? Number.parseFloat(getComputedStyle(hero).fontSize) : null,
      idleToastHidden: Boolean(
        toast &&
        !toast.classList.contains("show") &&
        (toastStyle?.visibility === "hidden" || toastStyle?.display === "none")
      ),
      enhancementScript: enhancementScript?.src ?? null,
      theme: document.documentElement.dataset.theme ?? null,
      bodyBackground: getComputedStyle(document.body).backgroundColor,
    };
  });
}

let report = { url: baseUrl, failures: [], consoleErrors };
try {
  const response = await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 45_000 });
  if (!response || response.status() !== 200) {
    throw new Error(`GAIA page returned HTTP ${response?.status() ?? "no response"}`);
  }

  await page.waitForSelector("#advanced-filters", { timeout: 15_000 });
  await page.waitForSelector("#job-grid .job-row", { timeout: 30_000 });
  await page.screenshot({ path: `${outputDir}/viewport.png`, fullPage: false });
  await page.screenshot({ path: `${outputDir}/full-page.png`, fullPage: true });

  const initial = await captureLayout();
  report = { ...report, ...initial, url: page.url() };
  const failures = report.failures;
  if (!initial.filtersExist) failures.push("advanced filter disclosure was not created");
  if (initial.filtersOpen) failures.push("advanced filters were open by default");
  if (!initial.filterSummaryVisible) failures.push("filter summary was not visible");
  if (!initial.filterGridHidden) failures.push("filter grid was not collapsed");
  if (!initial.quickActionsHorizontal) failures.push("quick presets were not a horizontal mobile rail");
  if (initial.documentWidth > initial.viewportWidth + 1) failures.push("document has horizontal overflow");
  if (initial.resultsTop === null || initial.resultsTop >= initial.viewportHeight) {
    failures.push("results header did not reach the initial phone viewport");
  }
  if (initial.firstJobTop === null || initial.firstJobTop >= initial.viewportHeight) {
    failures.push("first job did not reach the initial phone viewport");
  }
  if (initial.topbarHeight === null || initial.topbarHeight > 82) {
    failures.push(`mobile header is too tall (${initial.topbarHeight})`);
  }
  if (initial.heroFontSize === null || initial.heroFontSize > 42) {
    failures.push(`mobile hero remains oversized (${initial.heroFontSize}px)`);
  }
  if (!initial.idleToastHidden) failures.push("idle toast rendered as an empty overlay");
  if (!initial.enhancementScript?.includes("app-improvements.js?v=1.1.0")) {
    failures.push("production HTML did not load the cache-busted enhancement asset");
  }

  await page.locator("#theme-toggle").click();
  await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  await page.screenshot({ path: `${outputDir}/dark-viewport.png`, fullPage: false });
  const darkMode = await captureLayout();
  report.darkMode = darkMode;
  if (darkMode.theme !== "dark") failures.push("theme toggle did not enter dark mode");
  if (darkMode.filtersOpen) failures.push("dark mode unexpectedly opened advanced filters");
  if (darkMode.documentWidth > darkMode.viewportWidth + 1) failures.push("dark mode has horizontal overflow");
  if (darkMode.firstJobTop === null || darkMode.firstJobTop >= darkMode.viewportHeight) {
    failures.push("dark mode pushed the first job below the phone viewport");
  }
  if (!darkMode.idleToastHidden) failures.push("dark mode exposed the idle toast overlay");

  await page.locator('[data-preset="summer2027"]').click();
  await page.waitForFunction(() =>
    document.querySelector("#active-filter-count")?.textContent?.includes("2 active")
  );
  report.presetState = await page.evaluate(() => ({
    count: document.querySelector("#active-filter-count")?.textContent ?? null,
    trust: document.querySelector("#trust")?.value ?? null,
    target: document.querySelector("#target")?.value ?? null,
    filtersOpen: Boolean(document.querySelector("#advanced-filters")?.open),
  }));
  if (
    report.presetState.count !== "2 active" ||
    report.presetState.trust !== "verified" ||
    report.presetState.target !== "exact"
  ) {
    failures.push(`Summer 2027 preset was inconsistent: ${JSON.stringify(report.presetState)}`);
  }

  if (consoleErrors.length) failures.push(`browser console errors: ${consoleErrors.join(" | ")}`);
  await writeFile(`${outputDir}/report.json`, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  if (failures.length) throw new Error(failures.join("; "));
} catch (error) {
  if (!report.failures.includes(error.message)) report.failures.push(error.message);
  await writeFile(`${outputDir}/report.json`, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  throw error;
} finally {
  await browser.close();
}

console.log(JSON.stringify(report));
