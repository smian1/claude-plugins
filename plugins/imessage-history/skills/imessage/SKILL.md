---
name: imessage
description: Query the local iMessage database for conversations, group chats and attachments (photos, videos, voice messages, PDFs). Use when the user wants to search messages, export a conversation, find or save attachments, get message statistics (who they text most, activity over time), or analyze iMessage data.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/query_messages.py *), Read, Write
---

# iMessage Skill

Query and analyze the local iMessage database of the current macOS user. The script opens the database read-only.

## Requirements

- macOS with Messages signed in (reads `~/Library/Messages/chat.db`)
- Python 3, standard library only
- **Full Disk Access** for the app running the script (Terminal, iTerm, VS Code, ...): System Settings > Privacy & Security > Full Disk Access. Without it the script exits with that instruction
- Contact names come from the macOS Contacts app; unknown senders show as their phone number, email or business ID

Run `--selftest` first on a new Mac or after a macOS update. It checks the database format and decoder accuracy and prints no message text.

## Privacy Rules

These are other people's private messages. When using this skill:

- **Fetch only what the task needs.** Narrow with `--person`, `--chat`, dates and `--limit` rather than reading whole histories into context
- **Use `--stats` for "how much / who / when" questions.** It returns counts, never message text
- **Summarize rather than quote** unless the user asks for the messages themselves
- **One-time codes are masked by default** (`Your code is ••••••`). Use `--show-codes` only if the user explicitly asks for a code
- **Save exports only where the user asks** (default: current directory). Don't write them anywhere shared or synced, and don't send message content to external services, unless the user asks

## How to Query Messages

**Always use the Python script** instead of raw SQL:

```bash
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --person "Alex Smith" --sent --days 7
```

By default you get **received messages only, newest first, at most 50**. Add `--sent` for both sides of a conversation and `--limit 0` for no limit.

### Script Options

| Option | Example | Description |
|--------|---------|-------------|
| **Who** | | |
| `--person` | `"Alex Smith"` | Contact name; covers all their phone numbers and emails. If several contacts match, lists them and exits |
| `--phone` | `5551234567` | Phone (last 10 digits) or email. Matches one number only |
| `--chat` | `Family` / `123` / `123,456` | Chat by ROWID, or by group name / contact name / phone / email. If several match, lists them and exits so you can pick a ROWID |
| `--sent` | flag | Include my sent messages |
| **When** | | |
| `--date` | `2026-01-06` | One day |
| `--since` / `--until` | `2025-01-01` | Inclusive date range; either end alone is fine |
| `--days` | `7` | Last N days |
| **What** | | |
| `--search` | `vacation` | Case-insensitive text search (covers blob-only messages) |
| `--type` | `image` | Only messages with that attachment kind: `any`, `image`, `video`, `audio`, `voice`, `pdf`, `file`, `contact`, `location`, `sticker`, `other` |
| `--no-reactions` | flag | Leave out tapbacks, emoji reactions and stickers placed on messages |
| **Output** | | |
| `--format` | `md` | `text` (default), `md` (chronological, a heading per day), `json`, `csv` |
| `--stats` | flag | Counts instead of messages: totals, top senders, busiest chats, by month/year, weekday, hour, attachment kinds. Counts sent and received; ignores `--limit`. `text`/`md` or `json` |
| `--oldest-first` | flag | Read from the start of the range (default: newest first) |
| `--limit` | `100` | Max messages (default 50, `0` = no limit) |
| `--paths` | flag | Show attachment file paths, or `(not downloaded to this Mac)` |
| `--show-codes` | flag | Don't mask one-time verification codes |
| **Attachment files** | | |
| `--copy-to` | `./photos` | Copy the attachment files of the matching messages into a folder. Implies `--type any`; `--limit` still counts messages |
| `--convert-heic` | flag | With `--copy-to`, convert HEIC photos to JPG (uses macOS `sips`) |
| **Tools** | | |
| `--list-chats` | `Alex` | List matching chats with ROWID, last message date, message count |
| `--selftest` | flag | Check database format and decoder accuracy |

### Examples

```bash
# One person's conversation this week, both sides, all their numbers and emails
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --person "Alex Smith" --sent --days 7

# Messages containing "dinner" on a given day
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --date 2026-01-07 --search dinner

# A whole group chat including my replies (--person/--phone can't see my group replies)
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --list-chats Family
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --chat 123 --sent --days 30

# Year export of a conversation as Markdown, without reactions
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --chat 123 --sent --since 2025-01-01 --until 2025-12-31 \
  --limit 0 --no-reactions --format md > Alex_2025.md

# Who do I text most this year, and when (counts only)
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --stats --since 2026-01-01

# PDFs anyone sent me, with file locations
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --type pdf --paths --limit 20

# Save every photo from a group chat as JPG
python3 ${CLAUDE_SKILL_DIR}/query_messages.py --chat 123 --sent --type image --limit 0 \
  --copy-to ./family-photos --convert-heic
```

### Output Format

`YYYY-MM-DD HH:MM:SS | Sender (in “Group”): [↳ reply to Name: “preview”] text [kind: name, size] (edited)`

- **`(in …)`**: added for group-chat messages, unless all results come from one chat. Unnamed groups show as `group with A, B, C +2`
- **Replies**: `[↳ reply to Name: “first 60 characters”]` before the text
- **Edited messages**: show the latest text plus `(edited)`. Unsent messages show `[unsent a message]`
- **Attachments**: added after the text, e.g. `[image: IMG_1234.HEIC, 2.1 MB]`. A message with only a photo shows just the label. Link previews are skipped (the URL is already in the text)
- **Reactions**: shown as stored (`Loved “see you soon”`), or as `[reacted 😂 to a message]` / `[removed a reaction]`
- **Group events**: `[added Name to the group]`, `[removed …]`, `[renamed the group to “…”]`, `[left the group]`, `[changed the group photo]`, `[location sharing update]`, `[FaceTime call]`
- **iMessage apps** with no text: `[Apple Cash app message]`, `[Photos app message]`, `[link preview]`, etc.
- **`[empty message]`**: no text, attachment or app at all
- **JSON** records: `id, date, sender, handle, is_from_me, chat_id, chat, is_group, type, text, attachments[] (kind, name, mime_type, size, path, on_disk), is_reply, reply_to, edited`
- **Markdown** is always chronological. `--limit` still decides which messages are picked: newest N by default, oldest N with `--oldest-first`

### Attachment Files

- Files live under `~/Library/Messages/Attachments/`. Often only about half of recent attachments, and very few old ones, are stored on the Mac; the rest are in iCloud (Messages in iCloud). `--paths` shows which is which, and `--copy-to` skips and counts the missing ones
- Copies are named `YYYYMMDD_HHMMSS_<original name>` and never overwrite existing files
- Voice messages are `.caf` files and play with `afplay`

### Why Use the Script

Raw SQL misses or garbles messages because:
- On recent macOS most messages have `text = NULL` with content only in the `attributedBody` blob (so `LIKE` on `text` finds almost nothing)
- The blob is an NSAttributedString typedstream; the script parses it properly
- Phone numbers and emails need resolving to contact names, and one person can have several
- Your replies in group chats have no handle, so filtering by person or phone misses them (use `--chat`)

## Phone Numbers

Prefer `--person NAME`. To find a chat or a number, run `--list-chats NAME`; it matches Contacts names, phone numbers, emails and group names. `--phone` matches on digits, so use the last 10 digits without `+`, spaces or dashes.

## SQL Reference

See [reference.md](reference.md) for table schemas, common SQL and date conversion. For ad-hoc SQL always open the database read-only: `sqlite3 -readonly ~/Library/Messages/chat.db`.

## Exports

Save exports where the user asks; default to a Markdown (`.md`) file in the current directory (`--format md > file.md`).
