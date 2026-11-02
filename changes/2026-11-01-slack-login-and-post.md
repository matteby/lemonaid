# Minor

#### Added

- **`lemonaid slack login` signs you in to Slack.** It runs Slack's OAuth flow with PKCE in the browser, with a `localhost` redirect and no client secret, and stores your user token in the macOS keychain. lemonaid reads the token from there when neither `[slack] token_command` nor `$SLACK_USER_TOKEN` is set. The app comes from the new `[slack] client_id`, and [docs/slack.md](docs/slack.md) gives its manifest.
- **`lemonaid slack post` posts as you.** It posts to a channel or thread in `[slack.channels]`, adds the `--me` signature so the lemon's own `watch slack --me` skips the post, and prints the new message's `ts` and link.
