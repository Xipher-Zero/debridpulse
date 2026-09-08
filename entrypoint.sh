#!/bin/sh
# DebridPulse container entrypoint.
#
# Supports PUID / PGID environment variables so that downloaded files are owned
# by the same user as other host processes that consume downloaded files.
#
# Usage:
#   environment:
#     - PUID=1000
#     - PGID=1000
#
# When PUID/PGID are omitted DebridPulse uses the image defaults (99:100).
# To run as root deliberately set PUID=0.

set -e

warn() {
    printf '%s\n' "[entrypoint] WARNING: $*" >&2
}

fatal() {
    printf '%s\n' "[entrypoint] ERROR: $*" >&2
    exit 1
}

PUID="${PUID:-99}"
PGID="${PGID:-100}"

# ── Create / adjust group ─────────────────────────────────────────────────────
if [ "${PGID}" != "0" ]; then
    # Check if a group with this GID already exists.
    EXISTING_GROUP=$(getent group "${PGID}" | cut -d: -f1 || true)
    if [ -z "${EXISTING_GROUP}" ]; then
        if ! groupadd -g "${PGID}" appgroup; then
            # A concurrent bootstrap may have created the requested GID after
            # our initial lookup. Accept that postcondition, but do not hide a
            # genuine inability to establish the requested runtime group.
            EXISTING_GROUP=$(getent group "${PGID}" | cut -d: -f1 || true)
            if [ -z "${EXISTING_GROUP}" ]; then
                fatal "failed to create runtime group for PGID=${PGID}"
            fi
        fi
    fi
fi

# ── Create / adjust user ──────────────────────────────────────────────────────
if [ "${PUID}" != "0" ]; then
    EXISTING_USER=$(getent passwd "${PUID}" | cut -d: -f1 || true)
    if [ -z "${EXISTING_USER}" ]; then
        # Create the user with the requested UID, belonging to the requested GID.
        if useradd -u "${PUID}" -g "${PGID}" -M -s /bin/sh appuser; then
            RUN_USER="appuser"
        else
            # As with the group lookup, tolerate only the race where the
            # requested UID now exists. Any other failure is a bootstrap error.
            EXISTING_USER=$(getent passwd "${PUID}" | cut -d: -f1 || true)
            if [ -z "${EXISTING_USER}" ]; then
                fatal "failed to create runtime user for PUID=${PUID} PGID=${PGID}"
            fi
            RUN_USER="${EXISTING_USER}"
        fi
    else
        RUN_USER="${EXISTING_USER}"
    fi

    # The requested UID may already exist with a different primary group. The
    # runtime identity contract requires the requested PGID to be established;
    # unlike mount ownership reconciliation, this is not safely recoverable.
    CURRENT_GID=$(id -g "${RUN_USER}" 2>/dev/null || true)
    if [ "${CURRENT_GID}" != "${PGID}" ]; then
        if ! usermod -g "${PGID}" "${RUN_USER}"; then
            CURRENT_GID=$(id -g "${RUN_USER}" 2>/dev/null || true)
            if [ "${CURRENT_GID}" != "${PGID}" ]; then
                fatal "failed to set primary group for ${RUN_USER} to PGID=${PGID} (PUID=${PUID})"
            fi
        fi
    fi
else
    RUN_USER="root"
fi

echo "[entrypoint] PUID=${PUID} PGID=${PGID} → running as ${RUN_USER}"

# ── Apply umask ──────────────────────────────────────────────────────────────
UMASK="${UMASK:-002}"
if ! umask "${UMASK}" 2>/dev/null; then
    warn "invalid UMASK=${UMASK}; falling back to 002"
    umask 002
fi

# ── Fix ownership of app directories ─────────────────────────────────────────
# /app/data    — SQLite DB, backups, and aria2 session/log files
# /app/config  — config.json
# /download    — the mounted download target (most important for other containers)
#
# These paths may be bind/NFS/shared mounts whose ownership policy is controlled
# by the host. Reconciliation failures remain non-fatal so application storage
# health can report the live mount condition, but they must never be silent.
for DIR in /app/data /app/config; do
    if [ -d "${DIR}" ]; then
        if ! chown -R "${PUID}:${PGID}" "${DIR}"; then
            warn "failed to set ownership on ${DIR} to ${PUID}:${PGID}; continuing so runtime storage checks can diagnose mount permissions"
        fi
    fi
done
if [ -d /download ]; then
    if [ "${CHOWN_DOWNLOADS_RECURSIVE:-false}" = "true" ]; then
        if ! chown -R "${PUID}:${PGID}" /download; then
            warn "failed to set recursive ownership on /download to ${PUID}:${PGID}; continuing so runtime storage checks can diagnose mount permissions"
        fi
    else
        if ! chown "${PUID}:${PGID}" /download; then
            warn "failed to set ownership on /download to ${PUID}:${PGID}; continuing so runtime storage checks can diagnose mount permissions"
        fi
    fi
fi
if ! chmod 700 /app/config /app/data; then
    warn "failed to set mode 700 on /app/config and/or /app/data; continuing so runtime storage checks can diagnose mount permissions"
fi
if [ -f /app/config/config.json ] && ! chmod 600 /app/config/config.json; then
    warn "failed to set mode 600 on /app/config/config.json; continuing so runtime storage checks can diagnose mount permissions"
fi

# ── Hand off to the app ───────────────────────────────────────────────────────
if [ "${PUID}" = "0" ]; then
    # Explicit root — run directly
    exec "$@"
else
    exec gosu "${RUN_USER}" "$@"
fi
