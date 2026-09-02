# Sources & Providers UI and Provenance Presentation

Roadmap Item 10 is a presentation/configuration layer over the qualified provider-routing and durable-provenance contracts.

## Settings

- The existing AllDebrid card has the established header `Enable` toggle.
- `General Sources` groups non-service-specific providers.
- `HTTP & HTTPS` is the first General Sources provider and contains only its enablement control plus concise explanatory text.
- Both controls round-trip through `AppSettings.integrations[provider_id].enabled`.
- No frontend-only enablement state, duplicate backend setting, provider priority control, or HTTP/aria2 tuning surface is introduced.

## Transfer lists

Recent Activity and Downloads display a compact provider chip. Active transfers use `current_provider_id` from the latest durable route attempt. Completed transfers use `delivering_provider_id` from verified artifact delivery. Pending and legacy-unknown records are represented neutrally.

Provider identity is separate from transfer status and executor identity. `aria2` remains advanced execution detail.

## Details

Details exposes a safe original resource representation, current/final provider, ordered provider route history, and executor identity only under advanced acquisition details. HTTP(S) source presentation removes userinfo, query values, and fragments. Magnet presentation uses the existing secret-safe sanitizer. Provider candidate endpoint capabilities remain private.

## Historical truth

Presentation is derived from Item 9 records. It never derives historical provider identity from URL scheme/hostname, current provider enablement, current applicability classification, current AllDebrid host state, or executor identity.
