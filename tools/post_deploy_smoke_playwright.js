#!/usr/bin/env node
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing ${name}`);
  return value;
}

const cfg = {
  baseUrl: process.env.FAKTUREK_SMOKE_BASE_URL || 'https://app.fakturek.cz',
  email: required('FAKTUREK_SMOKE_EMAIL'),
  password: required('FAKTUREK_SMOKE_PASSWORD'),
  subjectId: required('FAKTUREK_SMOKE_SUBJECT_ID'),
  invoiceId: required('FAKTUREK_SMOKE_INVOICE_ID'),
  invoiceNumber: required('FAKTUREK_SMOKE_INVOICE_NUMBER'),
  publicUrl: required('FAKTUREK_SMOKE_PUBLIC_URL'),
  apiToken: required('FAKTUREK_SMOKE_API_TOKEN'),
  expectedAmount: process.env.FAKTUREK_SMOKE_AMOUNT || '',
  outDir: process.env.FAKTUREK_SMOKE_OUT_DIR || `/tmp/fakturek-postdeploy-${Date.now()}`,
};

fs.mkdirSync(cfg.outDir, { recursive: true });

async function shot(page, name) {
  await page.screenshot({ path: path.join(cfg.outDir, name), fullPage: true });
}

(async () => {
  const browser = await chromium.launch({ headless: process.env.FAKTUREK_SMOKE_HEADLESS !== '0' });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    ignoreHTTPSErrors: true,
    recordVideo: { dir: cfg.outDir, size: { width: 1440, height: 1000 } },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(Number(process.env.FAKTUREK_SMOKE_TIMEOUT_MS || 15000));

  const failures = [];
  async function check(label, fn) {
    try {
      await fn();
      console.log(`OK ${label}`);
    } catch (error) {
      failures.push(`${label}: ${error.message}`);
      console.error(`FAIL ${label}`, error.message);
      await shot(page, `FAIL-${label.replace(/[^a-z0-9]+/gi, '-')}.png`).catch(() => {});
    }
  }

  await check('login', async () => {
    await page.goto(`${cfg.baseUrl}/login`, { waitUntil: 'domcontentloaded' });
    await page.getByLabel(/uživatel|e-mail/i).fill(cfg.email);
    await page.getByLabel(/heslo/i).fill(cfg.password);
    await shot(page, '01-login.png');
    await page.getByRole('button', { name: /přihlásit/i }).click();
    await page.waitForLoadState('networkidle').catch(() => {});
    if (await page.getByText(/přihlášení do fakturku/i).isVisible().catch(() => false)) {
      throw new Error('login form is still visible');
    }
  });

  await check('dashboard', async () => {
    await page.goto(`${cfg.baseUrl}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.getByText(/Fakturek/i).first().waitFor();
    await shot(page, '02-dashboard.png');
  });

  await check('invoice list and filter', async () => {
    await page.goto(`${cfg.baseUrl}/invoices`, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.getByText(cfg.invoiceNumber).first().waitFor();
    await shot(page, '03-invoices-list.png');
    await page.getByText(/jen po splatnosti/i).click().catch(() => {});
    await page.getByRole('button', { name: /Filtrovat/i }).click();
    await page.waitForLoadState('networkidle').catch(() => {});
    await shot(page, '04-invoices-filtered.png');
  });

  await check('invoice detail', async () => {
    await page.goto(`${cfg.baseUrl}/invoices/${cfg.invoiceId}`, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.getByRole('heading', { name: cfg.invoiceNumber }).waitFor();
    await shot(page, '05-invoice-detail.png');
    await page.getByRole('button', { name: /více/i }).click().catch(() => {});
    await shot(page, '06-invoice-more-menu.png');
  });

  await check('payments page', async () => {
    await page.goto(`${cfg.baseUrl}/payments#unmatched-payments`, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle').catch(() => {});
    await shot(page, '07-payments.png');
    if (cfg.expectedAmount) {
      const section = page.locator('#unmatched-payments');
      const matchButton = section.getByRole('button', { name: /spárovat/i }).first();
      if (await matchButton.isVisible().catch(() => false)) {
        await matchButton.click();
        await page.waitForLoadState('networkidle').catch(() => {});
        await shot(page, '08-payments-after-match.png');
      }
    }
  });

  await check('public preview and pdf', async () => {
    await page.goto(cfg.publicUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.getByRole('heading', { name: cfg.invoiceNumber }).waitFor();
    await shot(page, '09-public-preview.png');
    const pdf = await context.request.get(`${cfg.publicUrl}/pdf`);
    if (pdf.status() !== 200) throw new Error(`public PDF HTTP ${pdf.status()}`);
    if (!String(pdf.headers()['content-type'] || '').includes('pdf')) throw new Error('public PDF is not PDF');
  });

  await check('api invoice and pdf', async () => {
    const headers = { Authorization: `Bearer ${cfg.apiToken}` };
    const invoice = await context.request.get(`${cfg.baseUrl}/api/v1/subjects/${cfg.subjectId}/invoices/${cfg.invoiceId}`, { headers });
    if (invoice.status() !== 200) throw new Error(`invoice API HTTP ${invoice.status()}`);
    const body = await invoice.json();
    if (body.number !== cfg.invoiceNumber) throw new Error(`unexpected invoice number ${body.number}`);
    const pdf = await context.request.get(`${cfg.baseUrl}/api/v1/subjects/${cfg.subjectId}/invoices/${cfg.invoiceId}/pdf`, { headers });
    if (pdf.status() !== 200) throw new Error(`API PDF HTTP ${pdf.status()}`);
    fs.writeFileSync(path.join(cfg.outDir, '10-api-smoke.json'), JSON.stringify({ invoice: body.number, pdfStatus: pdf.status() }, null, 2));
  });

  await check('mobile invoice list', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${cfg.baseUrl}/invoices`, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.getByText(cfg.invoiceNumber).first().waitFor();
    await shot(page, '11-mobile-invoices.png');
  });

  await context.close();
  await browser.close();
  console.log(`ARTIFACTS ${cfg.outDir}`);
  if (failures.length) {
    console.error(`FAILURES\n${failures.join('\n')}`);
    process.exit(1);
  }
})();
