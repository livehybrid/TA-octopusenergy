/*
 * Inputs page of TA-octopusenergy, driven in a browser on a real Splunk:
 * create, disable, enable, edit and delete one input of every service in
 * globalConfig.json, each step read back from splunkd's raw
 * configs/conf-inputs endpoint, which the page and the add-on's REST handler
 * never read.
 *
 * The account has a dummy password and the build points the add-on at the
 * compose mock Kraken, so the Meter dropdown (the add-on's account_info
 * handler) and enabled inputs reach the mock, never api.octopus.energy.
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const ROOT = GC.meta.restRoot.toLowerCase();
const ACCOUNT_CONF = `${ROOT}_account`;
const PAGE_TITLE = GC.pages.inputs.title;
const ACCOUNT_NAME = `e2e_inputs_${Date.now().toString(36)}`;
// The mock's one electricity meter (docker/mock/mock_kraken.py).
const METER_VALUE = 'Electric#1200012345678#MOCKELEC01#MOCK-DEVICE-01';
const METER_LABEL = 'Electric - MOCKELEC01';

// Visible label -> inputs.conf key per service, written out here.
const COMMON = { name: 'Name', interval: 'Interval', index: 'Index', account: 'OctopusEnergy Account' };
const FIELDS = {
    agile_rates: { ...COMMON, rate_code: 'Agile Tariff Code', start_date: 'Start Date' },
    meter_readings: { ...COMMON, meter: 'Meter' },
    live_readings: { ...COMMON, meter: 'Meter' },
    intelligent_octopus: COMMON,
};
const VALUES = {
    agile_rates: { create: { interval: '3600', index: 'main', rate_code: 'AGILE-24-10-01' },
        edit: { interval: '7200', start_date: '2026-11-01 00:00:00' } },
    meter_readings: { create: { interval: '3600', index: 'main' }, edit: { interval: '7200' }, meter: true },
    live_readings: { create: { interval: '3600', index: 'main' }, edit: { interval: '7200' }, meter: true },
    intelligent_octopus: { create: { interval: '3600', index: 'main' }, edit: { interval: '7200' } },
};

let rest;
test.beforeAll(async () => {
    rest = await ucc.restClient();
    const r = await rest.post(`/servicesNS/nobody/${APP}/${ROOT}_account`,
        { name: ACCOUNT_NAME, account_password: 'e2e-dummy-password' });
    expect([200, 201], `create account: ${r.status}`).toContain(r.status);
});
test.afterAll(async () => {
    await rest.del(`/servicesNS/nobody/${APP}/${ROOT}_account/${ACCOUNT_NAME}`);
    await rest.dispose();
});
test.beforeEach(async ({ page }) => ucc.login(page));

test('every input service has test values and labels', () => {
    const services = GC.pages.inputs.services.map((s) => s.name).sort();
    expect(services).toEqual(Object.keys(VALUES).sort());
    expect(services).toEqual(Object.keys(FIELDS).sort());
});

async function fillTexts(d, label, values) {
    for (const [f, v] of Object.entries(values)) {
        await ucc.fillText(d, label(f), v);
        await ucc.control(d, label(f)).press('Tab');
    }
}

for (const service of GC.pages.inputs.services) {
    const label = (key) => FIELDS[service.name][key];
    const stanza = (name) => rest.inputStanza(APP, service.name, name);
    const vals = VALUES[service.name];

    async function fillCreate(page, d, name) {
        await fillTexts(d, label, { name, ...vals.create });
        await ucc.selectOption(page, d, label('account'), ACCOUNT_NAME);
        if (vals.meter) await ucc.selectOption(page, d, label('meter'), METER_LABEL);
    }

    test(`${service.name}: create, disable, enable, edit and delete through the page`, async ({ page }) => {
        const errs = ucc.watchAppErrors(page, APP);
        const name = `e2e_${service.name}_${Date.now().toString(36)}`;
        try {
            await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);

            const d = await ucc.openCreateInput(page, service.title);
            await fillCreate(page, d, name);
            await ucc.submitDialog(page, 'Add');
            await expect(ucc.inputRow(page, name)).toBeVisible();
            const made = await ucc.expectConf(() => stanza(name), (c) => c !== null, `[${service.name}://${name}] created`);
            const want = { ...vals.create, account: ACCOUNT_NAME, ...(vals.meter ? { meter: METER_VALUE } : {}) };
            for (const [f, v] of Object.entries(want)) expect(made[f], `created ${f}`).toBe(v);
            expect(ucc.isTrue(made.disabled), 'a new input starts enabled').toBe(false);

            await ucc.toggleRow(page, name);
            await ucc.expectConf(() => stanza(name), (c) => c && ucc.isTrue(c.disabled), 'disabled after the switch');
            await ucc.toggleRow(page, name);
            await ucc.expectConf(() => stanza(name), (c) => c && !ucc.isTrue(c.disabled), 'enabled after the switch');

            await ucc.rowAction(page, name, 'Edit');
            const e = ucc.dialog(page);
            await expect(e).toBeVisible();
            for (const [f, v] of Object.entries(vals.create)) expect(await ucc.readText(e, label(f)), `edit shows ${f}`).toBe(v);
            await expect(ucc.controlGroup(e, label('account'))).toContainText(ACCOUNT_NAME);
            if (vals.meter) await expect(ucc.controlGroup(e, label('meter'))).toContainText(METER_LABEL);
            await fillTexts(e, label, vals.edit);
            await ucc.submitDialog(page, 'Update');
            const after = await ucc.expectConf(() => stanza(name),
                (c) => c && Object.entries(vals.edit).every(([f, v]) => c[f] === v), 'edited values saved');
            for (const [f, v] of Object.entries({ ...want, ...vals.edit })) expect(after[f], `after edit ${f}`).toBe(v);

            await ucc.rowAction(page, name, 'Delete');
            await ucc.submitDialog(page, 'Delete');
            await expect(ucc.inputRow(page, name)).toHaveCount(0);
            await ucc.expectConf(() => stanza(name), (c) => c === null, `[${service.name}://${name}] removed`);
            errs.assertNone();
        } finally {
            await rest.deleteInput(APP, service.name, name);
        }
    });

    test(`${service.name}: an invalid name or interval is refused and nothing is written`, async ({ page }) => {
        const bad = '9_starts_with_a_digit';
        await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);
        const d = await ucc.openCreateInput(page, service.title);
        await fillCreate(page, d, bad);
        await d.getByRole('button', { name: 'Add', exact: true }).click();
        const nameMsg = service.entity.find((x) => x.field === 'name').validators[0].errorMsg;
        await expect(d.getByText(nameMsg).first()).toBeVisible();
        const good = `e2e_badinterval_${Date.now().toString(36)}`;
        await fillTexts(d, label, { name: good, interval: 'hourly' });
        await d.getByRole('button', { name: 'Add', exact: true }).click();
        const intMsg = service.entity.find((x) => x.field === 'interval').validators[0].errorMsg;
        await expect(d.getByText(intMsg).first()).toBeVisible();
        await page.waitForTimeout(3000);
        expect(await stanza(bad)).toBeNull();
        expect(await stanza(good)).toBeNull();
        await d.getByRole('button', { name: 'Cancel', exact: true }).click();
    });
}

test('tokens the add-on fetches for an account are stored encrypted, never in the plain conf', async () => {
    // The Meter dropdown above made the add-on log in to the mock and write the
    // access and refresh tokens back into the account. The mock's token is a
    // JWT ("eyJ..."), so a plain-text write is recognisable without knowing it.
    const raw = await rest.confStanza(APP, ACCOUNT_CONF, ACCOUNT_NAME);
    expect(raw, `[${ACCOUNT_NAME}] exists`).toBeTruthy();
    for (const f of ['access_token', 'refresh_token', 'account_password']) {
        expect(String(raw[f] || '').startsWith('eyJ') || String(raw[f] || '') === 'e2e-dummy-password'
            || String(raw[f] || '') === 'mock-refresh-token', `${f} is in plain text in ${ACCOUNT_CONF}.conf`).toBe(false);
    }
    const stored = (await ucc.readCredential(rest, APP, ACCOUNT_CONF, ACCOUNT_NAME)) || {};
    expect(Object.keys(stored).sort(), 'encrypted fields held in storage/passwords').toEqual(
        expect.arrayContaining(['access_token', 'account_password', 'refresh_token']));
    expect(String(stored.access_token || '').startsWith('eyJ'), 'stored access_token is the mock JWT').toBe(true);
});
