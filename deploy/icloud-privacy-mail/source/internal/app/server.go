package app

import (
	"context"
	"crypto/subtle"
	"embed"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed templates/*.html
var webFS embed.FS

const mailboxCodeFreshWindow = 5 * time.Minute
const mailboxCreateMinInterval = 3 * time.Second
const mailboxCreateLimitCooldown = 2 * time.Minute
const mailboxListDefaultPageSize = 10
const mailboxListMaxPageSize = 500

var mailboxMailSyncMinInterval = 3 * time.Second
var mailboxCodeFastWait = 600 * time.Millisecond
var mailboxCodePollDebounce = 100 * time.Millisecond
var mailboxCodeLocalPollInterval = 100 * time.Millisecond
var mailboxCodeBatchSyncTimeout = 120 * time.Second
var mailboxCodeMaxClientWait = 30 * time.Second
var iCloudMailboxListAccountTimeout = 25 * time.Second
var mailWatcherPollInterval = 3 * time.Second
var mailWatcherActiveTTL = 20 * time.Minute

const (
	defaultMailWatcherFetchLimit        = 8
	defaultMailWatcherInitialFetchLimit = 20
	defaultMailWatcherLookback          = 24 * time.Hour
	mailWatcherSyncTimeout              = 90 * time.Second
)

type Server struct {
	cfg                            Config
	store                          *FileStore
	logger                         *slog.Logger
	mux                            *http.ServeMux
	icloudProtocolLogins           *appleAuthPendingStore
	appleAccountLogins             *appleAuthPendingStore
	icloudCreateMu                 sync.Mutex
	icloudCreateGates              map[string]chan struct{}
	icloudCreateLast               map[string]time.Time
	icloudCreateCooldown           map[string]time.Time
	icloudMailSyncMu               sync.Mutex
	icloudMailSyncGates            map[string]chan struct{}
	icloudMailSyncLast             map[string]time.Time
	mailboxSyncMinInterval         time.Duration
	mailboxCodeFastWait            time.Duration
	mailboxCodeMu                  sync.Mutex
	mailboxCodePollers             map[string]*mailboxCodePoller
	mailWatcherMu                  sync.Mutex
	mailWatcherCancel              context.CancelFunc
	mailWatcherWake                chan struct{}
	mailWatcherEnabled             bool
	mailWatcherInterval            time.Duration
	mailWatcherFetchLimit          int
	mailWatcherInitialFetchLimit   int
	mailWatcherLookback            time.Duration
	mailWatcherActiveUntil         map[string]time.Time
	appleAccountKeepAliveMu        sync.Mutex
	appleAccountKeepAliveCancel    context.CancelFunc
	appleAccountKeepAliveEnabled   bool
	appleAccountKeepAliveInterval  time.Duration
	schedulerMu                    sync.Mutex
	mailboxSchedulers              map[string]*mailboxSchedulerJob
	createMailboxForOwner          func(ctx context.Context, ownerID, accountID, label, note string) (Mailbox, ICloudRemoteMailbox, error)
	keepAliveAppleAccountState     func(ctx context.Context, state LoginState) (LoginState, error)
	syncMailboxMessages            func(ctx context.Context, session ICloudSession, mailbox Mailbox, after time.Time, keyword string, maxThreads int) ([]ICloudSyncedMessage, error)
	syncMailboxBatch               func(ctx context.Context, session ICloudSession, mailboxes []Mailbox, after time.Time, keyword string, maxThreads int) (map[string][]ICloudSyncedMessage, error)
	syncCodeMailboxBatch           func(ctx context.Context, state LoginState, mailboxes []Mailbox, after time.Time, keyword string, maxMessages int) (map[string][]ICloudSyncedMessage, error)
	syncCodeMailboxBatchWithCursor func(ctx context.Context, state LoginState, mailboxes []Mailbox, after time.Time, keyword string, maxMessages int) (iCloudIMAPSyncResult, error)
	latestIMAPUID                  func(ctx context.Context, state LoginState) (string, error)
	checkIMAPLogin                 func(ctx context.Context, email, appPassword string) error
	updateMu                       sync.Mutex
	updateApplyMu                  sync.Mutex
	updateCache                    updateCandidate
	updateCacheAt                  time.Time
}

type createMailboxFailure struct {
	AccountID string `json:"account_id,omitempty"`
	AppleID   string `json:"apple_id,omitempty"`
	Channel   string `json:"channel,omitempty"`
	Code      string `json:"code,omitempty"`
	Error     string `json:"error"`
}

type mailboxCreateChannel string

const (
	mailboxCreateChannelAuto         mailboxCreateChannel = ""
	mailboxCreateChannelAppleAccount mailboxCreateChannel = "apple_account"
	mailboxCreateChannelICloudWeb    mailboxCreateChannel = "icloud_web"
)

type mailboxCreateChannelContextKey struct{}

type mailboxCreateRequest struct {
	AccountID string
	Channel   mailboxCreateChannel
}

func contextWithMailboxCreateChannel(ctx context.Context, channel mailboxCreateChannel) context.Context {
	channel = normalizeMailboxCreateChannel(channel)
	if channel == mailboxCreateChannelAuto {
		return ctx
	}
	return context.WithValue(ctx, mailboxCreateChannelContextKey{}, channel)
}

func mailboxCreateChannelFromContext(ctx context.Context) mailboxCreateChannel {
	channel, _ := ctx.Value(mailboxCreateChannelContextKey{}).(mailboxCreateChannel)
	return normalizeMailboxCreateChannel(channel)
}

func normalizeMailboxCreateChannel(channel mailboxCreateChannel) mailboxCreateChannel {
	switch channel {
	case mailboxCreateChannelAppleAccount, mailboxCreateChannelICloudWeb:
		return channel
	default:
		return mailboxCreateChannelAuto
	}
}

func mailboxCreateChannelLabel(channel mailboxCreateChannel) string {
	switch normalizeMailboxCreateChannel(channel) {
	case mailboxCreateChannelAppleAccount:
		return "新接口"
	case mailboxCreateChannelICloudWeb:
		return "旧接口"
	default:
		return "自动接口"
	}
}

type syncICloudMailboxResult struct {
	AccountID string `json:"account_id,omitempty"`
	AppleID   string `json:"apple_id,omitempty"`
	Source    string `json:"source,omitempty"`
	Total     int    `json:"total"`
	Created   int    `json:"created"`
	Updated   int    `json:"updated"`
	Skipped   int    `json:"skipped"`
	Error     string `json:"error,omitempty"`
}

type mailboxCodeWaiter struct {
	ctx           context.Context
	mailboxID     string
	after         time.Time
	keyword       string
	forceSync     bool
	skipMessageID string
	result        chan mailboxCodeResult
}

type mailboxCodeResult struct {
	message Message
	code    string
	ok      bool
	syncErr error
}

type mailboxCodePoller struct {
	ownerID string
	waiters []*mailboxCodeWaiter
}

type mailboxWatcherOwnerGroup struct {
	ownerID   string
	mailboxes []Mailbox
}

type mailboxWatcherIMAPGroup struct {
	key       string
	ownerID   string
	state     LoginState
	mailboxes []Mailbox
	signature string
}

type mailboxWatcherIdleWorker struct {
	cancel    context.CancelFunc
	signature string
}

func NewServer(cfg Config, store *FileStore, logger *slog.Logger) http.Handler {
	s := &Server{
		cfg:                           cfg,
		store:                         store,
		logger:                        logger,
		mux:                           http.NewServeMux(),
		icloudProtocolLogins:          newAppleAuthPendingStore(),
		appleAccountLogins:            newAppleAuthPendingStore(),
		icloudCreateGates:             make(map[string]chan struct{}),
		icloudCreateLast:              make(map[string]time.Time),
		icloudCreateCooldown:          make(map[string]time.Time),
		icloudMailSyncGates:           make(map[string]chan struct{}),
		icloudMailSyncLast:            make(map[string]time.Time),
		mailboxSyncMinInterval:        mailboxMailSyncMinInterval,
		mailboxCodeFastWait:           mailboxCodeFastWait,
		mailboxCodePollers:            make(map[string]*mailboxCodePoller),
		mailWatcherWake:               make(chan struct{}, 1),
		mailWatcherEnabled:            cfg.MailWatcherEnabled,
		mailWatcherInterval:           mailWatcherPollInterval,
		mailWatcherFetchLimit:         defaultMailWatcherFetchLimit,
		mailWatcherInitialFetchLimit:  defaultMailWatcherInitialFetchLimit,
		mailWatcherLookback:           defaultMailWatcherLookback,
		mailWatcherActiveUntil:        make(map[string]time.Time),
		appleAccountKeepAliveEnabled:  cfg.AppleAccountKeepAliveEnabled,
		appleAccountKeepAliveInterval: appleAccountKeepAliveDefaultInterval,
		mailboxSchedulers:             make(map[string]*mailboxSchedulerJob),
	}
	if cfg.PublicSyncMinIntervalMS > 0 {
		s.mailboxSyncMinInterval = time.Duration(cfg.PublicSyncMinIntervalMS) * time.Millisecond
	}
	if cfg.PublicFastSyncWaitMS > 0 {
		s.mailboxCodeFastWait = time.Duration(cfg.PublicFastSyncWaitMS) * time.Millisecond
	}
	if cfg.MailWatcherPollMS > 0 {
		s.mailWatcherInterval = time.Duration(cfg.MailWatcherPollMS) * time.Millisecond
	}
	if cfg.MailWatcherFetchLimit > 0 {
		s.mailWatcherFetchLimit = cfg.MailWatcherFetchLimit
	}
	if cfg.MailWatcherInitialFetchLimit > 0 {
		s.mailWatcherInitialFetchLimit = cfg.MailWatcherInitialFetchLimit
	}
	if cfg.MailWatcherLookbackHours > 0 {
		s.mailWatcherLookback = time.Duration(cfg.MailWatcherLookbackHours) * time.Hour
	}
	if cfg.AppleAccountKeepAliveMS > 0 {
		s.appleAccountKeepAliveInterval = time.Duration(cfg.AppleAccountKeepAliveMS) * time.Millisecond
	}
	s.createMailboxForOwner = s.createICloudMailboxForOwner
	s.keepAliveAppleAccountState = func(ctx context.Context, state LoginState) (LoginState, error) {
		return NewICloudClient().keepAliveAppleAccountManageStateUnlocked(ctx, state)
	}
	s.syncMailboxMessages = func(ctx context.Context, session ICloudSession, mailbox Mailbox, after time.Time, keyword string, maxThreads int) ([]ICloudSyncedMessage, error) {
		return NewICloudClient().SyncMailboxMessages(ctx, session, mailbox, after, keyword, maxThreads)
	}
	s.syncMailboxBatch = func(ctx context.Context, session ICloudSession, mailboxes []Mailbox, after time.Time, keyword string, maxThreads int) (map[string][]ICloudSyncedMessage, error) {
		return NewICloudClient().SyncMailboxMessagesBatch(ctx, session, mailboxes, after, keyword, maxThreads)
	}
	s.checkIMAPLogin = CheckICloudIMAPLogin
	s.routes()
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if isInternalICloudRequest(r) {
		s.mux.ServeHTTP(w, r)
		return
	}
	if s.requiresAdmin(r) &&
		!s.authorizedAdminSession(r) &&
		!(s.allowsUserSession(r) && s.authorizedUserSession(r)) {
		writeError(w, http.StatusUnauthorized, errCode("auth_required", "请先登录账号", false))
		return
	}
	s.mux.ServeHTTP(w, r)
}

func isInternalICloudRequest(r *http.Request) bool {
	return r.Header.Get("X-ChatGPT2API-Internal") == "icloud-privacy-mail"
}

func (s *Server) StartMailWatcher(ctx context.Context) {
	if !s.mailWatcherEnabled {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	s.mailWatcherMu.Lock()
	if s.mailWatcherCancel != nil {
		s.mailWatcherMu.Unlock()
		return
	}
	watchCtx, cancel := context.WithCancel(ctx)
	s.mailWatcherCancel = cancel
	s.mailWatcherMu.Unlock()

	go s.runMailWatcher(watchCtx)
}

func (s *Server) StopMailWatcher() {
	s.mailWatcherMu.Lock()
	cancel := s.mailWatcherCancel
	s.mailWatcherCancel = nil
	s.mailWatcherMu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (s *Server) StartAppleAccountKeepAlive(ctx context.Context) {
	if !s.appleAccountKeepAliveEnabled {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	s.appleAccountKeepAliveMu.Lock()
	if s.appleAccountKeepAliveCancel != nil {
		s.appleAccountKeepAliveMu.Unlock()
		return
	}
	keepAliveCtx, cancel := context.WithCancel(ctx)
	s.appleAccountKeepAliveCancel = cancel
	s.appleAccountKeepAliveMu.Unlock()

	go s.runAppleAccountKeepAlive(keepAliveCtx)
}

func (s *Server) StopAppleAccountKeepAlive() {
	s.appleAccountKeepAliveMu.Lock()
	cancel := s.appleAccountKeepAliveCancel
	s.appleAccountKeepAliveCancel = nil
	s.appleAccountKeepAliveMu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (s *Server) routes() {
	s.mux.HandleFunc("GET /", s.handleHome)
	s.mux.HandleFunc("GET /login", s.handleLoginPage)
	s.mux.HandleFunc("GET /manage", s.handleManagePage)
	s.mux.HandleFunc("GET /api/auth/me", s.handleAuthMe)
	s.mux.HandleFunc("POST /api/auth/register", s.handleAuthRegister)
	s.mux.HandleFunc("POST /api/auth/login", s.handleAuthLogin)
	s.mux.HandleFunc("POST /api/auth/logout", s.handleAuthLogout)
	s.mux.HandleFunc("DELETE /api/admin/users/{id}", s.handleAdminDeleteUser)
	s.mux.HandleFunc("GET /api/status", s.handleStatus)
	s.mux.HandleFunc("GET /api/update/status", s.handleUpdateStatus)
	s.mux.HandleFunc("POST /api/update/apply", s.handleApplyUpdate)
	s.mux.HandleFunc("GET /api/create-settings", s.handleCreateSettings)
	s.mux.HandleFunc("POST /api/create-settings", s.handleSaveCreateSettings)
	s.mux.HandleFunc("GET /api/manage/data", s.handleManageData)
	s.mux.HandleFunc("GET /api/v1/health", s.handleHealth)
	s.mux.HandleFunc("POST /api/v1/mailboxes/claim", s.handleClaimMailbox)
	s.mux.HandleFunc("POST /api/v1/mailboxes/claim-status", s.handleClaimMailboxStatus)
	s.mux.HandleFunc("POST /api/v1/mailboxes/lookup", s.handleLookupMailboxes)
	s.mux.HandleFunc("GET /api/runtime/export", s.handleExportRuntimeData)
	s.mux.HandleFunc("GET /api/runtime/export-mailbox-apis", s.handleExportMailboxAPIs)
	s.mux.HandleFunc("GET /api/runtime/export-mailbox-emails", s.handleExportMailboxEmails)
	s.mux.HandleFunc("GET /api/icloud/session", s.handleICloudSession)
	s.mux.HandleFunc("POST /api/icloud/protocol-login/start", s.handleStartICloudProtocolLogin)
	s.mux.HandleFunc("POST /api/icloud/protocol-login/2fa", s.handleSubmitICloudProtocol2FA)
	s.mux.HandleFunc("POST /api/apple-account/login/start", s.handleStartAppleAccountLogin)
	s.mux.HandleFunc("POST /api/apple-account/login/2fa", s.handleSubmitAppleAccount2FA)
	s.mux.HandleFunc("POST /api/icloud/session/check", s.handleCheckICloudSession)
	s.mux.HandleFunc("POST /api/icloud/imap-login/save", s.handleSaveICloudIMAPLogin)
	s.mux.HandleFunc("POST /api/icloud/imap-login/check", s.handleCheckICloudIMAPLogin)
	s.mux.HandleFunc("POST /api/icloud/mailboxes/create", s.handleCreateICloudMailbox)
	s.mux.HandleFunc("POST /api/icloud/mailboxes/sync", s.handleSyncICloudMailboxes)
	s.mux.HandleFunc("GET /api/icloud/scheduler/status", s.handleMailboxSchedulerStatus)
	s.mux.HandleFunc("POST /api/icloud/scheduler/start", s.handleStartMailboxScheduler)
	s.mux.HandleFunc("POST /api/icloud/scheduler/stop", s.handleStopMailboxScheduler)
	s.mux.HandleFunc("POST /api/icloud/scheduler/logs/clear", s.handleClearMailboxSchedulerLogs)
	s.mux.HandleFunc("GET /api/accounts", s.handleListAccounts)
	s.mux.HandleFunc("POST /api/accounts", s.handleCreateAccount)
	s.mux.HandleFunc("GET /api/mailboxes", s.handleListMailboxes)
	s.mux.HandleFunc("POST /api/mailboxes", s.handleCreateMailbox)
	s.mux.HandleFunc("POST /api/mailboxes/remote-clean", s.handleCleanRemoteMailboxes)
	s.mux.HandleFunc("POST /api/mailboxes/{id}/verify", s.handleVerifyMailbox)
	s.mux.HandleFunc("POST /api/mailboxes/{id}/disable", s.handleDisableMailbox)
	s.mux.HandleFunc("POST /api/mailboxes/{id}/status", s.handleSetMailboxStatus)
	s.mux.HandleFunc("POST /api/mailboxes/{id}/sync", s.handleSyncMailbox)
	s.mux.HandleFunc("POST /api/mailboxes/{id}/remote-clean", s.handleCleanRemoteMailbox)
	s.mux.HandleFunc("DELETE /api/mailboxes/{id}", s.handleDeleteMailbox)
	s.mux.HandleFunc("GET /api/mailboxes/{id}/messages", s.handleListMessages)
	s.mux.HandleFunc("POST /api/mailboxes/{id}/messages", s.handleCreateMessage)
	s.mux.HandleFunc("GET /api/mailboxes/{id}/code", s.handleMailboxCodeByID)
	s.mux.HandleFunc("GET /api/v1/mailboxes/{email}/code", s.handleMailboxCodeByEmail)
}

func (s *Server) handleHome(w http.ResponseWriter, _ *http.Request) {
	s.writeTemplate(w, "templates/index.html")
}

func (s *Server) handleLoginPage(w http.ResponseWriter, _ *http.Request) {
	s.writeTemplate(w, "templates/login.html")
}

func (s *Server) handleManagePage(w http.ResponseWriter, _ *http.Request) {
	s.writeTemplate(w, "templates/manage.html")
}

func (s *Server) handleCreateSettings(w http.ResponseWriter, r *http.Request) {
	ownerID := requestOwnerID(r, s.store)
	if ownerID == "" {
		writeError(w, http.StatusUnauthorized, errCode("auth_required", "请先登录账号", false))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":  true,
		"settings": publicCreateSettings(s.store.CreateSettingsForOwner(ownerID)),
	})
}

func (s *Server) handleSaveCreateSettings(w http.ResponseWriter, r *http.Request) {
	ownerID := requestOwnerID(r, s.store)
	if ownerID == "" {
		writeError(w, http.StatusUnauthorized, errCode("auth_required", "请先登录账号", false))
		return
	}
	var payload struct {
		Label                         string   `json:"label"`
		Note                          string   `json:"note"`
		AccountIDs                    []string `json:"account_ids"`
		CreateChannel                 string   `json:"create_channel"`
		SchedulerCreateChannel        string   `json:"scheduler_create_channel"`
		AppleAccountTwoFactorMethod   string   `json:"apple_account_two_factor_method"`
		ICloudWebTwoFactorMethod      string   `json:"icloud_web_two_factor_method"`
		SchedulerIntervalMinutes      int      `json:"scheduler_interval_minutes"`
		SchedulerRoundIntervalSeconds int      `json:"scheduler_round_interval_seconds"`
		MailboxPageSize               int      `json:"mailbox_page_size"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	accountIDs := normalizeAccountIDSelection("", payload.AccountIDs)
	for _, accountID := range accountIDs {
		if !s.canAccessAccountIDForOwner(ownerID, accountID) {
			writeError(w, http.StatusNotFound, errCode("account_not_found", "账号不存在，配置未保存", false))
			return
		}
	}
	settings, err := s.store.SaveCreateSettingsForOwner(ownerID, CreateSettings{
		Label:                         payload.Label,
		Note:                          payload.Note,
		AccountIDs:                    accountIDs,
		CreateChannel:                 payload.CreateChannel,
		SchedulerCreateChannel:        payload.SchedulerCreateChannel,
		AppleAccountTwoFactorMethod:   payload.AppleAccountTwoFactorMethod,
		ICloudWebTwoFactorMethod:      payload.ICloudWebTwoFactorMethod,
		SchedulerIntervalMinutes:      payload.SchedulerIntervalMinutes,
		SchedulerRoundIntervalSeconds: payload.SchedulerRoundIntervalSeconds,
		MailboxPageSize:               payload.MailboxPageSize,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":  true,
		"message":  "创建配置已保存到服务器",
		"settings": publicCreateSettings(settings),
	})
}

func (s *Server) writeTemplate(w http.ResponseWriter, name string) {
	data, err := webFS.ReadFile(name)
	if err != nil {
		writeError(w, http.StatusInternalServerError, errCode("template_missing", "面板模板缺失", false))
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(data)
}

func (s *Server) handleAuthRegister(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	user, err := s.store.CreateUser(payload.Username, payload.Password)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	token, session, err := s.store.CreateWebSession(user.ID, user.IsAdmin, 30*24*time.Hour)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	s.setSessionCookie(w, r, token, session.ExpiresAt)
	writeJSON(w, http.StatusCreated, map[string]any{
		"success": true,
		"user":    publicUserFromUser(user),
		"message": firstNonEmpty(map[bool]string{true: "注册成功，当前账号是管理员", false: "注册成功"}[user.IsAdmin]),
	})
}

func (s *Server) handleAuthLogin(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	user, err := s.store.AuthenticateUser(payload.Username, payload.Password)
	if err != nil {
		writeError(w, http.StatusUnauthorized, err)
		return
	}
	token, session, err := s.store.CreateWebSession(user.ID, user.IsAdmin, 30*24*time.Hour)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	s.setSessionCookie(w, r, token, session.ExpiresAt)
	writeJSON(w, http.StatusOK, map[string]any{
		"success": true,
		"user":    publicUserFromUser(user),
	})
}

func (s *Server) handleAuthMe(w http.ResponseWriter, r *http.Request) {
	if session, user, ok := s.currentWebSession(r); ok {
		out := publicUserFromUser(user)
		out.IsAdmin = session.IsAdmin || user.IsAdmin
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "authenticated": true, "user": out})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "authenticated": false})
}

func (s *Server) handleAuthLogout(w http.ResponseWriter, r *http.Request) {
	if cookie, err := r.Cookie(sessionCookieName); err == nil {
		_ = s.store.DeleteWebSession(cookie.Value)
	}
	s.clearSessionCookie(w, r)
	writeJSON(w, http.StatusOK, map[string]any{"success": true})
}

func (s *Server) handleAdminDeleteUser(w http.ResponseWriter, r *http.Request) {
	session, currentUser, ok := s.currentWebSession(r)
	if !ok || (!session.IsAdmin && !currentUser.IsAdmin) {
		writeError(w, http.StatusForbidden, errCode("admin_required", "需要管理员权限", false))
		return
	}
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		writeError(w, http.StatusBadRequest, errCode("user_id_missing", "缺少账号 ID", false))
		return
	}
	if constantTimeEqual(currentUser.ID, id) {
		writeError(w, http.StatusBadRequest, errCode("cannot_delete_self", "不能删除当前登录的管理员账号", false))
		return
	}
	user, ok := s.store.UserByID(id)
	if !ok {
		writeError(w, http.StatusNotFound, errCode("user_not_found", "账号不存在", false))
		return
	}
	if user.IsAdmin {
		writeError(w, http.StatusBadRequest, errCode("cannot_delete_admin_user", "不能删除管理员账号", false))
		return
	}
	result, err := s.store.DeleteUser(id)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "deleted": result})
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	state := s.scopedState(r)
	currentUser := publicUser{}
	authenticated := false
	if session, user, ok := s.currentWebSession(r); ok {
		authenticated = true
		currentUser = publicUserFromUser(user)
		currentUser.IsAdmin = session.IsAdmin || user.IsAdmin
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":            true,
		"service":            "icloud-privacy-mail",
		"api_key_configured": strings.TrimSpace(s.cfg.APIKey) != "",
		"base_url":           requestBaseURL(r),
		"account_scoped":     scopedOwnerID(r, s.store) != "",
		"authenticated":      authenticated,
		"current_user":       currentUser,
		"accounts":           len(state.Accounts),
		"mailboxes":          len(state.Mailboxes),
		"messages":           len(state.Messages),
		"icloud_session":     s.publicSessionForRequest(r),
		"icloud_sessions":    s.publicSessionsForRequest(r),
		"version":            currentVersionInfo(),
	})
}

func (s *Server) handleManageData(w http.ResponseWriter, r *http.Request) {
	state := s.scopedState(r)
	users := state.Users
	if s.isAdminRequest(r) {
		users = s.store.Users()
	}
	publicUsers := make([]publicUser, 0, len(users))
	for _, user := range users {
		publicUsers = append(publicUsers, publicUserFromUser(user))
	}
	accounts := make([]publicAccount, 0, len(state.Accounts))
	for _, account := range state.Accounts {
		accounts = append(accounts, s.publicAccount(account))
	}
	mailboxes := make([]publicMailbox, 0, len(state.Mailboxes))
	for _, mailbox := range state.Mailboxes {
		mailboxes = append(mailboxes, s.publicMailbox(r, mailbox))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":         true,
		"is_admin":        s.isAdminRequest(r),
		"users":           publicUsers,
		"user_summaries":  s.publicUserSummaries(users, state),
		"accounts":        accounts,
		"mailboxes":       mailboxes,
		"messages":        len(state.Messages),
		"icloud_session":  s.publicSessionForRequest(r),
		"icloud_sessions": s.publicSessionsForRequest(r),
	})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if strings.TrimSpace(s.cfg.APIKey) != "" && !s.authorizedGlobalAPI(r) {
		writeError(w, http.StatusUnauthorized, errCode("invalid_api_key", "API Key 错误", false))
		return
	}
	session, ok := s.store.ICloudSession()
	icloudActive := ok && session.IsICloudPlus && session.CanCreateHME && len(session.Cookies) > 0
	if !icloudActive {
		for _, scopedSession := range s.store.Snapshot().ICloudSessions {
			if scopedSession.IsICloudPlus && scopedSession.CanCreateHME && len(scopedSession.Cookies) > 0 {
				icloudActive = true
				break
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":       true,
		"service":       "icloud-privacy-mail",
		"api_active":    strings.TrimSpace(s.cfg.APIKey) != "",
		"icloud_active": icloudActive,
		"time":          formatTime(time.Now()),
	})
}

func (s *Server) handleClaimMailbox(w http.ResponseWriter, r *http.Request) {
	if !s.authorizedGlobalAPI(r) && !isInternalICloudRequest(r) {
		writeError(w, http.StatusUnauthorized, errCode("global_api_key_required", "自动取号需要配置并提交全局 API Key", false))
		return
	}
	var payload struct {
		Project string `json:"project"`
		Purpose string `json:"purpose"`
		Count   int    `json:"count"`
	}
	if r.ContentLength != 0 {
		if err := decodeJSON(r, &payload); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	} else {
		_ = r.Body.Close()
	}
	note := strings.TrimSpace(payload.Project)
	if strings.TrimSpace(payload.Purpose) != "" {
		note = strings.TrimSpace(note + " " + payload.Purpose)
	}
	if note == "" {
		note = "外部 API 已领取"
	} else {
		note = "外部 API 已领取：" + note
	}
	mailbox, err := s.store.ClaimAvailableMailbox(payload.Project, note)
	if err != nil {
		writeError(w, http.StatusOK, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success": true,
		"mailbox": s.publicMailbox(r, mailbox),
	})
}

func (s *Server) handleClaimMailboxStatus(w http.ResponseWriter, r *http.Request) {
	if !s.authorizedGlobalAPI(r) && !isInternalICloudRequest(r) {
		writeError(w, http.StatusUnauthorized, errCode("global_api_key_required", "更新邮箱注册标签需要内部模块或全局 API Key", false))
		return
	}
	var payload struct {
		Project string   `json:"project"`
		Emails  []string `json:"emails"`
		Claimed *bool    `json:"claimed"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if len(payload.Emails) == 0 || len(payload.Emails) > 500 || payload.Claimed == nil {
		writeError(w, http.StatusBadRequest, errCode("invalid_claim_status", "邮箱数量必须是 1-500，且必须提供 claimed", false))
		return
	}
	updated, missing, err := s.store.SetMailboxClaimed(payload.Project, payload.Emails, *payload.Claimed)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "updated": updated, "missing": missing})
}

func (s *Server) handleLookupMailboxes(w http.ResponseWriter, r *http.Request) {
	if !s.authorizedGlobalAPI(r) {
		writeError(w, http.StatusUnauthorized, errCode("global_api_key_required", "查询邮箱 API 需要配置并提交全局 API Key", false))
		return
	}
	var payload struct {
		Emails []string `json:"emails"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if len(payload.Emails) == 0 || len(payload.Emails) > 500 {
		writeError(w, http.StatusBadRequest, errCode("invalid_email_count", "邮箱数量必须是 1-500", false))
		return
	}

	seen := make(map[string]struct{}, len(payload.Emails))
	missing := make([]string, 0)
	mailboxes := make([]publicMailbox, 0, len(payload.Emails))
	for _, rawEmail := range payload.Emails {
		email := strings.ToLower(strings.TrimSpace(rawEmail))
		if email == "" {
			continue
		}
		if _, ok := seen[email]; ok {
			continue
		}
		seen[email] = struct{}{}
		mailbox, ok := s.store.FindMailboxByEmail(email)
		if !ok {
			missing = append(missing, email)
			continue
		}
		mailboxes = append(mailboxes, s.publicMailbox(r, mailbox))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":   true,
		"mailboxes": mailboxes,
		"missing":   missing,
	})
}

func (s *Server) handleExportRuntimeData(w http.ResponseWriter, r *http.Request) {
	ownerID := scopedOwnerID(r, s.store)
	state := s.scopedState(r)
	payload := struct {
		ExportedAt      string          `json:"exported_at"`
		Scope           string          `json:"scope"`
		Owner           string          `json:"owner,omitempty"`
		DataPath        string          `json:"data_path,omitempty"`
		NextID          int             `json:"next_id"`
		Accounts        []Account       `json:"accounts"`
		Mailboxes       []Mailbox       `json:"mailboxes"`
		ICloudSession   *ICloudSession  `json:"icloud_session,omitempty"`
		ICloudSessions  []ICloudSession `json:"icloud_sessions,omitempty"`
		MessageCount    int             `json:"message_count"`
		Messages        []Message       `json:"messages,omitempty"`
		IncludeMessages bool            `json:"include_messages"`
	}{
		ExportedAt:      formatTime(time.Now()),
		Scope:           "all",
		NextID:          state.NextID,
		Accounts:        state.Accounts,
		Mailboxes:       state.Mailboxes,
		ICloudSession:   state.ICloudSession,
		ICloudSessions:  state.ICloudSessions,
		MessageCount:    len(state.Messages),
		IncludeMessages: truthy(r.URL.Query().Get("include_messages")),
	}
	if ownerID != "" {
		payload.Scope = "user"
		payload.Owner = s.ownerName(ownerID)
	} else {
		payload.DataPath = s.store.Path()
	}
	if payload.IncludeMessages {
		payload.Messages = state.Messages
	}
	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	filename := "icloud-privacy-mail-state-" + time.Now().Format("20060102-150405") + ".json"
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Content-Disposition", `attachment; filename="`+filename+`"`)
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(append(data, '\n'))
}

func (s *Server) handleExportMailboxAPIs(w http.ResponseWriter, r *http.Request) {
	s.writeMailboxTextExport(w, r, mailboxExportAPI)
}

func (s *Server) handleExportMailboxEmails(w http.ResponseWriter, r *http.Request) {
	s.writeMailboxTextExport(w, r, mailboxExportEmail)
}

type mailboxExportMode string

const (
	mailboxExportAPI   mailboxExportMode = "api"
	mailboxExportEmail mailboxExportMode = "email"
)

type mailboxExportFormat struct {
	ext         string
	contentType string
	separator   string
	csv         bool
	jsonl       bool
}

func (s *Server) writeMailboxTextExport(w http.ResponseWriter, r *http.Request, mode mailboxExportMode) {
	format, err := parseMailboxExportFormat(r.URL.Query().Get("format"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}

	state := s.mailboxExportState(r)
	accountID := normalizeExportAccountID(r.URL.Query().Get("account_id"))
	mailboxes := filterMailboxesForExport(state.Mailboxes, accountID)
	var out strings.Builder
	if format.jsonl {
		for _, mailbox := range mailboxes {
			record := s.mailboxExportRecord(r, mailbox, mode)
			if len(record) == 0 {
				continue
			}
			var line any
			if mode == mailboxExportEmail {
				line = map[string]string{"email": record[0]}
			} else {
				line = map[string]string{"email": record[0], "api": record[1]}
			}
			data, err := json.Marshal(line)
			if err != nil {
				writeError(w, http.StatusInternalServerError, err)
				return
			}
			out.Write(data)
			out.WriteByte('\n')
		}
	} else if format.csv {
		writer := csv.NewWriter(&out)
		if format.separator == "\t" {
			writer.Comma = '\t'
		}
		for _, mailbox := range mailboxes {
			record := s.mailboxExportRecord(r, mailbox, mode)
			if len(record) == 0 {
				continue
			}
			if err := writer.Write(record); err != nil {
				writeError(w, http.StatusInternalServerError, err)
				return
			}
		}
		writer.Flush()
		if err := writer.Error(); err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
	} else {
		for _, mailbox := range mailboxes {
			record := s.mailboxExportRecord(r, mailbox, mode)
			if len(record) == 0 {
				continue
			}
			out.WriteString(strings.Join(record, format.separator))
			out.WriteByte('\n')
		}
	}

	prefix := "icloud-mailbox-apis"
	if mode == mailboxExportEmail {
		prefix = "icloud-mailbox-emails"
	}
	if accountID != "" {
		prefix += "-" + safeFilenamePart(accountID)
	}
	filename := prefix + "-" + time.Now().Format("20060102-150405") + "." + format.ext
	w.Header().Set("Content-Type", format.contentType)
	w.Header().Set("Content-Disposition", `attachment; filename="`+filename+`"`)
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, out.String())
}

func (s *Server) mailboxExportState(r *http.Request) State {
	ownerID := strings.TrimSpace(r.URL.Query().Get("owner_id"))
	if s.isAdminRequest(r) && ownerID != "" && !strings.EqualFold(ownerID, "all") {
		return s.store.SnapshotForOwner(ownerID)
	}
	return s.scopedState(r)
}

func normalizeExportAccountID(value string) string {
	value = strings.TrimSpace(value)
	switch strings.ToLower(value) {
	case "", "all", "__all", "__current", "current":
		return ""
	default:
		return value
	}
}

func filterMailboxesForExport(mailboxes []Mailbox, accountID string) []Mailbox {
	accountID = strings.TrimSpace(accountID)
	if accountID == "" {
		return mailboxes
	}
	out := make([]Mailbox, 0, len(mailboxes))
	for _, mailbox := range mailboxes {
		if constantTimeEqual(accountID, strings.TrimSpace(mailbox.AccountID)) {
			out = append(out, mailbox)
		}
	}
	return out
}

func safeFilenamePart(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	var b strings.Builder
	for _, r := range value {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteByte('-')
		}
	}
	return strings.Trim(b.String(), "-")
}

func (s *Server) mailboxExportRecord(r *http.Request, mailbox Mailbox, mode mailboxExportMode) []string {
	email := strings.TrimSpace(mailbox.Email)
	if email == "" {
		return nil
	}
	if mode == mailboxExportEmail {
		return []string{email}
	}
	return []string{email, s.mailboxAPIURL(r, mailbox)}
}

func parseMailboxExportFormat(value string) (mailboxExportFormat, error) {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "", "txt", "text", "list":
		return mailboxExportFormat{ext: "txt", contentType: "text/plain; charset=utf-8", separator: "----"}, nil
	case "csv":
		return mailboxExportFormat{ext: "csv", contentType: "text/csv; charset=utf-8", separator: ",", csv: true}, nil
	case "tsv", "tab":
		return mailboxExportFormat{ext: "tsv", contentType: "text/tab-separated-values; charset=utf-8", separator: "\t", csv: true}, nil
	case "jsonl", "ndjson":
		return mailboxExportFormat{ext: "jsonl", contentType: "application/x-ndjson; charset=utf-8", jsonl: true}, nil
	default:
		return mailboxExportFormat{}, errCode("invalid_export_format", "导出格式只支持 txt、csv、tsv、jsonl", false)
	}
}

func (s *Server) handleICloudSession(w http.ResponseWriter, r *http.Request) {
	sessions := s.publicSessionsForRequest(r)
	session := publicSession(nil)
	if len(sessions) > 0 {
		session = sessions[0]
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "session": session, "sessions": sessions})
}

func (s *Server) handleCheckICloudSession(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AccountID string `json:"account_id"`
	}
	if r.ContentLength != 0 {
		if err := decodeJSON(r, &payload); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	} else {
		_ = r.Body.Close()
	}
	ownerID := requestOwnerID(r, s.store)
	sessions := s.sessionsForOwner(ownerID, payload.AccountID)
	if len(sessions) == 0 {
		writeError(w, http.StatusBadRequest, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先保存旧接口登录态", true))
		return
	}

	checkedAt := time.Now()
	client := NewICloudClient()
	failed := 0
	var lastErr error
	for _, session := range sessions {
		checkedSession, ok, err := checkSavedLoginStatesWithIMAP(r.Context(), client, session, checkedAt, s.checkSavedIMAPLogin)
		if !ok {
			failed++
			lastErr = err
		}
		if saveErr := s.store.SaveICloudSessionForOwner(ownerID, checkedSession); saveErr != nil {
			writeError(w, http.StatusInternalServerError, saveErr)
			return
		}
		if !ok {
			s.logger.Warn("login state check failed", "account_id", session.AccountID, "err", err)
		}
	}
	publicSessions := s.publicSessionsForOwner(ownerID)
	first := publicSession(nil)
	if len(publicSessions) > 0 {
		first = publicSessions[0]
	}
	if failed == len(sessions) {
		message := "全部登录态检测失败"
		if lastErr != nil {
			message += "：" + lastErr.Error()
		}
		s.logger.Warn("login state check failed", "err", lastErr)
		writeError(w, http.StatusBadGateway, errCode("icloud_session_check_failed", message, true))
		return
	}
	message := "登录态检测正常"
	if failed > 0 {
		message = fmt.Sprintf("登录态部分检测成功：成功 %d，失败 %d", len(sessions)-failed, failed)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":       true,
		"checked_at":    formatTime(checkedAt),
		"message":       message,
		"session":       first,
		"sessions":      publicSessions,
		"checked_count": len(sessions),
		"failed_count":  failed,
	})
}

func (s *Server) handleSaveICloudIMAPLogin(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AccountID   string `json:"account_id"`
		Email       string `json:"email"`
		AppPassword string `json:"app_password"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	ownerID := requestOwnerID(r, s.store)
	accountID := strings.TrimSpace(payload.AccountID)
	email := normalizeICloudIMAPEmail(payload.Email)
	appPassword := strings.TrimSpace(payload.AppPassword)
	if email == "" {
		writeError(w, http.StatusBadRequest, errCode("imap_email_missing", "请输入 iCloud 邮箱账号", false))
		return
	}
	if appPassword == "" {
		writeError(w, http.StatusBadRequest, errCode("imap_app_password_missing", "请输入 App 专用密码", false))
		return
	}
	if accountID != "" && !s.canAccessAccountID(r, accountID) {
		writeError(w, http.StatusForbidden, errCode("account_forbidden", "无权操作该 Apple 账号", false))
		return
	}

	if err := s.checkSavedIMAPLogin(r.Context(), email, appPassword); err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}

	now := time.Now()
	session, err := s.sessionForIMAPSave(ownerID, accountID, email)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if session.SavedAt.IsZero() {
		session.SavedAt = now
	}
	session.OwnerID = ownerID
	session.AppleID = firstNonEmpty(strings.TrimSpace(session.AppleID), email)
	session = withICloudIMAPLoginState(session, LoginState{
		Kind:              LoginStateICloudIMAP,
		Host:              defaultICloudIMAPHost,
		Origin:            "imaps://" + defaultICloudIMAPHost,
		SavedAt:           now,
		IMAPEmail:         email,
		IMAPUsername:      email,
		IMAPHost:          defaultICloudIMAPHost,
		IMAPPort:          defaultICloudIMAPPort,
		IMAPAppPassword:   appPassword,
		LastCheckedAt:     now,
		LastCheckOK:       true,
		LastStatusMessage: "取码登录正常",
	})
	session.LastCheckedAt = now
	session.LastCheckOK = true
	session.LastStatusMessage = "取码登录正常"
	if err := s.store.SaveICloudSessionForOwner(ownerID, session); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	sessions := s.publicSessionsForOwner(ownerID)
	publicSession := publicSessionForAccountID(sessions, session.AccountID)
	if strings.TrimSpace(session.AccountID) == "" {
		publicSession = publicSessionForAppleID(sessions, session.AppleID)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":  true,
		"message":  "取码登录已保存并检测正常",
		"session":  publicSession,
		"sessions": sessions,
	})
}

func (s *Server) handleCheckICloudIMAPLogin(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AccountID string `json:"account_id"`
	}
	if r.ContentLength != 0 {
		if err := decodeJSON(r, &payload); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	} else {
		_ = r.Body.Close()
	}
	ownerID := requestOwnerID(r, s.store)
	sessions := s.sessionsForOwner(ownerID, payload.AccountID)
	if len(sessions) == 0 {
		writeError(w, http.StatusBadRequest, errCode("imap_session_missing", "未保存取码登录，请先保存 iCloud 邮箱账号和 App 专用密码", true))
		return
	}

	checkedAt := time.Now()
	checks := 0
	failed := 0
	var lastErr error
	for _, session := range sessions {
		state, ok := iCloudIMAPLoginState(session)
		if !ok {
			continue
		}
		checks++
		if err := s.checkSavedIMAPLogin(r.Context(), state.IMAPEmail, state.IMAPAppPassword); err != nil {
			failed++
			lastErr = err
			state.LastCheckedAt = checkedAt
			state.LastCheckOK = false
			state.LastStatusMessage = "取码登录异常：" + err.Error()
		} else {
			state.LastCheckedAt = checkedAt
			state.LastCheckOK = true
			state.LastStatusMessage = "取码登录正常"
		}
		session = withICloudIMAPLoginState(session, state)
		if err := s.store.SaveICloudSessionForOwner(ownerID, session); err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
	}
	if checks == 0 {
		writeError(w, http.StatusBadRequest, errCode("imap_session_missing", "未保存取码登录，请先保存 iCloud 邮箱账号和 App 专用密码", true))
		return
	}
	publicSessions := s.publicSessionsForOwner(ownerID)
	if failed == checks {
		message := "取码登录检测失败"
		if lastErr != nil {
			message += "：" + lastErr.Error()
		}
		writeError(w, http.StatusBadGateway, errCode("imap_session_check_failed", message, true))
		return
	}
	message := "取码登录检测正常"
	if failed > 0 {
		message = fmt.Sprintf("取码登录部分检测成功：成功 %d，失败 %d", checks-failed, failed)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":       true,
		"message":       message,
		"checked_at":    formatTime(checkedAt),
		"sessions":      publicSessions,
		"checked_count": checks,
		"failed_count":  failed,
	})
}

func (s *Server) checkSavedIMAPLogin(ctx context.Context, email, appPassword string) error {
	if s.checkIMAPLogin != nil {
		return s.checkIMAPLogin(ctx, email, appPassword)
	}
	return CheckICloudIMAPLogin(ctx, email, appPassword)
}

func (s *Server) sessionForIMAPSave(ownerID, accountID, email string) (ICloudSession, error) {
	accountID = strings.TrimSpace(accountID)
	email = normalizeICloudIMAPEmail(email)
	if accountID != "" {
		accountAppleID := ""
		if account, ok := s.store.FindAccountByID(accountID); ok {
			accountAppleID = strings.TrimSpace(account.AppleID)
		}
		if session, ok := s.sessionForOwnerAccount(ownerID, accountID); ok {
			session.AppleID = firstNonEmpty(strings.TrimSpace(session.AppleID), accountAppleID, email)
			return session, nil
		}
		return ICloudSession{OwnerID: ownerID, AccountID: accountID, AppleID: firstNonEmpty(accountAppleID, email)}, nil
	}
	if session, ok := s.sessionForOwnerCreateEmailLocalPart(ownerID, email); ok {
		return session, nil
	}
	if session, ok := s.sessionForOwnerIMAPEmail(ownerID, email); ok {
		return session, nil
	}
	return ICloudSession{OwnerID: ownerID, AppleID: email}, nil
}

func (s *Server) sessionForOwnerCreateEmailLocalPart(ownerID, email string) (ICloudSession, bool) {
	local := emailLocalPart(email)
	if local == "" {
		return ICloudSession{}, false
	}
	if match, ok := s.sessionForOwnerCreateEmailLocalPartMatch(ownerID, local, false); ok {
		return match, true
	}
	return s.sessionForOwnerCreateEmailLocalPartMatch(ownerID, local, true)
}

func (s *Server) sessionForOwnerCreateEmailLocalPartMatch(ownerID, local string, allowAppleSecondaryPrefix bool) (ICloudSession, bool) {
	var match ICloudSession
	found := false
	for _, session := range s.sessionsForOwner(ownerID, "") {
		if !appleAccountLoginSaved(session) && !iCloudWebLoginSaved(session) {
			continue
		}
		if !emailLocalPartsMatch(emailLocalPart(session.AppleID), local, allowAppleSecondaryPrefix) {
			continue
		}
		if found && !sameICloudSessionPublicIdentity(match, session) {
			return ICloudSession{}, false
		}
		match = session
		found = true
	}
	return match, found
}

func emailLocalPartsMatch(accountLocal, imapLocal string, allowAppleSecondaryPrefix bool) bool {
	accountLocal = strings.ToLower(strings.TrimSpace(accountLocal))
	imapLocal = strings.ToLower(strings.TrimSpace(imapLocal))
	if accountLocal == "" || imapLocal == "" {
		return false
	}
	if accountLocal == imapLocal {
		return true
	}
	if !allowAppleSecondaryPrefix {
		return false
	}
	return "q"+accountLocal == imapLocal
}

func (s *Server) sessionForOwnerIMAPEmail(ownerID, email string) (ICloudSession, bool) {
	email = normalizeICloudIMAPEmail(email)
	for _, session := range s.sessionsForOwner(ownerID, "") {
		if email != "" && strings.EqualFold(normalizeICloudIMAPEmail(session.AppleID), email) {
			return session, true
		}
		if state, ok := iCloudIMAPLoginState(session); ok && strings.EqualFold(normalizeICloudIMAPEmail(state.IMAPEmail), email) {
			return session, true
		}
	}
	return ICloudSession{}, false
}

func emailLocalPart(value string) string {
	value = normalizeICloudIMAPEmail(value)
	if value == "" {
		return ""
	}
	if at := strings.Index(value, "@"); at > 0 {
		return value[:at]
	}
	return ""
}

func sameICloudSessionPublicIdentity(a, b ICloudSession) bool {
	if strings.TrimSpace(a.AccountID) != "" && strings.TrimSpace(b.AccountID) != "" {
		return constantTimeEqual(a.AccountID, b.AccountID)
	}
	if strings.TrimSpace(a.AppleID) != "" && strings.TrimSpace(b.AppleID) != "" {
		return strings.EqualFold(strings.TrimSpace(a.AppleID), strings.TrimSpace(b.AppleID))
	}
	return false
}

func checkSavedLoginStates(ctx context.Context, client *ICloudClient, session ICloudSession, checkedAt time.Time) (ICloudSession, bool, error) {
	return checkSavedLoginStatesWithIMAP(ctx, client, session, checkedAt, CheckICloudIMAPLogin)
}

func checkSavedLoginStatesWithIMAP(ctx context.Context, client *ICloudClient, session ICloudSession, checkedAt time.Time, imapChecker func(context.Context, string, string) error) (ICloudSession, bool, error) {
	var parts []string
	checks := 0
	successes := 0
	var lastErr error

	if appleAccountLoginSaved(session) {
		checks++
		updated, err := client.CheckAppleAccountManageSession(ctx, session)
		state, _ := appleAccountLoginState(session)
		if err != nil {
			lastErr = err
			state.LastCheckedAt = checkedAt
			state.LastCheckOK = false
			state.LastStatusMessage = "新接口登录态异常：" + err.Error()
			session = withAppleAccountLoginState(session, state)
			parts = append(parts, "新接口异常")
		} else {
			session = updated
			state, _ = appleAccountLoginState(session)
			state.LastCheckedAt = checkedAt
			state.LastCheckOK = true
			state.LastStatusMessage = "新接口登录态正常"
			session = withAppleAccountLoginState(session, state)
			successes++
			parts = append(parts, "新接口正常")
		}
	}

	if iCloudWebLoginSaved(session) {
		checks++
		state, _ := iCloudWebLoginState(session)
		if err := client.CheckMailSession(ctx, session); err != nil {
			lastErr = err
			state.LastCheckedAt = checkedAt
			state.LastCheckOK = false
			state.LastStatusMessage = "旧接口登录态异常：" + err.Error()
			session = withICloudWebLoginState(session, state)
			parts = append(parts, "旧接口异常")
		} else {
			state.LastCheckedAt = checkedAt
			state.LastCheckOK = true
			state.LastStatusMessage = "旧接口登录态正常"
			session = withICloudWebLoginState(session, state)
			successes++
			parts = append(parts, "旧接口正常")
		}
	}

	if iCloudIMAPLoginSaved(session) {
		checks++
		state, _ := iCloudIMAPLoginState(session)
		if imapChecker == nil {
			imapChecker = CheckICloudIMAPLogin
		}
		if err := imapChecker(ctx, state.IMAPEmail, state.IMAPAppPassword); err != nil {
			lastErr = err
			state.LastCheckedAt = checkedAt
			state.LastCheckOK = false
			state.LastStatusMessage = "取码登录异常：" + err.Error()
			session = withICloudIMAPLoginState(session, state)
			parts = append(parts, "取码登录异常")
		} else {
			state.LastCheckedAt = checkedAt
			state.LastCheckOK = true
			state.LastStatusMessage = "取码登录正常"
			session = withICloudIMAPLoginState(session, state)
			successes++
			parts = append(parts, "取码登录正常")
		}
	}

	if checks == 0 {
		lastErr = errCode("icloud_session_missing", "未保存新接口、旧接口或取码登录态，请先保存登录态", true)
		parts = append(parts, lastErr.Error())
	}

	session.LastCheckedAt = checkedAt
	session.LastCheckOK = successes > 0
	switch {
	case successes == checks && checks > 0:
		session.LastStatusMessage = "登录态正常：" + strings.Join(parts, "；")
	case successes > 0:
		session.LastStatusMessage = "登录态部分正常：" + strings.Join(parts, "；")
	default:
		session.LastStatusMessage = "登录态异常：" + strings.Join(parts, "；")
	}
	if session.LastCheckOK {
		return session, true, nil
	}
	if lastErr == nil {
		lastErr = errors.New(session.LastStatusMessage)
	}
	return session, false, lastErr
}

func (s *Server) handleStartICloudProtocolLogin(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AppleID         string `json:"apple_id"`
		Password        string `json:"password"`
		TwoFactorMethod string `json:"two_factor_method"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	result, err := NewAppleAuthClient().StartLogin(
		r.Context(),
		payload.AppleID,
		payload.Password,
		s.cfg.ICloudDefaultHost,
		s.cfg.ICloudClientID,
		s.icloudProtocolLogins,
		payload.TwoFactorMethod,
	)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	if result.Needs2FA {
		writeJSON(w, http.StatusOK, map[string]any{
			"success":    true,
			"needs_2fa":  true,
			"pending_id": result.PendingID,
			"apple_id":   result.AppleID,
			"expires_at": formatTime(result.ExpiresAt),
			"message":    result.Message,
		})
		return
	}
	if err := s.store.SaveICloudSessionForOwner(requestOwnerID(r, s.store), result.Session); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	sessions := s.publicSessionsForRequest(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"success":   true,
		"needs_2fa": false,
		"message":   result.Message,
		"session":   publicSessionForAppleID(sessions, result.Session.AppleID),
		"sessions":  sessions,
	})
}

func (s *Server) handleSubmitICloudProtocol2FA(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		PendingID string `json:"pending_id"`
		Code      string `json:"code"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	pending, ok := s.icloudProtocolLogins.get(payload.PendingID)
	if !ok {
		writeError(w, http.StatusBadRequest, errCode("apple_login_pending_expired", "旧接口登录已过期，请重新输入账号密码发起登录", true))
		return
	}
	session, err := NewAppleAuthClient().Submit2FA(r.Context(), pending, payload.Code)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	s.icloudProtocolLogins.delete(payload.PendingID)
	if err := s.store.SaveICloudSessionForOwner(requestOwnerID(r, s.store), session); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	sessions := s.publicSessionsForRequest(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"success":  true,
		"message":  "旧接口验证码登录成功，登录态已保存",
		"session":  publicSessionForAppleID(sessions, session.AppleID),
		"sessions": sessions,
	})
}

func (s *Server) handleStartAppleAccountLogin(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AppleID         string `json:"apple_id"`
		Password        string `json:"password"`
		TwoFactorMethod string `json:"two_factor_method"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	result, err := NewAppleAuthClient().StartAppleAccountManageLogin(
		r.Context(),
		payload.AppleID,
		payload.Password,
		s.appleAccountLogins,
		payload.TwoFactorMethod,
	)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	if result.Needs2FA {
		writeJSON(w, http.StatusOK, map[string]any{
			"success":    true,
			"needs_2fa":  true,
			"pending_id": result.PendingID,
			"apple_id":   result.AppleID,
			"expires_at": formatTime(result.ExpiresAt),
			"message":    result.Message,
		})
		return
	}
	if err := s.store.SaveICloudSessionForOwner(requestOwnerID(r, s.store), result.Session); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	sessions := s.publicSessionsForRequest(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"success":   true,
		"needs_2fa": false,
		"message":   result.Message,
		"session":   publicSessionForAppleID(sessions, result.Session.AppleID),
		"sessions":  sessions,
	})
}

func (s *Server) handleSubmitAppleAccount2FA(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		PendingID   string          `json:"pending_id"`
		Code        string          `json:"code"`
		PhoneNumber json.RawMessage `json:"phone_number,omitempty"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	pending, ok := s.appleAccountLogins.get(payload.PendingID)
	if !ok {
		writeError(w, http.StatusBadRequest, errCode("apple_login_pending_expired", "新接口登录已过期，请重新输入账号密码发起登录", true))
		return
	}
	session, err := NewAppleAuthClient().SubmitAppleAccountManage2FA(r.Context(), pending, payload.Code, payload.PhoneNumber)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	s.appleAccountLogins.delete(payload.PendingID)
	if err := s.store.SaveICloudSessionForOwner(requestOwnerID(r, s.store), session); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	sessions := s.publicSessionsForRequest(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"success":  true,
		"message":  "新接口验证码登录成功，登录态已保存",
		"session":  publicSessionForAppleID(sessions, session.AppleID),
		"sessions": sessions,
	})
}

func (s *Server) handleCreateICloudMailbox(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AccountID     string   `json:"account_id"`
		AccountIDs    []string `json:"account_ids"`
		Label         string   `json:"label"`
		Note          string   `json:"note"`
		CreateChannel string   `json:"create_channel"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	accountIDs := normalizeAccountIDSelection(payload.AccountID, payload.AccountIDs)
	if !s.canAccessAccountIDs(r, accountIDs) {
		writeError(w, http.StatusNotFound, errCode("account_not_found", "账号不存在", false))
		return
	}
	ownerID := requestOwnerID(r, s.store)
	channel := normalizeMailboxCreateChannel(mailboxCreateChannel(strings.ToLower(strings.TrimSpace(payload.CreateChannel))))
	requests := make([]mailboxCreateRequest, 0, len(accountIDs))
	for _, accountID := range accountIDs {
		requests = append(requests, mailboxCreateRequest{AccountID: accountID, Channel: channel})
	}
	mailboxes, remotes, failures, err := s.createMailboxesForOwnerWithChannels(r.Context(), ownerID, requests, payload.Label, payload.Note)
	if err != nil {
		s.logICloudCreateError(ownerID, err)
		if len(mailboxes) == 0 && len(failures) == 0 {
			writeError(w, http.StatusBadGateway, err)
			return
		}
	}
	out := make([]publicMailbox, 0, len(mailboxes))
	for _, mailbox := range mailboxes {
		out = append(out, s.publicMailbox(r, mailbox))
	}
	status := http.StatusCreated
	if len(failures) > 0 {
		status = http.StatusMultiStatus
	}
	remoteOut := make([]map[string]any, 0, len(remotes))
	for index, remote := range remotes {
		channel := mailboxCreateChannelFromRemote(remote)
		channelValue := string(channel)
		channelLabel := mailboxCreateChannelLabel(channel)
		if index < len(out) {
			out[index].CreateChannel = channelValue
			out[index].CreateChannelLabel = channelLabel
		}
		remoteOut = append(remoteOut, map[string]any{
			"anonymous_id":  remote.AnonymousID,
			"email":         remote.Email,
			"is_active":     remote.IsActive,
			"origin":        remote.Origin,
			"channel":       channelValue,
			"channel_label": channelLabel,
		})
	}
	firstMailbox := publicMailbox{}
	if len(out) > 0 {
		firstMailbox = out[0]
	}
	writeJSON(w, status, map[string]any{
		"success":   true,
		"remote":    firstMap(remoteOut),
		"remotes":   remoteOut,
		"mailbox":   firstMailbox,
		"mailboxes": out,
		"created":   len(out),
		"failures":  failures,
	})
}

func (s *Server) handleSyncICloudMailboxes(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AccountID string `json:"account_id"`
	}
	if r.ContentLength != 0 {
		if err := decodeJSON(r, &payload); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	} else {
		_ = r.Body.Close()
	}
	if !s.canAccessAccountID(r, payload.AccountID) {
		writeError(w, http.StatusNotFound, errCode("account_not_found", "账号不存在", false))
		return
	}
	ownerID := requestOwnerID(r, s.store)
	sessions := s.sessionsForOwner(ownerID, payload.AccountID)
	if len(sessions) == 0 {
		writeError(w, http.StatusBadRequest, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先保存旧接口登录态", true))
		return
	}

	created := 0
	updated := 0
	skipped := 0
	total := 0
	failed := 0
	out := make([]publicMailbox, 0)
	type syncJobResult struct {
		result syncICloudMailboxResult
		rows   []publicMailbox
		err    error
	}
	jobResults := make([]syncJobResult, len(sessions))
	var wg sync.WaitGroup
	for index, session := range sessions {
		wg.Add(1)
		go func(index int, session ICloudSession) {
			defer wg.Done()
			syncCtx, cancel := context.WithTimeout(r.Context(), iCloudMailboxListAccountTimeout)
			defer cancel()
			result, rows, err := s.syncICloudMailboxesForSession(syncCtx, r, ownerID, session)
			if err != nil && errors.Is(err, context.DeadlineExceeded) {
				err = errCode("icloud_sync_timeout", "该账号同步已有邮箱超时，请稍后单独重试", true)
				result.Error = err.Error()
			}
			jobResults[index] = syncJobResult{result: result, rows: rows, err: err}
		}(index, session)
	}
	wg.Wait()
	results := make([]syncICloudMailboxResult, 0, len(sessions))
	for _, job := range jobResults {
		result, rows, err := job.result, job.rows, job.err
		results = append(results, result)
		if err != nil {
			failed++
			s.logger.Warn("iCloud mailbox list failed", "account_id", result.AccountID, "err", err)
			continue
		}
		total += result.Total
		created += result.Created
		updated += result.Updated
		skipped += result.Skipped
		out = append(out, rows...)
	}
	message := ""
	if failed == len(sessions) {
		message = "全部 iCloud 账号同步失败，请看每个账号的失败原因"
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":   true,
		"message":   message,
		"total":     total,
		"created":   created,
		"updated":   updated,
		"skipped":   skipped,
		"failed":    failed,
		"results":   results,
		"mailboxes": out,
	})
}

func (s *Server) syncICloudMailboxesForSession(ctx context.Context, r *http.Request, ownerID string, session ICloudSession) (syncICloudMailboxResult, []publicMailbox, error) {
	result := syncICloudMailboxResult{
		AccountID: session.AccountID,
		AppleID:   strings.TrimSpace(session.AppleID),
		Source:    string(mailboxCreateChannelICloudWeb),
	}
	if !iCloudWebLoginSaved(session) {
		err := errCode("icloud_session_missing", "该账号没有可用于同步已有邮箱的旧接口登录态，请先完成旧接口登录", true)
		result.Error = err.Error()
		return result, nil, err
	}
	remotes, err := NewICloudClient().ListPrivacyMailboxes(ctx, session)
	if err != nil {
		result.Error = err.Error()
		return result, nil, err
	}
	result.Total = len(remotes)
	out := make([]publicMailbox, 0, len(remotes))
	accountID := strings.TrimSpace(session.AccountID)
	for _, remote := range remotes {
		mailbox, isCreated, err := s.store.UpsertMailboxFromRemote(ownerID, accountID, remote, "synced from iCloud HME list")
		if err != nil {
			var coded codedError
			if errors.As(err, &coded) && coded.code == "mailbox_exists_other_owner" {
				result.Skipped++
				continue
			}
			result.Error = err.Error()
			return result, out, err
		}
		if isCreated {
			result.Created++
		} else {
			result.Updated++
		}
		out = append(out, s.publicMailbox(r, mailbox))
	}
	return result, out, nil
}

func (s *Server) handleListAccounts(w http.ResponseWriter, r *http.Request) {
	state := s.scopedState(r)
	out := make([]publicAccount, 0, len(state.Accounts))
	for _, account := range state.Accounts {
		out = append(out, s.publicAccount(account))
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "accounts": out})
}

func (s *Server) handleCreateAccount(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		Label   string `json:"label"`
		AppleID string `json:"apple_id"`
		Note    string `json:"note"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	account, err := s.store.AddAccountForOwner(requestOwnerID(r, s.store), payload.Label, payload.AppleID, payload.Note)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"success": true, "account": account})
}

func (s *Server) handleListMailboxes(w http.ResponseWriter, r *http.Request) {
	state := s.scopedState(r)
	accountsByID := mailboxAccountMap(state.Accounts)
	base := filterMailboxesByOwner(state.Mailboxes, strings.TrimSpace(r.URL.Query().Get("owner_id")), scopedOwnerID(r, s.store), s.isAdminRequest(r))
	groups := publicMailboxGroups(base, accountsByID)
	filtered := filterMailboxesForList(base, accountsByID, r.URL.Query())
	sortMailboxesForList(filtered, accountsByID)

	page, pageSize, paged := mailboxListPagination(r)
	pageRows := filtered
	if paged {
		pageRows = paginateMailboxes(filtered, page, pageSize)
	}
	out := make([]publicMailbox, 0, len(pageRows))
	for _, mailbox := range pageRows {
		out = append(out, s.publicMailbox(r, mailbox))
	}
	response := map[string]any{
		"success":   true,
		"mailboxes": out,
		"groups":    groups,
		"pagination": publicPagination{
			Page:       page,
			PageSize:   pageSize,
			Total:      len(filtered),
			TotalAll:   len(base),
			TotalPages: totalPages(len(filtered), pageSize),
		},
	}
	writeJSON(w, http.StatusOK, response)
}

func filterMailboxesByOwner(mailboxes []Mailbox, ownerFilter, scopedOwner string, admin bool) []Mailbox {
	ownerFilter = strings.TrimSpace(ownerFilter)
	if ownerFilter == "" || ownerFilter == "all" {
		return append([]Mailbox(nil), mailboxes...)
	}
	if !admin {
		scopedOwner = strings.TrimSpace(scopedOwner)
		if scopedOwner == "" || !constantTimeEqual(scopedOwner, ownerFilter) {
			return nil
		}
	}
	out := make([]Mailbox, 0, len(mailboxes))
	for _, mailbox := range mailboxes {
		if constantTimeEqual(strings.TrimSpace(mailbox.OwnerID), ownerFilter) {
			out = append(out, mailbox)
		}
	}
	return out
}

func filterMailboxesForList(mailboxes []Mailbox, accountsByID map[string]Account, values url.Values) []Mailbox {
	accountKey := strings.TrimSpace(firstNonEmpty(values.Get("account_key"), values.Get("account_id")))
	keyword := strings.ToLower(strings.TrimSpace(firstNonEmpty(values.Get("search"), values.Get("q"))))
	out := make([]Mailbox, 0, len(mailboxes))
	for _, mailbox := range mailboxes {
		if keyword == "" && accountKey != "" && accountKey != "all" && !constantTimeEqual(mailboxListAccountKey(mailbox, accountsByID), accountKey) {
			continue
		}
		if keyword != "" && !mailboxListMatchesSearch(mailbox, accountsByID, keyword) {
			continue
		}
		out = append(out, mailbox)
	}
	return out
}

func mailboxListMatchesSearch(mailbox Mailbox, accountsByID map[string]Account, keyword string) bool {
	account := accountsByID[strings.TrimSpace(mailbox.AccountID)]
	haystack := strings.ToLower(strings.Join([]string{
		mailbox.Email,
		mailbox.Label,
		mailbox.ID,
		mailbox.AccountID,
		account.Label,
		account.AppleID,
		mailbox.Status,
		mailbox.OwnerID,
	}, " "))
	return strings.Contains(haystack, keyword)
}

func sortMailboxesForList(mailboxes []Mailbox, accountsByID map[string]Account) {
	sort.Slice(mailboxes, func(i, j int) bool {
		leftTitle := strings.ToLower(mailboxListAccountTitle(mailboxes[i], accountsByID))
		rightTitle := strings.ToLower(mailboxListAccountTitle(mailboxes[j], accountsByID))
		if leftTitle != rightTitle {
			return leftTitle < rightTitle
		}
		return strings.ToLower(mailboxes[i].Email) < strings.ToLower(mailboxes[j].Email)
	})
}

func paginateMailboxes(mailboxes []Mailbox, page, pageSize int) []Mailbox {
	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = mailboxListDefaultPageSize
	}
	start := (page - 1) * pageSize
	if start >= len(mailboxes) {
		return nil
	}
	end := start + pageSize
	if end > len(mailboxes) {
		end = len(mailboxes)
	}
	return mailboxes[start:end]
}

func mailboxListPagination(r *http.Request) (int, int, bool) {
	values := r.URL.Query()
	paged := values.Has("page") || values.Has("page_size") || values.Has("search") || values.Has("q") || values.Has("account_key") || values.Has("account_id") || values.Has("owner_id")
	page := parseBoundedPositiveInt(values.Get("page"), 1, 1, 1_000_000)
	pageSize := parseBoundedPositiveInt(values.Get("page_size"), mailboxListDefaultPageSize, 1, mailboxListMaxPageSize)
	return page, pageSize, paged
}

func parseBoundedPositiveInt(value string, fallback, minValue, maxValue int) int {
	n, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil {
		return fallback
	}
	if n < minValue {
		return minValue
	}
	if n > maxValue {
		return maxValue
	}
	return n
}

func totalPages(total, pageSize int) int {
	if pageSize < 1 {
		pageSize = mailboxListDefaultPageSize
	}
	if total <= 0 {
		return 1
	}
	return (total + pageSize - 1) / pageSize
}

func mailboxAccountMap(accounts []Account) map[string]Account {
	out := make(map[string]Account, len(accounts))
	for _, account := range accounts {
		if strings.TrimSpace(account.ID) != "" {
			out[account.ID] = account
		}
	}
	return out
}

func publicMailboxGroups(mailboxes []Mailbox, accountsByID map[string]Account) []publicMailboxGroup {
	byKey := map[string]publicMailboxGroup{}
	for _, mailbox := range mailboxes {
		key := mailboxListAccountKey(mailbox, accountsByID)
		group := byKey[key]
		if group.Key == "" {
			group = publicMailboxGroup{
				Key:       key,
				Title:     mailboxListAccountTitle(mailbox, accountsByID),
				AccountID: strings.TrimSpace(mailbox.AccountID),
			}
		}
		group.Count++
		byKey[key] = group
	}
	groups := make([]publicMailboxGroup, 0, len(byKey))
	for _, group := range byKey {
		groups = append(groups, group)
	}
	sort.Slice(groups, func(i, j int) bool {
		return strings.ToLower(groups[i].Title) < strings.ToLower(groups[j].Title)
	})
	return groups
}

func mailboxListAccountKey(mailbox Mailbox, accountsByID map[string]Account) string {
	if strings.TrimSpace(mailbox.AccountID) != "" {
		return strings.TrimSpace(mailbox.AccountID)
	}
	return "unbound"
}

func mailboxListAccountTitle(mailbox Mailbox, accountsByID map[string]Account) string {
	account := accountsByID[strings.TrimSpace(mailbox.AccountID)]
	return firstNonEmpty(account.Label, account.AppleID, mailbox.AccountID, "未绑定 Apple 账号")
}

func (s *Server) handleCreateMailbox(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AccountID string `json:"account_id"`
		Label     string `json:"label"`
		Email     string `json:"email"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if !s.canAccessAccountID(r, payload.AccountID) {
		writeError(w, http.StatusNotFound, errCode("account_not_found", "账号不存在", false))
		return
	}
	mailbox, err := s.store.AddMailboxForOwner(requestOwnerID(r, s.store), payload.AccountID, payload.Label, payload.Email)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"success": true, "mailbox": s.publicMailbox(r, mailbox)})
}

func (s *Server) handleVerifyMailbox(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.canAccessMailboxID(r, id) {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	active := true
	mailbox, err := s.store.SetMailboxStatus(id, &active, &active, StatusAvailable, "手动验证通过")
	if err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "mailbox": s.publicMailbox(r, mailbox)})
}

func (s *Server) handleDisableMailbox(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.canAccessMailboxID(r, id) {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	inactive := false
	mailbox, err := s.store.SetMailboxStatus(id, &inactive, nil, StatusDisabled, "API 已停用")
	if err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "mailbox": s.publicMailbox(r, mailbox)})
}

func (s *Server) handleSetMailboxStatus(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.canAccessMailboxID(r, id) {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	var payload struct {
		Status       string `json:"status"`
		Note         string `json:"note"`
		APIActive    *bool  `json:"api_active"`
		ICloudActive *bool  `json:"icloud_active"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	status := strings.ToLower(strings.TrimSpace(payload.Status))
	if status != "" && !validMailboxStatus(status) {
		writeError(w, http.StatusBadRequest, errCode("invalid_status", "状态只能是 available、used、failed、active、disabled", false))
		return
	}
	mailbox, err := s.store.SetMailboxStatus(id, payload.APIActive, payload.ICloudActive, status, payload.Note)
	if err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "mailbox": s.publicMailbox(r, mailbox)})
}

func (s *Server) handleSyncMailbox(w http.ResponseWriter, r *http.Request) {
	mailbox, ok := s.store.FindMailboxByID(r.PathValue("id"))
	if !ok {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	if !s.canAccessMailbox(r, mailbox) {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	after, err := parseAfter(r.URL.Query().Get("after"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	count, err := s.syncMailbox(r.Context(), mailbox, after, strings.TrimSpace(r.URL.Query().Get("keyword")))
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "synced": count})
}

func (s *Server) handleCleanRemoteMailbox(w http.ResponseWriter, r *http.Request) {
	mailbox, ok := s.store.FindMailboxByID(r.PathValue("id"))
	if !ok {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	if !s.canAccessMailbox(r, mailbox) {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	var payload struct {
		MoveSynced bool `json:"move_synced"`
		EmptyTrash bool `json:"empty_trash"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if !payload.MoveSynced && !payload.EmptyTrash {
		payload.MoveSynced = true
	}
	session, ok := s.sessionForMailbox(mailbox.OwnerID, mailbox.AccountID)
	if !ok {
		writeError(w, http.StatusBadRequest, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先保存旧接口登录态", true))
		return
	}

	client := NewICloudClient()
	result := ICloudMailCleanupResult{}
	if payload.MoveSynced {
		remoteIDs := icloudRemoteIDsFromMessages(s.store.MessagesForMailbox(mailbox.ID))
		moved, err := client.MoveRemoteMessagesToTrash(r.Context(), session, remoteIDs)
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		result.MovedToTrash += moved.MovedToTrash
		result.Skipped += moved.Skipped
	}
	if payload.EmptyTrash {
		destroyed, err := client.EmptyTrash(r.Context(), session)
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		result.Destroyed = destroyed
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "cleanup": result})
}

func (s *Server) handleCleanRemoteMailboxes(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		AccountID  string `json:"account_id"`
		MoveSynced bool   `json:"move_synced"`
		EmptyTrash bool   `json:"empty_trash"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if !payload.MoveSynced && !payload.EmptyTrash {
		payload.MoveSynced = true
		payload.EmptyTrash = true
	}
	accountID := strings.TrimSpace(payload.AccountID)
	if accountID != "" && !s.canAccessAccountID(r, accountID) {
		writeError(w, http.StatusNotFound, errCode("account_not_found", "账号不存在", false))
		return
	}

	state := s.scopedState(r)
	client := NewICloudClient()
	result := ICloudMailCleanupResult{}
	cleanedSessions := map[string]bool{}
	handledMailboxes := 0
	failedMailboxes := 0
	for _, mailbox := range state.Mailboxes {
		if accountID != "" && !constantTimeEqual(accountID, strings.TrimSpace(mailbox.AccountID)) {
			continue
		}
		if !mailbox.ICloudActive || mailbox.Status == StatusDisabled {
			result.Skipped++
			continue
		}
		session, ok := s.sessionForMailbox(mailbox.OwnerID, mailbox.AccountID)
		if !ok {
			result.Skipped++
			continue
		}
		sessionKey := firstNonEmpty(session.OwnerID, mailbox.OwnerID, "__legacy__") + ":" + firstNonEmpty(session.AccountID, session.DSID, session.AppleID, mailbox.AccountID, "__session__")
		if payload.MoveSynced {
			remoteIDs := icloudRemoteIDsFromMessages(s.store.MessagesForMailbox(mailbox.ID))
			moved, err := client.MoveRemoteMessagesToTrash(r.Context(), session, remoteIDs)
			if err != nil {
				s.logger.Warn("remote mail cleanup move failed", "mailbox_id", mailbox.ID, "err", err)
				failedMailboxes++
				continue
			}
			result.MovedToTrash += moved.MovedToTrash
			result.Skipped += moved.Skipped
		}
		handledMailboxes++
		if payload.EmptyTrash && !cleanedSessions[sessionKey] {
			destroyed, err := client.EmptyTrash(r.Context(), session)
			if err != nil {
				s.logger.Warn("remote mail cleanup empty trash failed", "account_id", mailbox.AccountID, "err", err)
				failedMailboxes++
				continue
			}
			result.Destroyed += destroyed
			cleanedSessions[sessionKey] = true
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success":          true,
		"cleanup":          result,
		"mailboxes":        handledMailboxes,
		"failed_mailboxes": failedMailboxes,
	})
}

func (s *Server) handleDeleteMailbox(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.canAccessMailboxID(r, id) {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	if err := s.store.DeleteMailbox(id); err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true})
}

func (s *Server) handleListMessages(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	mailbox, ok := s.store.FindMailboxByID(id)
	if !ok || !s.canAccessMailbox(r, mailbox) {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	messages := s.store.MessagesForMailbox(id)
	out := make([]publicMessage, 0, len(messages))
	for _, msg := range messages {
		out = append(out, publicMessage{
			ID:         msg.ID,
			OwnerID:    msg.OwnerID,
			Owner:      s.ownerName(msg.OwnerID),
			MailboxID:  msg.MailboxID,
			Subject:    msg.Subject,
			From:       msg.From,
			Body:       msg.Body,
			ReceivedAt: formatTime(msg.ReceivedAt),
			CreatedAt:  formatTime(msg.CreatedAt),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "messages": out})
}

func (s *Server) handleCreateMessage(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.canAccessMailboxID(r, id) {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	var payload struct {
		Subject    string `json:"subject"`
		From       string `json:"from"`
		Body       string `json:"body"`
		ReceivedAt string `json:"received_at"`
	}
	if err := decodeJSON(r, &payload); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	receivedAt := time.Now()
	if strings.TrimSpace(payload.ReceivedAt) != "" {
		parsed, err := time.Parse(time.RFC3339, payload.ReceivedAt)
		if err != nil {
			writeError(w, http.StatusBadRequest, errCode("invalid_received_at", "received_at 必须是 RFC3339 时间", false))
			return
		}
		receivedAt = parsed
	}
	msg, err := s.store.AddMessage(id, payload.Subject, payload.From, payload.Body, receivedAt)
	if err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"success": true, "message": msg})
}

func (s *Server) handleMailboxCodeByID(w http.ResponseWriter, r *http.Request) {
	mailbox, ok := s.store.FindMailboxByID(r.PathValue("id"))
	if !ok {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	s.writeMailboxCode(w, r, mailbox)
}

func (s *Server) handleMailboxCodeByEmail(w http.ResponseWriter, r *http.Request) {
	email, err := url.PathUnescape(r.PathValue("email"))
	if err != nil {
		writeError(w, http.StatusBadRequest, errCode("invalid_email", "邮箱路径非法", false))
		return
	}
	mailbox, ok := s.store.FindMailboxByEmail(email)
	if !ok {
		writeError(w, http.StatusNotFound, errCode("mailbox_not_found", "邮箱不存在", false))
		return
	}
	s.writeMailboxCode(w, r, mailbox)
}

func (s *Server) writeMailboxCode(w http.ResponseWriter, r *http.Request, mailbox Mailbox) {
	if !s.authorized(r, mailbox) {
		writeError(w, http.StatusUnauthorized, errCode("invalid_api_key", "API Key 错误", false))
		return
	}
	if !mailbox.APIActive || mailbox.Status == StatusDisabled {
		writeError(w, http.StatusForbidden, errCode("api_disabled", "API 已停用", false))
		return
	}
	if !mailbox.ICloudActive {
		writeError(w, http.StatusForbidden, errCode("icloud_inactive", "邮箱已停用或 iCloud 状态不可用", false))
		return
	}
	s.markMailWatcherActive(mailbox.ID)
	after, err := parseAfter(r.URL.Query().Get("after"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	keyword := strings.TrimSpace(r.URL.Query().Get("keyword"))
	if keyword == "" {
		keyword = "OpenAI"
	}
	now := time.Now()
	codeAfter := mailboxCodeAfter(after, now)
	allowStale := truthy(r.URL.Query().Get("allow_stale"))
	cacheOnly := truthy(r.URL.Query().Get("cache"))
	peekOnly := truthy(r.URL.Query().Get("peek")) || truthy(r.URL.Query().Get("preview"))
	skipMessageID := strings.TrimSpace(mailbox.LastCodeMessageID)
	if peekOnly {
		skipMessageID = ""
	}
	messages := s.store.MessagesForMailbox(mailbox.ID)
	if cacheOnly {
		if msg, code, ok := latestMailboxCode(messages, codeAfter, keyword, now); ok {
			s.writeMailboxCodeSuccess(w, mailbox, msg, code, "", false)
			return
		}
		writeError(w, http.StatusOK, errCode("no_code", "暂未收到验证码", true))
		return
	}
	if msg, code, ok := latestMailboxCodeSkipping(messages, codeAfter, keyword, now, skipMessageID); ok {
		s.writeMailboxCodeSuccess(w, mailbox, msg, code, "", !peekOnly)
		return
	}

	result := s.waitMailboxCode(r.Context(), mailbox, codeAfter, keyword, true, skipMessageID, s.mailboxCodeWaitDuration(r))
	if result.syncErr != nil {
		s.logger.Warn("icloud sync failed", "mailbox_id", mailbox.ID, "err", result.syncErr)
	}
	if result.ok {
		s.writeMailboxCodeSuccess(w, mailbox, result.message, result.code, staleCacheMessage(result.syncErr), !peekOnly)
		return
	}
	if msg, code, ok := latestMailboxCodeSkipping(s.store.MessagesForMailbox(mailbox.ID), codeAfter, keyword, time.Now(), skipMessageID); ok {
		s.writeMailboxCodeSuccess(w, mailbox, msg, code, staleCacheMessage(result.syncErr), !peekOnly)
		return
	}
	if result.syncErr != nil && allowStale {
		if msg, code, ok := latestMailboxCodeSkipping(s.store.MessagesForMailbox(mailbox.ID), codeAfter, keyword, time.Now(), skipMessageID); ok {
			s.writeMailboxCodeSuccess(w, mailbox, msg, code, "取码同步失败，当前验证码来自本地缓存", !peekOnly)
			return
		}
	}
	if result.syncErr != nil && !allowStale {
		writeError(w, http.StatusBadGateway, errCode("mail_sync_failed", "同步验证码邮件失败，已拒绝返回本地旧验证码；请检查取码登录或稍后重试", true))
		return
	}
	writeError(w, http.StatusOK, errCode("no_code", "暂未收到验证码", true))
}

func (s *Server) writeMailboxCodeSuccess(w http.ResponseWriter, mailbox Mailbox, msg Message, code string, staleMessage string, markServed bool) {
	if markServed {
		if _, err := s.store.SetMailboxLastCode(mailbox.ID, msg.ID, time.Now()); err != nil {
			s.logger.Warn("remember mailbox code failed", "mailbox_id", mailbox.ID, "message_id", msg.ID, "err", err)
			writeError(w, http.StatusInternalServerError, errCode("remember_code_failed", "保存验证码发放记录失败，请稍后重试", true))
			return
		}
	}
	payload := map[string]any{
		"success":     true,
		"email":       mailbox.Email,
		"code":        code,
		"subject":     msg.Subject,
		"received_at": formatTime(msg.ReceivedAt),
		"message_id":  msg.ID,
	}
	if staleMessage != "" {
		payload["stale_cache"] = true
		payload["sync_error"] = staleMessage
	}
	writeJSON(w, http.StatusOK, payload)
}

func staleCacheMessage(err error) string {
	if err == nil {
		return ""
	}
	return "取码同步失败，当前验证码来自本地缓存"
}

func mailboxCodeAfter(after, now time.Time) time.Time {
	if now.IsZero() {
		now = time.Now()
	}
	cutoff := now.Add(-mailboxCodeFreshWindow)
	if after.After(cutoff) {
		return after
	}
	return cutoff
}

func latestMailboxCode(messages []Message, after time.Time, keyword string, now time.Time) (Message, string, bool) {
	return latestMailboxCodeSkipping(messages, after, keyword, now, "")
}

func latestMailboxCodeSkipping(messages []Message, after time.Time, keyword string, now time.Time, skipMessageID string) (Message, string, bool) {
	keyword = strings.TrimSpace(keyword)
	if keyword == "" {
		keyword = "OpenAI"
	}
	skipMessageID = strings.TrimSpace(skipMessageID)
	after = mailboxCodeAfter(after, now)
	sort.SliceStable(messages, func(i, j int) bool {
		left := firstNonZeroTime(messages[i].ReceivedAt, messages[i].CreatedAt)
		right := firstNonZeroTime(messages[j].ReceivedAt, messages[j].CreatedAt)
		return left.After(right)
	})
	for _, msg := range messages {
		if skipMessageID != "" && msg.ID == skipMessageID {
			continue
		}
		msgTime := firstNonZeroTime(msg.ReceivedAt, msg.CreatedAt)
		if msgTime.IsZero() || msgTime.Before(after) {
			continue
		}
		text := msg.Subject + "\n" + msg.Body
		if !strings.Contains(strings.ToLower(text), strings.ToLower(keyword)) && keyword != "OpenAI" {
			continue
		}
		code := extractOTP(text)
		if code == "" {
			continue
		}
		return msg, code, true
	}
	return Message{}, "", false
}

func truthy(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "y", "on":
		return true
	default:
		return false
	}
}

func (s *Server) mailboxCodeWaitDuration(r *http.Request) time.Duration {
	fallback := s.mailboxCodeFastWait
	if r == nil {
		return fallback
	}
	raw := strings.TrimSpace(r.URL.Query().Get("wait_ms"))
	if raw == "" {
		return fallback
	}
	fallbackMS := int(fallback / time.Millisecond)
	maxMS := int(mailboxCodeMaxClientWait / time.Millisecond)
	waitMS := parseBoundedPositiveInt(raw, fallbackMS, 1, maxMS)
	return time.Duration(waitMS) * time.Millisecond
}

func (s *Server) waitMailboxCode(ctx context.Context, mailbox Mailbox, after time.Time, keyword string, forceSync bool, skipMessageID string, waitDuration time.Duration) mailboxCodeResult {
	waitCtx := context.Background()
	var requestDone <-chan struct{}
	if ctx != nil {
		waitCtx = context.WithoutCancel(ctx)
		requestDone = ctx.Done()
	}
	waiter := &mailboxCodeWaiter{
		ctx:           waitCtx,
		mailboxID:     mailbox.ID,
		after:         after,
		keyword:       keyword,
		forceSync:     forceSync,
		skipMessageID: skipMessageID,
		result:        make(chan mailboxCodeResult, 1),
	}
	ownerKey := mailboxSyncOwnerKey(mailbox.OwnerID)
	s.mailboxCodeMu.Lock()
	if s.mailboxCodePollers == nil {
		s.mailboxCodePollers = make(map[string]*mailboxCodePoller)
	}
	poller := s.mailboxCodePollers[ownerKey]
	if poller == nil {
		poller = &mailboxCodePoller{ownerID: mailbox.OwnerID}
		s.mailboxCodePollers[ownerKey] = poller
		go s.runMailboxCodePoller(ownerKey, poller)
	}
	poller.waiters = append(poller.waiters, waiter)
	s.mailboxCodeMu.Unlock()

	var timeout <-chan time.Time
	var timer *time.Timer
	if waitDuration > 0 {
		timer = time.NewTimer(waitDuration)
		timeout = timer.C
		defer timer.Stop()
	}
	var localTick <-chan time.Time
	var localTicker *time.Ticker
	if waitDuration > 0 {
		interval := mailboxCodeLocalPollInterval
		if interval <= 0 {
			interval = 100 * time.Millisecond
		}
		if interval > waitDuration {
			interval = waitDuration
		}
		localTicker = time.NewTicker(interval)
		localTick = localTicker.C
		defer localTicker.Stop()
	}
	for {
		select {
		case result := <-waiter.result:
			return result
		case <-localTick:
			if msg, code, ok := s.latestMailboxCodeForWaiter(waiter); ok {
				return mailboxCodeResult{message: msg, code: code, ok: true}
			}
		case <-timeout:
			if msg, code, ok := s.latestMailboxCodeForWaiter(waiter); ok {
				return mailboxCodeResult{message: msg, code: code, ok: true}
			}
			return mailboxCodeResult{}
		case <-requestDone:
			return mailboxCodeResult{syncErr: ctx.Err()}
		}
	}
}

func (s *Server) runMailboxCodePoller(ownerKey string, poller *mailboxCodePoller) {
	for {
		if debounce := mailboxCodePollDebounce; debounce > 0 {
			time.Sleep(debounce)
		}
		s.mailboxCodeMu.Lock()
		waiters := poller.waiters
		poller.waiters = nil
		if len(waiters) == 0 {
			delete(s.mailboxCodePollers, ownerKey)
			s.mailboxCodeMu.Unlock()
			return
		}
		s.mailboxCodeMu.Unlock()
		s.resolveMailboxCodeWaiters(poller.ownerID, waiters)
	}
}

func (s *Server) resolveMailboxCodeWaiters(ownerID string, waiters []*mailboxCodeWaiter) {
	active := activeMailboxCodeWaiters(waiters)
	if len(active) == 0 {
		return
	}
	pending := make([]*mailboxCodeWaiter, 0, len(active))
	for _, waiter := range active {
		if !waiter.forceSync {
			msg, code, ok := s.latestMailboxCodeForWaiter(waiter)
			if ok {
				deliverMailboxCodeResult(waiter, mailboxCodeResult{message: msg, code: code, ok: true})
				continue
			}
		}
		pending = append(pending, waiter)
	}
	if len(pending) == 0 {
		return
	}
	syncCtx := context.Background()
	if pending[0].ctx != nil {
		syncCtx = context.WithoutCancel(pending[0].ctx)
	}
	syncCtx, cancel := context.WithTimeout(syncCtx, mailboxCodeBatchSyncTimeout)
	defer cancel()
	syncErr := s.syncMailboxesForCodeWaiters(syncCtx, ownerID, pending)
	for _, waiter := range pending {
		if waiterCanceled(waiter) {
			continue
		}
		msg, code, ok := s.latestMailboxCodeForWaiter(waiter)
		deliverMailboxCodeResult(waiter, mailboxCodeResult{message: msg, code: code, ok: ok, syncErr: syncErr})
	}
}

func activeMailboxCodeWaiters(waiters []*mailboxCodeWaiter) []*mailboxCodeWaiter {
	active := make([]*mailboxCodeWaiter, 0, len(waiters))
	for _, waiter := range waiters {
		if waiter == nil || waiterCanceled(waiter) {
			continue
		}
		active = append(active, waiter)
	}
	return active
}

func waiterCanceled(waiter *mailboxCodeWaiter) bool {
	if waiter == nil || waiter.ctx == nil {
		return false
	}
	select {
	case <-waiter.ctx.Done():
		return true
	default:
		return false
	}
}

func deliverMailboxCodeResult(waiter *mailboxCodeWaiter, result mailboxCodeResult) {
	select {
	case waiter.result <- result:
	default:
	}
}

func (s *Server) latestMailboxCodeForWaiter(waiter *mailboxCodeWaiter) (Message, string, bool) {
	return latestMailboxCodeSkipping(s.store.MessagesForMailbox(waiter.mailboxID), waiter.after, waiter.keyword, time.Now(), waiter.skipMessageID)
}

func (s *Server) syncMailboxesForCodeWaiters(ctx context.Context, ownerID string, waiters []*mailboxCodeWaiter) error {
	type keywordGroup struct {
		keyword  string
		byID     map[string]Mailbox
		minAfter time.Time
	}
	groups := make(map[string]*keywordGroup)
	for _, waiter := range waiters {
		if waiterCanceled(waiter) {
			continue
		}
		mailbox, ok := s.store.FindMailboxByID(waiter.mailboxID)
		if !ok || !mailbox.APIActive || mailbox.Status == StatusDisabled || !mailbox.ICloudActive {
			continue
		}
		keyword := strings.TrimSpace(waiter.keyword)
		if keyword == "" {
			keyword = "OpenAI"
		}
		groupKey := strings.ToLower(keyword)
		group := groups[groupKey]
		if group == nil {
			group = &keywordGroup{keyword: keyword, byID: make(map[string]Mailbox)}
			groups[groupKey] = group
		}
		if group.minAfter.IsZero() || waiter.after.Before(group.minAfter) {
			group.minAfter = waiter.after
		}
		group.byID[waiter.mailboxID] = mailbox
	}
	if len(groups) == 0 {
		return nil
	}
	var firstErr error
	for _, group := range groups {
		mailboxes := make([]Mailbox, 0, len(group.byID))
		for _, mailbox := range group.byID {
			mailboxes = append(mailboxes, mailbox)
		}
		sort.Slice(mailboxes, func(i, j int) bool {
			return mailboxes[i].Email < mailboxes[j].Email
		})
		_, err := s.syncMailboxCodeBatchForOwnerWithLimit(ctx, ownerID, mailboxes, group.minAfter, group.keyword, 0)
		if err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func (s *Server) runMailWatcher(ctx context.Context) {
	defer func() {
		s.mailWatcherMu.Lock()
		s.mailWatcherCancel = nil
		s.mailWatcherMu.Unlock()
	}()

	idleWorkers := make(map[string]mailboxWatcherIdleWorker)
	stopIdleWorkers := func() {
		for key, worker := range idleWorkers {
			worker.cancel()
			delete(idleWorkers, key)
		}
	}
	defer stopIdleWorkers()
	s.ensureMailWatcherIdleWorkers(ctx, idleWorkers)
	s.syncMailWatcherRound(ctx, false)
	interval := s.mailWatcherInterval
	if interval <= 0 {
		interval = mailWatcherPollInterval
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-s.mailWatcherWake:
			s.ensureMailWatcherIdleWorkers(ctx, idleWorkers)
			s.syncMailWatcherRound(ctx, false)
		case <-ticker.C:
			s.ensureMailWatcherIdleWorkers(ctx, idleWorkers)
			s.syncMailWatcherRound(ctx, false)
		}
	}
}

func (s *Server) runAppleAccountKeepAlive(ctx context.Context) {
	defer func() {
		s.appleAccountKeepAliveMu.Lock()
		s.appleAccountKeepAliveCancel = nil
		s.appleAccountKeepAliveMu.Unlock()
	}()

	s.keepAliveAppleAccountRound(ctx)
	interval := s.appleAccountKeepAliveInterval
	if interval <= 0 {
		interval = appleAccountKeepAliveDefaultInterval
	}
	ticker := time.NewTicker(appleAccountKeepAliveScanInterval(interval))
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.keepAliveAppleAccountRound(ctx)
		}
	}
}

func appleAccountKeepAliveScanInterval(base time.Duration) time.Duration {
	if base <= 0 {
		base = appleAccountKeepAliveDefaultInterval
	}
	interval := base / 4
	if interval > 30*time.Second {
		return 30 * time.Second
	}
	if interval < 5*time.Second {
		return 5 * time.Second
	}
	return interval
}

func (s *Server) keepAliveAppleAccountRound(ctx context.Context) {
	if ctx.Err() != nil {
		return
	}
	keepAliveFn := s.keepAliveAppleAccountState
	if keepAliveFn == nil {
		keepAliveFn = func(ctx context.Context, state LoginState) (LoginState, error) {
			return NewICloudClient().keepAliveAppleAccountManageStateUnlocked(ctx, state)
		}
	}
	baseInterval := s.appleAccountKeepAliveInterval
	if baseInterval <= 0 {
		baseInterval = appleAccountKeepAliveDefaultInterval
	}
	now := time.Now()
	for _, session := range s.appleAccountKeepAliveSessions() {
		if ctx.Err() != nil {
			return
		}
		state, ok := appleAccountLoginState(session)
		if !ok || strings.TrimSpace(state.APIKey) == "" {
			continue
		}
		interval := appleAccountKeepAliveIntervalForSession(session, baseInterval)
		if !appleAccountKeepAliveDue(state, now, interval) {
			continue
		}
		callCtx, cancel := context.WithTimeout(ctx, appleAccountKeepAliveTimeout)
		release, gateErr := acquireAppleAccountOperationGate(callCtx, appleAccountOperationKey(session, state))
		if gateErr != nil {
			cancel()
			if s.logger != nil {
				s.logger.Warn("apple account keepalive gate failed", "owner", s.ownerName(session.OwnerID), "account_id", session.AccountID, "apple_id", session.AppleID, "err", gateErr)
			}
			continue
		}
		next, err := keepAliveFn(callCtx, state)
		release()
		cancel()
		if err != nil {
			if isCodedError(err, "apple_account_auth_failed") {
				state.LastCheckedAt = time.Now()
				state.LastCheckOK = false
				state.LastStatusMessage = "新接口登录态异常：" + err.Error()
				session = withAppleAccountLoginState(session, state)
				if saveErr := s.store.SaveICloudSessionForOwner(session.OwnerID, session); saveErr != nil && s.logger != nil {
					s.logger.Warn("apple account keepalive save failed", "owner", s.ownerName(session.OwnerID), "account_id", session.AccountID, "err", saveErr)
				}
			}
			if s.logger != nil {
				s.logger.Warn("apple account keepalive failed", "owner", s.ownerName(session.OwnerID), "account_id", session.AccountID, "apple_id", session.AppleID, "err", err)
			}
			continue
		}
		session = withAppleAccountLoginState(session, next)
		if err := s.store.SaveICloudSessionForOwner(session.OwnerID, session); err != nil {
			if s.logger != nil {
				s.logger.Warn("apple account keepalive save failed", "owner", s.ownerName(session.OwnerID), "account_id", session.AccountID, "err", err)
			}
			continue
		}
		if s.logger != nil {
			s.logger.Info("apple account keepalive ok", "owner", s.ownerName(session.OwnerID), "account_id", session.AccountID, "apple_id", session.AppleID)
		}
	}
}

func (s *Server) appleAccountKeepAliveSessions() []ICloudSession {
	state := s.store.Snapshot()
	out := make([]ICloudSession, 0, len(state.ICloudSessions)+1)
	if state.ICloudSession != nil && appleAccountKeepAliveEligible(*state.ICloudSession) {
		out = append(out, cloneICloudSession(*state.ICloudSession))
	}
	for _, session := range state.ICloudSessions {
		if !appleAccountKeepAliveEligible(session) {
			continue
		}
		out = append(out, cloneICloudSession(session))
	}
	return out
}

func appleAccountKeepAliveEligible(session ICloudSession) bool {
	state, ok := appleAccountLoginState(session)
	if !ok || strings.TrimSpace(state.APIKey) == "" {
		return false
	}
	if !state.LastCheckedAt.IsZero() && !state.LastCheckOK {
		return false
	}
	return true
}

func appleAccountKeepAliveIntervalForSession(session ICloudSession, base time.Duration) time.Duration {
	if base <= 0 {
		base = appleAccountKeepAliveDefaultInterval
	}
	jitter := base / 5
	if jitter > time.Minute {
		jitter = time.Minute
	}
	if jitter < time.Second {
		return base
	}
	steps := int64(jitter / time.Second)
	if steps <= 0 {
		return base
	}
	h := fnv.New32a()
	_, _ = io.WriteString(h, strings.TrimSpace(session.OwnerID)+"|"+strings.TrimSpace(session.AccountID)+"|"+strings.ToLower(strings.TrimSpace(session.AppleID)))
	offsetSteps := int64(h.Sum32()%uint32(steps*2+1)) - steps
	next := base + time.Duration(offsetSteps)*time.Second
	if base >= time.Minute && next < 30*time.Second {
		return 30 * time.Second
	}
	return next
}

func (s *Server) ensureMailWatcherIdleWorkers(ctx context.Context, workers map[string]mailboxWatcherIdleWorker) {
	groups := s.mailWatcherIMAPGroups()
	seen := make(map[string]struct{}, len(groups))
	for _, group := range groups {
		seen[group.key] = struct{}{}
		if worker, ok := workers[group.key]; ok && worker.signature == group.signature {
			continue
		}
		if worker, ok := workers[group.key]; ok {
			worker.cancel()
		}
		if err := s.ensureMailWatcherIMAPBaseline(ctx, group); err != nil {
			if s.logger != nil {
				s.logger.Warn("mail watcher imap baseline failed", "owner", s.ownerName(group.ownerID), "mailboxes", len(group.mailboxes), "err", err)
			}
			continue
		}
		workerCtx, cancel := context.WithCancel(ctx)
		workers[group.key] = mailboxWatcherIdleWorker{cancel: cancel, signature: group.signature}
		go s.runMailWatcherIdleWorker(workerCtx, group)
	}
	for key, worker := range workers {
		if _, ok := seen[key]; ok {
			continue
		}
		worker.cancel()
		delete(workers, key)
	}
}

func (s *Server) ensureMailWatcherIMAPBaseline(ctx context.Context, group mailboxWatcherIMAPGroup) error {
	if imapUIDNumber(group.state.IMAPLastSyncUID) > 0 {
		return nil
	}
	latestFn := s.latestIMAPUID
	if latestFn == nil {
		latestFn = LatestICloudIMAPUID
	}
	uid, err := latestFn(ctx, group.state)
	if err != nil {
		return err
	}
	if strings.TrimSpace(uid) == "" {
		return nil
	}
	accountID := ""
	resolver := s.imapSessionResolverForOwner(group.ownerID)
	for _, mailbox := range group.mailboxes {
		session, state, ok := resolver.sessionForMailbox(mailbox)
		if !ok || imapStateKey(state) != imapStateKey(group.state) {
			continue
		}
		accountID = strings.TrimSpace(session.AccountID)
		break
	}
	if _, err := s.store.SetICloudIMAPSyncCursor(group.ownerID, accountID, imapStateKey(group.state), time.Now(), uid); err != nil {
		return err
	}
	return nil
}

func (s *Server) runMailWatcherIdleWorker(ctx context.Context, group mailboxWatcherIMAPGroup) {
	backoff := time.Second
	for ctx.Err() == nil {
		err := WatchICloudIMAPExists(ctx, group.state, func() {
			if ctx.Err() != nil {
				return
			}
			syncCtx, cancel := context.WithTimeout(ctx, mailWatcherSyncTimeout)
			_, syncErr := s.syncMailboxCodeBatchForOwnerWithLimit(syncCtx, group.ownerID, group.mailboxes, time.Time{}, "ChatGPT", s.mailWatcherFetchLimit)
			cancel()
			if syncErr != nil && ctx.Err() == nil && s.logger != nil {
				s.logger.Warn("mail watcher idle sync failed", "owner", s.ownerName(group.ownerID), "mailboxes", len(group.mailboxes), "err", syncErr)
			}
		})
		if ctx.Err() != nil {
			return
		}
		if err != nil && s.logger != nil {
			s.logger.Warn("mail watcher idle disconnected", "owner", s.ownerName(group.ownerID), "mailboxes", len(group.mailboxes), "err", err)
		}
		timer := time.NewTimer(backoff)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
		if backoff < 15*time.Second {
			backoff *= 2
		}
	}
}

func (s *Server) syncMailWatcherRound(ctx context.Context, initial bool) {
	if ctx.Err() != nil {
		return
	}
	groups := s.mailWatcherGroups()
	if len(groups) == 0 {
		return
	}
	after := time.Time{}
	fetchLimit := s.mailWatcherFetchLimit
	if initial {
		fetchLimit = s.mailWatcherInitialFetchLimit
		if s.mailWatcherLookback > 0 {
			after = time.Now().Add(-s.mailWatcherLookback)
		}
	}
	if fetchLimit <= 0 {
		fetchLimit = defaultMailWatcherFetchLimit
	}
	for _, group := range groups {
		if ctx.Err() != nil {
			return
		}
		syncCtx, cancel := context.WithTimeout(ctx, mailWatcherSyncTimeout)
		_, err := s.syncMailboxCodeBatchForOwnerWithLimit(syncCtx, group.ownerID, group.mailboxes, after, "OpenAI", fetchLimit)
		cancel()
		if err != nil && ctx.Err() == nil && s.logger != nil {
			s.logger.Warn("mail watcher sync failed", "owner", s.ownerName(group.ownerID), "mailboxes", len(group.mailboxes), "initial", initial, "err", err)
		}
	}
}

func (s *Server) markMailWatcherActive(mailboxID string) {
	mailboxID = strings.TrimSpace(mailboxID)
	if mailboxID == "" {
		return
	}
	ttl := mailWatcherActiveTTL
	if ttl <= 0 {
		ttl = 20 * time.Minute
	}
	s.mailWatcherMu.Lock()
	if s.mailWatcherActiveUntil == nil {
		s.mailWatcherActiveUntil = make(map[string]time.Time)
	}
	s.mailWatcherActiveUntil[mailboxID] = time.Now().Add(ttl)
	s.mailWatcherMu.Unlock()
	s.pokeMailWatcher()
}

func (s *Server) pokeMailWatcher() {
	if s == nil || !s.mailWatcherEnabled || s.mailWatcherWake == nil {
		return
	}
	select {
	case s.mailWatcherWake <- struct{}{}:
	default:
	}
}

func (s *Server) activeMailWatcherMailboxIDs(now time.Time) map[string]struct{} {
	if now.IsZero() {
		now = time.Now()
	}
	s.mailWatcherMu.Lock()
	defer s.mailWatcherMu.Unlock()
	active := make(map[string]struct{})
	for id, until := range s.mailWatcherActiveUntil {
		if until.After(now) {
			active[id] = struct{}{}
			continue
		}
		delete(s.mailWatcherActiveUntil, id)
	}
	return active
}

func (s *Server) mailWatcherGroups() []mailboxWatcherOwnerGroup {
	state := s.store.Snapshot()
	activeIDs := s.activeMailWatcherMailboxIDs(time.Now())
	byOwner := make(map[string][]Mailbox)
	for _, mailbox := range state.Mailboxes {
		if !mailbox.APIActive || !mailbox.ICloudActive || mailbox.Status == StatusDisabled {
			continue
		}
		ownerID := strings.TrimSpace(mailbox.OwnerID)
		if _, ok := s.imapStateForMailbox(ownerID, mailbox); !ok {
			continue
		}
		byOwner[ownerID] = append(byOwner[ownerID], mailbox)
	}
	owners := make([]string, 0, len(byOwner))
	for ownerID := range byOwner {
		owners = append(owners, ownerID)
	}
	sort.Strings(owners)
	groups := make([]mailboxWatcherOwnerGroup, 0, len(owners))
	for _, ownerID := range owners {
		mailboxes := byOwner[ownerID]
		sort.Slice(mailboxes, func(i, j int) bool {
			_, iActive := activeIDs[mailboxes[i].ID]
			_, jActive := activeIDs[mailboxes[j].ID]
			if iActive != jActive {
				return iActive
			}
			return mailboxes[i].Email < mailboxes[j].Email
		})
		groups = append(groups, mailboxWatcherOwnerGroup{ownerID: ownerID, mailboxes: mailboxes})
	}
	return groups
}

func (s *Server) mailWatcherIMAPGroups() []mailboxWatcherIMAPGroup {
	state := s.store.Snapshot()
	type bucket struct {
		ownerID   string
		state     LoginState
		mailboxes []Mailbox
	}
	buckets := make(map[string]*bucket)
	for _, mailbox := range state.Mailboxes {
		if !mailbox.APIActive || !mailbox.ICloudActive || mailbox.Status == StatusDisabled {
			continue
		}
		ownerID := strings.TrimSpace(mailbox.OwnerID)
		imapState, ok := s.imapStateForMailbox(ownerID, mailbox)
		if !ok {
			continue
		}
		key := ownerID + "|" + imapStateKey(imapState)
		item := buckets[key]
		if item == nil {
			item = &bucket{ownerID: ownerID, state: imapState}
			buckets[key] = item
		}
		item.mailboxes = append(item.mailboxes, mailbox)
	}
	keys := make([]string, 0, len(buckets))
	for key := range buckets {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	groups := make([]mailboxWatcherIMAPGroup, 0, len(keys))
	for _, key := range keys {
		item := buckets[key]
		sort.Slice(item.mailboxes, func(i, j int) bool {
			return item.mailboxes[i].Email < item.mailboxes[j].Email
		})
		groups = append(groups, mailboxWatcherIMAPGroup{
			key:       key,
			ownerID:   item.ownerID,
			state:     item.state,
			mailboxes: item.mailboxes,
			signature: mailWatcherIMAPGroupSignature(item.state, item.mailboxes),
		})
	}
	return groups
}

func mailWatcherIMAPGroupSignature(state LoginState, mailboxes []Mailbox) string {
	parts := []string{
		normalizeICloudIMAPEmail(state.IMAPEmail),
		strings.TrimSpace(state.IMAPUsername),
		state.IMAPHost,
		strconv.Itoa(state.IMAPPort),
	}
	for _, mailbox := range mailboxes {
		parts = append(parts, strings.TrimSpace(mailbox.ID), normalizeICloudIMAPEmail(mailbox.Email))
	}
	return strings.Join(parts, "|")
}

func (s *Server) syncMailbox(ctx context.Context, mailbox Mailbox, after time.Time, keyword string) (int, error) {
	return s.syncMailboxCodeBatchForOwnerWithLimit(ctx, mailbox.OwnerID, []Mailbox{mailbox}, after, keyword, mailboxSyncThreadLimit(mailbox))
}

func (s *Server) syncMailboxCodeBatchForOwnerWithLimit(ctx context.Context, ownerID string, mailboxes []Mailbox, after time.Time, keyword string, maxMessages int) (int, error) {
	if len(mailboxes) == 0 {
		return 0, nil
	}
	release, err := s.acquireMailboxSyncSlot(ctx, ownerID)
	if err != nil {
		return 0, err
	}
	defer release()

	refreshed := make([]Mailbox, 0, len(mailboxes))
	for _, mailbox := range mailboxes {
		latest, ok := s.store.FindMailboxByID(mailbox.ID)
		if !ok || !latest.APIActive || latest.Status == StatusDisabled || !latest.ICloudActive {
			continue
		}
		refreshed = append(refreshed, latest)
	}
	if len(refreshed) == 0 {
		return 0, nil
	}
	if err := s.waitMailboxSyncInterval(ctx, ownerID); err != nil {
		return 0, err
	}
	defer s.markMailboxSyncFinished(ownerID)

	syncFn := s.syncCodeMailboxBatchWithCursor
	if syncFn == nil && s.syncCodeMailboxBatch != nil {
		syncFn = func(ctx context.Context, state LoginState, mailboxes []Mailbox, after time.Time, keyword string, maxMessages int) (iCloudIMAPSyncResult, error) {
			messagesByMailbox, err := s.syncCodeMailboxBatch(ctx, state, mailboxes, after, keyword, maxMessages)
			return iCloudIMAPSyncResult{
				MessagesByMailbox: messagesByMailbox,
				LastUID:           highestICloudMessageUID(messagesByMailbox),
			}, err
		}
	}
	if syncFn == nil {
		syncFn = SyncICloudIMAPMessagesWithCursor
	}
	type imapGroup struct {
		session   ICloudSession
		state     LoginState
		mailboxes []Mailbox
	}
	groups := make(map[string]*imapGroup)
	order := make([]string, 0)
	resolver := s.imapSessionResolverForOwner(ownerID)
	for _, mailbox := range refreshed {
		session, state, ok := resolver.sessionForMailbox(mailbox)
		if !ok {
			return 0, errCode("imap_session_missing", "未保存取码登录，请先保存 iCloud 邮箱账号和 App 专用密码", true)
		}
		key := firstNonEmpty(strings.TrimSpace(session.AccountID), "__imap__") + "|" + imapStateKey(state)
		group := groups[key]
		if group == nil {
			group = &imapGroup{session: session, state: state}
			groups[key] = group
			order = append(order, key)
		}
		group.mailboxes = append(group.mailboxes, mailbox)
	}
	now := time.Now()
	synced := 0
	for _, key := range order {
		group := groups[key]
		syncResult, err := syncFn(ctx, group.state, group.mailboxes, after, keyword, maxMessages)
		if err != nil {
			return synced, err
		}
		messagesByMailbox := syncResult.MessagesByMailbox
		if messagesByMailbox == nil {
			messagesByMailbox = map[string][]ICloudSyncedMessage{}
		}
		lastAccountUID := firstNonEmpty(syncResult.LastUID, highestICloudMessageUID(messagesByMailbox))
		for _, mailbox := range group.mailboxes {
			lastSyncUID := mailbox.LastSyncUID
			latestMessageAt := mailbox.LastSyncAt
			mailboxChanged := false
			for _, msg := range messagesByMailbox[mailbox.ID] {
				if extractOTP(msg.Subject+"\n"+msg.Body) == "" {
					continue
				}
				remoteID := strings.TrimSpace(msg.RemoteID)
				if remoteID == "" && strings.TrimSpace(msg.UID) != "" {
					remoteID = "imap:" + strings.TrimSpace(msg.UID)
				}
				_, created, err := s.store.UpsertMessage(mailbox.ID, remoteID, "imap", msg.Subject, msg.From, msg.Body, msg.ReceivedAt)
				if err != nil {
					return synced, err
				}
				if created {
					synced++
					mailboxChanged = true
				}
				candidateUID := firstNonEmpty(msg.UID, remoteID)
				if msg.ReceivedAt.After(latestMessageAt) {
					latestMessageAt = msg.ReceivedAt
					lastSyncUID = candidateUID
					mailboxChanged = true
				} else if strings.TrimSpace(lastSyncUID) == "" && strings.TrimSpace(candidateUID) != "" {
					lastSyncUID = candidateUID
					mailboxChanged = true
				}
			}
			if mailboxChanged {
				syncedAt := latestMessageAt
				if syncedAt.IsZero() {
					syncedAt = now
				}
				if _, err := s.store.SetMailboxSyncCursor(mailbox.ID, syncedAt, lastSyncUID); err != nil {
					return synced, err
				}
			}
		}
		if _, err := s.store.SetICloudIMAPSyncCursor(ownerID, group.session.AccountID, imapStateKey(group.state), now, lastAccountUID); err != nil {
			return synced, err
		}
	}
	return synced, nil
}

func (s *Server) syncMailboxBatchForOwner(ctx context.Context, ownerID string, mailboxes []Mailbox, after time.Time, keyword string) error {
	return s.syncMailboxBatchForOwnerWithLimit(ctx, ownerID, mailboxes, after, keyword, 0)
}

func (s *Server) syncMailboxBatchForOwnerWithLimit(ctx context.Context, ownerID string, mailboxes []Mailbox, after time.Time, keyword string, maxThreadsOverride int) error {
	if len(mailboxes) == 0 {
		return nil
	}
	release, err := s.acquireMailboxSyncSlot(ctx, ownerID)
	if err != nil {
		return err
	}
	defer release()

	refreshed := make([]Mailbox, 0, len(mailboxes))
	for _, mailbox := range mailboxes {
		latest, ok := s.store.FindMailboxByID(mailbox.ID)
		if !ok {
			continue
		}
		refreshed = append(refreshed, latest)
	}
	if len(refreshed) == 0 {
		return nil
	}
	if err := s.waitMailboxSyncInterval(ctx, ownerID); err != nil {
		return err
	}
	defer s.markMailboxSyncFinished(ownerID)
	syncFn := s.syncMailboxBatch
	if syncFn == nil {
		syncFn = func(ctx context.Context, session ICloudSession, mailboxes []Mailbox, after time.Time, keyword string, maxThreads int) (map[string][]ICloudSyncedMessage, error) {
			return NewICloudClient().SyncMailboxMessagesBatch(ctx, session, mailboxes, after, keyword, maxThreads)
		}
	}
	type sessionGroup struct {
		session   ICloudSession
		mailboxes []Mailbox
	}
	groups := make(map[string]*sessionGroup)
	order := make([]string, 0)
	for _, mailbox := range refreshed {
		session, ok := s.sessionForMailbox(ownerID, mailbox.AccountID)
		if !ok {
			return errCode("icloud_session_missing", "未保存 iCloud 登录态，请先保存旧接口登录态", true)
		}
		key := firstNonEmpty(session.AccountID, session.DSID, session.AppleID, "__legacy__")
		group := groups[key]
		if group == nil {
			group = &sessionGroup{session: session}
			groups[key] = group
			order = append(order, key)
		}
		group.mailboxes = append(group.mailboxes, mailbox)
	}
	now := time.Now()
	for _, key := range order {
		group := groups[key]
		maxThreads := mailboxBatchThreadLimit(group.mailboxes)
		if maxThreadsOverride > 0 {
			maxThreads = maxThreadsOverride
		}
		messagesByMailbox, err := syncFn(ctx, group.session, group.mailboxes, after, keyword, maxThreads)
		if err != nil {
			return err
		}
		for _, mailbox := range group.mailboxes {
			lastSyncUID := mailbox.LastSyncUID
			latestMessageAt := mailbox.LastSyncAt
			for _, msg := range messagesByMailbox[mailbox.ID] {
				if extractOTP(msg.Subject+"\n"+msg.Body) == "" {
					continue
				}
				_, _, err := s.store.UpsertMessage(mailbox.ID, msg.RemoteID, "icloud", msg.Subject, msg.From, msg.Body, msg.ReceivedAt)
				if err != nil {
					return err
				}
				if msg.ReceivedAt.After(latestMessageAt) {
					latestMessageAt = msg.ReceivedAt
					lastSyncUID = firstNonEmpty(msg.UID, msg.RemoteID)
				}
			}
			if _, err := s.store.SetMailboxSyncCursor(mailbox.ID, now, lastSyncUID); err != nil {
				return err
			}
		}
	}
	return nil
}

func highestICloudMessageUID(messagesByMailbox map[string][]ICloudSyncedMessage) string {
	highest := 0
	for _, messages := range messagesByMailbox {
		for _, msg := range messages {
			uid := imapUIDNumber(firstNonEmpty(msg.UID, msg.RemoteID))
			if uid > highest {
				highest = uid
			}
		}
	}
	if highest <= 0 {
		return ""
	}
	return strconv.Itoa(highest)
}

func icloudRemoteIDsFromMessages(messages []Message) []string {
	ids := make([]string, 0, len(messages))
	for _, msg := range messages {
		if extractOTP(msg.Subject+"\n"+msg.Body) == "" {
			continue
		}
		if strings.HasPrefix(strings.TrimSpace(msg.RemoteID), "icloud:") {
			ids = append(ids, msg.RemoteID)
		}
	}
	return uniqueStrings(ids)
}

func (s *Server) acquireMailboxSyncSlot(ctx context.Context, ownerID string) (func(), error) {
	key := mailboxSyncOwnerKey(ownerID)
	s.icloudMailSyncMu.Lock()
	if s.icloudMailSyncGates == nil {
		s.icloudMailSyncGates = make(map[string]chan struct{})
	}
	gate := s.icloudMailSyncGates[key]
	if gate == nil {
		gate = make(chan struct{}, 1)
		s.icloudMailSyncGates[key] = gate
	}
	s.icloudMailSyncMu.Unlock()

	select {
	case gate <- struct{}{}:
		return func() { <-gate }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (s *Server) waitMailboxSyncInterval(ctx context.Context, ownerID string) error {
	interval := s.mailboxSyncMinInterval
	if interval <= 0 {
		return nil
	}
	key := mailboxSyncOwnerKey(ownerID)
	s.icloudMailSyncMu.Lock()
	last := s.icloudMailSyncLast[key]
	s.icloudMailSyncMu.Unlock()
	if last.IsZero() {
		return nil
	}
	wait := interval - time.Since(last)
	if wait <= 0 {
		return nil
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *Server) markMailboxSyncFinished(ownerID string) {
	key := mailboxSyncOwnerKey(ownerID)
	s.icloudMailSyncMu.Lock()
	if s.icloudMailSyncLast == nil {
		s.icloudMailSyncLast = make(map[string]time.Time)
	}
	s.icloudMailSyncLast[key] = time.Now()
	s.icloudMailSyncMu.Unlock()
}

func mailboxSyncOwnerKey(ownerID string) string {
	key := strings.TrimSpace(ownerID)
	if key == "" {
		return "__legacy__"
	}
	return key
}

func mailboxSyncThreadLimit(mailbox Mailbox) int {
	if mailbox.LastSyncAt.IsZero() {
		return 20
	}
	return 10
}

func mailboxBatchThreadLimit(mailboxes []Mailbox) int {
	limit := 20
	for _, mailbox := range mailboxes {
		if mailbox.LastSyncAt.IsZero() {
			limit = 50
			break
		}
	}
	if len(mailboxes) >= 5 && limit < 50 {
		limit = 50
	}
	return limit
}

func (s *Server) createICloudMailboxForOwner(ctx context.Context, ownerID, accountID, label, note string) (Mailbox, ICloudRemoteMailbox, error) {
	session, ok := s.sessionForOwnerAccount(ownerID, accountID)
	if !ok {
		return Mailbox{}, ICloudRemoteMailbox{}, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先保存登录态", true)
	}
	accountID = firstNonEmpty(strings.TrimSpace(accountID), session.AccountID)
	remote, err := s.createICloudMailboxRemoteWithChannel(ctx, ownerID, session, label, note, mailboxCreateChannelFromContext(ctx))
	if err != nil {
		return Mailbox{}, ICloudRemoteMailbox{}, err
	}
	storeNote := strings.TrimSpace(remote.Note)
	if storeNote == "" {
		storeNote = "created by iCloud protocol"
	}
	mailbox, err := s.store.AddMailboxForOwner(ownerID, accountID, remote.Label, remote.Email)
	if err != nil {
		return Mailbox{}, remote, err
	}
	if storeNote != "" {
		updated, updateErr := s.store.SetMailboxStatus(mailbox.ID, nil, nil, StatusAvailable, storeNote)
		if updateErr == nil {
			mailbox = updated
		}
	}
	return mailbox, remote, nil
}

func (s *Server) createMailboxesForOwner(ctx context.Context, ownerID string, accountIDs []string, label, note string) ([]Mailbox, []ICloudRemoteMailbox, []createMailboxFailure, error) {
	accountIDs = normalizeAccountIDSelection("", accountIDs)
	requests := make([]mailboxCreateRequest, 0, len(accountIDs))
	for _, accountID := range accountIDs {
		requests = append(requests, mailboxCreateRequest{AccountID: accountID})
	}
	return s.createMailboxesForOwnerWithChannels(ctx, ownerID, requests, label, note)
}

func (s *Server) createMailboxesForOwnerWithChannels(ctx context.Context, ownerID string, requests []mailboxCreateRequest, label, note string) ([]Mailbox, []ICloudRemoteMailbox, []createMailboxFailure, error) {
	accountIDs, channels := normalizeMailboxCreateRequests(requests)
	sessions := s.sessionsForOwnerAccounts(ownerID, accountIDs)
	if len(sessions) == 0 {
		return nil, nil, nil, errCode("icloud_session_missing", "未找到可用于创建的 iCloud 登录态，请检查参与账号 ID 或先保存登录态", true)
	}
	mailboxes := make([]Mailbox, 0, len(sessions))
	remotes := make([]ICloudRemoteMailbox, 0, len(sessions))
	failures := make([]createMailboxFailure, 0)
	var firstErr error
	type createResult struct {
		session   ICloudSession
		mailbox   Mailbox
		remote    ICloudRemoteMailbox
		err       error
		accountID string
		channel   mailboxCreateChannel
	}
	results := make([]createResult, len(sessions))
	var wg sync.WaitGroup
	for index, session := range sessions {
		index, session := index, session
		wg.Add(1)
		go func() {
			defer wg.Done()
			effectiveAccountID := session.AccountID
			channel := channels[strings.TrimSpace(effectiveAccountID)]
			createCtx := contextWithMailboxCreateChannel(ctx, channel)
			mailbox, remote, err := s.createMailboxForOwner(createCtx, ownerID, effectiveAccountID, label, note)
			results[index] = createResult{
				session:   session,
				mailbox:   mailbox,
				remote:    remote,
				err:       err,
				accountID: effectiveAccountID,
				channel:   channel,
			}
		}()
	}
	wg.Wait()
	for _, result := range results {
		if result.err != nil {
			if firstErr == nil {
				firstErr = result.err
			}
			var coded codedError
			if !errors.As(result.err, &coded) {
				coded = codedError{}
			}
			failures = append(failures, createMailboxFailure{
				AccountID: result.accountID,
				AppleID:   strings.TrimSpace(result.session.AppleID),
				Channel:   string(result.channel),
				Code:      strings.TrimSpace(coded.code),
				Error:     result.err.Error(),
			})
			continue
		}
		mailboxes = append(mailboxes, result.mailbox)
		remotes = append(remotes, result.remote)
	}
	if len(mailboxes) == 0 && firstErr != nil {
		return mailboxes, remotes, failures, firstErr
	}
	return mailboxes, remotes, failures, nil
}

func (s *Server) createICloudMailboxRemote(ctx context.Context, ownerID string, session ICloudSession, label, note string) (ICloudRemoteMailbox, error) {
	return s.createICloudMailboxRemoteWithChannel(ctx, ownerID, session, label, note, mailboxCreateChannelAuto)
}

func (s *Server) createICloudMailboxRemoteWithChannel(ctx context.Context, ownerID string, session ICloudSession, label, note string, channel mailboxCreateChannel) (ICloudRemoteMailbox, error) {
	key := mailboxCreateAccountKey(ownerID, session)

	release, err := s.acquireMailboxCreateGate(ctx, key)
	if err != nil {
		return ICloudRemoteMailbox{}, err
	}
	defer release()
	if err := s.waitMailboxCreateInterval(ctx, key); err != nil {
		return ICloudRemoteMailbox{}, err
	}

	switch normalizeMailboxCreateChannel(channel) {
	case mailboxCreateChannelAppleAccount:
		return s.createICloudMailboxRemoteAppleAccount(ctx, ownerID, session, label, note, key)
	case mailboxCreateChannelICloudWeb:
		return s.createICloudMailboxRemoteICloudWeb(ctx, session, label, note, key)
	}

	var appleAccountErr error
	if _, ok := appleAccountLoginState(session); ok {
		remote, err := s.createICloudMailboxRemoteAppleAccount(ctx, ownerID, session, label, note, key)
		if err == nil {
			return remote, nil
		}
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return ICloudRemoteMailbox{}, err
		}
		appleAccountErr = err
		s.logger.Warn("Apple Account mailbox create failed; falling back to iCloud HME", "account_id", session.AccountID, "err", err)
	}
	remote, err := s.createICloudMailboxRemoteICloudWeb(ctx, session, label, note, key)
	if err != nil && appleAccountErr != nil {
		return ICloudRemoteMailbox{}, errCode("mailbox_create_all_channels_failed", "新接口失败："+appleAccountErr.Error()+"；旧接口失败："+err.Error(), true)
	}
	return remote, err
}

func mailboxCreateAccountKey(ownerID string, session ICloudSession) string {
	key := strings.TrimSpace(ownerID)
	if key == "" {
		key = "global"
	}
	key += ":" + firstNonEmpty(session.AccountID, session.DSID, session.AppleID, "default")
	return key
}

func mailboxCreateChannelCooldownKey(accountKey string, channel mailboxCreateChannel) string {
	key := strings.TrimSpace(accountKey)
	if key == "" {
		key = "global:default"
	}
	channel = normalizeMailboxCreateChannel(channel)
	if channel == mailboxCreateChannelAuto {
		channel = mailboxCreateChannelICloudWeb
	}
	return key + ":cooldown:" + string(channel)
}

func (s *Server) createICloudMailboxRemoteAppleAccount(ctx context.Context, ownerID string, session ICloudSession, label, note, key string) (ICloudRemoteMailbox, error) {
	if _, ok := appleAccountLoginState(session); !ok {
		return ICloudRemoteMailbox{}, errCode("apple_account_session_missing", "未保存新接口登录态，请先完成新接口登录", true)
	}
	cooldownKey := mailboxCreateChannelCooldownKey(key, mailboxCreateChannelAppleAccount)
	remote, updatedSession, err := NewICloudClient().CreatePrivacyMailboxWithAppleAccount(ctx, session, s.cfg.AppleAccountAPIKey, label, note)
	s.markMailboxCreateFinished(key)
	if isCodedError(err, "apple_account_hme_limit") {
		s.markMailboxCreateCooldown(cooldownKey, mailboxCreateLimitCooldown)
	}
	if _, ok := appleAccountLoginState(updatedSession); ok {
		if saveErr := s.store.SaveICloudSessionForOwner(ownerID, updatedSession); saveErr != nil {
			s.logger.Warn("failed to save updated Apple Account login state", "account_id", session.AccountID, "err", saveErr)
		}
	}
	return remote, err
}

func (s *Server) createICloudMailboxRemoteICloudWeb(ctx context.Context, session ICloudSession, label, note, key string) (ICloudRemoteMailbox, error) {
	if !iCloudWebLoginSaved(session) {
		return ICloudRemoteMailbox{}, errCode("icloud_session_missing", "未保存旧接口登录态，请先完成旧接口登录", true)
	}
	cooldownKey := mailboxCreateChannelCooldownKey(key, mailboxCreateChannelICloudWeb)
	if cooldownRemaining := s.mailboxCreateCooldownRemaining(cooldownKey); cooldownRemaining > 0 {
		remaining := int(cooldownRemaining.Round(time.Second).Seconds())
		if remaining < 1 {
			remaining = 1
		}
		return ICloudRemoteMailbox{}, errCode("icloud_hme_limit", fmt.Sprintf("iCloud 创建上限冷却中，请约 %d 秒后再试", remaining), true)
	}

	remote, err := NewICloudClient().CreatePrivacyMailbox(ctx, session, label, note)
	s.markMailboxCreateFinished(key)
	var coded codedError
	if errors.As(err, &coded) && coded.code == "icloud_hme_limit" {
		s.markMailboxCreateCooldown(cooldownKey, mailboxCreateLimitCooldown)
	}
	return remote, err
}

func normalizeMailboxCreateRequests(requests []mailboxCreateRequest) ([]string, map[string]mailboxCreateChannel) {
	accountIDs := make([]string, 0, len(requests))
	channels := make(map[string]mailboxCreateChannel, len(requests))
	for _, request := range requests {
		for _, accountID := range splitAccountIDTokens(request.AccountID) {
			if accountID == "" {
				continue
			}
			if _, ok := channels[accountID]; ok {
				continue
			}
			channels[accountID] = normalizeMailboxCreateChannel(request.Channel)
			accountIDs = append(accountIDs, accountID)
		}
	}
	return accountIDs, channels
}

func (s *Server) acquireMailboxCreateGate(ctx context.Context, key string) (func(), error) {
	s.icloudCreateMu.Lock()
	gate := s.icloudCreateGates[key]
	if gate == nil {
		gate = make(chan struct{}, 1)
		s.icloudCreateGates[key] = gate
	}
	s.icloudCreateMu.Unlock()

	select {
	case gate <- struct{}{}:
		return func() { <-gate }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (s *Server) waitMailboxCreateInterval(ctx context.Context, key string) error {
	interval := mailboxCreateMinInterval
	if interval <= 0 {
		return nil
	}
	s.icloudCreateMu.Lock()
	last := s.icloudCreateLast[key]
	s.icloudCreateMu.Unlock()
	if last.IsZero() {
		return nil
	}
	wait := interval - time.Since(last)
	if wait <= 0 {
		return nil
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *Server) markMailboxCreateFinished(key string) {
	s.icloudCreateMu.Lock()
	if s.icloudCreateLast == nil {
		s.icloudCreateLast = make(map[string]time.Time)
	}
	s.icloudCreateLast[key] = time.Now()
	s.icloudCreateMu.Unlock()
}

func (s *Server) mailboxCreateCooldownRemaining(key string) time.Duration {
	now := time.Now()
	s.icloudCreateMu.Lock()
	defer s.icloudCreateMu.Unlock()
	cooldownUntil := s.icloudCreateCooldown[key]
	if cooldownUntil.IsZero() {
		return 0
	}
	if !cooldownUntil.After(now) {
		delete(s.icloudCreateCooldown, key)
		return 0
	}
	return cooldownUntil.Sub(now)
}

func (s *Server) markMailboxCreateCooldown(key string, duration time.Duration) {
	if duration <= 0 {
		return
	}
	s.icloudCreateMu.Lock()
	if s.icloudCreateCooldown == nil {
		s.icloudCreateCooldown = make(map[string]time.Time)
	}
	s.icloudCreateCooldown[key] = time.Now().Add(duration)
	s.icloudCreateMu.Unlock()
}

func (s *Server) logICloudCreateError(ownerID string, err error) {
	var coded codedError
	if errors.As(err, &coded) {
		s.logger.Warn("iCloud mailbox create failed", "owner", s.ownerName(ownerID), "code", coded.code, "retryable", coded.retryable)
		return
	}
	s.logger.Warn("iCloud mailbox create failed", "owner", s.ownerName(ownerID), "err", err)
}

func (s *Server) authorized(r *http.Request, mailbox Mailbox) bool {
	queryKey := strings.TrimSpace(r.URL.Query().Get("key"))
	if constantTimeEqual(queryKey, mailbox.APIToken) {
		return true
	}
	candidates := []string{
		strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "),
		r.Header.Get("X-API-Key"),
	}
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		if constantTimeEqual(candidate, mailbox.APIToken) {
			return true
		}
		if strings.TrimSpace(s.cfg.APIKey) != "" && constantTimeEqual(candidate, s.cfg.APIKey) {
			return true
		}
	}
	return false
}

func (s *Server) authorizedGlobalAPI(r *http.Request) bool {
	want := strings.TrimSpace(s.cfg.APIKey)
	if want == "" {
		return false
	}
	candidates := []string{
		strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "),
		r.Header.Get("X-API-Key"),
	}
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if constantTimeEqual(candidate, want) {
			return true
		}
	}
	return false
}

func scopedOwnerID(r *http.Request, store *FileStore) string {
	if cookie, err := r.Cookie(sessionCookieName); err == nil {
		session, user, ok := store.WebSessionByToken(cookie.Value)
		if ok && !session.IsAdmin && user.ID != "" {
			return user.ID
		}
	}
	return ""
}

func requestOwnerID(r *http.Request, store *FileStore) string {
	if isInternalICloudRequest(r) {
		if adminID := singleActiveAdminID(store.Users()); adminID != "" {
			return adminID
		}
	}
	if cookie, err := r.Cookie(sessionCookieName); err == nil {
		_, user, ok := store.WebSessionByToken(cookie.Value)
		if ok && user.ID != "" {
			return user.ID
		}
	}
	return ""
}

func (s *Server) scopedState(r *http.Request) State {
	if s.isAdminRequest(r) {
		return s.store.Snapshot()
	}
	if key := scopedOwnerID(r, s.store); key != "" {
		return s.store.SnapshotForOwner(key)
	}
	return s.store.Snapshot()
}

func (s *Server) sessionForRequest(r *http.Request) (ICloudSession, bool) {
	session, _, ok := s.sessionForRequestWithOwner(r)
	return session, ok
}

func (s *Server) sessionForOwner(ownerID string) (ICloudSession, bool) {
	if ownerID = strings.TrimSpace(ownerID); ownerID != "" {
		return s.store.ICloudSessionForOwner(ownerID)
	}
	return s.store.ICloudSession()
}

func (s *Server) sessionForOwnerAccount(ownerID, accountID string) (ICloudSession, bool) {
	if ownerID = strings.TrimSpace(ownerID); ownerID != "" {
		if session, ok := s.store.ICloudSessionForOwnerAccount(ownerID, accountID); ok {
			return session, true
		}
		if user, ok := s.store.UserByID(ownerID); ok && user.IsAdmin {
			return s.store.ICloudSessionForOwnerAccount("", accountID)
		}
		return ICloudSession{}, false
	}
	return s.store.ICloudSessionForOwnerAccount("", accountID)
}

func (s *Server) sessionsForOwner(ownerID, accountID string) []ICloudSession {
	accountID = strings.TrimSpace(accountID)
	if accountID != "" {
		if session, ok := s.sessionForOwnerAccount(ownerID, accountID); ok {
			return []ICloudSession{session}
		}
		return nil
	}
	if ownerID = strings.TrimSpace(ownerID); ownerID != "" {
		sessions := s.store.ICloudSessionsForOwner(ownerID)
		if len(sessions) > 0 {
			return sessions
		}
		if user, ok := s.store.UserByID(ownerID); ok && user.IsAdmin {
			return s.store.ICloudSessionsForOwner("")
		}
		return nil
	}
	return s.store.ICloudSessionsForOwner("")
}

func (s *Server) sessionsForOwnerAccounts(ownerID string, accountIDs []string) []ICloudSession {
	accountIDs = normalizeAccountIDSelection("", accountIDs)
	if len(accountIDs) == 0 {
		return s.sessionsForOwner(ownerID, "")
	}
	sessions := make([]ICloudSession, 0, len(accountIDs))
	for _, accountID := range accountIDs {
		if session, ok := s.sessionForOwnerAccount(ownerID, accountID); ok {
			sessions = append(sessions, session)
		}
	}
	return sessions
}

func (s *Server) sessionForMailbox(ownerID, accountID string) (ICloudSession, bool) {
	if session, ok := s.sessionForOwnerAccount(ownerID, accountID); ok {
		return session, true
	}
	sessions := s.sessionsForOwner(ownerID, "")
	if len(sessions) == 1 {
		return sessions[0], true
	}
	return ICloudSession{}, false
}

func (s *Server) imapSessionForMailbox(ownerID string, mailbox Mailbox) (ICloudSession, LoginState, bool) {
	if session, ok := s.sessionForOwnerAccount(ownerID, mailbox.AccountID); ok {
		if state, ok := iCloudIMAPLoginState(session); ok {
			return session, state, true
		}
	}
	type match struct {
		session ICloudSession
		state   LoginState
	}
	var found []match
	for _, session := range s.sessionsForOwner(ownerID, "") {
		if state, ok := iCloudIMAPLoginState(session); ok {
			found = append(found, match{session: session, state: state})
		}
	}
	if len(found) == 1 {
		return found[0].session, found[0].state, true
	}
	return ICloudSession{}, LoginState{}, false
}

func (s *Server) imapStateForMailbox(ownerID string, mailbox Mailbox) (LoginState, bool) {
	_, state, ok := s.imapSessionForMailbox(ownerID, mailbox)
	return state, ok
}

type imapSessionResolver struct {
	byAccount map[string]imapSessionMatch
	single    imapSessionMatch
	hasSingle bool
}

type imapSessionMatch struct {
	session ICloudSession
	state   LoginState
}

func (s *Server) imapSessionResolverForOwner(ownerID string) imapSessionResolver {
	sessions := s.sessionsForOwner(ownerID, "")
	resolver := imapSessionResolver{byAccount: make(map[string]imapSessionMatch, len(sessions))}
	matches := make([]imapSessionMatch, 0, len(sessions))
	for _, session := range sessions {
		state, ok := iCloudIMAPLoginState(session)
		if !ok {
			continue
		}
		match := imapSessionMatch{session: session, state: state}
		if accountID := strings.TrimSpace(session.AccountID); accountID != "" {
			resolver.byAccount[accountID] = match
		}
		matches = append(matches, match)
	}
	if len(matches) == 1 {
		resolver.single = matches[0]
		resolver.hasSingle = true
	}
	return resolver
}

func (r imapSessionResolver) sessionForMailbox(mailbox Mailbox) (ICloudSession, LoginState, bool) {
	if match, ok := r.byAccount[strings.TrimSpace(mailbox.AccountID)]; ok {
		return match.session, match.state, true
	}
	if r.hasSingle {
		return r.single.session, r.single.state, true
	}
	return ICloudSession{}, LoginState{}, false
}

func imapStateKey(state LoginState) string {
	return strings.ToLower(strings.TrimSpace(firstNonEmpty(state.IMAPEmail, state.IMAPUsername))) + "|" +
		strings.ToLower(strings.TrimSpace(firstNonEmpty(state.IMAPHost, defaultICloudIMAPHost))) + "|" +
		strconv.Itoa(state.IMAPPort)
}

func (s *Server) publicSessionForRequest(r *http.Request) publicICloudSession {
	session, ok := s.sessionForRequest(r)
	if !ok {
		return publicSession(nil)
	}
	return s.publicSession(&session)
}

func (s *Server) publicSessionsForRequest(r *http.Request) []publicICloudSession {
	return s.publicSessionsForOwner(requestOwnerID(r, s.store))
}

func publicCreateSettings(settings CreateSettings) map[string]any {
	settings = normalizeCreateSettings(settings.OwnerID, settings)
	createChannel := normalizeMailboxCreateChannel(mailboxCreateChannel(settings.CreateChannel))
	schedulerChannel := normalizeMailboxCreateChannel(mailboxCreateChannel(settings.SchedulerCreateChannel))
	return map[string]any{
		"label":                            settings.Label,
		"note":                             settings.Note,
		"account_ids":                      append([]string(nil), settings.AccountIDs...),
		"create_channel":                   string(createChannel),
		"create_channel_label":             mailboxCreateChannelLabel(createChannel),
		"scheduler_create_channel":         string(schedulerChannel),
		"scheduler_create_channel_label":   mailboxCreateChannelLabel(schedulerChannel),
		"apple_account_two_factor_method":  normalizeAppleTwoFactorMethod(settings.AppleAccountTwoFactorMethod),
		"icloud_web_two_factor_method":     normalizeAppleTwoFactorMethod(settings.ICloudWebTwoFactorMethod),
		"scheduler_interval_minutes":       settings.SchedulerIntervalMinutes,
		"scheduler_round_interval_seconds": settings.SchedulerRoundIntervalSeconds,
		"mailbox_page_size":                settings.MailboxPageSize,
		"updated_at":                       formatTime(settings.UpdatedAt),
	}
}

func (s *Server) publicSessionsForOwner(ownerID string) []publicICloudSession {
	sessions := s.sessionsForOwner(ownerID, "")
	out := make([]publicICloudSession, 0, len(sessions))
	for _, session := range sessions {
		out = append(out, s.publicSession(&session))
	}
	return out
}

func (s *Server) sessionForRequestWithOwner(r *http.Request) (ICloudSession, string, bool) {
	if ownerID := requestOwnerID(r, s.store); ownerID != "" {
		if session, ok := s.store.ICloudSessionForOwner(ownerID); ok {
			return session, ownerID, true
		}
	}
	if s.isAdminRequest(r) {
		session, ok := s.store.ICloudSession()
		return session, "", ok
	}
	ownerID := scopedOwnerID(r, s.store)
	session, ok := s.store.ICloudSessionForOwner(ownerID)
	return session, ownerID, ok
}

func (s *Server) currentWebSession(r *http.Request) (WebSession, User, bool) {
	cookie, err := r.Cookie(sessionCookieName)
	if err != nil {
		return WebSession{}, User{}, false
	}
	return s.store.WebSessionByToken(cookie.Value)
}

func (s *Server) authorizedUserSession(r *http.Request) bool {
	session, user, ok := s.currentWebSession(r)
	return ok && !session.IsAdmin && user.ID != ""
}

func (s *Server) authorizedAdminSession(r *http.Request) bool {
	session, user, ok := s.currentWebSession(r)
	return ok && (session.IsAdmin || user.IsAdmin)
}

func (s *Server) isAdminRequest(r *http.Request) bool {
	return s.authorizedAdminSession(r)
}

func (s *Server) allowsUserSession(r *http.Request) bool {
	if r.Method == http.MethodGet && r.URL.Path == "/api/status" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/update/status" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/create-settings" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/manage/data" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/runtime/export" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/runtime/export-mailbox-apis" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/runtime/export-mailbox-emails" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/icloud/session" {
		return true
	}
	if r.Method == http.MethodPost {
		switch r.URL.Path {
		case "/api/create-settings",
			"/api/icloud/protocol-login/start",
			"/api/icloud/protocol-login/2fa",
			"/api/apple-account/login/start",
			"/api/apple-account/login/2fa",
			"/api/icloud/session/check",
			"/api/icloud/imap-login/save",
			"/api/icloud/imap-login/check",
			"/api/icloud/mailboxes/create",
			"/api/icloud/mailboxes/sync",
			"/api/icloud/scheduler/start",
			"/api/icloud/scheduler/stop",
			"/api/icloud/scheduler/logs/clear":
			return true
		}
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/icloud/scheduler/status" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/accounts" {
		return true
	}
	if r.Method == http.MethodPost && r.URL.Path == "/api/accounts" {
		return true
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/mailboxes" {
		return true
	}
	if r.Method == http.MethodPost && r.URL.Path == "/api/mailboxes" {
		return true
	}
	if strings.HasPrefix(r.URL.Path, "/api/mailboxes/") {
		return true
	}
	return false
}

func (s *Server) canAccessMailboxID(r *http.Request, id string) bool {
	mailbox, ok := s.store.FindMailboxByID(id)
	return ok && s.canAccessMailbox(r, mailbox)
}

func (s *Server) canAccessMailbox(r *http.Request, mailbox Mailbox) bool {
	if s.isAdminRequest(r) {
		return true
	}
	ownerID := scopedOwnerID(r, s.store)
	if ownerID == "" {
		return true
	}
	return constantTimeEqual(ownerID, mailbox.OwnerID)
}

func (s *Server) canAccessAccountID(r *http.Request, id string) bool {
	id = strings.TrimSpace(id)
	if id == "" {
		return true
	}
	if s.isAdminRequest(r) {
		return true
	}
	ownerID := scopedOwnerID(r, s.store)
	if ownerID == "" {
		return true
	}
	state := s.store.SnapshotForOwner(ownerID)
	for _, account := range state.Accounts {
		if account.ID == id {
			return true
		}
	}
	return false
}

func (s *Server) canAccessAccountIDs(r *http.Request, ids []string) bool {
	for _, id := range normalizeAccountIDSelection("", ids) {
		if !s.canAccessAccountID(r, id) {
			return false
		}
	}
	return true
}

func (s *Server) canAccessAccountIDForOwner(ownerID, id string) bool {
	id = strings.TrimSpace(id)
	if id == "" {
		return true
	}
	ownerID = strings.TrimSpace(ownerID)
	if ownerID == "" {
		return true
	}
	state := s.store.SnapshotForOwner(ownerID)
	for _, account := range state.Accounts {
		if account.ID == id {
			return true
		}
	}
	return false
}

func (s *Server) requiresAdmin(r *http.Request) bool {
	if r.URL.Path == "/" {
		return false
	}
	if strings.HasPrefix(r.URL.Path, "/api/auth/") {
		return false
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/v1/health" {
		return false
	}
	if r.Method == http.MethodPost && r.URL.Path == "/api/v1/mailboxes/claim" {
		return false
	}
	if r.Method == http.MethodPost && r.URL.Path == "/api/v1/mailboxes/claim-status" {
		return false
	}
	if r.Method == http.MethodPost && r.URL.Path == "/api/v1/mailboxes/lookup" {
		return false
	}
	if r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v1/mailboxes/") && strings.HasSuffix(r.URL.Path, "/code") {
		return false
	}
	if r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/mailboxes/") && strings.HasSuffix(r.URL.Path, "/code") {
		return false
	}
	return strings.HasPrefix(r.URL.Path, "/api/")
}

func constantTimeEqual(candidate, want string) bool {
	candidate = strings.TrimSpace(candidate)
	want = strings.TrimSpace(want)
	if candidate == "" || want == "" || len(candidate) != len(want) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(candidate), []byte(want)) == 1
}

func (s *Server) setSessionCookie(w http.ResponseWriter, r *http.Request, token string, expires time.Time) {
	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookieName,
		Value:    token,
		Path:     "/",
		Expires:  expires,
		MaxAge:   int(time.Until(expires).Seconds()),
		HttpOnly: true,
		Secure:   s.secureCookie(r),
		SameSite: http.SameSiteLaxMode,
	})
}

func (s *Server) clearSessionCookie(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookieName,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		Expires:  time.Unix(0, 0),
		HttpOnly: true,
		Secure:   s.secureCookie(r),
		SameSite: http.SameSiteLaxMode,
	})
}

func (s *Server) secureCookie(r *http.Request) bool {
	if r.TLS != nil {
		return true
	}
	if strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
		return true
	}
	return strings.HasPrefix(strings.ToLower(strings.TrimSpace(s.cfg.PublicBaseURL)), "https://")
}

func publicUserFromUser(user User) publicUser {
	return publicUser{
		ID:          user.ID,
		Username:    user.Username,
		Status:      user.Status,
		IsAdmin:     user.IsAdmin,
		CreatedAt:   formatTime(user.CreatedAt),
		UpdatedAt:   formatTime(user.UpdatedAt),
		LastLoginAt: formatTime(user.LastLoginAt),
	}
}

func (s *Server) ownerName(ownerID string) string {
	ownerID = strings.TrimSpace(ownerID)
	if ownerID == "" {
		return "管理员/全局"
	}
	if user, ok := s.store.UserByID(ownerID); ok {
		return user.Username
	}
	return "旧数据/未知归属"
}

func (s *Server) publicUserSummaries(users []User, state State) []publicUserSummary {
	type counter struct {
		publicUserSummary
	}
	rows := make(map[string]*counter, len(users)+1)
	order := make([]string, 0, len(users)+1)

	ensure := func(ownerID, username string) *counter {
		ownerID = strings.TrimSpace(ownerID)
		if row, ok := rows[ownerID]; ok {
			if row.Username == "" && username != "" {
				row.Username = username
			}
			return row
		}
		if username == "" {
			username = s.ownerName(ownerID)
		}
		row := &counter{publicUserSummary: publicUserSummary{
			OwnerID:  ownerID,
			Username: username,
			Status:   StatusActive,
		}}
		rows[ownerID] = row
		order = append(order, ownerID)
		return row
	}

	for _, user := range users {
		row := ensure(user.ID, user.Username)
		row.Status = user.Status
		row.IsAdmin = user.IsAdmin
		row.LastLoginAt = formatTime(user.LastLoginAt)
	}

	mailboxOwner := make(map[string]string, len(state.Mailboxes))
	for _, account := range state.Accounts {
		ensure(account.OwnerID, "").AccountCount++
	}
	for _, mailbox := range state.Mailboxes {
		row := ensure(mailbox.OwnerID, "")
		row.MailboxCount++
		switch mailbox.Status {
		case StatusAvailable:
			row.AvailableMailboxCount++
		case StatusUsed:
			row.UsedMailboxCount++
		}
		mailboxOwner[mailbox.ID] = mailbox.OwnerID
	}
	for _, msg := range state.Messages {
		ownerID := strings.TrimSpace(msg.OwnerID)
		if ownerID == "" {
			ownerID = mailboxOwner[msg.MailboxID]
		}
		ensure(ownerID, "").MessageCount++
	}
	if state.ICloudSession != nil && len(state.ICloudSession.Cookies) > 0 {
		ensure("", "管理员/全局").ICloudSessionSaved = true
	}
	for _, session := range state.ICloudSessions {
		if len(session.Cookies) > 0 {
			ensure(session.OwnerID, "").ICloudSessionSaved = true
		}
	}

	out := make([]publicUserSummary, 0, len(order))
	for _, ownerID := range order {
		row := rows[ownerID]
		if ownerID == "" && row.AccountCount == 0 && row.MailboxCount == 0 && row.MessageCount == 0 && !row.ICloudSessionSaved {
			continue
		}
		out = append(out, row.publicUserSummary)
	}
	return out
}

func (s *Server) publicAccount(account Account) publicAccount {
	return publicAccount{
		ID:           account.ID,
		OwnerID:      account.OwnerID,
		Owner:        s.ownerName(account.OwnerID),
		Label:        account.Label,
		AppleID:      strings.TrimSpace(account.AppleID),
		Status:       account.Status,
		ICloudStatus: account.ICloudStatus,
		Note:         account.Note,
		CreatedAt:    formatTime(account.CreatedAt),
		UpdatedAt:    formatTime(account.UpdatedAt),
	}
}

func (s *Server) publicMailbox(r *http.Request, mailbox Mailbox) publicMailbox {
	accountLabel := ""
	accountAppleID := ""
	if strings.TrimSpace(mailbox.AccountID) != "" {
		if account, ok := s.store.FindAccountByID(mailbox.AccountID); ok {
			accountLabel = account.Label
			accountAppleID = strings.TrimSpace(account.AppleID)
		}
	}
	return publicMailbox{
		ID:             mailbox.ID,
		OwnerID:        mailbox.OwnerID,
		Owner:          s.ownerName(mailbox.OwnerID),
		AccountID:      mailbox.AccountID,
		AccountLabel:   accountLabel,
		AccountAppleID: accountAppleID,
		Label:          mailbox.Label,
		Email:          mailbox.Email,
		APITokenMask:   maskSecret(mailbox.APIToken, 6),
		APIURL:         s.mailboxAPIURL(r, mailbox),
		APIActive:      mailbox.APIActive,
		ICloudActive:   mailbox.ICloudActive,
		ReceiveCount:   mailbox.ReceiveCount,
		OpenAIClaimed:  mailbox.OpenAIClaimed,
		GrokClaimed:    mailbox.GrokClaimed,
		Status:         mailbox.Status,
		Note:           mailbox.Note,
		LastSyncAt:     formatTime(mailbox.LastSyncAt),
		LastSyncUID:    mailbox.LastSyncUID,
		CreatedAt:      formatTime(mailbox.CreatedAt),
		UpdatedAt:      formatTime(mailbox.UpdatedAt),
	}
}

func (s *Server) mailboxAPIURL(r *http.Request, mailbox Mailbox) string {
	baseURL := firstNonEmpty(s.cfg.PublicBaseURL, requestBaseURL(r))
	return fmt.Sprintf("%s/api/v1/mailboxes/%s/code?key=%s", strings.TrimRight(baseURL, "/"), url.PathEscape(mailbox.Email), url.QueryEscape(mailbox.APIToken))
}

func (s *Server) publicSession(session *ICloudSession) publicICloudSession {
	interval := s.appleAccountKeepAliveInterval
	if interval <= 0 {
		interval = appleAccountKeepAliveDefaultInterval
	}
	return publicSessionWithKeepAliveInterval(session, interval)
}

func publicSession(session *ICloudSession) publicICloudSession {
	return publicSessionWithKeepAliveInterval(session, appleAccountKeepAliveDefaultInterval)
}

func publicSessionWithKeepAliveInterval(session *ICloudSession, keepAliveInterval time.Duration) publicICloudSession {
	if session == nil {
		return publicICloudSession{
			Saved:              false,
			NeedsManualLogin:   true,
			LastStatusMessage:  "未保存 iCloud 登录态",
			ProviderConfigured: false,
		}
	}
	message := strings.TrimSpace(session.LastStatusMessage)
	if message == "" {
		message = "登录态已保存；Cookie 原文只写入本地 data/state.json，不会返回前端"
	}
	icloudWebLoginSaved := iCloudWebLoginSaved(*session)
	appleAccountLoginSaved := appleAccountLoginSaved(*session)
	icloudIMAPLoginSaved := iCloudIMAPLoginSaved(*session)
	icloudWebState, _ := iCloudWebLoginState(*session)
	appleAccountState, _ := appleAccountLoginState(*session)
	icloudIMAPState, _ := iCloudIMAPLoginState(*session)
	appleAccountNextRefreshAt := time.Time{}
	if appleAccountKeepAliveEligible(*session) && !appleAccountState.LastCheckedAt.IsZero() {
		appleAccountNextRefreshAt = appleAccountState.LastCheckedAt.Add(appleAccountKeepAliveIntervalForSession(*session, keepAliveInterval))
	}
	return publicICloudSession{
		Saved:                       true,
		AccountID:                   session.AccountID,
		SavedAt:                     formatTime(session.SavedAt),
		AppleID:                     strings.TrimSpace(session.AppleID),
		DSIDMask:                    maskSecret(session.DSID, 4),
		ClientBuildNumber:           session.ClientBuildNumber,
		MasteringNumber:             session.MasteringNumber,
		PremiumMailBaseURL:          session.PremiumMailBaseURL,
		MailGatewayBaseURL:          session.MailGatewayBaseURL,
		MailBaseURL:                 session.MailBaseURL,
		Host:                        session.Host,
		IsICloudPlus:                session.IsICloudPlus,
		CanCreateHME:                session.CanCreateHME,
		CookieCount:                 len(session.Cookies),
		ICloudWebLoginSaved:         icloudWebLoginSaved,
		ICloudWebLoginChecked:       !icloudWebState.LastCheckedAt.IsZero(),
		ICloudWebLoginOK:            icloudWebState.LastCheckOK,
		ICloudWebLoginStatus:        loginStatePublicStatus(icloudWebLoginSaved, icloudWebState),
		AppleAccountLoginSaved:      appleAccountLoginSaved,
		AppleAccountLoginChecked:    !appleAccountState.LastCheckedAt.IsZero(),
		AppleAccountLoginOK:         appleAccountState.LastCheckOK,
		AppleAccountLoginStatus:     loginStatePublicStatus(appleAccountLoginSaved, appleAccountState),
		AppleAccountNextRefreshAt:   formatTime(appleAccountNextRefreshAt),
		AppleAccountManageExpiresAt: formatTime(appleAccountState.ManageExpiresAt),
		AppleAccountManageReady:     appleAccountManageReady(*session),
		ICloudIMAPLoginSaved:        icloudIMAPLoginSaved,
		ICloudIMAPLoginChecked:      !icloudIMAPState.LastCheckedAt.IsZero(),
		ICloudIMAPLoginOK:           icloudIMAPState.LastCheckOK,
		ICloudIMAPLoginStatus:       loginStatePublicStatus(icloudIMAPLoginSaved, icloudIMAPState),
		ICloudIMAPEmail:             normalizeICloudIMAPEmail(icloudIMAPState.IMAPEmail),
		ICloudIMAPHost:              firstNonEmpty(strings.TrimSpace(icloudIMAPState.IMAPHost), strings.TrimSpace(icloudIMAPState.Host)),
		ProviderConfigured:          session.IsICloudPlus && session.CanCreateHME && icloudWebLoginSaved,
		NeedsManualLogin:            !icloudWebLoginSaved && !appleAccountLoginSaved && !icloudIMAPLoginSaved,
		LastCheckedAt:               formatTime(session.LastCheckedAt),
		LastCheckOK:                 session.LastCheckOK,
		LastStatusMessage:           message,
	}
}

func loginStatePublicStatus(saved bool, state LoginState) string {
	if !saved {
		return "未登录"
	}
	if state.LastCheckedAt.IsZero() {
		return "已登录"
	}
	if state.LastCheckOK {
		return "登录态正常"
	}
	return "登录态异常"
}

func iCloudWebLoginSaved(session ICloudSession) bool {
	if len(session.Cookies) > 0 {
		return true
	}
	for _, state := range session.LoginStates {
		if state.Kind == LoginStateICloudWeb && len(state.Cookies) > 0 {
			return true
		}
	}
	return false
}

func iCloudWebLoginState(session ICloudSession) (LoginState, bool) {
	for _, state := range session.LoginStates {
		if state.Kind == LoginStateICloudWeb && len(state.Cookies) > 0 {
			return state, true
		}
	}
	if len(session.Cookies) == 0 {
		return LoginState{}, false
	}
	return LoginState{
		Kind:      LoginStateICloudWeb,
		Host:      session.Host,
		Origin:    iCloudOrigin(session),
		SavedAt:   session.SavedAt,
		Cookies:   append([]SessionCookie(nil), session.Cookies...),
		UserAgent: appleAuthUserAgent,
		Note:      "iCloud webservices login state",
	}, true
}

func withICloudWebLoginState(session ICloudSession, next LoginState) ICloudSession {
	next.Kind = LoginStateICloudWeb
	if len(next.Cookies) == 0 && len(session.Cookies) > 0 {
		next.Cookies = append([]SessionCookie(nil), session.Cookies...)
	}
	for i, state := range session.LoginStates {
		if state.Kind == LoginStateICloudWeb {
			session.LoginStates[i] = next
			return session
		}
	}
	session.LoginStates = append(session.LoginStates, next)
	return session
}

func iCloudIMAPLoginSaved(session ICloudSession) bool {
	_, ok := iCloudIMAPLoginState(session)
	return ok
}

func iCloudIMAPLoginState(session ICloudSession) (LoginState, bool) {
	for _, state := range session.LoginStates {
		if state.Kind != LoginStateICloudIMAP {
			continue
		}
		email := normalizeICloudIMAPEmail(state.IMAPEmail)
		if email == "" {
			email = normalizeICloudIMAPEmail(session.AppleID)
		}
		if email == "" || strings.TrimSpace(state.IMAPAppPassword) == "" {
			continue
		}
		state.IMAPEmail = email
		state.IMAPUsername = firstNonEmpty(strings.TrimSpace(state.IMAPUsername), email)
		state.IMAPHost = firstNonEmpty(strings.TrimSpace(state.IMAPHost), defaultICloudIMAPHost)
		if state.IMAPPort == 0 {
			state.IMAPPort = defaultICloudIMAPPort
		}
		return state, true
	}
	return LoginState{}, false
}

func withICloudIMAPLoginState(session ICloudSession, next LoginState) ICloudSession {
	next.Kind = LoginStateICloudIMAP
	next.IMAPEmail = normalizeICloudIMAPEmail(firstNonEmpty(next.IMAPEmail, session.AppleID))
	next.IMAPUsername = firstNonEmpty(strings.TrimSpace(next.IMAPUsername), next.IMAPEmail)
	next.IMAPHost = firstNonEmpty(strings.TrimSpace(next.IMAPHost), defaultICloudIMAPHost)
	next.Host = firstNonEmpty(strings.TrimSpace(next.Host), next.IMAPHost)
	next.Origin = firstNonEmpty(strings.TrimSpace(next.Origin), "imaps://"+next.IMAPHost)
	if next.IMAPPort == 0 {
		next.IMAPPort = defaultICloudIMAPPort
	}
	for i, state := range session.LoginStates {
		if state.Kind == LoginStateICloudIMAP {
			session.LoginStates[i] = next
			return session
		}
	}
	session.LoginStates = append(session.LoginStates, next)
	return session
}

func appleAccountLoginSaved(session ICloudSession) bool {
	_, ok := appleAccountLoginState(session)
	return ok
}

func normalizeICloudIMAPEmail(value string) string {
	return strings.ToLower(strings.TrimSpace(value))
}

func publicSessionForAppleID(sessions []publicICloudSession, appleID string) publicICloudSession {
	appleID = strings.TrimSpace(appleID)
	for _, session := range sessions {
		if appleID != "" && strings.EqualFold(strings.TrimSpace(session.AppleID), appleID) {
			return session
		}
	}
	if len(sessions) > 0 {
		return sessions[0]
	}
	return publicSession(nil)
}

func publicSessionForAccountID(sessions []publicICloudSession, accountID string) publicICloudSession {
	accountID = strings.TrimSpace(accountID)
	for _, session := range sessions {
		if accountID != "" && constantTimeEqual(strings.TrimSpace(session.AccountID), accountID) {
			return session
		}
	}
	if len(sessions) > 0 {
		return sessions[0]
	}
	return publicSession(nil)
}

func firstMap(rows []map[string]any) map[string]any {
	if len(rows) == 0 {
		return map[string]any{}
	}
	return rows[0]
}

func decodeJSON(r *http.Request, target any) error {
	defer r.Body.Close()
	limited := io.LimitReader(r.Body, 1<<20)
	decoder := json.NewDecoder(limited)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return errCode("bad_json", "JSON 请求体非法："+err.Error(), false)
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(payload)
}

func writeError(w http.ResponseWriter, status int, err error) {
	var coded codedError
	if errors.As(err, &coded) {
		writeJSON(w, status, apiError{
			Success:   false,
			Code:      coded.code,
			Message:   coded.message,
			Retryable: coded.retryable,
		})
		return
	}
	writeJSON(w, status, apiError{
		Success: false,
		Code:    "internal_error",
		Message: err.Error(),
	})
}

func requestBaseURL(r *http.Request) string {
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	return scheme + "://" + r.Host
}

func parseAfter(value string) (time.Time, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, nil
	}
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return time.Time{}, errCode("invalid_after", "after 必须是 RFC3339 时间", false)
	}
	return parsed, nil
}

var (
	xaiOTPRegex     = regexp.MustCompile(`(?i)\b([a-z0-9]{3}-[a-z0-9]{3})\b`)
	contextOTPRegex = regexp.MustCompile(`(?i)(?:openai|chatgpt|otp|code|verification|验证码|验证|代码)[^\d]{0,80}(\d{6})`)
	plainOTPRegex   = regexp.MustCompile(`\b(\d{6})\b`)
)

func extractOTP(text string) string {
	if matches := xaiOTPRegex.FindStringSubmatch(text); len(matches) == 2 && validOTP(matches[1]) {
		return strings.ToUpper(matches[1])
	}
	if matches := contextOTPRegex.FindStringSubmatch(text); len(matches) == 2 && validOTP(matches[1]) {
		return matches[1]
	}
	for _, matches := range plainOTPRegex.FindAllStringSubmatch(text, -1) {
		if len(matches) == 2 && validOTP(matches[1]) {
			return matches[1]
		}
	}
	return ""
}

func validOTP(code string) bool {
	if len(code) == 7 && code[3] == '-' {
		for index := range code {
			if index == 3 {
				continue
			}
			char := code[index]
			if !((char >= '0' && char <= '9') || (char >= 'A' && char <= 'Z') || (char >= 'a' && char <= 'z')) {
				return false
			}
		}
		return !strings.EqualFold(code, "000-000")
	}
	return len(code) == 6 && code != "000000"
}

func validMailboxStatus(status string) bool {
	switch status {
	case StatusActive, StatusAvailable, StatusUsed, StatusFailed, StatusDisabled:
		return true
	default:
		return false
	}
}

func maskAppleID(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	at := strings.Index(value, "@")
	if at <= 1 {
		return maskSecret(value, 4)
	}
	return value[:1] + "***" + value[at:]
}
