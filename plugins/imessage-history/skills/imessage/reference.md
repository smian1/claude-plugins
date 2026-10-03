# iMessage Database Reference

## Database Location

```
~/Library/Messages/chat.db
```

Prefer `query_messages.py`. For ad-hoc SQL always open the database **read-only**, because Messages writes to it constantly:

```bash
sqlite3 -readonly ~/Library/Messages/chat.db "SELECT count(*) FROM message;"
```

## Key Tables

| Table | Purpose |
|-------|---------|
| `message` | All messages (text, date, sender) |
| `chat` | Group chats and 1-on-1 conversations |
| `handle` | Phone numbers and email addresses |
| `chat_message_join` | Links messages to chats |
| `chat_handle_join` | Links participants to chats |
| `attachment` | Files, images, videos shared |

## Date Conversion

iMessage dates are nanoseconds since 2001-01-01. Convert to readable:

```sql
datetime(date/1000000000 + 978307200, 'unixepoch', 'localtime')
```

## AddressBook Database

One database per account source; the script loads all of them:

```
~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb
```

The folder names under `Sources/` are random IDs that differ per Mac and account; usually one source holds nearly all contacts.

---

## Common SQL Queries

### Find a group chat by participant

```sql
SELECT c.ROWID, c.chat_identifier, c.display_name
FROM chat c
JOIN chat_handle_join chj ON c.ROWID = chj.chat_id
JOIN handle h ON chj.handle_id = h.ROWID
WHERE c.display_name LIKE '%SearchTerm%'
   OR h.id LIKE '%phone_or_email%'
GROUP BY c.ROWID;
```

### Count messages in a chat for a year

```sql
SELECT COUNT(*)
FROM message m
JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
WHERE cmj.chat_id = [CHAT_ROWID]
  AND datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') >= '2025-01-01';
```

### Get messages with sender info

```sql
SELECT
    datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as date,
    CASE WHEN m.is_from_me = 1 THEN 'Me' ELSE h.id END as sender,
    m.text,
    m.attributedBody  -- usually the only copy of the text; decode with query_messages.extract_text()
FROM message m
JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
LEFT JOIN handle h ON m.handle_id = h.ROWID
WHERE cmj.chat_id = [CHAT_ROWID]
ORDER BY m.date;
```

### Get chat participants

```sql
SELECT h.id, h.service
FROM handle h
JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
WHERE chj.chat_id = [CHAT_ROWID];
```

### Get all contacts with phone numbers

```sql
sqlite3 -readonly ~/Library/Application\ Support/AddressBook/Sources/[SOURCE_ID]/AddressBook-v22.abcddb \
  "SELECT r.ZFIRSTNAME, r.ZLASTNAME, p.ZFULLNUMBER FROM ZABCDRECORD r JOIN ZABCDPHONENUMBER p ON r.Z_PK = p.ZOWNER WHERE p.ZFULLNUMBER IS NOT NULL;"
```

---

## Phone Number Normalization

Formats differ between databases. To match, extract last 10 digits:

```sql
-- Normalize: remove (, ), -, spaces
REPLACE(REPLACE(REPLACE(REPLACE(phone, '(', ''), ')', ''), '-', ''), ' ', '')
```

When matching, use last 10 digits since formats vary (+1, 1, or bare number).

---

## Attachments

```sql
SELECT a.transfer_name, a.mime_type, a.uti, a.total_bytes, a.filename, a.is_sticker
FROM attachment a
JOIN message_attachment_join j ON j.attachment_id = a.ROWID
WHERE j.message_id = [MESSAGE_ROWID] AND a.hide_attachment = 0;
```

- `message.cache_has_attachments = 1` is a reliable fast pre-filter
- `hide_attachment = 1` are mostly `.pluginPayloadAttachment` link-preview payloads (`uti` = `dyn.*`, `mime_type` NULL), not real attachments
- Kind comes from `mime_type` (image/*, video/*, audio/*, application/pdf, text/vcard, text/x-vlocation), `is_sticker`, and `message.is_audio_message` for voice messages (`.caf`, `uti` = `com.apple.coreaudio-format`)
- `filename` starts with `~`; expand it. Missing files are offloaded to iCloud

## Reactions, Group Events, App Messages

| Column | Values |
|--------|--------|
| `associated_message_type` | 2000 loved, 2001 liked, 2002 disliked, 2003 laughed, 2004 emphasized, 2005 questioned, 2006 emoji (`associated_message_emoji`), 2007 sticker; 3000–3007 = removed; 1000 = sticker placed on a message |
| `item_type` | 1 member added/removed (`group_action_type` 0/1, `other_handle`), 2 renamed (`group_title`), 3 left (0) / group photo (1) / other, 4 location sharing, 6 FaceTime |
| `balloon_bundle_id` | iMessage app; `URLBalloonProvider` = link preview |
| `thread_originator_guid` | Set on inline replies; the `guid` of the message being replied to |
| `date_edited` / `date_retracted` | Non-zero when edited / unsent. Text and blob hold the latest version |

Older macOS versions lack some of these columns; the script substitutes defaults and `--selftest` lists what's missing.

