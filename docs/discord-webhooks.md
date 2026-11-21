# Webhook Notifications

DownloadPulse sends instance-wide alerts to webhooks configured by an Admin under
**Settings → Notifications → Webhook Notifications**. Each field is saved when you leave it;
**Test** sends a test message to the saved webhooks and bypasses deduplication.

## Which webhooks work

| Where | What it accepts |
|---|---|
| **Settings → Notifications** (Admin) | Any `http://` or `https://` URL. A Discord URL receives each alert as a Discord embed; any other URL receives a generic JSON POST (below). |
| A User's own **Notifications** (Server deployment) | Discord webhooks only: HTTPS on `discord.com`, `canary.discord.com`, `ptb.discord.com` or the matching `discordapp.com` hosts, with the path `/api/webhooks/{id}/{token}` and nothing else. Posted to exactly; a redirect is refused, never followed. |

## Settings

```json
{
  "discord_notifications_enabled": true,
  "discord_webhook_url": "https://discord.com/api/webhooks/.../main",
  "discord_webhook_added": "https://discord.com/api/webhooks/.../added",
  "discord_username": "DownloadPulse",
  "discord_avatar_url": "https://example.com/avatar.png",
  "discord_notify_added": true,
  "discord_notify_finished": true,
  "discord_notify_error": true,
  "discord_notify_extract": true,
  "discord_notify_update": true,
  "update_check_interval_hours": 12
}
```

| Setting | Interface label | Notes |
|---|---|---|
| `discord_notifications_enabled` | Enable Webhook Notifications | Turns these alerts on or off; statistics reports are separate. |
| `discord_webhook_url` | Primary Webhook | Receives every alert. |
| `discord_webhook_added` | Download Added Webhook | Optional; receives Download Added alerts only. Empty falls back to the Primary Webhook. |
| `discord_username` | Display Name | Sender name of Discord messages. |
| `discord_avatar_url` | Avatar URL / Upload | Discord fetches the image itself, so it must be reachable over HTTP(S) from the internet. Upload accepts PNG, JPG, GIF and WebP up to 4 MB and is served at `/api/avatar`; set `PUBLIC_BASE_URL` so that address is public. Private addresses (`192.168.x.x`, `localhost`, `.local`) usually don't work for Discord. |
| `discord_notify_*` | Notification Events & Delivery Options | One switch per event group below. |
| `update_check_interval_hours` | Update Check Interval | 0–168 hours, 12 by default; 0 turns the update check off. |

## Events

| Alert | Method | Colour | Sent when | Switch |
|---|---|---|---|---|
| Download Added | `send_added()` | Purple | A new download is accepted | `discord_notify_added` |
| Download Completed | `send_complete()` | Green | A download finishes | `discord_notify_finished` |
| Partial download | `send_partial()` | Orange | It finished with some files blocked | `discord_notify_finished` |
| Completed with source warnings | `send()` | Orange | It finished, but some source requests failed | `discord_notify_finished` |
| Download Error | `send_error()` | Red | A download needs attention | `discord_notify_error` |
| Extraction complete / failed | `send_extract_complete()` / `send_extract_failed()` | Green / Red | Archive extraction completes or fails | `discord_notify_extract` |
| Update Available | `send_update()` | Blue | A newer release is detected | `discord_notify_update` |

A User's personal webhook receives these events only for downloads that User asked for, with the User's own switches. Update Available is instance-wide and never sent to personal webhooks.

## Generic JSON payload

A non-Discord URL receives a POST with this body:

| Field | Content |
|---|---|
| `event` | The alert's title |
| `event_key` | The title in lower case with underscores |
| `severity` | `success`, `info`, `warning` or `error` |
| `app` | `name`, `version` and `repository` |
| `description` | The alert's text |
| `color` | The colour a Discord embed would use |
| `fields` | The alert's details as name → value pairs |
| `timestamp` | When the alert was made (ISO 8601) |
| `embed` | The full Discord embed |

## Delivery

- **Deduplication:** the same message (same URL, title and description) within 10 seconds is sent once.
- **Rate limiting:** at least 2 seconds between messages to the same URL.
- **Discord 429:** DownloadPulse waits for the `retry_after` Discord returns and retries once (at most 10 seconds for a User's personal webhook).
