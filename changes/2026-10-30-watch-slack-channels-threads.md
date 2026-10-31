# Minor

#### Added

- **`lemonaid watch slack` wakes a lemon on new Slack messages.** `--wait <channel>` reports new top-level messages in a channel, and `--thread <ts>` new replies in one thread, polling the Slack Web API once a minute outside the agent loop. The token comes from the new `[slack] token_command` or `$SLACK_USER_TOKEN`, and only channels listed in `[slack.channels]` can be watched; that list limits lemons, not the token.
