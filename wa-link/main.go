// wa-link: celine-net's WhatsApp connector. A linked device that owns the WhatsApp
// connection and gives the app three doors on one port, each behind a bearer key
// (access.go):
//
//	/feed     every record it learned, as canonical NDJSON with a rising seq (journal.go)
//	/admin/*  status, QR code, marker, bridge lease, older messages, media (admin.go)
//	/mcp      the tools Celine uses (tools.go)
//
// It pairs with the phone like WhatsApp Web (QR code) and journals everything it
// receives in <state>/journal.db, never pruned. It never sends read receipts, presence
// or typing. It sends exactly four things, each to the user's OWN account and fixed
// to it in code: a request for older messages of one chat (history.go), a media retry
// receipt for expired media (media.go), and a text message or an image into the user's
// own "message yourself" chat (the `send` and `send_image` tools, tools.go), which is
// the chat bridge.
//
// When the phone unlinks it, wa-link exits; the app starts it again and it shows a
// new QR code.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	_ "modernc.org/sqlite"
)

// Status: what the connection is doing, in memory only.
type Status struct {
	State       string // starting, pairing, connected, disconnected, logged_out
	Since       int64
	Error       string
	QR          string
	QRAt        int64
	HistoryDone int
	Messages    int
}

var (
	mu       sync.Mutex
	status   = Status{State: "starting", Since: time.Now().UnixMilli()}
	pj       = protojson.MarshalOptions{UseProtoNames: false, EmitUnpopulated: false}
	jr       *Journal
	norm     *Normalizer
	normMu   sync.Mutex
	lease    Lease
	stateDir string
	exitFn   = os.Exit
	waClient *whatsmeow.Client
)

func now() int64 { return time.Now().UnixMilli() }

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func update(f func(*Status)) {
	mu.Lock()
	old := status.State
	f(&status)
	if status.State != old {
		status.Since = now()
	}
	mu.Unlock()
}

func snapshot() Status {
	mu.Lock()
	defer mu.Unlock()
	return status
}

// emit: one canonical record into the journal.
func emit(r Rec) {
	if _, err := jr.Append(r); err != nil {
		fmt.Fprintln(os.Stderr, "journal:", err)
	}
}

// ---- WhatsApp events ---------------------------------------------------------------------

func toMap(b []byte) Rec {
	var m Rec
	if err := json.Unmarshal(b, &m); err != nil {
		return nil
	}
	return m
}

// historyChunk: one HistorySync, as protojson. Returns the chats it spoke about.
func historyChunk(data Rec) []string {
	normMu.Lock()
	defer normMu.Unlock()
	return norm.History(data)
}

// liveLine: one live message as {"info", "message"}.
func liveLine(line Rec) string {
	normMu.Lock()
	defer normMu.Unlock()
	return norm.Live(line)
}

func handler(evt interface{}) {
	switch v := evt.(type) {
	case *events.HistorySync:
		b, err := pj.Marshal(v.Data)
		if err != nil {
			fmt.Fprintln(os.Stderr, "history marshal:", err)
			return
		}
		chats := historyChunk(toMap(b))
		update(func(s *Status) { s.HistoryDone++ })
		if v.Data.GetSyncType() == waHistorySync.HistorySync_ON_DEMAND {
			answered(chats)
		}
	case *events.Message:
		info, _ := json.Marshal(v.Info)
		msg, _ := pj.Marshal(v.Message)
		liveLine(Rec{"info": toMap(info), "message": toMap(msg)})
		update(func(s *Status) { s.Messages++ })
	case *events.PushName:
		normMu.Lock()
		key := norm.Canon(v.JID.String())
		if key != "" && v.NewPushName != "" && personish(key) {
			norm.push[key] = v.NewPushName
			if p := norm.person(key); p != nil {
				emit(Rec{"kind": "person", "person": p})
			}
		}
		normMu.Unlock()
	case *events.Contact:
		normMu.Lock()
		key := norm.Canon(v.JID.String())
		name := firstOf(v.Action.GetFullName(), v.Action.GetFirstName())
		if key != "" && name != "" && personish(key) {
			norm.contact[key] = name
			if p := norm.person(key); p != nil {
				emit(Rec{"kind": "person", "person": p})
			}
		}
		normMu.Unlock()
	case *events.MediaRetry:
		onMediaRetry(v) // media.go: the phone answered one of our media retry requests
	case *events.Connected:
		update(func(s *Status) { s.State = "connected"; s.QR = ""; s.Error = "" })
		ownPerson()
	case *events.Disconnected:
		update(func(s *Status) { s.State = "disconnected"; s.Error = "The connection to WhatsApp dropped." })
	case *events.StreamReplaced:
		update(func(s *Status) {
			s.State = "disconnected"
			s.Error = "Another WhatsApp Web session replaced this one."
		})
	case *events.LoggedOut:
		// the phone unlinked this device; whatsmeow deletes the session. Exit, so the
		// app starts a fresh wa-link that shows a new QR code.
		update(func(s *Status) { s.State = "logged_out"; s.Error = "The phone unlinked this device." })
		restartSoon()
	}
}

var restartDelay = 2 * time.Second

// restartSoon: exit after a moment (so the status is seen); the app, or Docker's
// restart policy, starts wa-link again and it shows a new QR code.
func restartSoon() {
	go func() {
		time.Sleep(restartDelay)
		exitFn(0)
	}()
}

// watchQR: the pairing codes into the status. When they run out unscanned, wa-link
// starts again for a new one; nobody has to restart it by hand.
func watchQR(qrChan <-chan whatsmeow.QRChannelItem) {
	for evt := range qrChan {
		switch evt.Event {
		case whatsmeow.QRChannelEventCode:
			update(func(s *Status) { s.State = "pairing"; s.QR = evt.Code; s.QRAt = now(); s.Error = "" })
		case whatsmeow.QRChannelSuccess.Event:
			update(func(s *Status) { s.QR = "" })
		default: // the codes ran out unscanned, or pairing failed
			text := "The QR code expired. A new one comes up in a moment."
			if evt.Event != whatsmeow.QRChannelTimeout.Event {
				text = "Pairing failed (" + evt.Event + "). A new code comes up in a moment."
			}
			update(func(s *Status) { s.State = "pairing"; s.QR = ""; s.Error = text })
			restartSoon()
		}
	}
}

// ownPerson: the account itself, marked as me, and its LID learned.
func ownPerson() {
	if waClient == nil || waClient.Store.ID == nil {
		return
	}
	own := waClient.Store.ID.ToNonAD()
	normMu.Lock()
	if lid := waClient.Store.GetLID(); !lid.IsEmpty() {
		norm.Learn(lid.User, own.User)
	}
	normMu.Unlock()
	p := Rec{"key": own.User + "@s.whatsapp.net", "is_me": true}
	if waClient.Store.PushName != "" {
		p["push_name"] = waClient.Store.PushName
	}
	emit(Rec{"kind": "person", "person": p})
}

// ---- the names whatsmeow's store knows ------------------------------------------------------

type storeNames struct{ cli *whatsmeow.Client }

func (s storeNames) PNForLID(lidUser string) string {
	if s.cli == nil || s.cli.Store == nil || s.cli.Store.LIDs == nil {
		return ""
	}
	pn, err := s.cli.Store.LIDs.GetPNForLID(context.Background(),
		types.JID{User: lidUser, Server: types.HiddenUserServer})
	if err != nil || pn.IsEmpty() {
		return ""
	}
	return pn.User
}

func (s storeNames) Contact(jid string) (string, string, string, string) {
	if s.cli == nil || s.cli.Store == nil || s.cli.Store.Contacts == nil {
		return "", "", "", ""
	}
	j, err := types.ParseJID(jid)
	if err != nil {
		return "", "", "", ""
	}
	c, err := s.cli.Store.Contacts.GetContact(context.Background(), j)
	if err != nil || !c.Found {
		return "", "", "", ""
	}
	return c.FullName, c.FirstName, c.PushName, c.BusinessName
}

// ---- start --------------------------------------------------------------------------------

func openState(dir string) (*os.File, error) {
	for _, d := range []string{dir, filepath.Join(dir, "media")} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return nil, err
		}
	}
	lock, err := lockState(dir)
	if err != nil {
		return nil, err
	}
	j, err := OpenJournal(filepath.Join(dir, "journal.db"))
	if err != nil {
		lock.Close()
		return nil, err
	}
	jr = j
	stateDir = dir
	return lock, nil
}

func main() {
	storePath := flag.String("store", "wa-link.db", "whatsmeow store (sqlite)")
	state := flag.String("state", "wa-link-state", "state directory: journal, media, lock")
	listen := flag.String("listen", "127.0.0.1:8767", "address for the doors")
	clientsFile := flag.String("clients-file", "/data/connector-clients",
		"who may connect: name:key:role per line or comma separated")
	flag.Parse()

	lock, err := openState(*state)
	if err != nil {
		fmt.Fprintln(os.Stderr, "wa-link:", err)
		os.Exit(1)
	}
	defer lock.Close()

	spec, err := os.ReadFile(*clientsFile)
	if err != nil {
		fmt.Fprintln(os.Stderr, "wa-link:", err)
		os.Exit(1)
	}
	clients, err := parseClients(string(spec))
	if err != nil {
		fmt.Fprintln(os.Stderr, "wa-link:", *clientsFile+":", err)
		os.Exit(1)
	}

	// ask the phone for as much history as it will give a desktop companion
	store.DeviceProps.Os = proto.String("celine-net")
	store.DeviceProps.PlatformType = waCompanionReg.DeviceProps_DESKTOP.Enum()
	store.DeviceProps.RequireFullSync = proto.Bool(true)
	store.DeviceProps.HistorySyncConfig = &waCompanionReg.DeviceProps_HistorySyncConfig{
		FullSyncDaysLimit:   proto.Uint32(36500),
		FullSyncSizeMbLimit: proto.Uint32(102400),
		StorageQuotaMb:      proto.Uint32(102400),
	}

	ctx := context.Background()
	client, err := openClient(ctx, *storePath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "wa-link: store:", err)
		os.Exit(1)
	}
	waClient = client
	norm = NewNormalizer(storeNames{client}, emit)
	client.AddEventHandler(handler)
	// a paired device that starts without internet keeps trying in the background;
	// WhatsApp then delivers what arrived meanwhile
	client.EnableAutoReconnect = true
	client.InitialAutoReconnect = true
	setRealSends(client)

	srv := &http.Server{Addr: *listen, Handler: routes(clients)}
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fmt.Fprintln(os.Stderr, "wa-link: listen:", err)
			exitFn(1)
		}
	}()

	if client.Store.ID == nil {
		qrChan, err := client.GetQRChannel(ctx)
		if err == nil {
			err = client.Connect()
		}
		if err != nil {
			// not paired and WhatsApp not reachable: one line, and the next start tries again
			fmt.Fprintln(os.Stderr, "wa-link: WhatsApp is not reachable:", err)
			os.Exit(1)
		}
		go watchQR(qrChan)
	} else {
		if err := client.Connect(); err != nil {
			update(func(s *Status) { s.State = "disconnected"; s.Error = "WhatsApp is not reachable." })
		}
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
	client.Disconnect()
	_ = srv.Close()
	_ = jr.Close()
}

// openClient: the device from whatsmeow's store, not connected.
func openClient(ctx context.Context, storePath string) (*whatsmeow.Client, error) {
	logger := waLog.Stdout("wa", "WARN", false)
	db, err := sql.Open("sqlite", "file:"+storePath+"?_pragma=foreign_keys(1)&_pragma=busy_timeout(10000)&_pragma=journal_mode(WAL)")
	if err != nil {
		return nil, err
	}
	container := sqlstore.NewWithDB(db, "sqlite3", logger)
	if err := container.Upgrade(ctx); err != nil {
		return nil, err
	}
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return nil, err
	}
	return whatsmeow.NewClient(device, logger), nil
}

// setRealSends: the sends and the image upload, each fixed to the own account.
func setRealSends(client *whatsmeow.Client) {
	historySend = func(ctx context.Context, msg *waE2E.Message) (string, error) {
		resp, err := client.SendPeerMessage(ctx, msg)
		return string(resp.ID), err
	}
	retrySend = func(ctx context.Context, info *types.MessageInfo, key []byte) error {
		return client.SendMediaRetryReceipt(ctx, info, key)
	}
	chatSend = func(ctx context.Context, to types.JID, msg *waE2E.Message, id string) error {
		_, err := client.SendMessage(ctx, to, msg, whatsmeow.SendRequestExtra{ID: types.MessageID(id)})
		return err
	}
	mediaUpload = func(ctx context.Context, data []byte) (whatsmeow.UploadResponse, error) {
		return client.Upload(ctx, data, whatsmeow.MediaImage)
	}
	newID = func() string { return string(client.GenerateMessageID()) }
	ownJID = func() (types.JID, bool) {
		if client.Store.ID == nil {
			return types.JID{}, false
		}
		return client.Store.ID.ToNonAD(), true
	}
	isConnected = func() bool { return client.IsConnected() && client.Store.ID != nil }
	liveLookups(client)
}

// routes: every door, each behind the key check.
func routes(clients map[string]Client) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/feed", authed(clients, false, jr.handleFeed))
	registerAdmin(mux, clients)
	mux.Handle("/mcp", mcpHandler(clients))
	return mux
}
