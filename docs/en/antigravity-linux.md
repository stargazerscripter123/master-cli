**English** | [中文](../zh-Hant/antigravity-linux.md)

# Antigravity CLI quotas on Linux

`tokenmaster` can read the default Antigravity consumer account's model quotas without a running IDE. It first tries the existing language-server RPC path, then falls back to Google's quota API using the existing Linux keyring sign-in.

## Requirements

- An existing Antigravity consumer sign-in in the user's unlocked Secret Service keyring.
- `secret-tool` on `PATH` (`libsecret-tools` on Ubuntu).
- Network access to `https://cloudcode-pa.googleapis.com`.
- The native `agy` executable for automatic credential refresh. It can be on `PATH` or at `~/.local/bin/agy`.

Run the installed toolkit as the same Linux user who signed in:

```bash
tokenmaster -p antigravity --detail
tokenmaster -p antigravity --json
```

The report identifies this source as `Antigravity CLI Google quota API`. It uses `loadCodeAssist` for the account's tier and quota project, then `fetchAvailableModels` for model quota readings and reset times. A zero balance may be represented by an omitted `remainingFraction` together with a reset time; that is displayed as zero remaining. Hidden utility models are excluded. Missing credits remain unavailable.

## SSH and sign-in context

The native CLI can use a separate file-backed credential store when it detects SSH. Consequently, `agy models` over SSH may ask for sign-in even when the desktop keyring contains a valid login. This adapter reads the default desktop keyring directly. The keyring and its D-Bus service must be accessible to that SSH session.

When a quota request returns HTTP 401, tokenmaster asks the native `agy models` command to refresh that same keyring session and retries once. The native CLI handles OAuth refresh and credential persistence; tokenmaster does not write a copy of the credentials. The command queries model metadata and does not submit a model prompt. HTTP 403 or 429 is reported without repeating authentication.

This fallback currently covers the **default Linux consumer keyring account**. Named profiles, custom `ANTIGRAVITY_DIR` / profile directories, and explicit RPC credentials retain their existing scoped behavior. They never fall back to the default keyring. File-only SSH logins and enterprise / ADC credentials are not handled by this adapter.

Weekly and short-window readings use the existing Antigravity pool parser and cache. The upstream endpoint may expose only the active quota window; a sign-in does not guarantee that both windows or a credit balance are available.

[Back to tokenmaster](tokenmaster.md) · [Toolkit README](../../README.md)
