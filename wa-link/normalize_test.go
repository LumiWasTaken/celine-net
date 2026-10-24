package main

import (
	"strings"
	"testing"
)

func msgOf(t *testing.T, recs []Rec, key string) (Rec, Rec) {
	t.Helper()
	r := find(recs, "message", key)
	if r == nil {
		t.Fatalf("message %s was not journaled", key)
	}
	return obj(r, "chat"), obj(r, "message")
}

func TestHistoryAndLiveMessagesBecomeCanonicalRecords(t *testing.T) {
	setup(t)
	if history, live := loadFixtures(t); history != 2 || live != 2 {
		t.Fatalf("loaded %d history chunks and %d live messages", history, live)
	}
	recs := journalRecs(t)

	// a chat addressed by its LID is archived under the phone JID; own messages have no sender
	chat, m := msgOf(t, recs, "A2")
	if str(chat, "key") != ALEX || str(chat, "kind") != "dm" || !truthy(m, "from_me") ||
		m["sender"] != nil || str(m, "text") != "no way!" || tsMs(m["ts"]) != T0+60_000 ||
		str(m, "type") != "text" {
		t.Fatalf("A2: chat %v message %v", chat, m)
	}
	if peer := obj(chat, "peer"); str(peer, "key") != ALEX || str(peer, "phone") != "+491700000001" {
		t.Fatalf("peer %v", peer)
	}
	// a group: its name, and a sender given by LID resolves to the phone JID
	chat, m = msgOf(t, recs, "G1")
	if str(chat, "key") != GROUP || str(chat, "kind") != "group" || str(chat, "name") != "Chess club" ||
		str(obj(m, "sender"), "key") != ALEX {
		t.Fatalf("G1: chat %v message %v", chat, m)
	}
	// media keeps what a later download needs; names come from the store and the history
	_, m = msgOf(t, recs, "G9")
	media, ref := obj(m, "media"), obj(obj(m, "media"), "ref")
	if str(m, "type") != "media" || str(media, "type") != "photo" || str(media, "caption") != "look" ||
		str(m, "text") != "look" || str(ref, "direct_path") != "/v/t62/abc" ||
		str(ref, "file_sha256") != strings.Repeat("01", 32) || str(ref, "mime") != "image/jpeg" ||
		str(ref, "size") != "1234" || str(ref, "media_key") == "" || str(ref, "enc_file_hash") != "AgI=" {
		t.Fatalf("G9: %v", m)
	}
	if s := obj(m, "sender"); str(s, "contact_name") != "Bea Example" || str(s, "push_name") != "bea!" {
		t.Fatalf("Bea: %v", s)
	}
	// a reaction is its own kind, a revoke stub is a system line, a missed call too
	if r := find(recs, "reaction", "G1"); r == nil || str(r, "emoji") != "👍" ||
		str(obj(r, "sender"), "key") != BEA {
		t.Fatalf("reaction %v", r)
	}
	if _, m = msgOf(t, recs, "G11"); str(m, "type") != "system" || str(m, "text") != "This message was deleted." {
		t.Fatalf("G11 %v", m)
	}
	if _, m = msgOf(t, recs, "G12"); str(m, "text") != "Missed call" {
		t.Fatalf("G12 %v", m)
	}
	// status broadcasts are not archived
	if find(recs, "message", "S1") != nil {
		t.Fatal("status@broadcast was journaled")
	}
	// edits and revokes name their target
	if e := find(recs, "edit", "A2"); e == nil || str(e, "text") != "no way!!" ||
		str(e, "chat_key") != ALEX || tsMs(e["ts"]) != T0+2*DAY {
		t.Fatalf("edit %v", e)
	}
	var del Rec
	for _, r := range recs {
		if r["kind"] == "delete" {
			del = r
		}
	}
	if del == nil || str(del, "chat_key") != ALEX || list(del, "message_keys")[0] != "A3" {
		t.Fatalf("delete %v", del)
	}
	// voice notes, replies; view-once and disappearing messages are never journaled
	if _, m = msgOf(t, recs, "V1"); str(obj(m, "media"), "type") != "voice" {
		t.Fatalf("V1 %v", m)
	}
	if find(recs, "message", "VO1") != nil || find(recs, "message", "EP1") != nil {
		t.Fatal("a view-once or disappearing message was journaled")
	}
	if _, m = msgOf(t, recs, "Q1"); str(m, "reply_key") != "A5" {
		t.Fatalf("Q1 %v", m)
	}
	// the phone's word on each chat: full sync "more", the on-demand answer "no_access"
	var ends []string
	for _, r := range recs {
		if r["kind"] == "history" && str(r, "chat_key") == ALEX {
			ends = append(ends, str(r, "end")+"/"+map[bool]string{true: "od", false: "full"}[truthy(r, "on_demand")])
		}
	}
	if strings.Join(ends, ",") != "more/full,no_access/od" {
		t.Fatalf("ends %v", ends)
	}
	// live lines: LID chat resolved through SenderAlt, push name on the sender
	chat, m = msgOf(t, recs, "L1")
	if str(chat, "key") != ALEX || str(obj(m, "sender"), "push_name") != "alex ✨" ||
		tsMs(m["ts"]) == 0 {
		t.Fatalf("L1: %v %v", chat, m)
	}
	if p := find(recs, "person", BEA); p == nil || str(obj(p, "person"), "push_name") != "bea!" {
		t.Fatalf("person %v", p)
	}
	// the same history and messages again (a fresh process) journal nothing new
	head := jr.Head()
	norm = NewNormalizer(fakeNames{}, emit)
	if loadFixtures(t); jr.Head() != head {
		t.Fatalf("the same input again appended: %d -> %d", head, jr.Head())
	}
}

func TestContentMapping(t *testing.T) {
	c := contentOf(Rec{"conversation": "hi"})
	if c.kind != "text" || c.text != "hi" {
		t.Fatal(c)
	}
	c = contentOf(Rec{"imageMessage": Rec{"caption": "look"}})
	if c.kind != "media" || str(c.media, "type") != "photo" || str(c.media, "caption") != "look" {
		t.Fatal(c)
	}
	c = contentOf(Rec{"videoMessage": Rec{"gifPlayback": true}})
	if str(c.media, "type") != "gif" {
		t.Fatal(c)
	}
	if contentOf(Rec{"reactionMessage": Rec{"text": "x"}}) != nil ||
		contentOf(Rec{"messageContextInfo": Rec{}}) != nil {
		t.Fatal("nothing to archive must be nil")
	}
	if c = contentOf(Rec{"pollCreationMessageV3": Rec{"name": "when?"}}); c.text != "Poll: when?" {
		t.Fatal(c)
	}
}

func TestCanonAndTimes(t *testing.T) {
	n := NewNormalizer(nil, func(Rec) {})
	n.Learn("777@lid", "4912345678@s.whatsapp.net")
	for in, want := range map[string]string{
		"777@lid": "4912345678@s.whatsapp.net", "888@lid": "888@lid",
		"4912345678:12@s.whatsapp.net": "4912345678@s.whatsapp.net", "4912@c.us": "4912@s.whatsapp.net",
		GROUP: GROUP, "": "",
	} {
		if got := n.Canon(in); got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
		}
	}
	if tsMs("1700000000") != T0 || tsMs(float64(T0)) != T0 || tsMs("2023-11-14T22:13:20Z") != T0 ||
		tsMs("0") != 0 {
		t.Fatal("times")
	}
}
