# imessage-history

A Claude Code plugin for searching, exporting and analyzing the iMessage history on your Mac. Ask things like:

- "Show my messages with Alex from last week"
- "Export my 2025 conversation with Alex to Markdown"
- "Who did I text most this year?"
- "Find the PDF Alex sent about the lease"
- "Save all the photos from the Family group chat as JPGs"

Claude uses the skill on its own when you ask about your messages, or you can call it with `/imessage-history:imessage`.

## Install

```
/plugin marketplace add smian1/claude-plugins
/plugin install imessage-history@smian1
```

## Requirements

- **macOS** with Messages signed in. It reads `~/Library/Messages/chat.db`
- **Claude Code** running on that Mac (the terminal, or the Code tab in the desktop app). It doesn't work in claude.ai or Claude Desktop chat, because those can't see your Mac's files
- **Python 3** (standard library only). macOS offers to install it the first time you run `python3`
- **Full Disk Access** for the app Claude Code runs in (Terminal, iTerm, VS Code, ...): System Settings > Privacy & Security > Full Disk Access, then restart that app

To check your setup, ask Claude to "run the iMessage self-test". It checks the database format and decoder accuracy and prints no message text.

## What it can do

| | |
|---|---|
| **Find messages** | By person (all their numbers and emails), phone, group chat, date or date range, text search |
| **Read them properly** | Decodes the `attributedBody` blob, where most messages on recent macOS keep their text. Plain SQL on the `text` column misses nearly everything |
| **Label everything** | Contact names, group chat names, replies, edits, reactions, group events, iMessage apps, and attachments by type (image, video, voice message, PDF, ...) |
| **Export** | Text, Markdown (a heading per day), JSON or CSV |
| **Stats** | Totals, top senders, busiest chats, activity by month/year, weekday and hour. Counts only, no message text |
| **Attachments** | Filter by type, show file paths, copy files to a folder, convert HEIC photos to JPG |

## Privacy

- **Read-only.** The database is opened read-only, and nothing is sent anywhere by the script itself
- **The skill tells Claude to keep things narrow.** It fetches only what a request needs, uses counts for "who / how much / when" questions, and summarizes rather than quotes
- **One-time codes are masked** in output (`Your code is ••••••`) unless you ask for them
- Message content Claude reads becomes part of your Claude conversation, like anything else you share with it

## Limits

- **Attachments in iCloud:** only files stored on your Mac can be copied. With Messages in iCloud, older attachments often aren't downloaded. The plugin shows which ones are missing
- **Your replies in group chats** are only found by chat, not by person (the database doesn't link them to a person)
- **A few rare group-event labels** ("left the group", "location sharing", "FaceTime call") are based on known database codes but haven't been checked against every macOS version

## Running the script directly

The skill wraps a single script, which you can also run yourself:

```bash
python3 skills/imessage/query_messages.py --help
python3 skills/imessage/query_messages.py --selftest
python3 skills/imessage/query_messages.py --stats --since 2026-01-01
```

See [skills/imessage/SKILL.md](skills/imessage/SKILL.md) for every option.
