# Slack

lemonaid reads and posts in Slack as you, with your own user token. `lemonaid slack login` gets the token, `lemonaid watch slack` waits for new messages ([watch](watch.md#watching-slack)), and `lemonaid slack post` posts. All three use one Slack app for the whole workspace. The app has no bot user, so nobody adds a bot to a channel.

## The app

One person creates the app once for the workspace, from this manifest, at <https://api.slack.com/apps> (**Create New App**, then **From a manifest**):

```yaml
display_information:
  name: lemonaid
  description: Lets lemonaid read and post in Slack channels as you.
oauth_config:
  redirect_urls:
    - http://localhost:8723/slack/oauth
  scopes:
    user:
      - channels:history
      - groups:history
      - chat:write
  pkce_enabled: true
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

- `channels:history` reads public channels, `groups:history` reads private channels, and `chat:write` posts. These are user scopes, so the token can do only what its user can: it cannot read a private channel its user is not in.
- `pkce_enabled: true` lets `lemonaid slack login` sign in without the app's client secret. Slack cannot turn PKCE off again without its support team.
- `token_rotation_enabled: false` gives tokens that last until revoked. lemonaid does not refresh tokens, and refuses a sign-in that returns a refresh token. Slack cannot turn rotation off once it is on.
- The manifest has no `features.bot_user`, so the app has no bot user.

Keep the app internal to the workspace, out of the Slack Marketplace. An internal app keeps Tier 3 rate limits for reading channels. Share the app's **Client ID** (under **Basic Information**) with everyone who uses lemonaid. A client ID is public; never share the client secret, which lemonaid does not use.

## Signing in

Each person signs in once:

1. Add the app's client ID and the channels lemons may use to lemonaid's config ([`[slack]`](config.md#slack)):

   ```toml
   [slack]
   client_id = "1234567890.1234567890"

   [slack.channels]
   my-hq = "C0123ABCDEF"
   team-alerts = "C0456GHIJKL"
   ```

2. Run `lemonaid slack login`. It opens Slack in the browser. Approve the app.
3. Slack sends the browser back to `http://localhost:8723/slack/oauth`, where lemonaid waits. lemonaid trades the code for your user token and stores the token in the macOS keychain (service `lemonaid-slack`, account your login name). It prints the Slack user and workspace it signed in as.

lemonaid then reads the token from the keychain, with no `token_command` needed. Run `lemonaid slack login` again to replace the token, for example after you revoke it in Slack.

`lemonaid slack login` gives up after 5 minutes. It needs port 8723 free. To use another port, set `[slack] redirect_port`, and add `http://localhost:<port>/slack/oauth` to the app's redirect URLs.

On Linux, `lemonaid slack login` stops, because it stores tokens only in the macOS keychain. Put a token in `[slack] token_command` or `$SLACK_USER_TOKEN` instead.

lemonaid keeps the token in memory. It never prints the token, puts it in a command line, or writes it to a cursor, a log or an event.

## Posting

```bash
lemonaid slack post my-hq --me "🍋 LemonAce" "The UA run finished."
lemonaid slack post my-hq --thread 1712345678.123456 --me "🍋 LemonAce" < reply.md
```

- `slack post` posts as you with `chat.postMessage`, to a channel listed in `[slack.channels]` by name or ID.
- `--me` is required. lemonaid puts the signature at the start of the message unless the message already starts with it. `lemonaid watch slack --me` with the same signature skips the post, so a lemon is not woken by its own messages.
- `--thread TS` replies in the thread whose parent message has that `ts`.
- Without the text argument, `slack post` reads the message from standard input.
- On success, it prints the new message's `ts` and link: `posted to #my-hq: ts 1793400000.000100 https://…`.
- A failed post is not retried. When the failure might hide a post that went through (a timeout, for example), it exits 1 and says to check the channel first. A post Slack refused, such as to a channel you are not in, exits 2.

Slack posts the message under your name. People see no sign that a lemon wrote it except the signature.
