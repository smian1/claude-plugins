#!/usr/bin/env python3
"""
iMessage Query Script
Usage: python3 query_messages.py [options]   (--help for all options)

Always use this script instead of raw SQL because:
- Most messages have text=NULL with content only in the attributedBody blob
- Phone numbers and emails must be resolved to contact names
- attributedBody is an NSAttributedString typedstream that must be parsed
- Attachments, reactions, replies and group events need labelling
"""

import argparse
import collections
import csv
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime

# Database paths (current macOS user)
MESSAGES_DB = os.path.expanduser('~/Library/Messages/chat.db')
# Every AddressBook source (iCloud, local, ...) is loaded; the source folder names differ per Mac
CONTACTS_DBS = os.path.expanduser('~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb')

# U+FFFC marks where an inline attachment sits; the text column leaves it out
OBJECT_REPLACEMENT_CHAR = '￼'

ATTACHMENT_KINDS = ['image', 'video', 'audio', 'voice', 'pdf', 'file',
                    'contact', 'location', 'sticker', 'other']

# message.associated_message_type; 3000-3007 are the matching removals, 1000 is a sticker placed on a message
TAPBACKS = {2000: 'loved', 2001: 'liked', 2002: 'disliked', 2003: 'laughed at',
            2004: 'emphasized', 2005: 'questioned', 2006: 'reacted to', 2007: 'stickered'}

# Readable names for iMessage app extensions (message.balloon_bundle_id suffix)
APP_NAMES = {'PeerPaymentMessagesExtension': 'Apple Cash', 'PhotosMessagesApp': 'Photos',
             'FindMyMessagesApp': 'Find My', 'AskToBuy': 'Ask to Buy', 'HandwritingProvider': 'handwriting'}

# message columns that older macOS versions may lack, with the value to use instead
OPTIONAL_MESSAGE_COLUMNS = {
    'cache_has_attachments': '1', 'is_audio_message': '0', 'associated_message_type': '0',
    'associated_message_emoji': 'NULL', 'item_type': '0', 'group_action_type': '0',
    'group_title': 'NULL', 'balloon_bundle_id': 'NULL', 'other_handle': '0',
    'thread_originator_guid': 'NULL', 'date_edited': '0', 'date_retracted': '0',
}
REQUIRED_COLUMNS = {
    'message': ['ROWID', 'guid', 'date', 'text', 'attributedBody', 'is_from_me', 'handle_id'],
    'handle': ['ROWID', 'id'],
    'chat': ['ROWID', 'display_name', 'chat_identifier', 'style'],
    'chat_message_join': ['chat_id', 'message_id', 'message_date'],
    'chat_handle_join': ['chat_id', 'handle_id'],
    'attachment': ['ROWID', 'filename', 'mime_type', 'uti', 'transfer_name', 'total_bytes',
                   'is_sticker', 'hide_attachment'],
    'message_attachment_join': ['message_id', 'attachment_id'],
}

# One-time codes: a hint word plus a 4-8 digit number (G-123456 matches, dates and amounts don't)
CODE_HINT = re.compile(r'(?i)\b(code|verification|verify|otp|passcode|pin|2fa|one[- ]?time|log[- ]?in|sign[- ]?in)\b')
CODE_DIGITS = re.compile(r'(?<![\d.,:/])\d{4,8}(?![\d:/-]|[.,]\d)')

DATE_SQL = "datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime')"


def open_messages_db():
    try:
        conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM message LIMIT 1")
        return conn
    except sqlite3.Error as e:
        sys.exit(f"Can't read {MESSAGES_DB} ({e}).\n"
                 "Give the app running this script (Terminal, iTerm, VS Code, ...) Full Disk Access in "
                 "System Settings > Privacy & Security > Full Disk Access, then restart it.")


def table_columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def message_column(columns, name):
    """SQL for a message column, or its fallback value when this macOS version lacks it"""
    return f"m.{name}" if name in columns else OPTIONAL_MESSAGE_COLUMNS[name]


# ---------------------------------------------------------------- contacts

def load_contact_cards():
    """Every contact with a phone or email as (name, phones, emails).

    Phones are last-10-digit strings, emails lowercase. Companies without a
    person name use the organization name.
    """
    cards = []
    for db in glob.glob(CONTACTS_DBS):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            org_column = 'ZORGANIZATION' if 'ZORGANIZATION' in table_columns(conn, 'ZABCDRECORD') else 'NULL'
            people = {}
            for pk, first, last, org in conn.execute(
                    f"SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, {org_column} FROM ZABCDRECORD"):
                name = f"{first or ''} {last or ''}".strip() or (org or '').strip()
                if name:
                    people[pk] = (name, set(), set())
            for owner, phone in conn.execute(
                    "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"):
                digits = re.sub(r'[^\d]', '', phone)[-10:]
                if owner in people and digits:
                    people[owner][1].add(digits)
            for owner, email in conn.execute(
                    "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"):
                if owner in people:
                    people[owner][2].add(email.strip().lower())
            conn.close()
            cards += [card for card in people.values() if card[1] or card[2]]
        except sqlite3.Error:
            pass
    return cards


def load_contacts(cards=None):
    """Map of phone (last 10 digits) or lowercase email -> contact name"""
    contacts = {}
    for name, phones, emails in cards if cards is not None else load_contact_cards():
        for key in phones | emails:
            contacts[key] = name
    return contacts


def get_contact_name(phone_or_email, contacts):
    """Resolve phone/email to contact name"""
    if not phone_or_email:
        return "Unknown"
    if '@' in phone_or_email:
        return contacts.get(phone_or_email.lower(), phone_or_email)
    normalized = re.sub(r'[^\d]', '', phone_or_email)[-10:]
    return contacts.get(normalized, phone_or_email)


def find_people(term, cards):
    """Contacts whose name contains term, merged by name: {name: (phones, emails)}"""
    people = {}
    for name, phones, emails in cards:
        if term.lower() in name.lower():
            known_phones, known_emails = people.setdefault(name, (set(), set()))
            known_phones |= phones
            known_emails |= emails
    return people


def handle_ids_for(conn, phones, emails):
    """handle ROWIDs (every service: iMessage, SMS, ...) for these phones and emails"""
    ids = []
    for rowid, handle in conn.execute("SELECT ROWID, id FROM handle"):
        if '@' in handle:
            matched = handle.lower() in emails
        else:
            matched = re.sub(r'[^\d]', '', handle)[-10:] in phones
        if matched:
            ids.append(rowid)
    return ids


# ---------------------------------------------------------------- decoding

def decode_attributed_body(blob):
    """Read the plain string out of an NSAttributedString typedstream blob.

    Layout: ...NSString <type bytes> '+' <length> <utf-8 bytes>...
    Length is one byte, or 0x81 + 2 bytes / 0x82 + 4 bytes (little-endian).
    """
    try:
        i = blob.find(b'NSString')
        if i < 0:
            return None
        i = blob.find(b'+', i + len(b'NSString'))
        if i < 0:
            return None
        i += 1
        length = blob[i]
        if length == 0x81:
            length = int.from_bytes(blob[i + 1:i + 3], 'little')
            i += 3
        elif length == 0x82:
            length = int.from_bytes(blob[i + 1:i + 5], 'little')
            i += 5
        else:
            i += 1
        text = blob[i:i + length].decode('utf-8', errors='replace')
        return text.replace(OBJECT_REPLACEMENT_CHAR, '').strip()
    except IndexError:
        return None


def message_body(text, attributed_body):
    """Plain message text ('' if there is none), or None if the blob can't be parsed"""
    if text:
        return text.replace(OBJECT_REPLACEMENT_CHAR, '').strip()
    if not attributed_body:
        return ''
    return decode_attributed_body(attributed_body)


def extract_text(text, attributed_body):
    """Extract message text from text column or attributedBody blob"""
    body = message_body(text, attributed_body)
    if body is None:
        return "[could not decode]"
    return body or "[attachment/reaction]"


def mask_codes(text):
    """Replace digits in one-time-code messages with bullets"""
    if text and CODE_HINT.search(text):
        return CODE_DIGITS.sub(lambda m: '•' * len(m.group()), text)
    return text


# ---------------------------------------------------------------- labels

def attachment_kind(mime_type, uti, is_sticker, is_voice_message):
    """Classify an attachment as one of ATTACHMENT_KINDS"""
    mime_type = mime_type or ''
    if is_sticker:
        return 'sticker'
    if is_voice_message:
        return 'voice'
    if mime_type.startswith('image/'):
        return 'image'
    if mime_type.startswith('video/'):
        return 'video'
    if mime_type.startswith('audio/') or uti == 'com.apple.coreaudio-format':
        return 'audio'
    if mime_type == 'application/pdf':
        return 'pdf'
    if mime_type == 'text/vcard':
        return 'contact'
    if mime_type == 'text/x-vlocation':
        return 'location'
    if mime_type.startswith(('application/', 'text/')):
        return 'file'
    return 'other'


def format_size(num_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num_bytes < 1024 or unit == 'GB':
            return f"{num_bytes:.0f} {unit}" if unit == 'B' else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024


def load_attachments(cursor, message_id, is_voice_message):
    """Visible attachments of a message (link-preview payloads are hidden and skipped)"""
    cursor.execute("""
        SELECT a.transfer_name, a.filename, a.mime_type, a.uti, a.is_sticker, a.total_bytes
        FROM attachment a
        JOIN message_attachment_join j ON j.attachment_id = a.ROWID
        WHERE j.message_id = ? AND a.hide_attachment = 0
    """, [message_id])
    attachments = []
    for name, filename, mime_type, uti, is_sticker, total_bytes in cursor.fetchall():
        path = os.path.expanduser(filename) if filename else None
        attachments.append({
            'kind': attachment_kind(mime_type, uti, is_sticker, is_voice_message),
            'name': name or (os.path.basename(filename) if filename else None),
            'mime_type': mime_type,
            'size': total_bytes or 0,
            'path': path,
            'on_disk': bool(path and os.path.exists(path)),
        })
    return attachments


def describe_attachment(attachment, show_paths=False):
    details = [d for d in [attachment['name'], attachment['size'] and format_size(attachment['size'])] if d]
    label = attachment['kind'] + (f": {', '.join(details)}" if details else '')
    if show_paths:
        label += f" → {attachment['path']}" if attachment['on_disk'] else " (not downloaded to this Mac)"
    return f"[{label}]"


def describe_reaction(body, associated_type, emoji):
    """Reaction text as stored ("Loved “...”"), or a label when only an emoji or nothing is stored"""
    if body and re.search(r'[^\W\d_]', body):
        return body
    if associated_type >= 3000:
        return "[removed a reaction]"
    if associated_type == 2006 and emoji:
        return f"[reacted {emoji} to a message]"
    return f"[{TAPBACKS.get(associated_type, 'reacted to')} a message]"


def describe_event(item_type, group_action_type, group_title, other_name):
    """Label for group/system events, which have no message text"""
    who = other_name or 'someone'
    if item_type == 1:
        return f"[removed {who} from the group]" if group_action_type == 1 else f"[added {who} to the group]"
    if item_type == 2:
        return f"[renamed the group to “{group_title}”]" if group_title else "[removed the group name]"
    if item_type == 3:
        return {0: "[left the group]", 1: "[changed the group photo]"}.get(group_action_type, "[group change]")
    if item_type == 4:
        return "[location sharing update]"
    if item_type == 6:
        return "[FaceTime call]"
    return "[system event]"


def describe_app_message(balloon_bundle_id):
    """Label for an iMessage app message that has no text of its own"""
    segments = balloon_bundle_id.split(':')[-1].split('.')
    # Third-party extensions often end in generic parts (com.vendor.App.Messages, com.vendor.messages.iMessageExt)
    while len(segments) > 1 and re.fullmatch(r'(?i)i?messages?(ext|extension|app)?', segments[-1]):
        segments.pop()
    app = segments[-1]
    if app == 'URLBalloonProvider':
        return "[link preview]"
    if app not in APP_NAMES:
        app = re.sub(r'(Messages)?(Extension|App|Provider)$', '', app)
        app = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', app) or 'iMessage'
    return f"[{APP_NAMES.get(app, app)} app message]"


# ---------------------------------------------------------------- chats

def load_chats(conn, contacts):
    """{chat ROWID: {'id', 'name', 'identifier', 'is_group', 'label', 'handles', 'names'}}"""
    participants = {}
    for chat_id, handle in conn.execute("""
            SELECT chj.chat_id, h.id FROM chat_handle_join chj
            JOIN handle h ON chj.handle_id = h.ROWID"""):
        participants.setdefault(chat_id, []).append(handle)
    chats = {}
    for rowid, display_name, identifier, style in conn.execute(
            "SELECT ROWID, display_name, chat_identifier, style FROM chat"):
        handles = participants.get(rowid, [])
        names = [get_contact_name(h, contacts) for h in handles]
        is_group = style == 43 or len(handles) > 1
        if display_name:
            label = f"“{display_name}”"
        elif is_group:
            label = "group with " + ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else '')
        else:
            label = names[0] if names else (identifier or 'unknown chat')
        chats[rowid] = {'id': rowid, 'name': display_name or '', 'identifier': identifier or '',
                        'is_group': is_group, 'label': label, 'handles': handles, 'names': names}
    return chats


def find_chats(term, contacts):
    """Chats whose ROWID, name, identifier or participants match term, most recent first.

    term is a chat ROWID, a comma-separated list of ROWIDs, or text such as a
    group name, contact name, phone number or email.
    """
    conn = open_messages_db()
    chats = load_chats(conn, contacts)
    activity = {chat_id: (last_date, count) for chat_id, last_date, count in conn.execute(f"""
        SELECT chat_id, datetime(max(message_date)/1000000000 + 978307200, 'unixepoch', 'localtime'), count(*)
        FROM chat_message_join GROUP BY chat_id""")}
    conn.close()

    ids = [p.strip() for p in term.split(',')]
    by_rowid = all(p.isdigit() and len(p) <= 6 for p in ids)
    needle = term.lower()
    digits = re.sub(r'[^\d]', '', term)
    matches = []
    for chat in chats.values():
        if by_rowid:
            matched = str(chat['id']) in ids
        else:
            haystack = [chat['name'], chat['identifier']] + chat['handles'] + chat['names']
            matched = any(needle in s.lower() for s in haystack) or \
                (len(digits) >= 7 and any(digits[-10:] in re.sub(r'[^\d]', '', h) for h in chat['handles']))
        if matched:
            last_date, count = activity.get(chat['id'], (None, 0))
            members = ', '.join(chat['names'][:6]) + (f" +{len(chat['names']) - 6}" if len(chat['names']) > 6 else '')
            label = f"{chat['label']}: {members}" if chat['name'] and members else chat['label']
            matches.append({'rowid': chat['id'], 'label': label, 'kind': 'group' if chat['is_group'] else '1:1',
                            'last_date': last_date, 'count': count})
    matches.sort(key=lambda c: c['last_date'] or '', reverse=True)
    return matches


# ---------------------------------------------------------------- query

def load_reply_target(cursor, guid, contacts, hide_codes):
    """Sender and a short preview of the message a reply points to"""
    cursor.execute("""
        SELECT m.text, m.attributedBody, m.is_from_me, h.id
        FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.guid = ?""", [guid])
    row = cursor.fetchone()
    if not row:
        return None
    text, attributed_body, is_from_me, handle = row
    body = message_body(text, attributed_body)
    body = "[could not decode]" if body is None else (body or "[attachment]")
    if hide_codes:
        body = mask_codes(body)
    body = body.replace('\n', ' ')
    return {'sender': 'Me' if is_from_me else get_contact_name(handle, contacts),
            'preview': body if len(body) <= 60 else body[:57].rstrip() + '…'}


def query_messages(where_clause="1=1", params=(), limit=None, search=None, attachment_type=None,
                   no_reactions=False, oldest_first=False, resolve_replies=True, hide_codes=True):
    """Message records matching the filters, newest first unless oldest_first.

    Each record is a dict: id, date, sender, handle, is_from_me, chat_id, chat,
    is_group, type (message/reaction/event/app/unsent/empty), text, attachments,
    is_reply, reply_to, edited.
    search is matched case-insensitively against the decoded text, so it also
    finds messages whose content only lives in attributedBody.
    attachment_type keeps only messages with an attachment of that kind ('any' for all).
    """
    contacts = load_contacts()
    conn = open_messages_db()
    chats = load_chats(conn, contacts)
    cursor = conn.cursor()
    lookup_cursor = conn.cursor()
    columns = table_columns(conn, 'message')

    def col(name):
        return message_column(columns, name)

    if attachment_type:
        where_clause = f"({where_clause}) AND {col('cache_has_attachments')} = 1"
    if no_reactions:
        where_clause = (f"({where_clause}) AND NOT ({col('associated_message_type')} BETWEEN 2000 AND 3007"
                        f" OR {col('associated_message_type')} = 1000)")

    query = f"""
    SELECT
        m.ROWID,
        {DATE_SQL} as date_time,
        h.id,
        m.text,
        m.attributedBody,
        m.is_from_me,
        {col('cache_has_attachments')},
        {col('is_audio_message')},
        {col('associated_message_type')},
        {col('associated_message_emoji')},
        {col('item_type')},
        {col('group_action_type')},
        {col('group_title')},
        {col('balloon_bundle_id')},
        oh.id,
        {col('thread_originator_guid')},
        {col('date_edited')},
        {col('date_retracted')},
        (SELECT cmj.chat_id FROM chat_message_join cmj WHERE cmj.message_id = m.ROWID LIMIT 1)
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    LEFT JOIN handle oh ON oh.ROWID = {col('other_handle')} AND {col('other_handle')} <> 0
    WHERE {where_clause}
    ORDER BY m.date {'ASC' if oldest_first else 'DESC'}
    """
    params = list(params)
    # With a search term or attachment filter the limit is applied after decoding, not in SQL
    if limit and not search and not attachment_type:
        query += " LIMIT ?"
        params.append(limit)

    cursor.execute(query, params)
    needle = search.lower() if search else None
    records = []
    for (rowid, date_time, handle, text, attributed_body, is_from_me, has_attachments,
         is_voice_message, associated_type, emoji, item_type, group_action_type, group_title,
         balloon_bundle_id, other_handle, thread_guid, date_edited, date_retracted, chat_id) in cursor:
        body = message_body(text, attributed_body)
        if needle and needle not in (body or '').lower():
            continue

        attachments = load_attachments(lookup_cursor, rowid, is_voice_message) if has_attachments else []
        if attachment_type and not any(attachment_type in ('any', a['kind']) for a in attachments):
            continue

        if date_retracted:
            kind, message_text, attachments = 'unsent', "[unsent a message]", []
        elif item_type:
            other_name = get_contact_name(other_handle, contacts) if other_handle else None
            kind, attachments = 'event', []
            message_text = describe_event(item_type, group_action_type, group_title, other_name)
        elif 2000 <= associated_type <= 3007:
            kind, attachments = 'reaction', []
            message_text = describe_reaction(body, associated_type, emoji)
        elif body is None:
            kind, message_text = 'message', "[could not decode]"
        elif body or attachments:
            kind, message_text = 'message', body
        elif balloon_bundle_id:
            kind, message_text = 'app', describe_app_message(balloon_bundle_id)
        else:
            kind, message_text = 'empty', "[empty message]"

        if hide_codes:
            message_text = mask_codes(message_text)
        chat = chats.get(chat_id)
        records.append({
            'id': rowid,
            'date': date_time,
            'sender': "Me" if is_from_me else get_contact_name(handle, contacts),
            'handle': None if is_from_me else handle,
            'is_from_me': bool(is_from_me),
            'chat_id': chat_id,
            'chat': chat['label'] if chat else None,
            'is_group': bool(chat and chat['is_group']),
            'type': kind,
            'text': message_text,
            'attachments': attachments,
            'is_reply': bool(thread_guid),
            'reply_to': load_reply_target(lookup_cursor, thread_guid, contacts, hide_codes)
                        if thread_guid and resolve_replies else None,
            'edited': bool(date_edited),
        })
        if limit and len(records) >= limit:
            break

    conn.close()
    return records


# Common where clause builders: each returns (sql, params)
def by_phone(phone_pattern):
    """Filter by phone number (last 10 digits) or email"""
    return "h.id LIKE ?", [f"%{phone_pattern}%"]


def by_handle_ids(handle_ids):
    """Filter by handle ROWIDs (see handle_ids_for)"""
    return f"m.handle_id IN ({', '.join('?' for _ in handle_ids)})", list(handle_ids)


def by_date(date_str):
    """Filter by specific date (YYYY-MM-DD)"""
    return f"date({DATE_SQL}) = ?", [date_str]


def by_date_range(since=None, until=None):
    """Filter by inclusive date range (YYYY-MM-DD); either end may be None"""
    clauses, params = [], []
    if since:
        clauses.append(f"date({DATE_SQL}) >= ?")
        params.append(since)
    if until:
        clauses.append(f"date({DATE_SQL}) <= ?")
        params.append(until)
    return " AND ".join(clauses) or "1=1", params


def by_days_ago(days):
    """Filter by last N days"""
    # CAST needed: strftime returns TEXT, but date math returns INTEGER
    return ("m.date/1000000000 + 978307200 > CAST(strftime('%s', 'now', ?) AS INTEGER)",
            [f"-{int(days)} days"])


def by_chat_ids(chat_ids):
    """Filter by chat ROWIDs (includes my sent messages in group chats)"""
    placeholders = ', '.join('?' for _ in chat_ids)
    return (f"m.ROWID IN (SELECT message_id FROM chat_message_join WHERE chat_id IN ({placeholders}))",
            [int(c) for c in chat_ids])


def received_only():
    """Exclude messages sent by me"""
    return "m.is_from_me = 0", []


# ---------------------------------------------------------------- output

def display_text(record, show_paths=False):
    """One-line rendering of a record's content: reply marker, text, attachments, (edited)"""
    parts = []
    if record['reply_to']:
        parts.append(f"[↳ reply to {record['reply_to']['sender']}: “{record['reply_to']['preview']}”]")
    elif record['is_reply']:
        parts.append("[↳ reply]")
    if record['text']:
        parts.append(record['text'])
    parts += [describe_attachment(a, show_paths) for a in record['attachments']]
    if record['edited']:
        parts.append("(edited)")
    return " ".join(parts)


def chat_suffix(record, show_chat):
    return f" (in {record['chat']})" if show_chat and record['is_group'] and record['chat'] else ''


def print_text(records, show_chat, show_paths):
    for r in records:
        print(f"{r['date']} | {r['sender']}{chat_suffix(r, show_chat)}: {display_text(r, show_paths)}")


def print_markdown(records, title, show_chat, show_paths):
    """Markdown export in chronological order with a heading per day"""
    records = sorted(records, key=lambda r: (r['date'], r['id']))
    print(f"# {title}\n")
    if records:
        print(f"_{len(records)} messages · {records[0]['date'][:10]} → {records[-1]['date'][:10]}"
              f" · exported {date.today().isoformat()}_")
    day = None
    for r in records:
        if r['date'][:10] != day:
            day = r['date'][:10]
            d = datetime.strptime(day, '%Y-%m-%d')
            print(f"\n## {d:%A, %B} {d.day}, {d.year}\n")
        chat = f" _(in {r['chat']})_" if show_chat and r['is_group'] and r['chat'] else ''
        body = display_text(r, show_paths).replace('\n', '\n  ')
        print(f"- **{r['date'][11:16]} {r['sender']}**{chat}: {body}")


def print_csv(records, show_paths):
    writer = csv.writer(sys.stdout)
    writer.writerow(['date', 'sender', 'handle', 'chat', 'is_group', 'is_from_me', 'type', 'text',
                     'attachments', 'reply_to', 'edited'])
    for r in records:
        reply = f"{r['reply_to']['sender']}: {r['reply_to']['preview']}" if r['reply_to'] else ('yes' if r['is_reply'] else '')
        writer.writerow([r['date'], r['sender'], r['handle'] or '', r['chat'] or '', int(r['is_group']),
                         int(r['is_from_me']), r['type'], r['text'],
                         '; '.join(describe_attachment(a, show_paths) for a in r['attachments']),
                         reply, int(r['edited'])])


def compute_stats(records):
    """Counts only (no message text) for a set of records"""
    if not records:
        return {'messages': 0}
    dates = [r['date'] for r in records]
    months = collections.Counter(d[:7] for d in dates)
    by_period = months if len(months) <= 24 else collections.Counter(d[:4] for d in dates)
    weekdays = collections.Counter(datetime.strptime(d[:10], '%Y-%m-%d').strftime('%A') for d in dates)
    chats = collections.Counter(r['chat'] or 'unknown chat' for r in records)
    return {
        'messages': len(records),
        'first': min(dates),
        'last': max(dates),
        'sent': sum(r['is_from_me'] for r in records),
        'received': sum(not r['is_from_me'] for r in records),
        'by_sender': collections.Counter(r['sender'] for r in records).most_common(15),
        'by_chat': chats.most_common(10) if len(chats) > 1 else [],
        'period': 'month' if by_period is months else 'year',
        'by_period': sorted(by_period.items()),
        'by_weekday': [(day, weekdays[day]) for day in
                       ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']],
        'busiest_hours': collections.Counter(int(d[11:13]) for d in dates).most_common(5),
        'attachments': collections.Counter(a['kind'] for r in records for a in r['attachments']).most_common(),
        'types': collections.Counter(r['type'] for r in records).most_common(),
        'replies': sum(r['is_reply'] for r in records),
        'edited': sum(r['edited'] for r in records),
    }


def print_stats(stats):
    if not stats['messages']:
        print("No messages match.")
        return
    share = lambda n: f"{n * 100 / stats['messages']:.1f}%"
    print(f"## Summary\n\n- **Messages:** {stats['messages']:,} (sent {stats['sent']:,}, received {stats['received']:,})")
    print(f"- **Range:** {stats['first']} → {stats['last']}")
    print(f"- **Types:** " + ", ".join(f"{k} {v:,}" for k, v in stats['types']))
    if stats['attachments']:
        print(f"- **Attachments:** " + ", ".join(f"{k} {v:,}" for k, v in stats['attachments']))
    print(f"- **Replies:** {stats['replies']:,} · **Edited:** {stats['edited']:,}")
    print(f"- **Busiest hours:** " + ", ".join(f"{h:02d}:00 ({n:,})" for h, n in stats['busiest_hours']))
    print("\n## Top senders\n\n| Sender | Messages | Share |\n|---|---:|---:|")
    for sender, n in stats['by_sender']:
        print(f"| {sender} | {n:,} | {share(n)} |")
    if stats['by_chat']:
        print("\n## Busiest chats\n\n| Chat | Messages | Share |\n|---|---:|---:|")
        for chat, n in stats['by_chat']:
            print(f"| {chat} | {n:,} | {share(n)} |")
    print(f"\n## By {stats['period']}\n\n| {stats['period'].title()} | Messages |\n|---|---:|")
    for period, n in stats['by_period']:
        print(f"| {period} | {n:,} |")
    print("\n## By weekday\n\n| Day | Messages |\n|---|---:|")
    for day, n in stats['by_weekday']:
        print(f"| {day} | {n:,} |")


# ---------------------------------------------------------------- attachments export

def unique_path(directory, stem, ext):
    path = os.path.join(directory, stem + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{stem}_{n}{ext}")
        n += 1
    return path


def copy_attachments(records, dest_dir, convert_heic=False):
    """Copy attachment files into dest_dir as <date>_<time>_<name>; returns (copied, converted, not_downloaded)"""
    os.makedirs(dest_dir, exist_ok=True)
    copied = converted = missing = 0
    for r in records:
        stamp = r['date'].replace('-', '').replace(':', '').replace(' ', '_')
        for a in r['attachments']:
            if not a['on_disk']:
                missing += 1
                continue
            name = re.sub(r'[/:\x00]', '_', a['name'] or os.path.basename(a['path']))
            stem, ext = os.path.splitext(name)
            if convert_heic and ext.lower() in ('.heic', '.heif'):
                target = unique_path(dest_dir, f"{stamp}_{stem}", '.jpg')
                result = subprocess.run(['sips', '-s', 'format', 'jpeg', a['path'], '--out', target],
                                        capture_output=True)
                if result.returncode == 0:
                    converted += 1
                    continue
            shutil.copy2(a['path'], unique_path(dest_dir, f"{stamp}_{stem}", ext))
            copied += 1
    return copied, converted, missing


# ---------------------------------------------------------------- self-test

def selftest():
    """Check that this Mac's Messages database still matches what the script expects. No message text is printed."""
    conn = open_messages_db()
    failed = False

    def report(status, message):
        nonlocal failed
        failed = failed or status == 'FAIL'
        print(f"{status:<4}  {message}")

    for table, required in REQUIRED_COLUMNS.items():
        missing = set(required) - table_columns(conn, table)
        report('FAIL' if missing else 'PASS', f"table {table}: " + (f"missing {sorted(missing)}" if missing else "columns present"))
    missing_optional = set(OPTIONAL_MESSAGE_COLUMNS) - table_columns(conn, 'message')
    report('WARN' if missing_optional else 'PASS',
           f"optional message columns: " + (f"missing {sorted(missing_optional)} (those features are skipped)"
                                            if missing_optional else "all present"))

    rows = conn.execute("""SELECT text, attributedBody FROM message
        WHERE text <> '' AND attributedBody IS NOT NULL ORDER BY date DESC LIMIT 2000""").fetchall()
    if rows:
        exact = sum(decode_attributed_body(b) == t.replace(OBJECT_REPLACEMENT_CHAR, '').strip() for t, b in rows)
        pct = exact * 100 / len(rows)
        report('PASS' if pct >= 95 else 'FAIL',
               f"decoder matches the text column on {exact}/{len(rows)} messages ({pct:.1f}%)")
    else:
        report('WARN', "no messages store both text and attributedBody, so decoder accuracy can't be checked")

    blobs = [b for (b,) in conn.execute("""SELECT attributedBody FROM message
        WHERE (text IS NULL OR text = '') AND attributedBody IS NOT NULL ORDER BY date DESC LIMIT 2000""")]
    if blobs:
        decoded = sum(decode_attributed_body(b) is not None for b in blobs)
        pct = decoded * 100 / len(blobs)
        report('PASS' if pct >= 98 else 'FAIL', f"blob-only messages decoded: {decoded}/{len(blobs)} ({pct:.1f}%)")

    contacts = load_contacts()
    report('PASS' if contacts else 'WARN',
           f"contacts loaded: {len(contacts)} phone numbers/emails" if contacts else
           "no contacts loaded; senders will show as phone numbers/emails")

    files = [f for (f,) in conn.execute("""SELECT a.filename FROM attachment a
        WHERE a.hide_attachment = 0 AND a.filename IS NOT NULL ORDER BY a.ROWID DESC LIMIT 500""")]
    if files:
        on_disk = sum(os.path.exists(os.path.expanduser(f)) for f in files)
        report('INFO', f"recent attachments stored on this Mac: {on_disk}/{len(files)} (the rest are in iCloud)")
    conn.close()
    return 1 if failed else 0


# ---------------------------------------------------------------- CLI

def iso_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


def print_chats(chats):
    print(f"{'ROWID':>6}  {'last message':<19}  {'msgs':>6}  {'type':<5}  chat")
    for c in chats:
        print(f"{c['rowid']:>6}  {c['last_date'] or '-':<19}  {c['count']:>6}  {c['kind']:<5}  {c['label']}")


def main():
    parser = argparse.ArgumentParser(description='Query the local iMessage database (read-only)')
    who = parser.add_argument_group('who')
    who.add_argument('--person', help='Contact name; includes all their phone numbers and emails')
    who.add_argument('--phone', help='Phone number pattern (last 10 digits) or email')
    who.add_argument('--chat', help='Chat ROWID(s, comma-separated) or text matching a group name, '
                                    'contact name, phone or email; lists the chats if several match')
    who.add_argument('--sent', action='store_true', help='Include sent messages (default: received only)')
    when = parser.add_argument_group('when')
    when.add_argument('--date', type=iso_date, help='Specific date (YYYY-MM-DD)')
    when.add_argument('--since', type=iso_date, help='From this date, inclusive (YYYY-MM-DD)')
    when.add_argument('--until', type=iso_date, help='Up to this date, inclusive (YYYY-MM-DD)')
    when.add_argument('--days', type=int, help='Messages from last N days')
    what = parser.add_argument_group('what')
    what.add_argument('--search', help='Search text in messages (case-insensitive)')
    what.add_argument('--type', choices=['any'] + ATTACHMENT_KINDS,
                      help='Only messages with an attachment of this kind')
    what.add_argument('--no-reactions', action='store_true', help='Leave out tapbacks/emoji reactions and stickers')
    out = parser.add_argument_group('output')
    out.add_argument('--format', choices=['text', 'md', 'json', 'csv'], default='text',
                     help='text (default), md (chronological, a heading per day), json, csv')
    out.add_argument('--stats', action='store_true',
                     help='Counts instead of messages (sent and received, ignores --limit)')
    out.add_argument('--oldest-first', action='store_true',
                     help='Read from the start of the range (default: newest first)')
    out.add_argument('--limit', type=int, default=50, help='Max messages (default: 50, 0 = no limit)')
    out.add_argument('--paths', action='store_true',
                     help='Show attachment file paths (or that the file is not downloaded)')
    out.add_argument('--show-codes', action='store_true', help="Don't mask one-time verification codes")
    files = parser.add_argument_group('attachment files')
    files.add_argument('--copy-to', metavar='DIR', help='Copy the matching messages\' attachment files into DIR')
    files.add_argument('--convert-heic', action='store_true', help='With --copy-to, convert HEIC photos to JPG')
    tools = parser.add_argument_group('tools')
    tools.add_argument('--list-chats', metavar='TERM', help='List chats matching TERM with their ROWIDs and exit')
    tools.add_argument('--selftest', action='store_true',
                       help='Check the database format and decoder accuracy (prints no message text)')
    args = parser.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if args.stats and args.format == 'csv':
        parser.error('--stats supports text, md and json')
    if args.convert_heic and not args.copy_to:
        parser.error('--convert-heic needs --copy-to')

    cards = load_contact_cards()
    contacts = load_contacts(cards)

    if args.list_chats:
        chats = find_chats(args.list_chats, contacts)
        if not chats:
            sys.exit(f'No chats match "{args.list_chats}"')
        print_chats(chats)
        sys.exit(0)

    filters = []
    title = "iMessage export"
    if args.chat:
        chats = find_chats(args.chat, contacts)
        if not chats:
            sys.exit(f'No chats match "{args.chat}"')
        if len(chats) > 1 and not all(p.strip().isdigit() for p in args.chat.split(',')):
            print(f'{len(chats)} chats match "{args.chat}". Rerun with --chat ROWID (or several, comma-separated):')
            print_chats(chats)
            sys.exit(2)
        filters.append(by_chat_ids([c['rowid'] for c in chats]))
        title = f"iMessage: {chats[0]['label']}" if len(chats) == 1 else title
    if args.person:
        people = find_people(args.person, cards)
        exact = {n: v for n, v in people.items() if n.lower() == args.person.lower()}
        people = exact or people
        if not people:
            sys.exit(f'No contact named "{args.person}" with a phone number or email')
        if len(people) > 1:
            print(f'{len(people)} contacts match "{args.person}". Rerun with --person "FULL NAME":')
            for name in sorted(people):
                print(f"  {name}")
            sys.exit(2)
        (name, (phones, emails)), = people.items()
        conn = open_messages_db()
        handle_ids = handle_ids_for(conn, phones, emails)
        conn.close()
        if not handle_ids:
            sys.exit(f'No messages with any of {name}\'s numbers or emails')
        print(f"{name}: {len(phones)} phone number(s), {len(emails)} email(s), "
              f"{len(handle_ids)} message handle(s)", file=sys.stderr)
        filters.append(by_handle_ids(handle_ids))
        title = f"iMessage: {name}"
    if args.phone:
        filters.append(by_phone(args.phone))
    if args.date:
        filters.append(by_date(args.date))
    if args.since or args.until:
        filters.append(by_date_range(args.since, args.until))
    if args.days:
        filters.append(by_days_ago(args.days))
    if not args.sent and not args.stats:
        filters.append(received_only())

    where = " AND ".join(sql for sql, _ in filters) if filters else "1=1"
    params = [p for _, ps in filters for p in ps]

    records = query_messages(
        where, params,
        limit=None if args.stats else (args.limit or None),
        search=args.search,
        attachment_type=args.type or ('any' if args.copy_to else None),
        no_reactions=args.no_reactions,
        oldest_first=args.oldest_first,
        resolve_replies=not args.stats,
        hide_codes=not args.show_codes,
    )
    show_chat = len({r['chat_id'] for r in records}) > 1

    if args.stats:
        stats = compute_stats(records)
        if args.format == 'json':
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print_stats(stats)
    elif args.format == 'md':
        print_markdown(records, title, show_chat, args.paths)
    elif args.format == 'json':
        print(json.dumps(records, ensure_ascii=False, indent=2))
    elif args.format == 'csv':
        print_csv(records, args.paths)
    else:
        print_text(records, show_chat, args.paths)

    if args.copy_to:
        copied, converted, missing = copy_attachments(records, args.copy_to, args.convert_heic)
        print(f"Copied {copied} file(s), converted {converted} HEIC to JPG, skipped {missing} not downloaded "
              f"to this Mac → {args.copy_to}", file=sys.stderr)


if __name__ == "__main__":
    main()
