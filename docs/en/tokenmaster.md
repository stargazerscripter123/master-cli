# tokenmaster

One terminal view of weekly quota and prepaid credits for Codex, Claude, Antigravity, Grok, Cursor and OpenRouter.

Credentials stay on the machine where you already signed in. tokenmaster reads them from each product's config directory or, on macOS, from the keychain. It does not store tokens in this repository. Do not commit `~/.config/tokenmaster` or paste a live API key into a ticket.

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

Calls run concurrently. A deadline (`TOKENMASTER_DEADLINE` or `deadline_seconds` in the config) keeps a slow provider from blocking the others. Partial data is kept.

## Accounts

You can watch more than one subscription for the same provider. Each named account gets its own directory under `~/.config/tokenmaster/profiles/<provider>/<account>`.

```bash
tokenmaster login claude work
tokenmaster account snapshot codex personal
tokenmaster account add openrouter team --api-key "$OPENROUTER_API_KEY"
tokenmaster account list
tokenmaster exec claude work
tokenmaster exec claude:auto          # the account with the most headroom
eval "$(tokenmaster env claude work)" # this shell only
tokenmaster -p claude:default,claude:work
tokenmaster account remove claude work --purge
```

`tokenmaster pick claude` prints the account name. Strategies are `smart` (the default), `most-remaining` and `expiring-first`.

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

tokenmaster statusline                # one line from a snapshot, for a prompt
tokenmaster statusline --format tmux
tokenmaster statusline --format claude-code

tokenmaster history --days 30 --by day
tokenmaster history --by provider
tokenmaster history --by model
tokenmaster history --by project

tokenmaster notify                    # desktop notification at 80%, 95%, 100%
tokenmaster notify --threshold 75,90,100 --dry-run
tokenmaster --watch --notify
```

The reset column includes a pace (`surplus`, `on_track`, `fast`, `overburn`) and an estimate of when the window runs out. `statusline` reads the last snapshot and does not block the prompt; a refresh happens in the background when the snapshot is old. `history` reads a local SQLite file, `warehouse.db`, so a later cleanup of the upstream CLI logs does not erase the ledger.

Project names stored in that ledger are reduced to a short label. Home-directory prefixes are stripped before anything is written.

## The screen

The dashboard is five columns: provider, tier, weekly remaining, reset, and credit. Color follows the remaining fraction: red at or below 15%, yellow at or below 40%, green above that. `--detail` adds the per-model pools. The layout accounts for East Asian wide characters, and `--width` (or `TOKENMASTER_MAX_WIDTH`) compresses columns when the terminal is narrow.
