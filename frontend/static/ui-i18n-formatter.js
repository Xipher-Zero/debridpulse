/* Attaches the vendored ICU MessageFormat formatter (FormatJS
 * intl-messageformat 12.1.4, vendor/intl-messageformat.esm.js, an ES module)
 * to the localization runtime (ui-i18n.js, window.DPI18n). A module script:
 * it runs in document order with the deferred scripts, before /app.js, so
 * every owner can format a message from its first line. */
import {IntlMessageFormat} from '/vendor/intl-messageformat.esm.js?v=12.1.4';

window.DPI18n?.attachFormatter(IntlMessageFormat);
