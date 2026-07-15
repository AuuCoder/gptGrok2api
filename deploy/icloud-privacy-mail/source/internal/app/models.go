package app

import (
	"encoding/json"
	"strings"
	"time"
)

const (
	StatusActive    = "active"
	StatusAvailable = "available"
	StatusUsed      = "used"
	StatusFailed    = "failed"
	StatusDisabled  = "disabled"

	ICloudStatusActive       = "active"
	ICloudStatusNeedLogin    = "need_login"
	ICloudStatusNeed2FA      = "need_2fa"
	ICloudStatusNoICloudPlus = "no_icloud_plus"
	ICloudStatusRateLimited  = "rate_limited"
	ICloudStatusFailed       = "failed"
)

type State struct {
	NextID         int              `json:"next_id"`
	Users          []User           `json:"users,omitempty"`
	WebSessions    []WebSession     `json:"web_sessions,omitempty"`
	Accounts       []Account        `json:"accounts"`
	Mailboxes      []Mailbox        `json:"mailboxes"`
	Messages       []Message        `json:"messages"`
	ICloudSession  *ICloudSession   `json:"icloud_session,omitempty"`
	ICloudSessions []ICloudSession  `json:"icloud_sessions,omitempty"`
	CreateSettings []CreateSettings `json:"create_settings,omitempty"`
}

const (
	LoginStateICloudWeb    = "icloud_web"
	LoginStateAppleAccount = "apple_account"
	LoginStateICloudIMAP   = "icloud_imap"
)

type User struct {
	ID           string    `json:"id"`
	Username     string    `json:"username"`
	PasswordHash string    `json:"password_hash"`
	IsAdmin      bool      `json:"is_admin,omitempty"`
	Status       string    `json:"status"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
	LastLoginAt  time.Time `json:"last_login_at,omitempty"`
}

type WebSession struct {
	TokenHash  string    `json:"token_hash"`
	UserID     string    `json:"user_id,omitempty"`
	IsAdmin    bool      `json:"is_admin,omitempty"`
	CreatedAt  time.Time `json:"created_at"`
	LastSeenAt time.Time `json:"last_seen_at"`
	ExpiresAt  time.Time `json:"expires_at"`
}

type Account struct {
	ID           string    `json:"id"`
	OwnerID      string    `json:"owner_id,omitempty"`
	Label        string    `json:"label"`
	AppleID      string    `json:"apple_id"`
	Status       string    `json:"status"`
	ICloudStatus string    `json:"icloud_status"`
	Note         string    `json:"note"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

type Mailbox struct {
	ID                string    `json:"id"`
	OwnerID           string    `json:"owner_id,omitempty"`
	AccountID         string    `json:"account_id"`
	Label             string    `json:"label"`
	Email             string    `json:"email"`
	APIToken          string    `json:"api_token"`
	APIActive         bool      `json:"api_active"`
	ICloudActive      bool      `json:"icloud_active"`
	ReceiveCount      int       `json:"receive_count"`
	OpenAIClaimed     bool      `json:"openai_claimed,omitempty"`
	GrokClaimed       bool      `json:"grok_claimed,omitempty"`
	Status            string    `json:"status"`
	Note              string    `json:"note"`
	LastSyncAt        time.Time `json:"last_sync_at,omitempty"`
	LastSyncUID       string    `json:"last_sync_uid,omitempty"`
	LastCodeMessageID string    `json:"last_code_message_id,omitempty"`
	LastCodeAt        time.Time `json:"last_code_at,omitempty"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`
}

type Message struct {
	ID         string    `json:"id"`
	OwnerID    string    `json:"owner_id,omitempty"`
	MailboxID  string    `json:"mailbox_id"`
	RemoteID   string    `json:"remote_id,omitempty"`
	Source     string    `json:"source,omitempty"`
	Subject    string    `json:"subject"`
	From       string    `json:"from"`
	Body       string    `json:"body"`
	ReceivedAt time.Time `json:"received_at"`
	CreatedAt  time.Time `json:"created_at"`
}

type ICloudSession struct {
	OwnerID            string          `json:"owner_id,omitempty"`
	AccountID          string          `json:"account_id,omitempty"`
	SavedAt            time.Time       `json:"saved_at"`
	AppleID            string          `json:"apple_id,omitempty"`
	DSID               string          `json:"dsid"`
	ClientID           string          `json:"client_id"`
	ClientBuildNumber  string          `json:"client_build_number"`
	MasteringNumber    string          `json:"client_mastering_number"`
	PremiumMailBaseURL string          `json:"premium_mail_base_url"`
	MailGatewayBaseURL string          `json:"mail_gateway_base_url,omitempty"`
	MailBaseURL        string          `json:"mail_base_url,omitempty"`
	Host               string          `json:"host"`
	IsICloudPlus       bool            `json:"is_icloud_plus"`
	CanCreateHME       bool            `json:"can_create_hme"`
	Cookies            []SessionCookie `json:"cookies"`
	LoginStates        []LoginState    `json:"login_states,omitempty"`
	Note               string          `json:"note,omitempty"`
	LastCheckedAt      time.Time       `json:"last_checked_at,omitempty"`
	LastCheckOK        bool            `json:"last_check_ok,omitempty"`
	LastStatusMessage  string          `json:"last_status_message,omitempty"`
}

type LoginState struct {
	Kind              string          `json:"kind"`
	Host              string          `json:"host,omitempty"`
	Origin            string          `json:"origin,omitempty"`
	SavedAt           time.Time       `json:"saved_at,omitempty"`
	Cookies           []SessionCookie `json:"cookies,omitempty"`
	Scnt              string          `json:"scnt,omitempty"`
	SessionID         string          `json:"session_id,omitempty"`
	APIKey            string          `json:"api_key,omitempty"`
	DataAccessToken   string          `json:"data_access_token,omitempty"`
	UserAgent         string          `json:"user_agent,omitempty"`
	Note              string          `json:"note,omitempty"`
	IMAPEmail         string          `json:"imap_email,omitempty"`
	IMAPUsername      string          `json:"imap_username,omitempty"`
	IMAPHost          string          `json:"imap_host,omitempty"`
	IMAPPort          int             `json:"imap_port,omitempty"`
	IMAPAppPassword   string          `json:"imap_app_password,omitempty"`
	IMAPLastSyncAt    time.Time       `json:"imap_last_sync_at,omitempty"`
	IMAPLastSyncUID   string          `json:"imap_last_sync_uid,omitempty"`
	ManageExpiresAt   time.Time       `json:"manage_expires_at,omitempty"`
	LastCheckedAt     time.Time       `json:"last_checked_at,omitempty"`
	LastCheckOK       bool            `json:"last_check_ok,omitempty"`
	LastStatusMessage string          `json:"last_status_message,omitempty"`
}

type CreateSettings struct {
	OwnerID                       string    `json:"owner_id,omitempty"`
	Label                         string    `json:"label,omitempty"`
	Note                          string    `json:"note,omitempty"`
	AccountIDs                    []string  `json:"account_ids,omitempty"`
	CreateChannel                 string    `json:"create_channel,omitempty"`
	SchedulerCreateChannel        string    `json:"scheduler_create_channel,omitempty"`
	AppleAccountTwoFactorMethod   string    `json:"apple_account_two_factor_method,omitempty"`
	ICloudWebTwoFactorMethod      string    `json:"icloud_web_two_factor_method,omitempty"`
	SchedulerIntervalMinutes      int       `json:"scheduler_interval_minutes,omitempty"`
	SchedulerRoundIntervalSeconds int       `json:"scheduler_round_interval_seconds,omitempty"`
	MailboxPageSize               int       `json:"mailbox_page_size,omitempty"`
	UpdatedAt                     time.Time `json:"updated_at,omitempty"`
}

func (a *Account) UnmarshalJSON(data []byte) error {
	type alias Account
	aux := struct {
		*alias
		LegacyOwnerID string `json:"browser_key,omitempty"`
	}{alias: (*alias)(a)}
	if err := json.Unmarshal(data, &aux); err != nil {
		return err
	}
	if a.OwnerID == "" {
		a.OwnerID = aux.LegacyOwnerID
	}
	return nil
}

func (m *Mailbox) UnmarshalJSON(data []byte) error {
	type alias Mailbox
	aux := struct {
		*alias
		LegacyOwnerID string `json:"browser_key,omitempty"`
	}{alias: (*alias)(m)}
	if err := json.Unmarshal(data, &aux); err != nil {
		return err
	}
	if m.OwnerID == "" {
		m.OwnerID = aux.LegacyOwnerID
	}
	return nil
}

func (m *Message) UnmarshalJSON(data []byte) error {
	type alias Message
	aux := struct {
		*alias
		LegacyOwnerID string `json:"browser_key,omitempty"`
	}{alias: (*alias)(m)}
	if err := json.Unmarshal(data, &aux); err != nil {
		return err
	}
	if m.OwnerID == "" {
		m.OwnerID = aux.LegacyOwnerID
	}
	return nil
}

func (s *ICloudSession) UnmarshalJSON(data []byte) error {
	type alias ICloudSession
	aux := struct {
		*alias
		LegacyOwnerID               string `json:"browser_key,omitempty"`
		LegacyAppleAccountScnt      string `json:"apple_account_scnt,omitempty"`
		LegacyAppleAccountSessionID string `json:"apple_account_session_id,omitempty"`
	}{alias: (*alias)(s)}
	if err := json.Unmarshal(data, &aux); err != nil {
		return err
	}
	if s.OwnerID == "" {
		s.OwnerID = aux.LegacyOwnerID
	}
	if len(s.Cookies) > 0 && !hasLoginStateKind(s.LoginStates, LoginStateICloudWeb) {
		s.LoginStates = append(s.LoginStates, LoginState{
			Kind:    LoginStateICloudWeb,
			Host:    s.Host,
			Origin:  iCloudOrigin(*s),
			SavedAt: s.SavedAt,
			Cookies: append([]SessionCookie(nil), s.Cookies...),
			Note:    "iCloud web login state migrated from legacy session",
		})
	}
	if strings.TrimSpace(aux.LegacyAppleAccountScnt) != "" && !hasLoginStateKind(s.LoginStates, LoginStateAppleAccount) {
		s.LoginStates = append(s.LoginStates, LoginState{
			Kind:      LoginStateAppleAccount,
			Host:      appleAccountManageHostForICloudHost(s.Host),
			Origin:    appleAccountManageOriginForHost(s.Host),
			SavedAt:   s.SavedAt,
			Scnt:      aux.LegacyAppleAccountScnt,
			SessionID: aux.LegacyAppleAccountSessionID,
			UserAgent: appleAccountManageUserAgent,
			Note:      "Apple Account login state migrated from legacy session",
		})
	}
	return nil
}

func hasLoginStateKind(states []LoginState, kind string) bool {
	for _, state := range states {
		if state.Kind == kind {
			return true
		}
	}
	return false
}

type SessionCookie struct {
	Name     string  `json:"name"`
	Value    string  `json:"value"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Expires  float64 `json:"expires,omitempty"`
	Secure   bool    `json:"secure,omitempty"`
	HTTPOnly bool    `json:"http_only,omitempty"`
	SameSite string  `json:"same_site,omitempty"`
}

type publicAccount struct {
	ID           string `json:"id"`
	OwnerID      string `json:"owner_id,omitempty"`
	Owner        string `json:"owner,omitempty"`
	Label        string `json:"label"`
	AppleID      string `json:"apple_id"`
	Status       string `json:"status"`
	ICloudStatus string `json:"icloud_status"`
	Note         string `json:"note"`
	CreatedAt    string `json:"created_at"`
	UpdatedAt    string `json:"updated_at"`
}

type publicMailbox struct {
	ID                 string `json:"id"`
	OwnerID            string `json:"owner_id,omitempty"`
	Owner              string `json:"owner,omitempty"`
	AccountID          string `json:"account_id"`
	AccountLabel       string `json:"account_label,omitempty"`
	AccountAppleID     string `json:"account_apple_id,omitempty"`
	CreateChannel      string `json:"create_channel,omitempty"`
	CreateChannelLabel string `json:"create_channel_label,omitempty"`
	Label              string `json:"label"`
	Email              string `json:"email"`
	APITokenMask       string `json:"api_token_mask"`
	APIURL             string `json:"api_url"`
	APIActive          bool   `json:"api_active"`
	ICloudActive       bool   `json:"icloud_active"`
	ReceiveCount       int    `json:"receive_count"`
	OpenAIClaimed      bool   `json:"openai_claimed"`
	GrokClaimed        bool   `json:"grok_claimed"`
	Status             string `json:"status"`
	Note               string `json:"note"`
	LastSyncAt         string `json:"last_sync_at,omitempty"`
	LastSyncUID        string `json:"last_sync_uid,omitempty"`
	CreatedAt          string `json:"created_at"`
	UpdatedAt          string `json:"updated_at"`
}

type publicMailboxGroup struct {
	Key       string `json:"key"`
	Title     string `json:"title"`
	Count     int    `json:"count"`
	AccountID string `json:"account_id,omitempty"`
}

type publicPagination struct {
	Page       int `json:"page"`
	PageSize   int `json:"page_size"`
	Total      int `json:"total"`
	TotalAll   int `json:"total_all"`
	TotalPages int `json:"total_pages"`
}

type publicMessage struct {
	ID         string `json:"id"`
	OwnerID    string `json:"owner_id,omitempty"`
	Owner      string `json:"owner,omitempty"`
	MailboxID  string `json:"mailbox_id"`
	Subject    string `json:"subject"`
	From       string `json:"from"`
	Body       string `json:"body"`
	ReceivedAt string `json:"received_at"`
	CreatedAt  string `json:"created_at"`
}

type publicUser struct {
	ID          string `json:"id"`
	Username    string `json:"username"`
	Status      string `json:"status"`
	IsAdmin     bool   `json:"is_admin,omitempty"`
	CreatedAt   string `json:"created_at,omitempty"`
	UpdatedAt   string `json:"updated_at,omitempty"`
	LastLoginAt string `json:"last_login_at,omitempty"`
}

type publicUserSummary struct {
	OwnerID               string `json:"owner_id"`
	Username              string `json:"username"`
	Status                string `json:"status"`
	IsAdmin               bool   `json:"is_admin,omitempty"`
	AccountCount          int    `json:"account_count"`
	MailboxCount          int    `json:"mailbox_count"`
	AvailableMailboxCount int    `json:"available_mailbox_count"`
	UsedMailboxCount      int    `json:"used_mailbox_count"`
	MessageCount          int    `json:"message_count"`
	ICloudSessionSaved    bool   `json:"icloud_session_saved"`
	LastLoginAt           string `json:"last_login_at,omitempty"`
}

type publicICloudSession struct {
	Saved                       bool   `json:"saved"`
	AccountID                   string `json:"account_id,omitempty"`
	SavedAt                     string `json:"saved_at,omitempty"`
	AppleID                     string `json:"apple_id,omitempty"`
	DSIDMask                    string `json:"dsid_mask,omitempty"`
	ClientBuildNumber           string `json:"client_build_number,omitempty"`
	MasteringNumber             string `json:"client_mastering_number,omitempty"`
	PremiumMailBaseURL          string `json:"premium_mail_base_url,omitempty"`
	MailGatewayBaseURL          string `json:"mail_gateway_base_url,omitempty"`
	MailBaseURL                 string `json:"mail_base_url,omitempty"`
	Host                        string `json:"host,omitempty"`
	IsICloudPlus                bool   `json:"is_icloud_plus"`
	CanCreateHME                bool   `json:"can_create_hme"`
	CookieCount                 int    `json:"cookie_count"`
	ICloudWebLoginSaved         bool   `json:"icloud_web_login_saved"`
	ICloudWebLoginChecked       bool   `json:"icloud_web_login_checked"`
	ICloudWebLoginOK            bool   `json:"icloud_web_login_ok"`
	ICloudWebLoginStatus        string `json:"icloud_web_login_status,omitempty"`
	AppleAccountLoginSaved      bool   `json:"apple_account_login_saved"`
	AppleAccountLoginChecked    bool   `json:"apple_account_login_checked"`
	AppleAccountLoginOK         bool   `json:"apple_account_login_ok"`
	AppleAccountLoginStatus     string `json:"apple_account_login_status,omitempty"`
	AppleAccountNextRefreshAt   string `json:"apple_account_next_refresh_at,omitempty"`
	AppleAccountManageExpiresAt string `json:"apple_account_manage_expires_at,omitempty"`
	AppleAccountManageReady     bool   `json:"apple_account_manage_ready"`
	ICloudIMAPLoginSaved        bool   `json:"icloud_imap_login_saved"`
	ICloudIMAPLoginChecked      bool   `json:"icloud_imap_login_checked"`
	ICloudIMAPLoginOK           bool   `json:"icloud_imap_login_ok"`
	ICloudIMAPLoginStatus       string `json:"icloud_imap_login_status,omitempty"`
	ICloudIMAPEmail             string `json:"icloud_imap_email,omitempty"`
	ICloudIMAPHost              string `json:"icloud_imap_host,omitempty"`
	ProviderConfigured          bool   `json:"provider_configured"`
	NeedsManualLogin            bool   `json:"needs_manual_login"`
	LastCheckedAt               string `json:"last_checked_at,omitempty"`
	LastCheckOK                 bool   `json:"last_check_ok"`
	LastStatusMessage           string `json:"last_status_message,omitempty"`
}

type apiError struct {
	Success   bool   `json:"success"`
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}
