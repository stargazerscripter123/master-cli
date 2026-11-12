# tokenmaster

One terminal view of weekly quota and prepaid credits for Codex, Claude, Antigravity, Grok, Cursor and OpenRouter.

tokenmaster discovers existing credentials in each product's config directory or, on macOS, in the keychain. Run `tokenmaster doctor` to inspect availability, then select your providers with `-p`.

`auth`, `login` and account snapshots can save or copy credentials into tokenmaster configuration or named profiles. Some providers refresh tokens and persist them back to their credential source. Configuration defaults to `~/.config/tokenmaster`; if that location is not writable, it falls back to `.tokenmaster/` in the working directory. This repository ignores that fallback; other repositories need their own ignore rule. Set `TOKENMASTER_CONFIG_DIR` to choose a fixed configuration directory. Keep configuration and profiles private.

```bash
tokenmaster                         # dashboard
tokenmaster --detail                # model pools and the 5-hour window
tokenmaster -p codex,claude         # a subset of providers
tokenmaster --width 110             # fit a narrow terminal
tokenmaster --watch                 # refresh in place
tokenmaster watch -i 10 -d
tokenmaster --json
```

`master token` is the same command.

## Providers

| Id | What it reads |
|---|---|
| `codex` | `~/.codex/auth.json` and the Codex usage API. Local session JSONL is a fallback |
| `claude` | `~/.claude/.credentials.json` or the macOS keychain, then the Anthropic usage API |
| `antigravity` | Local language-server Connect-RPC; default Linux consumer sign-ins also support the [native keyring quota API](antigravity-linux.md) |
| `grok` | The `grok` CLI auth file, a grok.com cookie, or an xAI API key |
| `cursor` | `cursor-agent` auth, the macOS keychain, or `state.vscdb` |
| `openrouter` | `OPENROUTER_API_KEY` or a key saved with `tokenmaster auth` |

Calls run concurrently. Set `deadline_seconds` in the configuration file shown by `tokenmaster config path` to bound a dashboard refresh. The default is 2.5 times `timeout_seconds` (20 seconds with the default 8-second timeout); `0` disables the global deadline. Partial data is kept. Credential persistence already in progress may still need to finish before process exit.

Provider integrations depend on upstream responses and credential formats. Available quota windows and credits differ by account; an API key alone does not guarantee subscription quota data. Local session usage and API-equivalent cost estimates are not provider billing statements.

## Accounts

You can watch more than one subscription for the same provider. Each named account gets its own directory under `~/.config/tokenmaster/profiles/<provider>/<account>`.

```bash
tokenmaster login claude work
tokenmaster account snapshot codex personal
tokenmaster account add openrouter team --api-key "$OPENROUTER_API_KEY"
tokenmaster account list
tokenmaster exec claude work
tokenmaster exec claude:auto          # select using the default smart strategy
eval "$(tokenmaster env claude work)" # this shell only
tokenmaster -p claude:default,claude:work
tokenmaster account remove claude work --purge
```

`login` and `exec` require the provider's native CLI to be installed. `snapshot` copies an existing sign-in; it does not create a new subscription. `pick` recommends an account and reports its reasoning; add `--quiet` to print only the account name. Strategies are `smart` (the default), `most-remaining` and `expiring-first`. Selection considers usable quota, the short-window limit and reset timing. `exec` selects at launch; it does not switch accounts inside an already running session.

## Other commands

```bash
tokenmaster auth                      # credential wizard
tokenmaster auth openrouter --api-key "$OPENROUTER_API_KEY"
tokenmaster quota openrouter --weekly-limit 30 --weekly-unit USD
tokenmaster quota grok --credit-limit 50 --credit-used 12.5 --credit-unit USD
tokenmaster quota openrouter --reset

tokenmaster doctor                    # local credentials and caches
tokenmaster config show
tokenmaster config path
tokenmaster config clear-cache

tokenmaster statusline --no-refresh   # cached data only; no provider queries
tokenmaster statusline --format tmux --async-refresh
tokenmaster statusline --format claude-code --async-refresh

tokenmaster history daily --days 30
tokenmaster history daily -p codex --days 30
tokenmaster history models
tokenmaster history projects
tokenmaster history projects --export csv --output usage.csv

tokenmaster notify                    # desktop notification at 80%, 95%, 100%
tokenmaster notify --threshold 75,90,100 --dry-run
tokenmaster --watch --notify
```

The reset column includes a pace (`surplus`, `on_track`, `fast`, `overburn`) and an estimate of when the window runs out. `statusline` reuses a fresh snapshot but can query providers synchronously when it is missing or stale. `--async-refresh` returns an existing stale snapshot while refreshing in the background; the first fetch can still block. Use `--no-refresh` for a prompt that never queries providers, and populate its snapshot by running the dashboard first.

`history` reads a local SQLite ledger, normally `~/.local/share/tokenmaster/warehouse.db`, and syncs available local sessions unless `--no-sync` is given. It supports `daily`, `weekly`, `monthly`, `projects`, `models` and `snapshots` views. Imported records survive later cleanup of the upstream CLI logs; history coverage depends on which data has been imported.

Project names stored in that ledger are reduced to a short label. Home-directory prefixes are stripped before anything is written.

## The screen

The dashboard is five columns: provider, tier, weekly remaining, reset, and credit. Color follows the remaining fraction: red at or below 15%, yellow at or below 40%, green above that. `--detail` adds the per-model pools. The layout accounts for East Asian wide characters, and `--width` (or `TOKENMASTER_MAX_WIDTH`) compresses columns when the terminal is narrow.
