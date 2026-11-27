/*
 * Configuration page of TA-octopusenergy, driven in a browser on a real
 * Splunk. Each save is read back from splunkd's raw configs/conf-* endpoint
 * and secrets from storage/passwords, neither of which the page or the
 * add-on's REST handlers read. Credentials are dummies; the build points the
 * add-on at the compose mock (local/octopus_api_base).
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const ROOT = GC.meta.restRoot.toLowerCase();
const SETTINGS_CONF = `${ROOT}_settings`;
const ACCOUNT_CONF = `${ROOT}_account`;
const PAGE_TITLE = GC.pages.configuration.title;
const tab = (name) => GC.pages.configuration.tabs.find((t) => t.name === name);
const entity = (tabName, field) => tab(tabName).entity.find((e) => e.field === field);

// Visible label -> conf key, written out rather than looked up by field in
// globalConfig.json, so a remapped field cannot move the lookup with it.
const ACCOUNT = { name: 'OctopusEnergy account username', account_password: 'API key or account password',
    account_number: 'Account Number' };
const PROXY = { proxy_enabled: 'Enable', proxy_type: 'Proxy Type', proxy_url: 'Host', proxy_port: 'Port',
    proxy_username: 'Username', proxy_password: 'Password', proxy_rdns: 'Reverse DNS resolution' };
const LOGGING = { loglevel: 'Log level' };

let rest;
const saved = {};

test.beforeAll(async () => {
    rest = await ucc.restClient();
    for (const s of ['logging', 'proxy']) saved[s] = await rest.confStanza(APP, SETTINGS_CONF, s);
});

test.afterAll(async () => {
    await rest.setConf(APP, SETTINGS_CONF, 'logging', { loglevel: (saved.logging && saved.logging.loglevel) || 'INFO' });
    // Never leave a proxy on: the collectors would route the mock through it.
    await rest.setConf(APP, SETTINGS_CONF, 'proxy', { proxy_enabled: '0', proxy_url: '', proxy_port: '',
        proxy_username: '', proxy_rdns: '0' });
    await rest.dispose();
});

test.beforeEach(async ({ page }) => {
    await ucc.login(page);
});

const title = (t) => t.title || (t.type === 'loggingTab' ? 'Logging' : t.name);

test('Configuration page loads every tab without app errors', async ({ page }) => {
    const errs = ucc.watchAppErrors(page, APP);
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    for (const t of GC.pages.configuration.tabs) {
        await ucc.openTab(page, title(t));
        if (t.table) {
            await expect(page.getByRole('button', { name: 'Add', exact: true })).toBeVisible();
        } else {
            for (const e of t.entity) await expect(ucc.controlGroup(page, e.label)).toBeVisible();
        }
    }
    errs.assertNone();
});

test('Logging tab saves the chosen level and it survives a reload', async ({ page }) => {
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, title(tab('logging')));
    const target = saved.logging && saved.logging.loglevel === 'DEBUG' ? 'WARNING' : 'DEBUG';
    await ucc.selectOption(page, page, LOGGING.loglevel, target);
    expect(await ucc.saveConfigTab(page)).toBe(200);
    await ucc.expectConf(() => rest.confStanza(APP, SETTINGS_CONF, 'logging'),
        (c) => c && c.loglevel === target, `[logging] loglevel=${target} after Save`);
    await page.reload();
    await ucc.openTab(page, title(tab('logging')));
    await expect(ucc.controlGroup(page, LOGGING.loglevel)).toContainText(target);
});

test('Proxy tab saves a dotted host, survives a reload, and the password never comes back', async ({ page }) => {
    const secret = `e2e-dummy-proxy-${Math.random().toString(36).slice(2)}`;
    const want = { proxy_url: 'proxy.e2e.example', proxy_port: '3128', proxy_username: 'e2euser' };
    try {
        await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
        await ucc.openTab(page, title(tab('proxy')));
        await ucc.setCheckbox(page, PROXY.proxy_enabled, true);
        await ucc.selectOption(page, page, PROXY.proxy_type, 'socks5');
        for (const [f, v] of Object.entries(want)) await ucc.fillText(page, PROXY[f], v);
        await ucc.fillText(page, PROXY.proxy_password, secret);
        await ucc.setCheckbox(page, PROXY.proxy_rdns, true);
        expect(await ucc.saveConfigTab(page)).toBe(200);

        const got = await ucc.expectConf(() => rest.confStanza(APP, SETTINGS_CONF, 'proxy'),
            (c) => c && c.proxy_url === want.proxy_url, '[proxy] after Save');
        for (const [f, v] of Object.entries(want)) expect(got[f], f).toBe(v);
        expect(got.proxy_type, 'proxy_type').toBe('socks5');
        expect(ucc.isTrue(got.proxy_enabled), 'proxy_enabled').toBe(true);
        expect(ucc.isTrue(got.proxy_rdns), 'proxy_rdns').toBe(true);
        await ucc.expectSecretNotEchoed(page, rest, { app: APP, conf: SETTINGS_CONF, stanza: 'proxy',
            field: 'proxy_password', secret, handlerPath: `/servicesNS/nobody/${APP}/${ROOT}_settings/proxy` });

        await page.reload();
        await ucc.openTab(page, title(tab('proxy')));
        for (const [f, v] of Object.entries(want)) expect(await ucc.readText(page, PROXY[f]), f).toBe(v);
        expect.soft(await ucc.domHolds(page, secret), 'proxy password in the DOM after reload').toBe(false);
    } finally {
        await rest.setConf(APP, SETTINGS_CONF, 'proxy', { proxy_enabled: '0' });
    }
});

test('Proxy tab refuses a host with spaces and writes nothing', async ({ page }) => {
    const before = await rest.confStanza(APP, SETTINGS_CONF, 'proxy');
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, title(tab('proxy')));
    await ucc.fillText(page, PROXY.proxy_url, 'bad host!');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByText(entity('proxy', 'proxy_url').validators[0].errorMsg).first()).toBeVisible();
    await page.waitForTimeout(3000);
    expect(await rest.confStanza(APP, SETTINGS_CONF, 'proxy')).toEqual(before);
});

test('account: create, edit and delete; the key never comes back and lives in storage/passwords', async ({ page }) => {
    const errs = ucc.watchAppErrors(page, APP);
    const name = `e2e_acct_${Date.now().toString(36)}@example.com`;
    const secret = `e2e-dummy-key-${Math.random().toString(36).slice(2)}`;
    const check = { app: APP, conf: ACCOUNT_CONF, stanza: name, field: 'account_password', secret,
        handlerPath: `/servicesNS/nobody/${APP}/${ROOT}_account/${encodeURIComponent(name)}` };
    const stanza = () => rest.confStanza(APP, ACCOUNT_CONF, name);
    try {
        await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
        await ucc.openTab(page, title(tab('account')));

        await page.getByRole('button', { name: 'Add', exact: true }).click();
        const d = ucc.dialog(page);
        await expect(d).toBeVisible();
        await ucc.fillText(d, ACCOUNT.name, name);
        await ucc.fillText(d, ACCOUNT.account_password, secret);
        await ucc.fillText(d, ACCOUNT.account_number, 'A-E2E0001');
        await ucc.submitDialog(page, 'Add');
        await expect(ucc.inputRow(page, name)).toBeVisible();
        const made = await ucc.expectConf(stanza, (c) => c !== null, `[${name}] created`);
        expect(made.account_number, 'account_number').toBe('A-E2E0001');
        await ucc.expectSecretNotEchoed(page, rest, check);

        await page.reload();
        await ucc.openTab(page, title(tab('account')));
        await expect(ucc.inputRow(page, name)).toBeVisible();
        expect.soft(await ucc.domHolds(page, secret), 'key in the DOM after reload').toBe(false);

        await ucc.rowAction(page, name, 'Edit');
        const e = ucc.dialog(page);
        await expect(e).toBeVisible();
        expect(await ucc.readText(e, ACCOUNT.account_number), 'edit shows account_number').toBe('A-E2E0001');
        expect.soft(await ucc.domHolds(page, secret), 'key in the edit dialog').toBe(false);
        await ucc.fillText(e, ACCOUNT.account_number, 'A-E2E0002');
        await ucc.submitDialog(page, 'Update');
        await ucc.expectConf(stanza, (c) => c && c.account_number === 'A-E2E0002', 'edited account_number saved');
        // The key survives an edit that did not touch it.
        await ucc.expectSecretNotEchoed(page, rest, check);

        await ucc.rowAction(page, name, 'Delete');
        await ucc.submitDialog(page, 'Delete');
        await expect(ucc.inputRow(page, name)).toHaveCount(0);
        await ucc.expectConf(stanza, (c) => c === null, `[${name}] removed`);
        await ucc.expectSecretGone(rest, { app: APP, conf: ACCOUNT_CONF, stanza: name });
        errs.assertNone();
    } finally {
        await rest.del(`/servicesNS/nobody/${APP}/${ROOT}_account/${encodeURIComponent(name)}`);
    }
});

test('account: an invalid name is refused and nothing is written', async ({ page }) => {
    const bad = '9_starts_with_a_digit';
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, title(tab('account')));
    await page.getByRole('button', { name: 'Add', exact: true }).click();
    const d = ucc.dialog(page);
    await ucc.fillText(d, ACCOUNT.name, bad);
    await ucc.fillText(d, ACCOUNT.account_password, 'e2e-dummy-refused');
    await d.getByRole('button', { name: 'Add', exact: true }).click();
    const msg = entity('account', 'name').validators.find((v) => v.type === 'regex').errorMsg;
    await expect(d.getByText(msg).first()).toBeVisible();
    await page.waitForTimeout(3000);
    expect(await rest.confStanza(APP, ACCOUNT_CONF, bad)).toBeNull();
    await d.getByRole('button', { name: 'Cancel', exact: true }).click();
});
