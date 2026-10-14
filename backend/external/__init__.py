"""DebridPulse's native external-client foundation (protocol v1).

External clients -- an *arr installation, a browser extension -- are Integration
tokens with a client binding (``auth.accounts`` ``account_tokens.client_id``).
They own their logical requests (``external_jobs``) and their interest in a
delivery (``external_deliveries``); DebridPulse alone owns the canonical
transfer, its execution and its artifacts. See
``docs/architecture/EXTERNAL_CLIENT_PROTOCOL.md``.
"""
