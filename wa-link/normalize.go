// The normaliser: WhatsApp's history chunks and live messages, as protojson, into the
// canonical records the app's feed consumer applies (server/celine/kb/feed.py):
//
//   - chats and people are keyed by phone JID (`<digits>@s.whatsapp.net`); LIDs resolve
//     through the history's phoneNumberToLidMappings, whatsmeow's LID store and a live
//     message's SenderAlt/RecipientAlt; an unresolved LID stays `<user>@lid`.
//   - message keys are WhatsApp's message ids.
//   - edits and revokes become "edit" and "delete" records; the app applies them when
//     their message is in the archive.
//
// Both inputs are handled as generic JSON: a history chunk is protojson of a
// HistorySync, a live message is {"info": MessageInfo as encoding/json, "message":
// protojson of the Message} (main.go's handler).
package main

import (
	"encoding/hex"
	"strconv"
	"strings"
	"time"
)

type Rec = map[string]any

// NameSource: what whatsmeow's store knows. A nil source is fine (tests).
type NameSource interface {
	PNForLID(lidUser string) string                          // "" when unknown
	Contact(jid string) (full, first, push, business string) // all "" when unknown
}

type Normalizer struct {
	lids    map[string]string // lid user -> phone user
	contact map[string]string // canonical key -> address book name
	push    map[string]string // canonical key -> push name
	src     NameSource
	emit    func(Rec)
}

func NewNormalizer(src NameSource, emit func(Rec)) *Normalizer {
	return &Normalizer{lids: map[string]string{}, contact: map[string]string{},
		push: map[string]string{}, src: src, emit: emit}
}

// ---- generic JSON helpers -----------------------------------------------------------

func obj(m Rec, k string) Rec {
	if m == nil {
		return nil
	}
	v, _ := m[k].(map[string]any)
	return v
}

func str(m Rec, k string) string {
	if m == nil {
		return ""
	}
	switch v := m[k].(type) {
	case string:
		return v
	case float64:
		return strconv.FormatInt(int64(v), 10)
	case int64:
		return strconv.FormatInt(v, 10)
	case int:
		return strconv.Itoa(v)
	}
	return ""
}

func list(m Rec, k string) []any {
	if m == nil {
		return nil
	}
	v, _ := m[k].([]any)
	return v
}

func truthy(m Rec, k string) bool {
	if m == nil {
		return false
	}
	switch v := m[k].(type) {
	case bool:
		return v
	case string:
		return v == "true"
	}
	return false
}

// tsMs: seconds or ms as a number or digit string, or an RFC 3339 time.
func tsMs(v any) int64 {
	switch t := v.(type) {
	case float64:
		return scale(int64(t))
	case int64:
		return scale(t)
	case int:
		return scale(int64(t))
	case string:
		if t == "" || t == "0" {
			return 0
		}
		if n, err := strconv.ParseInt(t, 10, 64); err == nil {
			return scale(n)
		}
		if tt, err := time.Parse(time.RFC3339Nano, t); err == nil {
			return tt.UnixMilli()
		}
	}
	return 0
}

func scale(n int64) int64 {
	if n <= 0 {
		return 0
	}
	if n < 10_000_000_000 {
		return n * 1000
	}
	return n
}

// ---- JIDs ---------------------------------------------------------------------------

func splitJID(jid string) (string, string) {
	at := strings.IndexByte(jid, '@')
	if jid == "" || at < 0 {
		return "", ""
	}
	user, server := jid[:at], jid[at+1:]
	if i := strings.IndexByte(user, ':'); i >= 0 {
		user = user[:i]
	}
	if i := strings.IndexByte(user, '.'); i >= 0 {
		user = user[:i]
	}
	return user, server
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// Learn: a LID and its phone JID (either may be a bare user).
func (n *Normalizer) Learn(lid, pn string) {
	lu, pu := lid, pn
	if strings.Contains(lid, "@") {
		lu, _ = splitJID(lid)
	}
	if strings.Contains(pn, "@") {
		pu, _ = splitJID(pn)
	}
	if lu != "" && isDigits(pu) {
		n.lids[lu] = pu
	}
}

// Canon: the key a JID is archived under, "" for none.
func (n *Normalizer) Canon(jid string) string {
	user, server := splitJID(jid)
	if user == "" {
		return ""
	}
	switch server {
	case "lid":
		pn := n.lids[user]
		if pn == "" && n.src != nil {
			if pn = n.src.PNForLID(user); pn != "" {
				n.lids[user] = pn
			}
		}
		if pn != "" {
			return pn + "@s.whatsapp.net"
		}
		return user + "@lid"
	case "s.whatsapp.net", "c.us":
		return user + "@s.whatsapp.net"
	}
	return user + "@" + server
}

func personish(key string) bool {
	return strings.HasSuffix(key, "@s.whatsapp.net") || strings.HasSuffix(key, "@lid") ||
		strings.HasSuffix(key, "@bot")
}

// person: the canonical person object for a key, names from what is known.
func (n *Normalizer) person(key string) Rec {
	if key == "" || !personish(key) {
		return nil
	}
	p := Rec{"key": key}
	user := key[:strings.IndexByte(key, '@')]
	if strings.HasSuffix(key, "@s.whatsapp.net") && isDigits(user) && len(user) >= 6 {
		p["phone"] = "+" + user
	}
	contact, push := n.contact[key], n.push[key]
	if n.src != nil {
		full, first, sp, business := n.src.Contact(key)
		if contact == "" {
			contact = firstOf(full, first, business)
		}
		if push == "" {
			push = sp
		}
	}
	if contact != "" {
		p["contact_name"] = contact
	}
	if push != "" {
		p["push_name"] = push
	}
	return p
}

func firstOf(s ...string) string {
	for _, v := range s {
		if v != "" {
			return v
		}
	}
	return ""
}

// chat: the canonical chat object, nil for chats that are not archived.
func (n *Normalizer) chat(key, name string) Rec {
	user, server := splitJID(key)
	if key == "status@broadcast" || server == "status_me" || server == "lid_me" || user == "" {
		return nil
	}
	c := Rec{"key": key}
	switch server {
	case "g.us":
		c["kind"] = "group"
	case "newsletter":
		c["kind"] = "channel"
	case "broadcast":
		c["kind"] = "group"
		if name == "" {
			name = "Broadcast list"
		}
	default:
		c["kind"] = "dm"
		if name != "" && n.contact[key] == "" {
			n.contact[key] = name // the phone's name for this chat
		}
		name = ""
		if p := n.person(key); p != nil {
			c["peer"] = p
		}
	}
	if name != "" {
		c["name"] = name
	}
	return c
}

// ---- message content ----------------------------------------------------------------

var wrappers = []string{"documentWithCaptionMessage", "editedMessage",
	"botInvokeMessage", "groupMentionedMessage"}

// vanishing: a view-once or disappearing message, which the sender meant not to last;
// the archive keeps none.
var vanishing = []string{"ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
	"viewOnceMessageV2Extension"}

func vanishes(msg Rec) bool {
	for _, w := range vanishing {
		if obj(msg, w) != nil {
			return true
		}
	}
	for _, v := range msg {
		if d, ok := v.(map[string]any); ok && truthy(d, "viewOnce") {
			return true
		}
	}
	return false
}

type mediaField struct{ field, mtype string }

var mediaFields = []mediaField{{"imageMessage", "photo"}, {"videoMessage", "video"},
	{"ptvMessage", "video"}, {"audioMessage", "audio"}, {"documentMessage", "document"},
	{"stickerMessage", "sticker"}, {"lottieStickerMessage", "sticker"},
	{"locationMessage", "location"}, {"liveLocationMessage", "location"},
	{"contactMessage", "contact"}, {"contactsArrayMessage", "contact"}}

func unwrap(msg Rec) Rec {
	if msg == nil {
		return Rec{}
	}
	for i := 0; i < 4; i++ {
		found := false
		for _, w := range wrappers {
			if inner := obj(msg, w); inner != nil {
				if m := obj(inner, "message"); m != nil {
					msg, found = m, true
					break
				}
			}
		}
		if !found {
			return msg
		}
	}
	return msg
}

type content struct {
	kind, text, reply string
	media             Rec
}

func b64hex(s string) string {
	if s == "" {
		return ""
	}
	if b := b64(s); len(b) == 32 {
		return hex.EncodeToString(b)
	}
	if len(s) == 64 {
		if _, err := hex.DecodeString(s); err == nil {
			return strings.ToLower(s)
		}
	}
	return ""
}

func mediaRefOf(body Rec) Rec {
	ref := Rec{}
	if v := str(body, "directPath"); v != "" {
		ref["direct_path"] = v
	}
	if v := str(body, "mediaKey"); v != "" {
		ref["media_key"] = v
	}
	if v := b64hex(str(body, "fileSHA256")); v != "" {
		ref["file_sha256"] = v
	}
	if v := str(body, "fileEncSHA256"); v != "" {
		ref["enc_file_hash"] = v
	}
	if v := str(body, "fileLength"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			ref["size"] = n
		}
	}
	if v := str(body, "mimetype"); v != "" {
		ref["mime"] = v
	}
	if len(ref) == 0 {
		return nil
	}
	return ref
}

// contentOf: what a Message carries, or nil for nothing to archive.
func contentOf(msg Rec) *content {
	m := unwrap(msg)
	if vanishes(msg) || vanishes(m) {
		return nil
	}
	reply := ""
	for _, v := range m {
		if d, ok := v.(map[string]any); ok {
			if ci := obj(d, "contextInfo"); ci != nil {
				if s := str(ci, "stanzaID"); s != "" {
					reply = s
				}
			}
		}
	}
	if s := str(m, "conversation"); s != "" {
		return &content{kind: "text", text: s, reply: reply}
	}
	if e := obj(m, "extendedTextMessage"); e != nil {
		if t := str(e, "text"); t != "" {
			return &content{kind: "text", text: t, reply: reply}
		}
		return nil
	}
	for _, f := range mediaFields {
		raw, ok := m[f.field]
		if !ok {
			continue
		}
		body, _ := raw.(map[string]any)
		mtype := f.mtype
		if mtype == "audio" && truthy(body, "PTT") {
			mtype = "voice"
		}
		if mtype == "video" && truthy(body, "gifPlayback") {
			mtype = "gif"
		}
		caption := str(body, "caption")
		media := Rec{"type": mtype}
		if caption != "" {
			media["caption"] = caption
		}
		if name := firstOf(str(body, "fileName"), str(body, "displayName")); name != "" {
			media["name"] = name
		}
		if ref := mediaRefOf(body); ref != nil {
			media["ref"] = ref
		}
		text := caption
		if mtype == "location" {
			text = firstOf(str(body, "name"), str(body, "address"), caption)
		}
		return &content{kind: "media", text: text, media: media, reply: reply}
	}
	if p := firstObj(m, "pollCreationMessage", "pollCreationMessageV3"); p != nil {
		return &content{kind: "text", text: "Poll: " + str(p, "name"), reply: reply}
	}
	if g := obj(m, "groupInviteMessage"); g != nil {
		return &content{kind: "system", text: strings.TrimSpace("Group invite: " + str(g, "groupName"))}
	}
	return nil
}

func firstObj(m Rec, keys ...string) Rec {
	for _, k := range keys {
		if o := obj(m, k); o != nil {
			return o
		}
	}
	return nil
}

// ---- history ------------------------------------------------------------------------

var historyEnds = map[string]string{
	"COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY":                 "complete",
	"COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY":                   "more",
	"COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY":         "more",
	"COMPLETE_ON_DEMAND_SYNC_WITH_MORE_MSG_ON_PRIMARY_BUT_NO_ACCESS": "no_access",
}

// History: one HistorySync chunk. Returns the chat keys it carried messages or an
// end for (for the `older` tool's waiters).
func (n *Normalizer) History(data Rec) []string {
	onDemand := str(data, "syncType") == "ON_DEMAND"
	for _, x := range list(data, "phoneNumberToLidMappings") {
		m, _ := x.(map[string]any)
		n.Learn(str(m, "lidJID"), str(m, "pnJID"))
	}
	for _, x := range list(data, "conversations") {
		c, _ := x.(map[string]any)
		if str(c, "ID") != "" && str(c, "pnJID") != "" {
			n.Learn(str(c, "ID"), str(c, "pnJID"))
		}
	}
	for _, x := range list(data, "pushnames") {
		p, _ := x.(map[string]any)
		key := n.Canon(str(p, "ID"))
		if name := str(p, "pushname"); key != "" && name != "" && personish(key) {
			n.push[key] = name
			if per := n.person(key); per != nil {
				n.emit(Rec{"kind": "person", "person": per})
			}
		}
	}
	var chats []string
	for _, x := range list(data, "conversations") {
		conv, _ := x.(map[string]any)
		id := str(conv, "ID")
		raw := id
		if strings.HasSuffix(id, "@lid") && str(conv, "pnJID") != "" {
			raw = str(conv, "pnJID")
		}
		key := n.Canon(raw)
		if key == "" {
			continue
		}
		name := firstOf(str(conv, "name"), str(conv, "displayName"))
		group := strings.HasSuffix(key, "@g.us") || strings.HasSuffix(key, "@newsletter")
		chatName := ""
		if group {
			chatName = name
		}
		ch := n.chat(key, chatName)
		if ch == nil {
			continue
		}
		if !group && name != "" && n.contact[key] == "" {
			n.contact[key] = name
		}
		for _, it := range list(conv, "messages") {
			item, _ := it.(map[string]any)
			n.webMessage(key, ch, obj(item, "message"))
		}
		if end, ok := historyEnds[str(conv, "endOfHistoryTransferType")]; ok {
			n.emit(Rec{"kind": "history", "chat_key": key, "end": end, "on_demand": onDemand})
		}
		chats = append(chats, key)
	}
	return chats
}

var missedCalls = map[string]bool{"CALL_MISSED_VOICE": true, "CALL_MISSED_VIDEO": true,
	"CALL_MISSED_GROUP_VOICE": true, "CALL_MISSED_GROUP_VIDEO": true}

func (n *Normalizer) webMessage(chatKey string, ch Rec, w Rec) {
	k := obj(w, "key")
	mid := str(k, "ID")
	ts := tsMs(w["messageTimestamp"])
	if mid == "" || ts == 0 {
		return
	}
	fromMe := truthy(k, "fromMe")
	msg := obj(w, "message")
	um := unwrap(msg)
	if proto := obj(um, "protocolMessage"); proto != nil {
		n.protocol(chatKey, proto, ts)
		return
	}
	var sender Rec
	if !fromMe {
		if ch["kind"] == "dm" {
			sender = n.person(chatKey)
		} else {
			sender = n.person(n.Canon(firstOf(str(w, "participant"), str(k, "participant"))))
		}
	}
	if r := obj(um, "reactionMessage"); r != nil {
		target := str(obj(r, "key"), "ID")
		if target != "" {
			rec := Rec{"kind": "reaction", "chat_key": chatKey, "message_key": target,
				"emoji": str(r, "text"), "ts": ts}
			if sender != nil {
				rec["sender"] = sender
			}
			n.emit(rec)
		}
		return
	}
	stub := str(w, "messageStubType")
	if stub == "REVOKE" || (stub != "" && msg == nil) {
		if stub == "REVOKE" {
			n.write(ch, mid, ts, fromMe, nil, "system", "This message was deleted.", nil, "")
		} else if missedCalls[stub] {
			n.write(ch, mid, ts, fromMe, nil, "system", "Missed call", nil, "")
		}
		return
	}
	c := contentOf(msg)
	if c == nil {
		return
	}
	n.write(ch, mid, ts, fromMe, sender, c.kind, c.text, c.media, c.reply)
}

func (n *Normalizer) write(ch Rec, mid string, ts int64, fromMe bool, sender Rec, kind,
	text string, media Rec, reply string) {
	m := Rec{"key": mid, "ts": ts, "from_me": fromMe, "type": kind, "text": text}
	if sender != nil {
		m["sender"] = sender
	}
	if media != nil {
		m["media"] = media
	}
	if reply != "" {
		m["reply_key"] = reply
	}
	n.emit(Rec{"kind": "message", "chat": ch, "message": m})
}

func (n *Normalizer) protocol(chatKey string, proto Rec, ts int64) {
	typ := str(proto, "type")
	target := str(obj(proto, "key"), "ID")
	if target == "" {
		return
	}
	switch typ {
	case "REVOKE":
		n.emit(Rec{"kind": "delete", "chat_key": chatKey, "message_keys": []any{target}, "ts": ts})
	case "MESSAGE_EDIT":
		if c := contentOf(obj(proto, "editedMessage")); c != nil {
			when := tsMs(proto["timestampMS"])
			if when == 0 {
				when = ts
			}
			n.emit(Rec{"kind": "edit", "chat_key": chatKey, "message_key": target,
				"ts": when, "text": c.text})
		}
	}
}

// ---- live ---------------------------------------------------------------------------

// Live: one live message line {"info": ..., "message": ...}. Returns its chat key.
func (n *Normalizer) Live(line Rec) string {
	info := obj(line, "info")
	msg := obj(line, "message")
	chatRaw, senderRaw := str(info, "Chat"), str(info, "Sender")
	if alt := str(info, "SenderAlt"); alt != "" {
		if strings.HasSuffix(senderRaw, "@lid") {
			n.Learn(senderRaw, alt)
		} else {
			n.Learn(alt, senderRaw)
		}
	}
	if alt := str(info, "RecipientAlt"); alt != "" && strings.HasSuffix(chatRaw, "@lid") {
		n.Learn(chatRaw, alt)
	}
	if !truthy(info, "IsGroup") && strings.HasSuffix(chatRaw, "@lid") &&
		str(info, "SenderAlt") != "" && !truthy(info, "IsFromMe") {
		n.Learn(chatRaw, str(info, "SenderAlt"))
	}
	key := n.Canon(chatRaw)
	ts := tsMs(info["Timestamp"])
	if key == "" || ts == 0 || str(info, "ID") == "" {
		return ""
	}
	if pn := str(info, "PushName"); pn != "" && !truthy(info, "IsFromMe") {
		if skey := n.Canon(senderRaw); skey != "" {
			n.push[skey] = pn
		}
	}
	ch := n.chat(key, "")
	if ch == nil {
		return ""
	}
	var participant any
	if truthy(info, "IsGroup") {
		participant = senderRaw
	}
	w := Rec{"key": Rec{"ID": str(info, "ID"), "fromMe": truthy(info, "IsFromMe"),
		"participant": participant},
		"messageTimestamp": float64(ts), "message": msg, "participant": participant}
	if msg == nil {
		delete(w, "message")
	}
	n.webMessage(key, ch, w)
	return key
}
