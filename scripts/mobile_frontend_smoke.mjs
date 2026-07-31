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

let report;
try {
  const response = await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 45_000 });
  if (!response || response.status() !== 200) {
    throw new Error(`GAIA page returned HTTP ${response?.status() ?? "no response"}`);
  }

  await page.waitForSelector("#advanced-filters", { timeout: 15_000 });
  await page.waitForSelector("#job-grid .job-row", { timeout: 30_000 });
  await page.screenshot({ path: `${outputDir}/viewport.png`, fullPage: false });
  await page.screenshot({ path: `${outputDir}/full-page.png`, fullPage: true });

  report = await page.evaluate(() => {
    const details = document.querySelector("#advanced-filters");
    const summary = details?.querySelector("summary");
    const filterGrid = details?.querySelector(".filter-grid");
    const quickActions = document.querySelector(".quick-actions");
    const firstJob = document.querySelector("#job-grid .job-row");
    const results = document.querySelector("#results");
    const topbar = document.querySelector(".topbar");
    const hero = document.querySelector(".page-intro h1");
    const enhancementScript = [...document.scripts].find(script =>
      script.src.includes("/assets/app-improvements.js")
    );
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    return {
      url: location.href,
      viewportWidth,
      viewportHeight,
      documentWidth: document.documentElement.scrollWidth,
      filtersExist: Boolean(details),
      filtersOpenInitially: Boolean(details?.open),
      filterSummaryVisible: Boolean(summary && getComputedStyle(summary).display !== "none"),
      filterGridHiddenInitially: Boolean(filterGrid && getComputedStyle(filterGrid).display === "none"),
      quickActionsHorizontal: Boolean(
        quickActions &&
        ["auto", "scroll"].includes(getComputedStyle(quickActions).overflowX) &&
        quickActions.scrollWidth > quickActions.clientWidth
      ),
      resultsTop: results?.getBoundingClientRect().top ?? null,
      firstJobTop: firstJob?.getBoundingClientRect().top ?? null,
      topbarHeight: topbar?.getBoundingClientRect().height ?? null,
      heroFontSize: hero ? Number.parseFloat(getComputedStyle(hero).fontSize) : null,
      enhancementScript: enhancementScript?.src ?? null,
    };
  });

  const initialFailures = [];
  if (!report.filtersExist) initialFailures.push("advanced filter disclosure was not created");
  if (report.filtersOpenInitially) initialFailures.push("advanced filters were open by default");
  if (!report.filterSummaryVisible) initialFailures.push("filter summary was not visible");
  if (!report.filterGridHiddenInitially) initialFailures.push("filter grid was not collapsed");
  if (!report.quickActionsHorizontal) initialFailures.push("quick presets were not a horizontal mobile rail");
  if (report.documentWidth > report.viewportWidth + 1) initialFailures.push("document has horizontal overflow");
  if (report.resultsTop === null || report.resultsTop >= report.viewportHeight) {
    initialFailures.push("results header did not reach the initial phone viewport");
  }
  if (report.firstJobTop === null || report.firstJobTop >= report.viewportHeight) {
    initialFailures.push("first job did not reach the initial phone viewport");
  }
  if (report.topbarHeight === null || report.topbarHeight > 82) {
    initialFailures.push(`mobile header is too tall (${report.topbarHeight})`);
  }
  if (report.heroFontSize === null || report.heroFontSize > 42) {
    initialFailures.push(`mobile hero remains oversized (${report.heroFontSize}px)`);
  }
  if (!report.enhancementScript?.includes("app-improvements.js?v=1.1.0")) {
    initialFailures.push("production HTML did not load the cache-busted enhancement asset");
  }

  await page.locator('[data-preset="summer2027"]').click();
  await page.waitForFunction(() =>
    document.querySelector("#active-filter-count")?.textContent?.includes("2 active")
  );
  const presetState = await page.evaluate(() => ({
    count: document.querySelector("#active-filter-count")?.textContent ?? null,
    trust: document.querySelector("#trust")?.value ?? null,
    target: document.querySelector("#target")?.value ?? null,
    filtersOpen: Boolean(document.querySelector("#advanced-filters")?.open),
  }));
  report.presetState = presetState;
  report.consoleErrors = consoleErrors;
  report.failures = initialFailures;

  await writeFile(`${outputDir}/report.json`, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  if (initialFailures.length) throw new Error(initialFailures.join("; "));
  if (presetState.count !== "2 active" || presetState.trust !== "verified" || presetState.target !== "exact") {
    throw new Error(`Summer 2027 preset did not update filters truthfully: ${JSON.stringify(presetState)}`);
  }
} catch (error) {
  if (!report) {
    report = { url: baseUrl, consoleErrors, failures: [error.message] };
    await writeFile(`${outputDir}/report.json`, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  }
  throw error;
} finally {
  await browser.close();
}

console.log(JSON.stringify(report));
