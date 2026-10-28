const { defineConfig, devices } = require('@playwright/test');
const base = require('./playwright.config.js');

/* The navigation-motion cases tagged @cross-engine, in Chromium, Firefox and
 * WebKit (DP 1.0.14: one Web Animations slide in every engine). The default
 * configuration -- the whole suite, Chromium only -- is unchanged. Run where
 * all three engines are installed, e.g. the official Playwright container. */
module.exports = defineConfig({
  ...base,
  grep: /@cross-engine/,
  projects: ['chromium', 'firefox', 'webkit'].map(name => ({
    name,
    use: {...devices[{chromium: 'Desktop Chrome', firefox: 'Desktop Firefox', webkit: 'Desktop Safari'}[name]],
          viewport: base.use.viewport},
  })),
});
