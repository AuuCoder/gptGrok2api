package app

import (
	"net/http/httptest"
	"testing"
	"time"
)

func TestMigrateLegacyICloudSessionToSingleAdmin(t *testing.T) {
	now := time.Now()
	store := &FileStore{state: State{
		Users: []User{{ID: "user_admin", IsAdmin: true, Status: StatusActive}},
		Accounts: []Account{
			{ID: "acc_admin", OwnerID: "user_admin", AppleID: "owner@example.com"},
			{ID: "acc_legacy", AppleID: "owner@example.com"},
		},
		Mailboxes: []Mailbox{{
			ID:        "mail_legacy",
			AccountID: "acc_legacy",
			Email:     "alias@icloud.com",
			APIToken:  "preserve-token",
		}},
		Messages: []Message{{ID: "msg_legacy", MailboxID: "mail_legacy"}},
		ICloudSession: &ICloudSession{
			AccountID: "acc_legacy",
			AppleID:   "owner@example.com",
			LoginStates: []LoginState{{
				Kind:        LoginStateAppleAccount,
				APIKey:      "valid-api-key",
				LastCheckOK: true,
				SavedAt:     now,
			}},
		},
		ICloudSessions: []ICloudSession{{
			OwnerID:   "user_admin",
			AccountID: "acc_admin",
			AppleID:   "owner@example.com",
			LoginStates: []LoginState{{
				Kind:            LoginStateICloudIMAP,
				IMAPEmail:       "owner@icloud.com",
				IMAPAppPassword: "preserve-app-password",
				LastCheckOK:     true,
			}},
		}},
		CreateSettings: []CreateSettings{{AccountIDs: []string{"acc_legacy"}}},
	}}

	if !store.migrateLegacyICloudSessionToAdminLocked() {
		t.Fatal("expected legacy session migration")
	}
	if store.state.ICloudSession != nil {
		t.Fatal("legacy global session was not removed")
	}
	if len(store.state.ICloudSessions) != 1 {
		t.Fatalf("sessions = %d, want 1", len(store.state.ICloudSessions))
	}
	session := store.state.ICloudSessions[0]
	if session.OwnerID != "user_admin" || session.AccountID != "acc_admin" {
		t.Fatalf("session owner/account = %q/%q", session.OwnerID, session.AccountID)
	}
	appleState, ok := loginStateByKind(session, LoginStateAppleAccount)
	if !ok || appleState.APIKey != "valid-api-key" {
		t.Fatalf("Apple Account state was not adopted: %+v", appleState)
	}
	imapState, ok := loginStateByKind(session, LoginStateICloudIMAP)
	if !ok || imapState.IMAPAppPassword != "preserve-app-password" {
		t.Fatalf("IMAP state was not preserved: %+v", imapState)
	}
	if len(store.state.Accounts) != 1 || store.state.Accounts[0].ID != "acc_admin" {
		t.Fatalf("accounts = %+v, want only acc_admin", store.state.Accounts)
	}
	if mailbox := store.state.Mailboxes[0]; mailbox.OwnerID != "user_admin" || mailbox.AccountID != "acc_admin" || mailbox.APIToken != "preserve-token" {
		t.Fatalf("mailbox migration lost ownership or token: %+v", mailbox)
	}
	if store.state.Messages[0].OwnerID != "user_admin" {
		t.Fatalf("message owner = %q", store.state.Messages[0].OwnerID)
	}
	if settings := store.state.CreateSettings[0]; settings.OwnerID != "user_admin" || len(settings.AccountIDs) != 1 || settings.AccountIDs[0] != "acc_admin" {
		t.Fatalf("create settings = %+v", settings)
	}
}

func TestMigrateLegacyICloudSessionRequiresSingleAdmin(t *testing.T) {
	store := &FileStore{state: State{
		Users: []User{
			{ID: "admin_one", IsAdmin: true, Status: StatusActive},
			{ID: "admin_two", IsAdmin: true, Status: StatusActive},
		},
		ICloudSession: &ICloudSession{AppleID: "owner@example.com"},
	}}
	if store.migrateLegacyICloudSessionToAdminLocked() {
		t.Fatal("legacy session should not move when ownership is ambiguous")
	}
	if store.state.ICloudSession == nil {
		t.Fatal("legacy session was unexpectedly removed")
	}
}

func TestInternalRequestUsesSingleAdminOwner(t *testing.T) {
	store := &FileStore{state: State{Users: []User{{
		ID:      "user_admin",
		IsAdmin: true,
		Status:  StatusActive,
	}}}}
	request := httptest.NewRequest("GET", "/api/icloud/session", nil)
	request.Header.Set("X-ChatGPT2API-Internal", "icloud-privacy-mail")
	if ownerID := requestOwnerID(request, store); ownerID != "user_admin" {
		t.Fatalf("ownerID = %q, want user_admin", ownerID)
	}
}

func TestInternalRequestDoesNotGuessBetweenAdmins(t *testing.T) {
	store := &FileStore{state: State{Users: []User{
		{ID: "admin_one", IsAdmin: true, Status: StatusActive},
		{ID: "admin_two", IsAdmin: true, Status: StatusActive},
	}}}
	request := httptest.NewRequest("GET", "/api/icloud/session", nil)
	request.Header.Set("X-ChatGPT2API-Internal", "icloud-privacy-mail")
	if ownerID := requestOwnerID(request, store); ownerID != "" {
		t.Fatalf("ownerID = %q, want empty for ambiguous admins", ownerID)
	}
}

func loginStateByKind(session ICloudSession, kind string) (LoginState, bool) {
	for _, state := range session.LoginStates {
		if state.Kind == kind {
			return state, true
		}
	}
	return LoginState{}, false
}
