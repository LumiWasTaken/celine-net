// The tools Celine uses (POST /mcp, the official MCP Go SDK, streamable HTTP). Every
// tool only reads.
//
// Results: text lines for the model plus structuredContent {"records": [canonical
// message records with their seq], "where", "note"}; read_messages and
// read_around_message add "earlier" and "later" handles when there is more. The app
// applies the records to its chat archive and renders them with archive ids, so what
// Celine reads here it can cite.
package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

var (
	// swapped by setRealSends (main.go) and by tests
	ownJID = func() (types.JID, bool) { return types.JID{}, false }
	// live lookups (reads)
	lookupContact = func(ctx context.Context, jid types.JID) (Rec, error) {
		return nil, errors.New("WhatsApp is not reachable.")
	}
	lookupGroup = func(ctx context.Context, jid types.JID) (Rec, error) {
		return nil, errors.New("WhatsApp is not reachable.")
	}
	olderWait = 120 * time.Second
)

func ptr[T any](v T) *T { return &v }

func readOnly(open bool) *mcp.ToolAnnotations {
	return &mcp.ToolAnnotations{ReadOnlyHint: true, IdempotentHint: true, OpenWorldHint: ptr(open)}
}

// ---- rendering ----------------------------------------------------------------------------

func clockOf(ms int64) string {
	return time.UnixMilli(ms).Format("2006-01-02 15:04")
}

func who(m Rec) string {
	if truthy(m, "from_me") {
		return "me"
	}
	if n := personName(obj(m, "sender")); n != "" {
		return n
	}
	return "someone"
}

func line(msg Msg) string {
	m := obj(msg.Rec, "message")
	text := str(m, "text")
	if media := obj(m, "media"); media != nil {
		text = strings.TrimSpace("[" + str(media, "type") + "] " + text)
	}
	if msg.Deleted {
		text += " (deleted)"
	}
	if truthy(m, "edited") {
		text += " (edited)"
	}
	return fmt.Sprintf("%s %s %s: %s", msg.Key, clockOf(msg.TS), who(m), text)
}

func chatName(key string) string {
	chats, _ := jr.Chats()
	for _, c := range chats {
		if c.Key == key {
			return c.Name
		}
	}
	return key
}

func result(lines []string, structured Rec) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		Content:           []mcp.Content{&mcp.TextContent{Text: strings.Join(lines, "\n")}},
		StructuredContent: structured,
	}
}

func toolError(msg string) *mcp.CallToolResult {
	return &mcp.CallToolResult{IsError: true, Content: []mcp.Content{&mcp.TextContent{Text: msg}}}
}

func recordsOf(msgs []Msg) []Rec {
	out := make([]Rec, 0, len(msgs))
	for _, m := range msgs {
		out = append(out, m.Rec)
	}
	return out
}

func indexOf(msgs []Msg, key string) int {
	for i, m := range msgs {
		if m.Key == key {
			return i
		}
	}
	return -1
}

// canonChat: a JID as the journal keys it.
func canonChat(chat string) string {
	normMu.Lock()
	defer normMu.Unlock()
	if !strings.Contains(chat, "@") {
		chat = strings.TrimPrefix(chat, "+") + "@s.whatsapp.net"
	}
	return norm.Canon(chat)
}

// window: messages [lo, hi) of a chat with the handles.
func window(chat string, msgs []Msg, lo, hi int, note string) *mcp.CallToolResult {
	part := msgs[lo:hi]
	name := chatName(chat)
	lines := []string{fmt.Sprintf("%s: %d messages", name, len(part))}
	for _, m := range part {
		lines = append(lines, line(m))
	}
	st := Rec{"records": recordsOf(part), "where": name, "note": note}
	if lo > 0 && len(part) > 0 {
		st["earlier"] = Rec{"chat": chat, "before": part[0].Key}
		lines = append(lines, fmt.Sprintf("earlier: read chat=%s before=%s", chat, part[0].Key))
	}
	if hi < len(msgs) && len(part) > 0 {
		st["later"] = Rec{"chat": chat, "after": part[len(part)-1].Key}
		lines = append(lines, fmt.Sprintf("later: read chat=%s after=%s", chat, part[len(part)-1].Key))
	}
	return result(lines, st)
}

func clamp(v, def, max int) int {
	if v <= 0 {
		return def
	}
	if v > max {
		return max
	}
	return v
}

// ---- the tools ------------------------------------------------------------------------------

type chatsIn struct {
	Query string `json:"query,omitempty" jsonschema:"Part of a chat name or number."`
	Limit int    `json:"limit,omitempty" jsonschema:"The most chats, default 50."`
}

type readIn struct {
	Chat   string `json:"chat" jsonschema:"The chat JID, such as 491700000001@s.whatsapp.net or 12345-678@g.us"`
	Before string `json:"before,omitempty" jsonschema:"Only messages before this message id"`
	After  string `json:"after,omitempty" jsonschema:"Only messages after this message id"`
	Limit  int    `json:"limit,omitempty" jsonschema:"The most messages, default 50, at most 200."`
}

type aroundIn struct {
	Chat    string `json:"chat" jsonschema:"The chat JID"`
	Message string `json:"message" jsonschema:"The message id"`
	Limit   int    `json:"limit,omitempty" jsonschema:"Messages in all, default 40, at most 100."`
}

type olderIn struct {
	Chat  string `json:"chat" jsonschema:"The chat JID"`
	Count int    `json:"count,omitempty" jsonschema:"How many older messages, at most 50 (the default)."`
}

type contactIn struct {
	JID   string `json:"jid,omitempty" jsonschema:"The contact's JID."`
	Phone string `json:"phone,omitempty" jsonschema:"Or the phone number with country code."`
}

type groupIn struct {
	JID string `json:"jid" jsonschema:"The group JID, ending in @g.us."`
}

func toolChats(_ context.Context, _ *mcp.CallToolRequest, in chatsIn) (*mcp.CallToolResult, any, error) {
	all, err := jr.Chats()
	if err != nil {
		return toolError("The journal could not be read."), nil, nil
	}
	limit := clamp(in.Limit, 50, 500)
	q := strings.ToLower(strings.TrimSpace(in.Query))
	lines := []string{}
	var rows []Rec
	for _, c := range all {
		if q != "" && !strings.Contains(strings.ToLower(c.Name), q) && !strings.Contains(c.Key, q) {
			continue
		}
		if len(rows) >= limit {
			break
		}
		lines = append(lines, fmt.Sprintf("%s %s (%s, %d messages, last %s)", c.Key, c.Name,
			firstOf(c.Kind, "dm"), c.Messages, clockOf(c.LastTS)))
		rows = append(rows, Rec{"chat": c.Key, "name": c.Name, "kind": c.Kind,
			"last_ts": c.LastTS, "messages": c.Messages})
	}
	note := fmt.Sprintf("Listed %d WhatsApp chats, most recently active first.", len(rows))
	if len(rows) == 0 {
		lines = append(lines, "No chat matches.")
	}
	return result(append([]string{note}, lines...), Rec{"chats": rows, "where": "WhatsApp",
		"note": note}), nil, nil
}

func toolRead(_ context.Context, _ *mcp.CallToolRequest, in readIn) (*mcp.CallToolResult, any, error) {
	chat := canonChat(in.Chat)
	if chat == "" {
		return toolError("That is not a WhatsApp chat id."), nil, nil
	}
	msgs, err := jr.ChatMessages(chat)
	if err != nil {
		return toolError("The journal could not be read."), nil, nil
	}
	if len(msgs) == 0 {
		return toolError("No message of this chat is known."), nil, nil
	}
	limit := clamp(in.Limit, 50, 200)
	lo, hi := len(msgs)-limit, len(msgs)
	switch {
	case in.Before != "":
		i := indexOf(msgs, in.Before)
		if i < 0 {
			return toolError("That message is not in this chat."), nil, nil
		}
		lo, hi = i-limit, i
	case in.After != "":
		i := indexOf(msgs, in.After)
		if i < 0 {
			return toolError("That message is not in this chat."), nil, nil
		}
		lo, hi = i+1, i+1+limit
	}
	if lo < 0 {
		lo = 0
	}
	if hi > len(msgs) {
		hi = len(msgs)
	}
	name := chatName(chat)
	return window(chat, msgs, lo, hi, fmt.Sprintf("Read %d messages of %s.", hi-lo, name)), nil, nil
}

func toolAround(_ context.Context, _ *mcp.CallToolRequest, in aroundIn) (*mcp.CallToolResult, any, error) {
	chat := canonChat(in.Chat)
	msgs, err := jr.ChatMessages(chat)
	if err != nil {
		return toolError("The journal could not be read."), nil, nil
	}
	i := indexOf(msgs, in.Message)
	if i < 0 {
		return toolError("That message is not in this chat."), nil, nil
	}
	limit := clamp(in.Limit, 40, 100)
	lo, hi := i-limit/2, i+limit/2+1
	if lo < 0 {
		lo = 0
	}
	if hi > len(msgs) {
		hi = len(msgs)
	}
	name := chatName(chat)
	return window(chat, msgs, lo, hi, fmt.Sprintf("Read %d messages around one message in %s.",
		hi-lo, name)), nil, nil
}

func toolOlder(ctx context.Context, _ *mcp.CallToolRequest, in olderIn) (*mcp.CallToolResult, any, error) {
	chat := canonChat(in.Chat)
	if chat == "" {
		return toolError("That is not a WhatsApp chat id."), nil, nil
	}
	head := jr.Head()
	wait := waitAnswer(chat)
	defer dropWaiter(chat, wait)
	if _, _, e := askOlder(ctx, olderReq{Chat: chat, Count: in.Count}); e != nil {
		msg, _ := e.body["error"].(string)
		return toolError(msg), nil, nil
	}
	select {
	case <-wait:
	case <-time.After(olderWait):
		return toolError("The phone did not answer within two minutes. Open WhatsApp on the phone and ask again."), nil, nil
	case <-ctx.Done():
		return toolError("The request was stopped."), nil, nil
	}
	msgs, err := jr.ChatMessages(chat)
	if err != nil {
		return toolError("The journal could not be read."), nil, nil
	}
	var got []Msg
	for _, m := range msgs {
		if m.Seq > head {
			got = append(got, m)
		}
	}
	name := chatName(chat)
	note := fmt.Sprintf("The phone sent %d older messages of %s.", len(got), name)
	lines := []string{note}
	for _, m := range got {
		lines = append(lines, line(m))
	}
	return result(lines, Rec{"records": recordsOf(got), "where": name, "note": note}), nil, nil
}

func parsePerson(jid, phone string) (types.JID, bool) {
	if jid == "" && phone != "" {
		digits := strings.Map(func(r rune) rune {
			if r >= '0' && r <= '9' {
				return r
			}
			return -1
		}, phone)
		jid = digits + "@s.whatsapp.net"
	}
	j, err := types.ParseJID(jid)
	return j, err == nil && j.User != ""
}

func toolContact(ctx context.Context, _ *mcp.CallToolRequest, in contactIn) (*mcp.CallToolResult, any, error) {
	j, ok := parsePerson(in.JID, in.Phone)
	if !ok {
		return toolError("Pass a JID or a phone number with country code."), nil, nil
	}
	info, err := lookupContact(ctx, j)
	if err != nil {
		return toolError(err.Error()), nil, nil
	}
	lines := []string{fmt.Sprintf("Contact %s", j.String())}
	for _, k := range []string{"contact_name", "push_name", "business_name", "about", "verified_name"} {
		if v := str(info, k); v != "" {
			lines = append(lines, fmt.Sprintf("%s: %s", strings.ReplaceAll(k, "_", " "), v))
		}
	}
	info["note"] = "Looked up one WhatsApp contact."
	info["where"] = j.String()
	return result(lines, info), nil, nil
}

func toolGroup(ctx context.Context, _ *mcp.CallToolRequest, in groupIn) (*mcp.CallToolResult, any, error) {
	j, err := types.ParseJID(in.JID)
	if err != nil || j.Server != types.GroupServer {
		return toolError("That is not a group JID. A group JID ends in @g.us."), nil, nil
	}
	info, err := lookupGroup(ctx, j)
	if err != nil {
		return toolError(err.Error()), nil, nil
	}
	lines := []string{fmt.Sprintf("Group %s: %s", j.String(), str(info, "subject"))}
	if d := str(info, "description"); d != "" {
		lines = append(lines, "description: "+d)
	}
	if ps, ok := info["participants"].([]Rec); ok {
		lines = append(lines, fmt.Sprintf("%d participants", len(ps)))
		for _, p := range ps {
			lines = append(lines, fmt.Sprintf("- %s %s", str(p, "jid"), str(p, "name")))
		}
	}
	info["note"] = "Looked up one WhatsApp group."
	info["where"] = str(info, "subject")
	return result(lines, info), nil, nil
}

// ---- the server ------------------------------------------------------------------------------

func newServer() *mcp.Server {
	s := mcp.NewServer(&mcp.Implementation{Name: "whatsapp", Version: "1"}, nil)
	mcp.AddTool(s, &mcp.Tool{Name: "list_chats", Annotations: readOnly(false),
		Description: "List the WhatsApp chats that the linked device knows, most recently active first, with their JIDs. For anything already in the chat archive, the chat_archive tools are faster."},
		toolChats)
	mcp.AddTool(s, &mcp.Tool{Name: "read_messages", Annotations: readOnly(false),
		Description: "Read messages of one WhatsApp chat as the linked device has them, newest last. Use `before` or `after` to page."},
		toolRead)
	mcp.AddTool(s, &mcp.Tool{Name: "read_around_message", Annotations: readOnly(false),
		Description: "Read the messages before and after one WhatsApp message, half on each side."},
		toolAround)
	mcp.AddTool(s, &mcp.Tool{Name: "request_older_messages", Annotations: readOnly(true),
		Description: "Request messages older than the oldest the linked device has, from {{username}}'s phone. The call waits up to two minutes for the phone. Nothing is sent to any contact."},
		toolOlder)
	mcp.AddTool(s, &mcp.Tool{Name: "get_contact", Annotations: readOnly(true),
		Description: "Get one WhatsApp contact's profile: names, about text, business or not."},
		toolContact)
	mcp.AddTool(s, &mcp.Tool{Name: "get_group", Annotations: readOnly(true),
		Description: "Get one WhatsApp group's subject, description and participants."},
		toolGroup)
	return s
}

func mcpHandler(clients map[string]Client) http.Handler {
	s := newServer()
	h := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return s }, nil)
	return authed(clients, false, h.ServeHTTP)
}

// liveLookups: contact and group through the client (reads only).
func liveLookups(client *whatsmeow.Client) {
	lookupContact = func(ctx context.Context, j types.JID) (Rec, error) {
		if !isConnected() {
			return nil, errors.New("WhatsApp is not reachable.")
		}
		out := Rec{"jid": j.String()}
		if infos, err := client.GetUserInfo(ctx, []types.JID{j}); err == nil {
			if u, ok := infos[j]; ok {
				out["about"] = u.Status
				if u.VerifiedName != nil && u.VerifiedName.Details != nil {
					out["verified_name"] = u.VerifiedName.Details.GetVerifiedName()
				}
				out["has_picture"] = u.PictureID != ""
			}
		} else {
			return nil, errors.New("WhatsApp did not answer for this contact.")
		}
		if c, err := client.Store.Contacts.GetContact(ctx, j); err == nil && c.Found {
			out["contact_name"] = firstOf(c.FullName, c.FirstName)
			out["push_name"] = c.PushName
			out["business_name"] = c.BusinessName
		}
		return out, nil
	}
	lookupGroup = func(ctx context.Context, j types.JID) (Rec, error) {
		if !isConnected() {
			return nil, errors.New("WhatsApp is not reachable.")
		}
		g, err := client.GetGroupInfo(ctx, j)
		if err != nil {
			return nil, errors.New("WhatsApp did not answer for this group.")
		}
		var ps []Rec
		for _, p := range g.Participants {
			key := p.JID
			if !p.PhoneNumber.IsEmpty() {
				key = p.PhoneNumber
			}
			name := ""
			if c, err := client.Store.Contacts.GetContact(ctx, key); err == nil && c.Found {
				name = firstOf(c.FullName, c.FirstName, c.PushName)
			}
			ps = append(ps, Rec{"jid": key.String(), "name": name, "admin": p.IsAdmin || p.IsSuperAdmin})
		}
		return Rec{"jid": j.String(), "subject": g.Name, "description": g.Topic,
			"participants": ps}, nil
	}
}
