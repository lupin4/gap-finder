# Gap Finder

A [daily.dev Marketplace](https://daily.dev/marketplace) skill that finds the tags in your niche where readers engage more than publishers cover them.

Gap Finder ranks the tags in your niche on daily.dev by how much readers engage per post against how many sources publish there. It finds the under-served tags, the ones a single outlet owns and the crowded ones, and pulls the best posts for the top five so your agent can suggest an angle nobody has taken.

- **What it calls:** only GET requests, only to `https://api.daily.dev/public/v1`: `/search/tags`, `/feeds/tag/{tag}`, `/feeds/discussed` and, if you ask for it, `/search/sources`.
- **What it never does:** write to your account. The script refuses any method but GET, and never follows a redirect.
- **Your token:** you create a dedicated "Gap Finder" token with a 30- or 90-day expiry, and paste it into `~/.config/gap-finder/.env` yourself. Only the script reads it. The agent never asks for it, opens the file or prints it.
- **Known limits:** the numbers come from a sample of at most 150 posts per tag, not platform totals. Tag feeds may leave out posts you've already clicked or seen, and tags or sources you've blocked. This hasn't been checked yet; if they do, the tags you read most are undercounted. Views aren't exposed, so a tag with many silent readers looks weaker than it is.

## Use it

You need an agent that can run Python 3 (Claude Code, Cursor, Codex). The script uses the standard library only, so there is nothing to install.

1. Add the skill from the daily.dev Marketplace, or give your agent [`SKILL.md`](SKILL.md).
2. Ask it what to write about in your niche, for example "find the gaps for houdini, procedural".
3. On the first run the agent runs `gapfinder.py setup`, which creates `~/.config/gap-finder/.env` with an empty `DAILY_DEV_TOKEN=` line. Create a token named "Gap Finder" at https://daily.dev/settings/api, paste it after the `=`, and ask again. The `DAILY_DEV_TOKEN` environment variable works too.

When the token expires, the next run tells you to repeat step 3.

## Files

| File | What it is |
|------|------------|
| `SKILL.md` | The skill: agent instructions with `gapfinder.py` embedded. This is what the Marketplace ships. |
| `gapfinder.py` | The script on its own, for reading or running by hand. |
| `sync_skill.py` | Copies `gapfinder.py` into `SKILL.md`. Run it after editing the script. |
| `tests/` | Offline tests; they make no network calls. |

## Develop

```
python -m unittest discover -s tests
python sync_skill.py
```

The tests fail if `SKILL.md` no longer embeds the current `gapfinder.py`.
