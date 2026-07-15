package app

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type ICloudClient struct {
	client *http.Client
}

type ICloudRemoteMailbox struct {
	AnonymousID    string
	Email          string
	Label          string
	Note           string
	ForwardToEmail string
	IsActive       bool
	Origin         string
}

type ICloudSyncedMessage struct {
	RemoteID   string
	UID        string
	Subject    string
	From       string
	Body       string
	ReceivedAt time.Time
}

type ICloudMailCleanupResult struct {
	MovedToTrash int `json:"moved_to_trash"`
	Destroyed    int `json:"destroyed"`
	Skipped      int `json:"skipped"`
}

func NewICloudClient() *ICloudClient {
	return &ICloudClient{client: &http.Client{Timeout: 30 * time.Second}}
}

const mailboxSyncCursorOverlap = 2 * time.Minute
const appleAccountManageRefreshSkew = 0 * time.Second
const appleAccountKeepAliveDefaultInterval = 4 * time.Minute
const appleAccountKeepAliveTimeout = 25 * time.Second

var appleAccountManageBaseURL = "https://appleid.apple.com"
var appleAccountOperationMu sync.Mutex
var appleAccountOperationGates = make(map[string]chan struct{})

const (
	appleAccountManageOrigin     = "https://account.apple.com"
	appleAccountManageUserAgent  = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
	appleAccountManageRequestCtx = "ca"
	appleAccountManageLanguage   = "zh"
	appleAccountManageTimeZone   = "Asia/Shanghai"
	appleAccountManageGMTOffset  = "GMT+08:00"
	appleAccountManagePlatform   = `"macOS"`
	appleAccountManageTZOffset   = 8 * 60 * 60
)

const appleAccountHTTPStatusSessionTimeout = 419

func appleAccountManageHostForICloudHost(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	if strings.Contains(host, "appleid.apple.com.cn") {
		return "appleid.apple.com.cn"
	}
	return "appleid.apple.com"
}

func appleAccountManageOriginForHost(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	if strings.Contains(host, "account.apple.com.cn") || strings.Contains(host, "appleid.apple.com.cn") {
		return "https://account.apple.com.cn"
	}
	return appleAccountManageOrigin
}

func appleAccountManageBaseForState(state LoginState) string {
	baseURL := strings.TrimSpace(appleAccountManageBaseURL)
	if baseURL == "" {
		baseURL = "https://appleid.apple.com"
	}
	if strings.TrimRight(baseURL, "/") != "https://appleid.apple.com" {
		return baseURL
	}
	host := strings.TrimSpace(state.Host)
	if host == "" {
		return baseURL
	}
	host = strings.TrimPrefix(host, "https://")
	host = strings.TrimPrefix(host, "http://")
	host = strings.Trim(host, "/")
	if host == "" {
		return baseURL
	}
	return "https://" + host
}

func appleAccountPortalBaseForState(state LoginState) string {
	baseURL := strings.TrimSpace(appleAccountManageBaseURL)
	if baseURL != "" && strings.TrimRight(baseURL, "/") != "https://appleid.apple.com" {
		return baseURL
	}
	return strings.TrimRight(firstNonEmpty(state.Origin, appleAccountManageOriginForHost(state.Host), appleAccountManageOrigin), "/")
}

func appleAccountLoginState(session ICloudSession) (LoginState, bool) {
	for _, state := range session.LoginStates {
		if state.Kind != LoginStateAppleAccount {
			continue
		}
		if strings.TrimSpace(state.Scnt) == "" {
			continue
		}
		return state, true
	}
	return LoginState{}, false
}

func appleAccountManageReady(session ICloudSession) bool {
	state, ok := appleAccountLoginState(session)
	return ok && strings.TrimSpace(state.APIKey) != ""
}

func appleAccountManageNeedsCreateRefresh(loginState LoginState, now time.Time) bool {
	return !appleAccountManageRecentStateUsable(loginState, now)
}

func appleAccountManageRecentStateUsable(loginState LoginState, now time.Time) bool {
	if strings.TrimSpace(loginState.Scnt) == "" {
		return false
	}
	if strings.TrimSpace(loginState.APIKey) == "" {
		return false
	}
	if loginState.LastCheckedAt.IsZero() {
		return false
	}
	if loginState.ManageExpiresAt.IsZero() {
		return false
	}
	return now.Before(loginState.ManageExpiresAt.Add(-appleAccountManageRefreshSkew))
}

func appleAccountManageRecentlyOK(loginState LoginState, now time.Time) bool {
	return loginState.LastCheckOK && appleAccountManageRecentStateUsable(loginState, now)
}

func markAppleAccountManageOK(loginState *LoginState) {
	if loginState == nil {
		return
	}
	loginState.LastCheckedAt = time.Now()
	loginState.LastCheckOK = true
	loginState.LastStatusMessage = "新接口登录态正常"
}

func markAppleAccountManageTokenTTL(loginState *LoginState, timeoutMinutes int, now time.Time) {
	if loginState == nil || timeoutMinutes <= 0 {
		return
	}
	loginState.ManageExpiresAt = now.Add(time.Duration(timeoutMinutes) * time.Minute)
}

func (c *ICloudClient) CheckAppleAccountManageSession(ctx context.Context, session ICloudSession) (ICloudSession, error) {
	loginState, ok := appleAccountLoginState(session)
	if !ok {
		return session, errCode("apple_account_session_missing", "未保存新接口登录态，请先完成新接口登录", true)
	}
	now := time.Now()
	if appleAccountManageRecentlyOK(loginState, now) && !appleAccountKeepAliveDue(loginState, now, appleAccountKeepAliveDefaultInterval) {
		return withAppleAccountLoginState(session, loginState), nil
	}
	release, err := acquireAppleAccountOperationGate(ctx, appleAccountOperationKey(session, loginState))
	if err != nil {
		return session, err
	}
	defer release()
	kept, err := c.keepAliveAppleAccountManageStateUnlocked(ctx, loginState)
	if err != nil {
		return session, err
	}
	return withAppleAccountLoginState(session, kept), nil
}

func withAppleAccountLoginState(session ICloudSession, next LoginState) ICloudSession {
	next.Kind = LoginStateAppleAccount
	for i, state := range session.LoginStates {
		if state.Kind == LoginStateAppleAccount {
			session.LoginStates[i] = next
			return session
		}
	}
	session.LoginStates = append(session.LoginStates, next)
	return session
}

func (c *ICloudClient) CreatePrivacyMailbox(ctx context.Context, session ICloudSession, label, note string) (ICloudRemoteMailbox, error) {
	if strings.TrimSpace(session.PremiumMailBaseURL) == "" || strings.TrimSpace(session.DSID) == "" || len(session.Cookies) == 0 {
		return ICloudRemoteMailbox{}, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先协议登录", true)
	}
	if !session.IsICloudPlus || !session.CanCreateHME {
		return ICloudRemoteMailbox{}, errCode("icloud_hme_unavailable", "当前登录态没有可创建隐私邮箱的 iCloud+ 权限", false)
	}

	generated, err := c.generate(ctx, session)
	if err != nil {
		return ICloudRemoteMailbox{}, err
	}
	label = strings.TrimSpace(label)
	if label == "" {
		label = "UPI-" + time.Now().Format("0102-150405")
	}
	return c.reserve(ctx, session, generated, label, strings.TrimSpace(note))
}

func (c *ICloudClient) CreatePrivacyMailboxWithAppleAccount(ctx context.Context, session ICloudSession, fallbackAPIKey, label, note string) (ICloudRemoteMailbox, ICloudSession, error) {
	loginState, ok := appleAccountLoginState(session)
	if !ok {
		return ICloudRemoteMailbox{}, session, errCode("apple_account_session_missing", "当前登录态缺少 Apple Account 管理态，请重新协议登录", true)
	}
	release, err := acquireAppleAccountOperationGate(ctx, appleAccountOperationKey(session, loginState))
	if err != nil {
		return ICloudRemoteMailbox{}, session, err
	}
	defer release()

	fallbackAPIKey = strings.TrimSpace(fallbackAPIKey)
	refreshedBeforeCreate := false
	if appleAccountManageNeedsCreateRefresh(loginState, time.Now()) {
		refreshedBeforeCreate = true
		loginState, session, err = c.refreshAppleAccountManageStateForCreate(ctx, session, loginState, fallbackAPIKey)
		if err != nil {
			return ICloudRemoteMailbox{}, session, err
		}
	}

	remote, updatedSession, err := c.createPrivacyMailboxWithAppleAccountState(ctx, session, loginState, fallbackAPIKey, label, note)
	if err == nil || !isCodedError(err, "apple_account_auth_failed") || refreshedBeforeCreate {
		return remote, updatedSession, err
	}

	retryState, ok := appleAccountLoginState(updatedSession)
	if !ok {
		retryState = loginState
	}
	retryState, updatedSession, refreshErr := c.refreshAppleAccountManageStateForCreate(ctx, updatedSession, retryState, fallbackAPIKey)
	if refreshErr != nil {
		return ICloudRemoteMailbox{}, updatedSession, refreshErr
	}
	return c.createPrivacyMailboxWithAppleAccountState(ctx, updatedSession, retryState, fallbackAPIKey, label, note)
}

func (c *ICloudClient) refreshAppleAccountManageStateForCreate(ctx context.Context, session ICloudSession, loginState LoginState, fallbackAPIKey string) (LoginState, ICloudSession, error) {
	refreshed, err := c.refreshAppleAccountManageStateUnlocked(ctx, loginState)
	loginState = refreshed
	session = withAppleAccountLoginState(session, loginState)
	if err != nil && (fallbackAPIKey == "" || !isCodedError(err, "apple_account_api_key_missing")) {
		return loginState, session, err
	}
	return loginState, session, nil
}

func (c *ICloudClient) createPrivacyMailboxWithAppleAccountState(ctx context.Context, session ICloudSession, loginState LoginState, fallbackAPIKey, label, note string) (ICloudRemoteMailbox, ICloudSession, error) {
	apiKey := strings.TrimSpace(firstNonEmpty(loginState.APIKey, fallbackAPIKey))
	if apiKey == "" {
		return ICloudRemoteMailbox{}, session, errCode("apple_account_api_key_missing", "Apple Account 管理态缺少 api_key，请重新完成 Apple Account 登录流程", true)
	}
	loginState.APIKey = apiKey
	session = withAppleAccountLoginState(session, loginState)
	label = strings.TrimSpace(label)
	if label == "" {
		label = "UPI-" + time.Now().Format("0102-150405")
	}
	note = strings.TrimSpace(note)

	var generated struct {
		EmailAddress string `json:"emailAddress"`
	}
	generatedRaw, err := c.callAppleAccountRaw(ctx, &loginState, apiKey, http.MethodPost, "/account/manage/email/private/add", map[string]any{}, &generated)
	if err != nil {
		return ICloudRemoteMailbox{}, withAppleAccountLoginState(session, loginState), err
	}
	if strings.TrimSpace(generated.EmailAddress) == "" {
		return ICloudRemoteMailbox{}, withAppleAccountLoginState(session, loginState), errCode("apple_account_generate_empty", "Apple Account 未返回候选隐私邮箱；"+appleAccountRawResponseDetail("生成候选隐私邮箱", generatedRaw), true)
	}

	var completed struct {
		EmailAddress string `json:"emailAddress"`
		Label        string `json:"label"`
		Note         string `json:"note"`
		ID           string `json:"id"`
		Active       bool   `json:"active"`
	}
	completedRaw, err := c.callAppleAccountRaw(ctx, &loginState, apiKey, http.MethodPut, "/account/manage/email/private/add/complete", map[string]string{
		"emailAddress": generated.EmailAddress,
		"label":        label,
		"note":         note,
	}, &completed)
	if err != nil {
		return ICloudRemoteMailbox{}, withAppleAccountLoginState(session, loginState), err
	}

	remote := ICloudRemoteMailbox{
		AnonymousID: strings.TrimSpace(completed.ID),
		Email:       strings.ToLower(strings.TrimSpace(firstNonEmpty(completed.EmailAddress, generated.EmailAddress))),
		Label:       strings.TrimSpace(firstNonEmpty(completed.Label, label)),
		Note:        strings.TrimSpace(firstNonEmpty(completed.Note, note, "created by Apple Account private email API")),
		IsActive:    completed.Active,
		Origin:      "APPLE_ACCOUNT",
	}
	if remote.AnonymousID != "" {
		var confirmed struct {
			EmailAddress   string `json:"emailAddress"`
			Label          string `json:"label"`
			Note           string `json:"note"`
			ID             string `json:"id"`
			ForwardToEmail string `json:"forwardToEmail"`
			Active         bool   `json:"active"`
		}
		path := "/account/manage/email/private/" + url.PathEscape(remote.AnonymousID) + ".em"
		if err := c.callAppleAccount(ctx, &loginState, apiKey, http.MethodGet, path, nil, &confirmed); err == nil {
			remote.Email = strings.ToLower(strings.TrimSpace(firstNonEmpty(confirmed.EmailAddress, remote.Email)))
			remote.Label = strings.TrimSpace(firstNonEmpty(confirmed.Label, remote.Label))
			remote.Note = strings.TrimSpace(firstNonEmpty(confirmed.Note, remote.Note))
			remote.ForwardToEmail = strings.TrimSpace(confirmed.ForwardToEmail)
			remote.IsActive = confirmed.Active
		}
	}
	if remote.Email == "" {
		return ICloudRemoteMailbox{}, session, errCode("apple_account_create_empty", "Apple Account 创建后未返回隐私邮箱；"+appleAccountRawResponseDetail("确认创建隐私邮箱", completedRaw), true)
	}
	markAppleAccountManageOK(&loginState)
	session = withAppleAccountLoginState(session, loginState)
	return remote, session, nil
}

func (c *ICloudClient) RefreshAppleAccountManageState(ctx context.Context, loginState LoginState) (LoginState, error) {
	release, err := acquireAppleAccountOperationGate(ctx, appleAccountOperationKey(ICloudSession{}, loginState))
	if err != nil {
		return loginState, err
	}
	defer release()
	return c.refreshAppleAccountManageStateUnlocked(ctx, loginState)
}

func (c *ICloudClient) KeepAliveAppleAccountManageState(ctx context.Context, loginState LoginState) (LoginState, error) {
	release, err := acquireAppleAccountOperationGate(ctx, appleAccountOperationKey(ICloudSession{}, loginState))
	if err != nil {
		return loginState, err
	}
	defer release()
	return c.keepAliveAppleAccountManageStateUnlocked(ctx, loginState)
}

func (c *ICloudClient) keepAliveAppleAccountManageStateUnlocked(ctx context.Context, loginState LoginState) (LoginState, error) {
	if strings.TrimSpace(loginState.Scnt) == "" {
		return loginState, errCode("apple_account_session_missing", "当前登录态缺少 Apple Account 管理态，请重新协议登录", true)
	}
	loginState, err := c.refreshAppleAccountManageStateUnlocked(ctx, loginState)
	if err != nil {
		return loginState, err
	}
	if _, err := c.callAppleAccountRaw(ctx, &loginState, loginState.APIKey, http.MethodGet, "/account/manage/forwardemail", nil, nil); err != nil {
		return loginState, err
	}
	if _, err := c.callAppleAccountRaw(ctx, &loginState, loginState.APIKey, http.MethodPost, "/v2/jslogs", appleAccountJSLogBody(), nil); err != nil {
		if os.Getenv("IPM_DEBUG_APPLE_ACCOUNT") == "1" {
			fmt.Fprintf(os.Stderr, "APPLE_ACCOUNT_DEBUG keepalive jslogs ignored err=%v\n", err)
		}
	}
	markAppleAccountManageOK(&loginState)
	return loginState, nil
}

func appleAccountJSLogBody() []map[string]any {
	return []map[string]any{{
		"eventId":       appleAccountJSLogEventID(),
		"timestamp":     time.Now().UnixMilli(),
		"eventType":     "custom",
		"componentName": "performance",
		"action":        "memory",
		"metadata": map[string]any{
			"domNodeCount":    0,
			"usedJSHeapSize":  0,
			"totalJSHeapSize": 0,
			"heapUtilization": 0,
			"createdAt":       0,
			"elapsedTime":     0,
			"marks": map[string]any{
				"startTime": 0,
			},
			"eventVersion": 1,
		},
	}}
}

func appleAccountKeepAliveDue(loginState LoginState, now time.Time, interval time.Duration) bool {
	if interval <= 0 {
		interval = appleAccountKeepAliveDefaultInterval
	}
	if loginState.LastCheckedAt.IsZero() {
		return true
	}
	return !now.Before(loginState.LastCheckedAt.Add(interval))
}

func (c *ICloudClient) refreshAppleAccountManageStateUnlocked(ctx context.Context, loginState LoginState) (LoginState, error) {
	if strings.TrimSpace(loginState.Scnt) == "" {
		return loginState, errCode("apple_account_session_missing", "当前登录态缺少 Apple Account 管理态，请重新协议登录", true)
	}
	var token struct {
		TimeOutInterval int `json:"timeOutInterval"`
	}
	if err := c.callAppleAccount(ctx, &loginState, "", http.MethodGet, "/account/manage/gs/ws/token", nil, &token); err != nil {
		return loginState, err
	}
	markAppleAccountManageTokenTTL(&loginState, token.TimeOutInterval, time.Now())
	if token.TimeOutInterval <= 0 {
		if err := c.warmAppleAccountPortal(ctx, &loginState); err == nil {
			token = struct {
				TimeOutInterval int `json:"timeOutInterval"`
			}{}
			withoutScnt := loginState
			withoutScnt.Scnt = ""
			if err := c.callAppleAccount(ctx, &withoutScnt, "", http.MethodGet, "/account/manage/gs/ws/token", nil, &token); err == nil {
				loginState = withoutScnt
				markAppleAccountManageTokenTTL(&loginState, token.TimeOutInterval, time.Now())
			} else if err := c.callAppleAccount(ctx, &loginState, "", http.MethodGet, "/account/manage/gs/ws/token", nil, &token); err == nil {
				markAppleAccountManageTokenTTL(&loginState, token.TimeOutInterval, time.Now())
			}
		}
	}
	tokenScnt := strings.TrimSpace(loginState.Scnt)
	if err := c.loadAppleAccountManageAPIKey(ctx, &loginState); err == nil {
		markAppleAccountManageOK(&loginState)
		return loginState, nil
	}
	if err := c.warmAppleAccountPortal(ctx, &loginState); err != nil {
		return loginState, err
	}
	withoutScnt := loginState
	withoutScnt.Scnt = ""
	if err := c.callAppleAccount(ctx, &withoutScnt, "", http.MethodGet, "/account/manage/gs/ws/token", nil, &token); err == nil {
		markAppleAccountManageTokenTTL(&withoutScnt, token.TimeOutInterval, time.Now())
		loginState = withoutScnt
	} else if err := c.callAppleAccount(ctx, &loginState, "", http.MethodGet, "/account/manage/gs/ws/token", nil, &token); err != nil {
		if tokenScnt == "" {
			return loginState, err
		}
		loginState.Scnt = tokenScnt
		if retryErr := c.callAppleAccount(ctx, &loginState, "", http.MethodGet, "/account/manage/gs/ws/token", nil, &token); retryErr != nil {
			return loginState, err
		}
		markAppleAccountManageTokenTTL(&loginState, token.TimeOutInterval, time.Now())
	} else {
		markAppleAccountManageTokenTTL(&loginState, token.TimeOutInterval, time.Now())
	}
	if err := c.loadAppleAccountManageAPIKey(ctx, &loginState); err != nil {
		return loginState, err
	}
	markAppleAccountManageOK(&loginState)
	return loginState, nil
}

func appleAccountOperationKey(session ICloudSession, loginState LoginState) string {
	owner := firstNonEmpty(session.OwnerID, "global")
	identity := firstNonEmpty(session.AccountID, session.DSID, session.AppleID, loginState.SessionID, loginState.Scnt, loginState.APIKey, "default")
	return owner + ":" + identity
}

func acquireAppleAccountOperationGate(ctx context.Context, key string) (func(), error) {
	key = strings.TrimSpace(key)
	if key == "" {
		key = "global:default"
	}
	appleAccountOperationMu.Lock()
	gate := appleAccountOperationGates[key]
	if gate == nil {
		gate = make(chan struct{}, 1)
		appleAccountOperationGates[key] = gate
	}
	appleAccountOperationMu.Unlock()

	select {
	case gate <- struct{}{}:
		return func() { <-gate }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (c *ICloudClient) loadAppleAccountManageAPIKey(ctx context.Context, loginState *LoginState) error {
	var manage struct {
		APIKey string `json:"apiKey"`
	}
	if err := c.callAppleAccount(ctx, loginState, "", http.MethodGet, "/account/manage", nil, &manage); err != nil {
		return err
	}
	if apiKey := strings.TrimSpace(manage.APIKey); apiKey != "" {
		loginState.APIKey = apiKey
	}
	if strings.TrimSpace(loginState.APIKey) == "" {
		return errCode("apple_account_api_key_missing", "Apple Account 管理接口未返回 api_key，请重新完成 Apple Account 登录流程", true)
	}
	return nil
}

func (c *ICloudClient) warmAppleAccountPortal(ctx context.Context, loginState *LoginState) error {
	if _, err := c.callAppleAccountPortal(ctx, loginState, "/account/manage/section/privacy", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7", false, "document", "navigate"); err != nil {
		return err
	}
	data, err := c.callAppleAccountPortal(ctx, loginState, "/bootstrap/portal", "application/json, text/plain, */*", true, "empty", "cors")
	if err != nil {
		return err
	}
	var portal struct {
		TimeOutInterval int `json:"timeOutInterval"`
	}
	if json.Unmarshal(data, &portal) == nil {
		markAppleAccountManageTokenTTL(loginState, portal.TimeOutInterval, time.Now())
	}
	return nil
}

func (c *ICloudClient) callAppleAccountPortal(ctx context.Context, loginState *LoginState, path, accept string, jsonContent bool, secFetchDest, secFetchMode string) ([]byte, error) {
	var data []byte
	err := retryAppleTransient(ctx, func() error {
		next, err := c.callAppleAccountPortalOnce(ctx, loginState, path, accept, jsonContent, secFetchDest, secFetchMode)
		if err != nil {
			return err
		}
		data = next
		return nil
	})
	return data, err
}

func (c *ICloudClient) callAppleAccountPortalOnce(ctx context.Context, loginState *LoginState, path, accept string, jsonContent bool, secFetchDest, secFetchMode string) ([]byte, error) {
	if loginState == nil {
		return nil, errCode("apple_account_session_missing", "当前登录态缺少 Apple Account 管理态，请重新协议登录", true)
	}
	base, err := url.Parse(strings.TrimRight(appleAccountPortalBaseForState(*loginState), "/") + "/")
	if err != nil {
		return nil, err
	}
	rel := &url.URL{Path: strings.TrimLeft(path, "/")}
	rawURL := base.ResolveReference(rel).String()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	userAgent := firstNonEmpty(loginState.UserAgent, appleAccountManageUserAgent)
	req.Header.Set("Accept", accept)
	if jsonContent {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Referer", strings.TrimRight(firstNonEmpty(loginState.Origin, appleAccountManageOrigin), "/")+"/")
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept-Language", appleAccountManageLanguage+",en;q=0.9")
	req.Header.Set("Sec-Fetch-Site", "same-origin")
	req.Header.Set("Sec-Fetch-Mode", secFetchMode)
	req.Header.Set("Sec-Fetch-Dest", secFetchDest)
	req.Header.Set("Sec-CH-UA-Platform", appleAccountManagePlatform)
	req.Header.Set("Sec-CH-UA", `"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"`)
	req.Header.Set("Sec-CH-UA-Mobile", "?0")
	if cookie := cookieHeader(loginState.Cookies, rawURL); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	if jsonContent {
		req.Header.Set("X-Apple-I-Request-Context", appleAccountManageRequestCtx)
		req.Header.Set("X-Apple-I-TimeZone", appleAccountManageTimeZone)
		req.Header.Set("X-Apple-I-FD-Client-Info", appleAccountFDClientInfo(userAgent))
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if os.Getenv("IPM_DEBUG_APPLE_ACCOUNT") == "1" {
		fmt.Fprintf(os.Stderr, "APPLE_ACCOUNT_PORTAL_DEBUG method=GET path=%s status=%d req_cookie_len=%d req_scnt=%s res_scnt=%s res_session=%s set_cookie=%d body=%q\n",
			path,
			resp.StatusCode,
			len(req.Header.Get("Cookie")),
			appleDebugFingerprint(req.Header.Get("scnt")),
			appleDebugFingerprint(resp.Header.Get("scnt")),
			appleDebugFingerprint(resp.Header.Get("X-Apple-ID-Session-Id")),
			len(resp.Cookies()),
			appleDebugBody(data),
		)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return data, appleAccountAPIError(resp.StatusCode, data, appleAccountRequestStage(http.MethodGet, path))
	}
	mergeSessionCookies(&loginState.Cookies, resp.Request.URL, resp.Cookies())
	updateAppleAccountLoginStateFromHeaders(loginState, resp.Header)
	return data, nil
}

func (c *ICloudClient) ListPrivacyMailboxes(ctx context.Context, session ICloudSession) ([]ICloudRemoteMailbox, error) {
	if strings.TrimSpace(session.PremiumMailBaseURL) == "" || strings.TrimSpace(session.DSID) == "" || len(session.Cookies) == 0 {
		return nil, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先协议登录", true)
	}
	var out struct {
		HMEEmails []struct {
			AnonymousID    string `json:"anonymousId"`
			HME            string `json:"hme"`
			Label          string `json:"label"`
			ForwardToEmail string `json:"forwardToEmail"`
			IsActive       bool   `json:"isActive"`
			Origin         string `json:"origin"`
		} `json:"hmeEmails"`
	}
	if err := retryAppleTransient(ctx, func() error {
		return c.call(ctx, session, http.MethodGet, "/v2/hme/list", nil, &out)
	}); err != nil {
		return nil, err
	}
	remotes := make([]ICloudRemoteMailbox, 0, len(out.HMEEmails))
	for _, item := range out.HMEEmails {
		email := strings.ToLower(strings.TrimSpace(item.HME))
		if email == "" {
			continue
		}
		remotes = append(remotes, ICloudRemoteMailbox{
			AnonymousID:    strings.TrimSpace(item.AnonymousID),
			Email:          email,
			Label:          strings.TrimSpace(item.Label),
			ForwardToEmail: strings.TrimSpace(item.ForwardToEmail),
			IsActive:       item.IsActive,
			Origin:         strings.TrimSpace(item.Origin),
		})
	}
	return remotes, nil
}

func (c *ICloudClient) generate(ctx context.Context, session ICloudSession) (string, error) {
	var out struct {
		HME string `json:"hme"`
	}
	if err := c.call(ctx, session, http.MethodPost, "/v1/hme/generate", map[string]string{
		"langCode": "zh-cn",
	}, &out); err != nil {
		return "", err
	}
	if strings.TrimSpace(out.HME) == "" {
		return "", errCode("icloud_generate_empty", "iCloud 未返回候选隐私邮箱", true)
	}
	return strings.TrimSpace(out.HME), nil
}

func (c *ICloudClient) reserve(ctx context.Context, session ICloudSession, hme, label, note string) (ICloudRemoteMailbox, error) {
	var out struct {
		HME struct {
			AnonymousID    string `json:"anonymousId"`
			HME            string `json:"hme"`
			Label          string `json:"label"`
			Note           string `json:"note"`
			ForwardToEmail string `json:"forwardToEmail"`
			IsActive       bool   `json:"isActive"`
		} `json:"hme"`
	}
	if err := c.call(ctx, session, http.MethodPost, "/v1/hme/reserve", map[string]string{
		"hme":   hme,
		"label": label,
		"note":  note,
	}, &out); err != nil {
		return ICloudRemoteMailbox{}, err
	}
	return ICloudRemoteMailbox{
		AnonymousID:    out.HME.AnonymousID,
		Email:          out.HME.HME,
		Label:          out.HME.Label,
		Note:           out.HME.Note,
		ForwardToEmail: out.HME.ForwardToEmail,
		IsActive:       out.HME.IsActive,
		Origin:         "ICLOUD_WEB",
	}, nil
}

func (c *ICloudClient) callAppleAccount(ctx context.Context, loginState *LoginState, apiKey, method, path string, body any, result any) error {
	_, err := c.callAppleAccountRaw(ctx, loginState, apiKey, method, path, body, result)
	return err
}

type appleAccountRawResponse struct {
	StatusCode int
	Body       []byte
}

func (c *ICloudClient) fetchAppleAccountManageTokenScnt(ctx context.Context, loginState LoginState, result any) (string, error) {
	var scnt string
	err := retryAppleTransient(ctx, func() error {
		next, err := c.fetchAppleAccountManageTokenScntOnce(ctx, loginState, result)
		if strings.TrimSpace(next) != "" {
			scnt = next
		}
		if err != nil {
			return err
		}
		return nil
	})
	return scnt, err
}

func (c *ICloudClient) fetchAppleAccountManageTokenScntOnce(ctx context.Context, loginState LoginState, result any) (string, error) {
	base, err := url.Parse(strings.TrimRight(appleAccountManageBaseForState(loginState), "/") + "/")
	if err != nil {
		return "", err
	}
	rawURL := base.ResolveReference(&url.URL{Path: "account/manage/gs/ws/token"}).String()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", err
	}
	origin := strings.TrimRight(firstNonEmpty(loginState.Origin, appleAccountManageOriginForHost(loginState.Host), appleAccountManageOrigin), "/")
	userAgent := firstNonEmpty(loginState.UserAgent, appleAccountManageUserAgent)
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", origin)
	req.Header.Set("Referer", origin+"/")
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept-Language", appleAccountManageLanguage+",en;q=0.9")
	req.Header.Set("Sec-Fetch-Site", "same-site")
	req.Header.Set("Sec-Fetch-Mode", "cors")
	req.Header.Set("Sec-Fetch-Dest", "empty")
	req.Header.Set("Sec-CH-UA-Platform", appleAccountManagePlatform)
	req.Header.Set("Sec-CH-UA", `"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"`)
	req.Header.Set("Sec-CH-UA-Mobile", "?0")
	req.Header.Set("X-Apple-I-FD-Client-Info", appleAccountFDClientInfo(userAgent))
	req.Header.Set("X-Apple-I-Request-Context", appleAccountManageRequestCtx)
	req.Header.Set("X-Apple-I-TimeZone", appleAccountManageTimeZone)
	if cookie := cookieHeader(loginState.Cookies, rawURL); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}

	resp, err := c.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return "", err
	}
	scnt := strings.TrimSpace(resp.Header.Get("scnt"))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return scnt, appleAccountAPIError(resp.StatusCode, data, appleAccountRequestStage(http.MethodGet, "/account/manage/gs/ws/token"))
	}
	if result != nil && len(bytes.TrimSpace(data)) > 0 {
		if err := json.Unmarshal(data, result); err != nil {
			return scnt, errCode("apple_account_bad_response", "Apple Account 返回无法解析；"+appleAccountResponseDetail(appleAccountRequestStage(http.MethodGet, "/account/manage/gs/ws/token"), data), true)
		}
	}
	return scnt, nil
}

func (c *ICloudClient) callAppleAccountRaw(ctx context.Context, loginState *LoginState, apiKey, method, path string, body any, result any) (appleAccountRawResponse, error) {
	var raw appleAccountRawResponse
	err := retryAppleTransient(ctx, func() error {
		next, err := c.callAppleAccountRawOnce(ctx, loginState, apiKey, method, path, body, result)
		if err != nil {
			return err
		}
		raw = next
		return nil
	})
	return raw, err
}

func (c *ICloudClient) callAppleAccountRawOnce(ctx context.Context, loginState *LoginState, apiKey, method, path string, body any, result any) (appleAccountRawResponse, error) {
	if loginState == nil {
		return appleAccountRawResponse{}, errCode("apple_account_session_missing", "当前登录态缺少 Apple Account 管理态，请重新协议登录", true)
	}
	base, err := url.Parse(strings.TrimRight(appleAccountManageBaseForState(*loginState), "/") + "/")
	if err != nil {
		return appleAccountRawResponse{}, err
	}
	rel := &url.URL{Path: strings.TrimLeft(path, "/")}
	rawURL := base.ResolveReference(rel).String()

	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return appleAccountRawResponse{}, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, rawURL, reader)
	if err != nil {
		return appleAccountRawResponse{}, err
	}
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("Content-Type", "application/json")
	origin := strings.TrimRight(firstNonEmpty(loginState.Origin, appleAccountManageOriginForHost(loginState.Host), appleAccountManageOrigin), "/")
	userAgent := firstNonEmpty(loginState.UserAgent, appleAccountManageUserAgent)
	req.Header.Set("Origin", origin)
	req.Header.Set("Referer", origin+"/")
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept-Language", appleAccountManageLanguage+",en;q=0.9")
	req.Header.Set("Sec-Fetch-Site", "same-site")
	req.Header.Set("Sec-Fetch-Mode", "cors")
	req.Header.Set("Sec-Fetch-Dest", "empty")
	req.Header.Set("Sec-CH-UA-Platform", appleAccountManagePlatform)
	req.Header.Set("Sec-CH-UA", `"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"`)
	req.Header.Set("Sec-CH-UA-Mobile", "?0")
	if scnt := strings.TrimSpace(loginState.Scnt); scnt != "" {
		req.Header.Set("scnt", scnt)
	}
	if apiKey = strings.TrimSpace(apiKey); apiKey != "" {
		req.Header.Set("X-Apple-Api-Key", apiKey)
	}
	req.Header.Set("X-Apple-I-FD-Client-Info", appleAccountFDClientInfo(userAgent))
	req.Header.Set("X-Apple-I-Request-Context", appleAccountManageRequestCtx)
	req.Header.Set("X-Apple-I-TimeZone", appleAccountManageTimeZone)
	if cookie := cookieHeader(loginState.Cookies, rawURL); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}

	resp, err := c.client.Do(req)
	if err != nil {
		return appleAccountRawResponse{}, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	raw := appleAccountRawResponse{StatusCode: resp.StatusCode, Body: data}
	if err != nil {
		return raw, err
	}
	if os.Getenv("IPM_DEBUG_APPLE_ACCOUNT") == "1" {
		fmt.Fprintf(os.Stderr, "APPLE_ACCOUNT_DEBUG method=%s path=%s status=%d req_cookie_len=%d req_scnt=%s res_scnt=%s res_session=%s set_cookie=%d body=%q\n",
			method,
			path,
			resp.StatusCode,
			len(req.Header.Get("Cookie")),
			appleDebugFingerprint(req.Header.Get("scnt")),
			appleDebugFingerprint(resp.Header.Get("scnt")),
			appleDebugFingerprint(resp.Header.Get("X-Apple-ID-Session-Id")),
			len(resp.Cookies()),
			appleDebugBody(data),
		)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		if os.Getenv("IPM_DEBUG_APPLE_ACCOUNT") == "1" {
			fmt.Fprintf(os.Stderr, "APPLE_ACCOUNT_DEBUG method=%s path=%s status=%d body=%q\n", method, path, resp.StatusCode, appleDebugBody(data))
		}
		return raw, appleAccountAPIError(resp.StatusCode, data, appleAccountRequestStage(method, path))
	}
	mergeSessionCookies(&loginState.Cookies, resp.Request.URL, resp.Cookies())
	updateAppleAccountLoginStateFromHeaders(loginState, resp.Header)
	if result != nil && len(bytes.TrimSpace(data)) > 0 {
		if err := json.Unmarshal(data, result); err != nil {
			return raw, errCode("apple_account_bad_response", "Apple Account 返回无法解析；"+appleAccountRawResponseDetail(appleAccountRequestStage(method, path), raw), true)
		}
	}
	return raw, nil
}

func updateAppleAccountLoginStateFromHeaders(loginState *LoginState, header http.Header) {
	if loginState == nil {
		return
	}
	if scnt := strings.TrimSpace(header.Get("scnt")); scnt != "" {
		loginState.Scnt = scnt
	}
	if sessionID := strings.TrimSpace(header.Get("X-Apple-ID-Session-Id")); sessionID != "" {
		loginState.SessionID = sessionID
	}
	if token := strings.TrimSpace(firstNonEmpty(header.Get("X-Apple-I-DA-Token"), header.Get("X-Apple-I-Cont-X-Apple-I-DA-Token"))); token != "" {
		loginState.DataAccessToken = token
	}
	loginState.SavedAt = time.Now()
}

func appleAccountJSLogEventID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("ipm-%d", time.Now().UnixNano())
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		b[0:4],
		b[4:6],
		b[6:8],
		b[8:10],
		b[10:16],
	)
}

func appleAccountFDClientInfo(userAgent string) string {
	info := map[string]string{
		"U": firstNonEmpty(userAgent, appleAccountManageUserAgent),
		"L": appleAccountManageLanguage,
		"Z": appleAccountManageGMTOffset,
		"V": "1.1",
		"F": appleAccountCompressedFingerprint(time.Now()),
	}
	data, _ := json.Marshal(info)
	return string(data)
}

func appleAccountCompressedFingerprint(now time.Time) string {
	raw := appleAccountFingerprintPayload(now.In(time.FixedZone("apple-account", appleAccountManageTZOffset)))
	replaced := raw
	for idx, token := range appleAccountFingerprintDictionary {
		replaced = strings.ReplaceAll(replaced, token, string(rune(idx+1)))
	}
	encoded, ok := appleAccountFingerprintHuffman(replaced)
	if !ok {
		return raw
	}
	checksum := 65535
	for _, b := range []byte(raw) {
		checksum = ((checksum >> 8) | (checksum << 8)) & 0xffff
		checksum ^= int(b) & 0xff
		checksum ^= (checksum & 0xff) >> 4
		checksum ^= (checksum << 12) & 0xffff
		checksum ^= ((checksum & 0xff) << 5) & 0xffff
	}
	return encoded +
		string(appleAccountFingerprintAlphabet[(checksum>>12)&63]) +
		string(appleAccountFingerprintAlphabet[(checksum>>6)&63]) +
		string(appleAccountFingerprintAlphabet[checksum&63])
}

func appleAccountFingerprintPayload(now time.Time) string {
	values := []string{
		"TF1", "020",
	}
	for i := 0; i < 39; i++ {
		values = append(values, "")
	}
	values = append(values,
		"true",
		"true",
		strconv.FormatInt(now.UnixMilli(), 10),
		"-6",
		"6/7/2005, 9:33:44 PM",
		"", "", "", "", "", "",
		strconv.FormatInt(now.UnixMilli(), 10),
		"0",
		appleAccountUSLocaleString(now),
	)
	for i := 0; i < 34; i++ {
		values = append(values, "")
	}
	values = append(values, "5.6.1-0", "")

	var b strings.Builder
	for _, value := range values {
		b.WriteString(appleAccountJSEscape(value))
		b.WriteByte(';')
	}
	return b.String()
}

func appleAccountUSLocaleString(t time.Time) string {
	hour := t.Hour()
	ampm := "AM"
	if hour >= 12 {
		ampm = "PM"
	}
	hour12 := hour % 12
	if hour12 == 0 {
		hour12 = 12
	}
	return fmt.Sprintf("%d/%d/%d, %d:%02d:%02d %s", int(t.Month()), t.Day(), t.Year(), hour12, t.Minute(), t.Second(), ampm)
}

func appleAccountJSEscape(value string) string {
	const hex = "0123456789ABCDEF"
	var b strings.Builder
	for _, r := range value {
		if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') ||
			r == '@' || r == '*' || r == '_' || r == '+' || r == '-' || r == '.' || r == '/' {
			b.WriteRune(r)
			continue
		}
		if r <= 0xff {
			b.WriteByte('%')
			b.WriteByte(hex[(r>>4)&0xf])
			b.WriteByte(hex[r&0xf])
			continue
		}
		b.WriteString("%u")
		b.WriteByte(hex[(r>>12)&0xf])
		b.WriteByte(hex[(r>>8)&0xf])
		b.WriteByte(hex[(r>>4)&0xf])
		b.WriteByte(hex[r&0xf])
	}
	return b.String()
}

func appleAccountFingerprintHuffman(value string) (string, bool) {
	var b strings.Builder
	bitBuffer := 0
	bitCount := 0
	push := func(width, code int) {
		bitBuffer = (bitBuffer << width) | code
		bitCount += width
		for bitCount >= 6 {
			idx := (bitBuffer >> (bitCount - 6)) & 63
			b.WriteByte(appleAccountFingerprintAlphabet[idx])
			bitCount -= 6
			bitBuffer ^= idx << bitCount
		}
	}
	push(6, (len(value)&7)<<3)
	push(6, (len(value)&56)|1)
	for _, r := range value {
		code, ok := appleAccountFingerprintCodes[int(r)]
		if !ok {
			return "", false
		}
		push(code.width, code.value)
	}
	code := appleAccountFingerprintCodes[0]
	push(code.width, code.value)
	if bitCount > 0 {
		push(6-bitCount, 0)
	}
	return b.String(), true
}

type appleAccountFingerprintCode struct {
	width int
	value int
}

var appleAccountFingerprintDictionary = []string{
	"%20", ";;;", "%3B", "%2C", "und", "fin", "ed;", "%28", "%29", "%3A", "/53", "ike", "Web", "0;", ".0", "e;", "on", "il", "ck", "01", "in", "Mo", "fa", "00", "32", "la", ".1", "ri", "it", "%u", "le",
}

const appleAccountFingerprintAlphabet = ".0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"

var appleAccountFingerprintCodes = map[int]appleAccountFingerprintCode{
	1: {4, 15}, 110: {8, 239}, 74: {8, 238}, 57: {7, 118}, 56: {7, 117}, 71: {8, 233},
	25: {8, 232}, 101: {5, 28}, 104: {7, 111}, 4: {7, 110}, 105: {6, 54}, 5: {7, 107},
	109: {7, 106}, 103: {9, 423}, 82: {9, 422}, 26: {8, 210}, 6: {7, 104}, 46: {6, 51},
	97: {6, 50}, 111: {6, 49}, 7: {7, 97}, 45: {7, 96}, 59: {5, 23}, 15: {7, 91},
	11: {8, 181}, 72: {8, 180}, 27: {8, 179}, 28: {8, 178}, 16: {7, 88}, 88: {10, 703},
	113: {11, 1405}, 89: {12, 2809}, 107: {13, 5617}, 90: {14, 11233}, 42: {15, 22465},
	64: {16, 44929}, 0: {16, 44928}, 81: {9, 350}, 29: {8, 174}, 118: {8, 173}, 30: {8, 172},
	98: {8, 171}, 12: {8, 170}, 99: {7, 84}, 117: {6, 41}, 112: {6, 40}, 102: {9, 319},
	68: {9, 318}, 31: {8, 158}, 100: {7, 78}, 84: {6, 38}, 55: {6, 37}, 17: {7, 73},
	8: {7, 72}, 9: {7, 71}, 77: {7, 70}, 18: {7, 69}, 65: {7, 68}, 48: {6, 33},
	116: {6, 32}, 10: {7, 63}, 121: {8, 125}, 78: {8, 124}, 80: {7, 61}, 69: {7, 60},
	119: {7, 59}, 13: {8, 117}, 79: {8, 116}, 19: {7, 57}, 67: {7, 56}, 114: {6, 27},
	83: {6, 26}, 115: {6, 25}, 14: {6, 24}, 122: {8, 95}, 95: {8, 94}, 76: {7, 46},
	24: {7, 45}, 37: {7, 44}, 50: {5, 10}, 51: {5, 9}, 108: {6, 17}, 22: {7, 33},
	120: {8, 65}, 66: {8, 64}, 21: {7, 31}, 106: {7, 30}, 47: {6, 14}, 53: {5, 6},
	49: {5, 5}, 86: {8, 39}, 85: {8, 38}, 23: {7, 18}, 75: {7, 17}, 20: {7, 16},
	2: {5, 3}, 73: {8, 23}, 43: {9, 45}, 87: {9, 44}, 70: {7, 10}, 3: {6, 4},
	52: {5, 1}, 54: {5, 0},
}

func appleAccountAPIError(status int, data []byte, stage string) error {
	detail := appleAccountErrorDetail(status, data, stage)
	if isAppleTransientHTTPStatus(status) {
		return errCode("apple_account_http_transient", "Apple Account 服务暂时不可用；"+detail, true)
	}
	msg := strings.TrimSpace(appleDebugBody(data))
	if msg == "" {
		msg = "空响应"
	}
	lower := strings.ToLower(msg)
	if strings.Contains(lower, "limit") || strings.Contains(lower, "too many") || strings.Contains(lower, "rate") {
		return errCode("apple_account_hme_limit", "Apple Account 已达到当前隐私邮箱创建上限，请稍后再试；"+detail, true)
	}
	if status == appleAccountHTTPStatusSessionTimeout || ((status == http.StatusUnauthorized || status == http.StatusForbidden) && appleAccountBodyLooksAuthExpired(lower)) {
		return errCode("apple_account_auth_failed", "Apple Account 管理态已失效，请重新协议登录；"+detail, true)
	}
	return errCode("apple_account_api_failed", "Apple Account 接口失败；"+detail, true)
}

func appleAccountBodyLooksAuthExpired(lower string) bool {
	lower = strings.ToLower(strings.TrimSpace(lower))
	if lower == "" {
		return false
	}
	authTokens := []string{
		"authentication_failed",
		"authentication failed",
		"auth_failed",
		"auth failed",
		"gsa_invalid_session",
		"invalid session",
		"scnt_expired",
		"session expired",
		"session has expired",
	}
	for _, token := range authTokens {
		if strings.Contains(lower, token) {
			return true
		}
	}
	return strings.Contains(lower, "authentication") && (strings.Contains(lower, "failed") || strings.Contains(lower, "expired"))
}

func appleAccountErrorDetail(status int, data []byte, stage string) string {
	stage = strings.TrimSpace(stage)
	if stage == "" {
		stage = "未知阶段"
	}
	body := strings.TrimSpace(appleDebugBody(data))
	if body == "" {
		body = "空响应"
	}
	return fmt.Sprintf("阶段：%s；HTTP %d；原始返回：%s", stage, status, body)
}

func appleAccountResponseDetail(stage string, data []byte) string {
	stage = strings.TrimSpace(stage)
	if stage == "" {
		stage = "未知阶段"
	}
	body := strings.TrimSpace(appleDebugBody(data))
	if body == "" {
		body = "空响应"
	}
	return fmt.Sprintf("阶段：%s；原始返回：%s", stage, body)
}

func appleAccountRawResponseDetail(stage string, raw appleAccountRawResponse) string {
	stage = strings.TrimSpace(stage)
	if stage == "" {
		stage = "未知阶段"
	}
	body := strings.TrimSpace(appleDebugBody(raw.Body))
	if body == "" {
		body = "空响应"
	}
	if raw.StatusCode > 0 {
		return fmt.Sprintf("阶段：%s；HTTP %d；原始返回：%s", stage, raw.StatusCode, body)
	}
	return fmt.Sprintf("阶段：%s；原始返回：%s", stage, body)
}

func appleAccountRequestStage(method, path string) string {
	method = strings.ToUpper(strings.TrimSpace(method))
	path = strings.TrimSpace(path)
	switch {
	case path == "/account/manage/gs/ws/token":
		return "刷新管理 token"
	case path == "/account/manage":
		return "读取管理入口"
	case path == "/account/manage/section/privacy":
		return "打开隐私页面"
	case path == "/bootstrap/portal":
		return "预热门户"
	case method == http.MethodPost && path == "/v2/jslogs":
		return "新接口保活"
	case method == http.MethodGet && path == "/account/manage/forwardemail":
		return "读取转发邮箱"
	case method == http.MethodPost && path == "/account/manage/email/private/add":
		return "生成候选隐私邮箱"
	case method == http.MethodPut && path == "/account/manage/email/private/add/complete":
		return "确认创建隐私邮箱"
	case method == http.MethodGet && strings.HasPrefix(path, "/account/manage/email/private/") && strings.HasSuffix(path, ".em"):
		return "确认隐私邮箱详情"
	default:
		return strings.TrimSpace(method + " " + path)
	}
}

func isCodedError(err error, code string) bool {
	var coded codedError
	return errors.As(err, &coded) && coded.code == code
}

func (c *ICloudClient) call(ctx context.Context, session ICloudSession, method, path string, body any, result any) error {
	u, err := c.endpoint(session, path)
	if err != nil {
		return err
	}
	return c.callEnvelope(ctx, session, method, u, body, result)
}

func (c *ICloudClient) callEnvelope(ctx context.Context, session ICloudSession, method, rawURL string, body any, result any) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, rawURL, reader)
	if err != nil {
		return err
	}
	contentType := "text/plain;charset=UTF-8"
	if body == nil {
		contentType = ""
	}
	setICloudFetchHeaders(req, session, "application/json", contentType)
	if cookie := cookieHeader(session.Cookies, rawURL); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}

	resp, err := c.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("icloud HTTP %d: %s", resp.StatusCode, trimForError(data))
	}
	var envelope struct {
		Success bool            `json:"success"`
		Result  json.RawMessage `json:"result"`
		Error   *struct {
			Code    string `json:"errorCode"`
			Message string `json:"errorMessage"`
		} `json:"error"`
		Timestamp int64 `json:"timestamp"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		return errCode("icloud_bad_response", "iCloud 返回无法解析；原始返回："+trimForError(data), true)
	}
	if !envelope.Success {
		msg := "iCloud 接口返回失败"
		if envelope.Error != nil && strings.TrimSpace(envelope.Error.Message) != "" {
			msg = envelope.Error.Message
		}
		return iCloudAPIError(msg)
	}
	if result != nil {
		if err := json.Unmarshal(envelope.Result, result); err != nil {
			return errCode("icloud_bad_result", "iCloud 结果无法解析；原始返回："+trimForError(envelope.Result), true)
		}
	}
	return nil
}

func iCloudAPIError(message string) error {
	message = strings.TrimSpace(message)
	if message == "" {
		message = "iCloud 接口返回失败"
	}
	if isICloudHMELimitMessage(message) {
		return errCode("icloud_hme_limit", "iCloud 已达到当前隐私邮箱创建上限，请稍后再试；原始返回："+trimForError([]byte(message)), true)
	}
	return errCode("icloud_api_failed", message, true)
}

func isICloudHMELimitMessage(message string) bool {
	normalized := strings.ToLower(strings.TrimSpace(message))
	return strings.Contains(normalized, "reached the limit of addresses") ||
		(strings.Contains(normalized, "limit") && strings.Contains(normalized, "try again later")) ||
		strings.Contains(message, "创建上限")
}

func (c *ICloudClient) SyncMailboxMessages(ctx context.Context, session ICloudSession, mailbox Mailbox, after time.Time, keyword string, maxThreads int) ([]ICloudSyncedMessage, error) {
	if strings.TrimSpace(session.DSID) == "" || len(session.Cookies) == 0 {
		return nil, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先协议登录", true)
	}
	if strings.TrimSpace(mailbox.Email) == "" {
		return nil, errCode("mailbox_email_missing", "邮箱地址为空", false)
	}
	if maxThreads <= 0 || maxThreads > 50 {
		maxThreads = 20
	}
	if keyword = strings.TrimSpace(keyword); keyword == "" {
		keyword = "OpenAI"
	}
	queryAfter := mailboxSyncAfter(mailbox, after, time.Now())
	folders, err := c.mailFolders(ctx, session)
	if err != nil {
		return nil, err
	}
	folders = preferredMailFolders(folders)
	var out []ICloudSyncedMessage
	for _, folder := range folders {
		threads, err := c.searchThreads(ctx, session, folder, maxThreads)
		if err != nil {
			return out, err
		}
		for _, thread := range threads {
			if shouldSkipSyncedThread(thread, queryAfter) {
				continue
			}
			text := thread.Subject + "\n" + thread.Preview
			if !looksLikeVerificationText(text, keyword) {
				continue
			}
			messages, err := c.threadMessages(ctx, session, folder, thread.ThreadID, mailbox.Email, queryAfter)
			if err != nil {
				return out, err
			}
			out = append(out, messages...)
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		return out[i].ReceivedAt.After(out[j].ReceivedAt)
	})
	return out, nil
}

func (c *ICloudClient) SyncMailboxMessagesBatch(ctx context.Context, session ICloudSession, mailboxes []Mailbox, after time.Time, keyword string, maxThreads int) (map[string][]ICloudSyncedMessage, error) {
	if strings.TrimSpace(session.DSID) == "" || len(session.Cookies) == 0 {
		return nil, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先协议登录", true)
	}
	if maxThreads <= 0 || maxThreads > 50 {
		maxThreads = 50
	}
	if keyword = strings.TrimSpace(keyword); keyword == "" {
		keyword = "OpenAI"
	}
	now := time.Now()
	aliases := make(map[string]string)
	afterByMailbox := make(map[string]time.Time)
	var queryAfter time.Time
	for _, mailbox := range mailboxes {
		email := strings.ToLower(strings.TrimSpace(mailbox.Email))
		if strings.TrimSpace(mailbox.ID) == "" || email == "" {
			continue
		}
		aliases[mailbox.ID] = email
		mailboxAfter := mailboxSyncAfter(mailbox, after, now)
		afterByMailbox[mailbox.ID] = mailboxAfter
		if queryAfter.IsZero() || mailboxAfter.Before(queryAfter) {
			queryAfter = mailboxAfter
		}
	}
	if len(aliases) == 0 {
		return map[string][]ICloudSyncedMessage{}, nil
	}
	folders, err := c.mailFolders(ctx, session)
	if err != nil {
		return nil, err
	}
	folders = preferredMailFolders(folders)
	out := make(map[string][]ICloudSyncedMessage, len(aliases))
	for _, folder := range folders {
		threads, err := c.searchThreads(ctx, session, folder, maxThreads)
		if err != nil {
			return out, err
		}
		for _, thread := range threads {
			if shouldSkipSyncedThread(thread, queryAfter) {
				continue
			}
			text := thread.Subject + "\n" + thread.Preview
			if !looksLikeVerificationText(text, keyword) {
				continue
			}
			messagesByMailbox, err := c.threadMessagesForAliases(ctx, session, folder, thread.ThreadID, aliases, afterByMailbox)
			if err != nil {
				return out, err
			}
			for mailboxID, messages := range messagesByMailbox {
				out[mailboxID] = append(out[mailboxID], messages...)
			}
		}
	}
	for mailboxID := range out {
		sort.SliceStable(out[mailboxID], func(i, j int) bool {
			return out[mailboxID][i].ReceivedAt.After(out[mailboxID][j].ReceivedAt)
		})
	}
	return out, nil
}

func mailboxSyncAfter(mailbox Mailbox, after time.Time, now time.Time) time.Time {
	queryAfter := after
	if !mailbox.LastSyncAt.IsZero() {
		cursor := mailbox.LastSyncAt.Add(-mailboxSyncCursorOverlap)
		if cursor.After(queryAfter) {
			queryAfter = cursor
		}
	}
	if queryAfter.After(now) {
		return now.Add(-mailboxSyncCursorOverlap)
	}
	return queryAfter
}

func shouldSkipSyncedThread(thread mailThread, after time.Time) bool {
	return !after.IsZero() && !thread.ReceivedAt.IsZero() && thread.ReceivedAt.Before(after)
}

func looksLikeVerificationText(text, keyword string) bool {
	if extractOTP(text) != "" {
		return true
	}
	needles := []string{"openai", "chatgpt", "code", "otp", "verification", "verify", "验证码", "验证", "代码"}
	if strings.TrimSpace(keyword) != "" {
		needles = append(needles, strings.ToLower(strings.TrimSpace(keyword)))
	}
	lower := strings.ToLower(text)
	for _, needle := range needles {
		if needle != "" && strings.Contains(lower, needle) {
			return true
		}
	}
	return false
}

func (c *ICloudClient) CheckMailSession(ctx context.Context, session ICloudSession) error {
	if strings.TrimSpace(session.DSID) == "" || len(session.Cookies) == 0 {
		return errCode("icloud_session_missing", "未保存 iCloud 登录态，请先协议登录", true)
	}
	if _, err := mailGatewayBaseURL(session); err != nil {
		return err
	}
	_, err := c.mailFolders(ctx, session)
	return err
}

type mailFolder struct {
	ID           string `json:"identifier"`
	Name         string `json:"name"`
	MessageCount int    `json:"messageCount"`
}

type mailThread struct {
	ThreadID   string
	Subject    string
	Preview    string
	ReceivedAt time.Time
}

func (c *ICloudClient) mailFolders(ctx context.Context, session ICloudSession) ([]mailFolder, error) {
	var out struct {
		DomainObjects []mailFolder `json:"domainObjects"`
	}
	body := map[string]any{
		"domain":        "mailbox",
		"includeLabels": true,
		"predicate": map[string]any{
			"type": "eq",
			"expression": map[string]any{
				"type":     "property",
				"property": "isMboxDeleted",
			},
			"value": false,
		},
		"properties": []string{"identifier", "name", "uidValidity", "unseenCount", "seenDeletedCount", "unseenDeletedCount", "messageCount", "flags"},
	}
	if err := c.callMail(ctx, session, "/mailws2/v1/geqs/query", body, "fetchMailboxCountQuery", &out); err != nil {
		return nil, err
	}
	return out.DomainObjects, nil
}

func preferredMailFolders(folders []mailFolder) []mailFolder {
	if len(folders) == 0 {
		return []mailFolder{{Name: "INBOX"}}
	}
	var out []mailFolder
	seen := map[string]bool{}
	add := func(folder mailFolder) {
		name := strings.TrimSpace(folder.Name)
		if name == "" || seen[name] {
			return
		}
		seen[name] = true
		out = append(out, folder)
	}
	for _, folder := range folders {
		if folder.Name == "INBOX" || strings.HasPrefix(folder.Name, "INBOX/$category$_") {
			add(folder)
		}
	}
	for _, folder := range folders {
		if folder.MessageCount > 0 && !strings.Contains(folder.Name, "Deleted") && folder.Name != "Sent Messages" && folder.Name != "Drafts" {
			add(folder)
		}
	}
	if len(out) == 0 {
		out = append(out, mailFolder{Name: "INBOX"})
	}
	return out
}

func (c *ICloudClient) searchThreads(ctx context.Context, session ICloudSession, folder mailFolder, maxThreads int) ([]mailThread, error) {
	var out struct {
		ThreadList []struct {
			ThreadID  string          `json:"threadId"`
			Subject   string          `json:"subject"`
			Preview   string          `json:"preview"`
			Timestamp json.RawMessage `json:"timestamp"`
		} `json:"threadList"`
	}
	body := map[string]any{
		"responseType":        "THREAD_DIGEST",
		"includeFolderStatus": false,
		"maxResults":          maxThreads,
		"sessionHeaders":      mailSessionHeaders(folder.Name, false),
	}
	if err := c.callMail(ctx, session, "/mailws2/v1/thread/search", body, "", &out); err != nil {
		return nil, err
	}
	threads := make([]mailThread, 0, len(out.ThreadList))
	for _, item := range out.ThreadList {
		threads = append(threads, mailThread{
			ThreadID:   item.ThreadID,
			Subject:    item.Subject,
			Preview:    item.Preview,
			ReceivedAt: parseMailTime(item.Timestamp),
		})
	}
	return threads, nil
}

func (c *ICloudClient) threadMessages(ctx context.Context, session ICloudSession, folder mailFolder, threadID, alias string, after time.Time) ([]ICloudSyncedMessage, error) {
	messagesByAlias, err := c.threadMessagesForAliases(ctx, session, folder, threadID, map[string]string{"single": alias}, map[string]time.Time{"single": after})
	if err != nil {
		return nil, err
	}
	return messagesByAlias["single"], nil
}

func (c *ICloudClient) threadMessagesForAliases(ctx context.Context, session ICloudSession, folder mailFolder, threadID string, aliases map[string]string, afterByMailbox map[string]time.Time) (map[string][]ICloudSyncedMessage, error) {
	var out struct {
		MessageMetadataList []struct {
			UID       json.RawMessage `json:"uid"`
			Folder    string          `json:"folder"`
			MessageID string          `json:"messageId"`
			Subject   string          `json:"subject"`
			Preview   string          `json:"preview"`
			Date      json.RawMessage `json:"date"`
			From      json.RawMessage `json:"from"`
			To        json.RawMessage `json:"to"`
			CC        json.RawMessage `json:"cc"`
			BCC       json.RawMessage `json:"bcc"`
			Parts     []struct {
				PartID      string `json:"partId"`
				ContentType string `json:"contentType"`
				IsAttach    bool   `json:"isAttach"`
				FileName    string `json:"fileName"`
				Disposition string `json:"disposition"`
			} `json:"parts"`
		} `json:"messageMetadataList"`
	}
	body := map[string]any{
		"threadId":       threadID,
		"sessionHeaders": mailSessionHeaders(folder.Name, false),
	}
	if err := c.callMail(ctx, session, "/mailws2/v1/thread/get", body, "", &out); err != nil {
		return nil, err
	}
	messages := make(map[string][]ICloudSyncedMessage)
	for _, meta := range out.MessageMetadataList {
		uid := rawScalarString(meta.UID)
		if uid == "" {
			continue
		}
		receivedAt := firstNonZeroTime(parseMailTime(meta.Date), time.Now())
		folderName := firstNonEmpty(cleanMailFolder(meta.Folder), folder.Name)
		from := addressSummary(meta.From)
		recipients := string(meta.To) + "\n" + string(meta.CC) + "\n" + string(meta.BCC)
		bodyText := meta.Subject + "\n" + meta.Preview
		partIDs := textPartIDs(meta.Parts)
		if len(partIDs) > 0 {
			detail, err := c.messageBody(ctx, session, folderName, uid, partIDs)
			if err != nil {
				return messages, err
			}
			recipients += "\n" + detail.LongHeader
			bodyText += "\n" + detail.Body
		}
		matchedMailboxIDs := matchingMailboxIDs(recipients, aliases)
		if len(matchedMailboxIDs) == 0 {
			continue
		}
		message := ICloudSyncedMessage{
			RemoteID:   "icloud:" + folderName + ":" + uid,
			UID:        uid,
			Subject:    meta.Subject,
			From:       from,
			Body:       normalizeMailBody(bodyText),
			ReceivedAt: receivedAt,
		}
		for _, mailboxID := range matchedMailboxIDs {
			after := afterByMailbox[mailboxID]
			if !after.IsZero() && receivedAt.Before(after) {
				continue
			}
			messages[mailboxID] = append(messages[mailboxID], message)
		}
	}
	return messages, nil
}

func matchingMailboxIDs(recipients string, aliases map[string]string) []string {
	if len(aliases) == 0 {
		return nil
	}
	var ids []string
	for mailboxID, alias := range aliases {
		if strings.TrimSpace(mailboxID) == "" || strings.TrimSpace(alias) == "" {
			continue
		}
		if containsFold(recipients, alias) {
			ids = append(ids, mailboxID)
		}
	}
	sort.Strings(ids)
	return ids
}

type mailMessageDetail struct {
	LongHeader string
	Body       string
}

func (c *ICloudClient) messageBody(ctx context.Context, session ICloudSession, folderName, uid string, partIDs []string) (mailMessageDetail, error) {
	var out struct {
		LongHeader string `json:"longHeader"`
		Parts      []struct {
			GUID    string `json:"guid"`
			Content string `json:"content"`
		} `json:"parts"`
	}
	body := map[string]any{
		"uid":            uid,
		"parts":          partIDs,
		"dontMarkAsRead": true,
		"sessionHeaders": mailSessionHeaders(folderName, false),
	}
	if err := c.callMail(ctx, session, "/mailws2/v1/message/get", body, "", &out); err != nil {
		return mailMessageDetail{}, err
	}
	var parts []string
	for _, part := range out.Parts {
		parts = append(parts, part.Content)
	}
	return mailMessageDetail{LongHeader: out.LongHeader, Body: strings.Join(parts, "\n")}, nil
}

func (c *ICloudClient) MoveRemoteMessagesToTrash(ctx context.Context, session ICloudSession, remoteIDs []string) (ICloudMailCleanupResult, error) {
	var result ICloudMailCleanupResult
	if strings.TrimSpace(session.DSID) == "" || len(session.Cookies) == 0 {
		return result, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先协议登录", true)
	}
	folders, err := c.mailFolders(ctx, session)
	if err != nil {
		return result, err
	}
	trash, ok := trashMailFolder(folders)
	if !ok || strings.TrimSpace(trash.ID) == "" {
		return result, errCode("icloud_trash_not_found", "未找到 iCloud 废纸篓文件夹", true)
	}

	byFolder := make(map[string][]string)
	for _, remoteID := range remoteIDs {
		folderName, uid, ok := parseICloudRemoteID(remoteID)
		if !ok {
			result.Skipped++
			continue
		}
		if strings.EqualFold(folderName, trash.Name) {
			result.Skipped++
			continue
		}
		byFolder[folderName] = append(byFolder[folderName], uid)
	}
	for folderName, uids := range byFolder {
		folder, ok := findMailFolderByName(folders, folderName)
		if !ok || strings.TrimSpace(folder.ID) == "" {
			result.Skipped += len(uids)
			continue
		}
		ids, err := c.mailMessageIdentifiers(ctx, session, folder, uids)
		if err != nil {
			return result, err
		}
		for _, uid := range uniqueStrings(uids) {
			if strings.TrimSpace(ids[uid]) == "" {
				result.Skipped++
			}
		}
		moved, err := c.moveMailIdentifiersToTrash(ctx, session, mapValues(ids), trash.ID)
		result.MovedToTrash += moved
		if err != nil {
			return result, err
		}
	}
	return result, nil
}

func (c *ICloudClient) EmptyTrash(ctx context.Context, session ICloudSession) (int, error) {
	if strings.TrimSpace(session.DSID) == "" || len(session.Cookies) == 0 {
		return 0, errCode("icloud_session_missing", "未保存 iCloud 登录态，请先协议登录", true)
	}
	folders, err := c.mailFolders(ctx, session)
	if err != nil {
		return 0, err
	}
	trash, ok := trashMailFolder(folders)
	if !ok || strings.TrimSpace(trash.ID) == "" {
		return 0, errCode("icloud_trash_not_found", "未找到 iCloud 废纸篓文件夹", true)
	}

	total := 0
	for i := 0; i < 20; i++ {
		ids, err := c.mailFolderMessageIdentifiers(ctx, session, trash, 1000)
		if err != nil {
			return total, err
		}
		if len(ids) == 0 {
			return total, nil
		}
		destroyed, err := c.destroyMailIdentifiers(ctx, session, ids)
		total += destroyed
		if err != nil {
			return total, err
		}
		if len(ids) < 1000 {
			return total, nil
		}
	}
	return total, errCode("icloud_trash_not_empty", "废纸篓邮件过多，本次已分批清理一部分，请再点一次", true)
}

type mailEmailObject struct {
	UID        json.RawMessage `json:"uid"`
	Identifier string          `json:"identifier"`
	MboxRef    struct {
		ID string `json:"id"`
	} `json:"mboxRef"`
}

func (c *ICloudClient) mailMessageIdentifiers(ctx context.Context, session ICloudSession, folder mailFolder, uids []string) (map[string]string, error) {
	out := make(map[string]string)
	for _, chunk := range chunkStrings(uniqueStrings(uids), 200) {
		var resp struct {
			DomainObjects []mailEmailObject `json:"domainObjects"`
		}
		body := map[string]any{
			"domain": "email",
			"predicate": map[string]any{
				"type":       "in",
				"expression": map[string]any{"property": "uid"},
				"value":      mailUIDValues(chunk),
				"and": []any{
					map[string]any{
						"type": "eq",
						"expression": map[string]any{
							"type":     "fieldOf",
							"property": "flags",
							"value":    "DELETED",
						},
						"value": false,
					},
					map[string]any{
						"type":       "eq",
						"expression": map[string]any{"property": "mboxRef"},
						"value":      folder.ID,
					},
				},
			},
			"properties": []string{"uid", "identifier", "mboxRef"},
		}
		if err := c.callMail(ctx, session, "/mailws2/v1/message/list", body, "", &resp); err != nil {
			return out, err
		}
		for _, item := range resp.DomainObjects {
			uid := rawScalarString(item.UID)
			identifier := strings.TrimSpace(item.Identifier)
			if uid == "" || identifier == "" {
				continue
			}
			out[uid] = identifier
		}
	}
	return out, nil
}

func (c *ICloudClient) mailFolderMessageIdentifiers(ctx context.Context, session ICloudSession, folder mailFolder, limit int) ([]string, error) {
	if limit <= 0 || limit > 1000 {
		limit = 1000
	}
	var resp struct {
		DomainObjects []mailEmailObject `json:"domainObjects"`
	}
	body := map[string]any{
		"domain":     "email",
		"properties": []string{"uid", "identifier", "stateInternalDate", "mboxRef"},
		"limit":      limit,
		"predicate": map[string]any{
			"type":       "eq",
			"expression": map[string]any{"property": "mboxRef"},
			"value":      folder.ID,
			"and": []any{
				map[string]any{
					"type": "eq",
					"expression": map[string]any{
						"type":     "fieldOf",
						"property": "flags",
						"value":    "DELETED",
					},
					"value": false,
				},
			},
		},
		"orderby": map[string]any{
			"expressions": []any{map[string]any{"property": "stateInternalDate", "type": "property"}},
			"ascending":   false,
		},
	}
	if err := c.callMail(ctx, session, "/mailws2/v1/message/list", body, "", &resp); err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(resp.DomainObjects))
	for _, item := range resp.DomainObjects {
		if identifier := strings.TrimSpace(item.Identifier); identifier != "" {
			ids = append(ids, identifier)
		}
	}
	return ids, nil
}

type mailSetResponse struct {
	Updated      map[string]json.RawMessage `json:"updated"`
	Destroyed    []string                   `json:"destroyed"`
	NotUpdated   map[string]json.RawMessage `json:"notUpdated"`
	NotDestroyed map[string]json.RawMessage `json:"notDestroyed"`
}

func (c *ICloudClient) moveMailIdentifiersToTrash(ctx context.Context, session ICloudSession, identifiers []string, trashFolderID string) (int, error) {
	identifiers = uniqueStrings(identifiers)
	if len(identifiers) == 0 {
		return 0, nil
	}
	moved := 0
	for _, chunk := range chunkStrings(identifiers, 200) {
		var resp mailSetResponse
		body := map[string]any{
			"domain": "email",
			"batchUpdate": []any{
				map[string]any{
					"ids": chunk,
					"patch": map[string]any{
						"flags":   map[string]any{"set": []string{"seen"}},
						"mboxRef": map[string]any{"replace": []string{trashFolderID}},
					},
				},
			},
		}
		if err := c.callMail(ctx, session, "/mailws2/v1/email/set", body, "", &resp); err != nil {
			return moved, err
		}
		if len(resp.NotUpdated) > 0 {
			return moved + len(resp.Updated), fmt.Errorf("icloud mail move notUpdated=%d", len(resp.NotUpdated))
		}
		if len(resp.Updated) > 0 {
			moved += len(resp.Updated)
		} else {
			moved += len(chunk)
		}
	}
	return moved, nil
}

func (c *ICloudClient) destroyMailIdentifiers(ctx context.Context, session ICloudSession, identifiers []string) (int, error) {
	identifiers = uniqueStrings(identifiers)
	if len(identifiers) == 0 {
		return 0, nil
	}
	destroyed := 0
	for _, chunk := range chunkStrings(identifiers, 200) {
		var resp mailSetResponse
		body := map[string]any{
			"domain":  "email",
			"destroy": chunk,
		}
		if err := c.callMail(ctx, session, "/mailws2/v1/email/set", body, "", &resp); err != nil {
			return destroyed, err
		}
		if len(resp.NotDestroyed) > 0 {
			return destroyed + len(resp.Destroyed), fmt.Errorf("icloud mail destroy notDestroyed=%d", len(resp.NotDestroyed))
		}
		if len(resp.Destroyed) > 0 {
			destroyed += len(resp.Destroyed)
		} else {
			destroyed += len(chunk)
		}
	}
	return destroyed, nil
}

func parseICloudRemoteID(remoteID string) (string, string, bool) {
	rest, ok := strings.CutPrefix(strings.TrimSpace(remoteID), "icloud:")
	if !ok {
		return "", "", false
	}
	folderName, uid, ok := strings.Cut(rest, ":")
	if !ok || strings.TrimSpace(folderName) == "" || strings.TrimSpace(uid) == "" {
		return "", "", false
	}
	return strings.TrimSpace(folderName), strings.TrimSpace(uid), true
}

func findMailFolderByName(folders []mailFolder, name string) (mailFolder, bool) {
	name = strings.TrimSpace(name)
	for _, folder := range folders {
		if strings.EqualFold(strings.TrimSpace(folder.Name), name) {
			return folder, true
		}
	}
	return mailFolder{}, false
}

func trashMailFolder(folders []mailFolder) (mailFolder, bool) {
	for _, folder := range folders {
		name := strings.ToLower(strings.TrimSpace(folder.Name))
		if name == "deleted messages" || name == "trash" || strings.Contains(name, "deleted") || strings.Contains(name, "trash") || strings.Contains(name, "废纸") {
			return folder, true
		}
	}
	return mailFolder{}, false
}

func mailUIDValues(uids []string) []any {
	out := make([]any, 0, len(uids))
	for _, uid := range uids {
		uid = strings.TrimSpace(uid)
		if uid == "" {
			continue
		}
		if n, err := strconv.Atoi(uid); err == nil {
			out = append(out, n)
			continue
		}
		out = append(out, uid)
	}
	return out
}

func uniqueStrings(values []string) []string {
	seen := make(map[string]bool, len(values))
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" || seen[value] {
			continue
		}
		seen[value] = true
		out = append(out, value)
	}
	return out
}

func mapValues(values map[string]string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			out = append(out, value)
		}
	}
	sort.Strings(out)
	return out
}

func chunkStrings(values []string, size int) [][]string {
	if size <= 0 {
		size = len(values)
	}
	var chunks [][]string
	for len(values) > 0 {
		n := size
		if len(values) < n {
			n = len(values)
		}
		chunks = append(chunks, values[:n])
		values = values[n:]
	}
	return chunks
}

func (c *ICloudClient) callMail(ctx context.Context, session ICloudSession, path string, body any, clientIntent string, result any) error {
	base, err := mailGatewayBaseURL(session)
	if err != nil {
		return err
	}
	u, err := c.endpointWithBase(session, base, path)
	if err != nil {
		return err
	}
	if clientIntent != "" {
		parsed, _ := url.Parse(u)
		q := parsed.Query()
		q.Set("clientIntent", clientIntent)
		parsed.RawQuery = q.Encode()
		u = parsed.String()
	}
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, reader)
	if err != nil {
		return err
	}
	if clientIntent != "" {
		req.Header.Set("Content-Type", "text/plain;charset=UTF-8")
	} else {
		req.Header.Set("Content-Type", "application/json")
	}
	setICloudFetchHeaders(req, session, "*/*", req.Header.Get("Content-Type"))
	if cookie := cookieHeader(session.Cookies, u); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("icloud mail %s HTTP %d: %s", path, resp.StatusCode, trimForError(data))
	}
	if result != nil {
		if err := json.Unmarshal(data, result); err != nil {
			return errCode("icloud_mail_bad_response", "iCloud 邮件返回无法解析", true)
		}
	}
	return nil
}

func (c *ICloudClient) endpoint(session ICloudSession, path string) (string, error) {
	return c.endpointWithBase(session, session.PremiumMailBaseURL, path)
}

func (c *ICloudClient) endpointWithBase(session ICloudSession, baseURL, path string) (string, error) {
	base, err := url.Parse(strings.TrimRight(baseURL, "/") + "/")
	if err != nil {
		return "", err
	}
	rel := &url.URL{Path: strings.TrimLeft(path, "/")}
	u := base.ResolveReference(rel)
	q := u.Query()
	q.Set("clientBuildNumber", firstNonEmpty(session.ClientBuildNumber, "2622Build20"))
	q.Set("clientMasteringNumber", firstNonEmpty(session.MasteringNumber, session.ClientBuildNumber, "2622Build20"))
	q.Set("clientId", firstNonEmpty(session.ClientID, "local-panel"))
	q.Set("dsid", session.DSID)
	u.RawQuery = q.Encode()
	return u.String(), nil
}

func mailGatewayBaseURL(session ICloudSession) (string, error) {
	if strings.TrimSpace(session.MailGatewayBaseURL) != "" {
		return strings.TrimRight(strings.TrimSpace(session.MailGatewayBaseURL), "/"), nil
	}
	if strings.TrimSpace(session.MailBaseURL) != "" {
		base := strings.TrimRight(strings.TrimSpace(session.MailBaseURL), "/")
		base = strings.Replace(base, "-mailws.", "-mccgateway.", 1)
		return base, nil
	}
	if strings.TrimSpace(session.PremiumMailBaseURL) != "" {
		base := strings.TrimRight(strings.TrimSpace(session.PremiumMailBaseURL), "/")
		base = strings.Replace(base, "-maildomainws.", "-mccgateway.", 1)
		return base, nil
	}
	return "", errCode("icloud_mail_missing", "未保存 iCloud 邮件服务地址，请重新保存登录态", true)
}

func mailSessionHeaders(folder string, reset bool) map[string]any {
	headers := map[string]any{
		"folder":       folder,
		"modseq":       nil,
		"threadmodseq": nil,
		"condstore":    1,
		"qresync":      1,
		"threadmode":   1,
	}
	if reset {
		headers["modseq"] = nil
		headers["threadmodseq"] = nil
	}
	return headers
}

func textPartIDs(parts []struct {
	PartID      string `json:"partId"`
	ContentType string `json:"contentType"`
	IsAttach    bool   `json:"isAttach"`
	FileName    string `json:"fileName"`
	Disposition string `json:"disposition"`
}) []string {
	var ids []string
	for _, part := range parts {
		if part.PartID == "" || part.IsAttach || part.FileName != "" {
			continue
		}
		contentType := strings.ToLower(part.ContentType)
		if strings.Contains(contentType, "text/plain") || strings.Contains(contentType, "text/html") {
			ids = append(ids, strings.TrimSpace(part.PartID))
		}
	}
	return ids
}

func parseMailTime(raw json.RawMessage) time.Time {
	value := strings.Trim(strings.TrimSpace(string(raw)), `"`)
	if value == "" || value == "null" {
		return time.Time{}
	}
	if n, err := strconv.ParseInt(value, 10, 64); err == nil {
		if n > 1_000_000_000_000 {
			return time.UnixMilli(n)
		}
		if n > 1_000_000_000 {
			return time.Unix(n, 0)
		}
	}
	if f, err := strconv.ParseFloat(value, 64); err == nil {
		n := int64(f)
		if n > 1_000_000_000_000 {
			return time.UnixMilli(n)
		}
		if n > 1_000_000_000 {
			return time.Unix(n, 0)
		}
	}
	for _, layout := range []string{time.RFC3339, time.RFC1123Z, time.RFC1123, time.RFC822Z, time.RFC822} {
		if t, err := time.Parse(layout, value); err == nil {
			return t
		}
	}
	return time.Time{}
}

func rawScalarString(raw json.RawMessage) string {
	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		return strings.TrimSpace(text)
	}
	var number json.Number
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	if err := decoder.Decode(&number); err == nil {
		return number.String()
	}
	return strings.Trim(strings.TrimSpace(string(raw)), `"`)
}

func addressSummary(raw json.RawMessage) string {
	var values []any
	if err := json.Unmarshal(raw, &values); err != nil {
		return strings.Trim(strings.TrimSpace(string(raw)), `"`)
	}
	var out []string
	for _, value := range values {
		switch v := value.(type) {
		case string:
			out = append(out, v)
		case map[string]any:
			email, _ := v["email"].(string)
			name, _ := v["name"].(string)
			out = append(out, strings.TrimSpace(name+" <"+email+">"))
		}
	}
	return strings.Join(out, ", ")
}

func cleanMailFolder(value string) string {
	value = strings.TrimSpace(value)
	if idx := strings.Index(value, ":"); idx >= 0 && idx+1 < len(value) {
		return value[idx+1:]
	}
	return value
}

func containsFold(text, needle string) bool {
	return strings.Contains(strings.ToLower(text), strings.ToLower(strings.TrimSpace(needle)))
}

var htmlTagRegex = regexp.MustCompile(`<[^>]+>`)

func normalizeMailBody(value string) string {
	value = html.UnescapeString(value)
	value = htmlTagRegex.ReplaceAllString(value, " ")
	value = strings.Join(strings.Fields(value), " ")
	return value
}

func cookieHeader(cookies []SessionCookie, rawURL string) string {
	u, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	host := strings.ToLower(u.Hostname())
	path := u.EscapedPath()
	if path == "" {
		path = "/"
	}
	nowUnix := float64(time.Now().Unix())
	type cookiePair struct {
		name  string
		value string
		order int
		index int
	}
	var pairs []cookiePair
	for i, cookie := range cookies {
		if cookie.Name == "" || cookie.Value == "" {
			continue
		}
		if cookie.Expires > 0 && cookie.Expires < nowUnix {
			continue
		}
		if !cookieDomainMatch(host, cookie.Domain) {
			continue
		}
		if cookie.Path != "" && !strings.HasPrefix(path, cookie.Path) {
			continue
		}
		pairs = append(pairs, cookiePair{
			name:  cookie.Name,
			value: cookie.Value,
			order: preferredICloudCookieOrder(cookie.Name),
			index: i,
		})
	}
	sort.SliceStable(pairs, func(i, j int) bool {
		if pairs[i].order != pairs[j].order {
			return pairs[i].order < pairs[j].order
		}
		return pairs[i].index < pairs[j].index
	})
	out := make([]string, 0, len(pairs))
	for _, pair := range pairs {
		out = append(out, pair.name+"="+pair.value)
	}
	return strings.Join(out, "; ")
}

func mergeSessionCookies(cookies *[]SessionCookie, requestURL *url.URL, setCookies []*http.Cookie) {
	if cookies == nil {
		return
	}
	for _, c := range setCookies {
		domain := strings.TrimSpace(c.Domain)
		if domain == "" && requestURL != nil {
			domain = requestURL.Hostname()
		}
		path := c.Path
		if path == "" {
			path = "/"
		}
		next := SessionCookie{
			Name:     c.Name,
			Value:    c.Value,
			Domain:   domain,
			Path:     path,
			Secure:   c.Secure,
			HTTPOnly: c.HttpOnly,
		}
		if !c.Expires.IsZero() {
			next.Expires = float64(c.Expires.Unix())
		}
		replaced := false
		for i, old := range *cookies {
			if old.Name == next.Name && strings.EqualFold(strings.TrimPrefix(old.Domain, "."), strings.TrimPrefix(next.Domain, ".")) && old.Path == next.Path {
				if next.Value == "" {
					*cookies = append((*cookies)[:i], (*cookies)[i+1:]...)
				} else {
					(*cookies)[i] = next
				}
				replaced = true
				break
			}
		}
		if !replaced && next.Value != "" {
			*cookies = append(*cookies, next)
		}
	}
}

func preferredICloudCookieOrder(name string) int {
	for i, candidate := range preferredICloudCookies {
		if candidate == "X_APPLE_WEB_KB-" && strings.HasPrefix(name, candidate) {
			return i
		}
		if candidate != "X_APPLE_WEB_KB-" && name == candidate {
			return i
		}
	}
	return len(preferredICloudCookies) + 100
}

var preferredICloudCookies = []string{
	"X-APPLE-UNIQUE-CLIENT-ID",
	"X-APPLE-WEBAUTH-USER",
	"X_APPLE_WEB_KB-",
	"X-Apple-GCBD-Cookie",
	"X-APPLE-WEBAUTH-HSA-TRUST",
	"X-APPLE-WEBAUTH-PCS-Documents",
	"X-APPLE-WEBAUTH-PCS-Photos",
	"X-APPLE-WEBAUTH-PCS-Cloudkit",
	"X-APPLE-WEBAUTH-PCS-Safari",
	"X-APPLE-WEBAUTH-PCS-Mail",
	"X-APPLE-WEBAUTH-PCS-Notes",
	"X-APPLE-WEBAUTH-PCS-News",
	"X-APPLE-WEBAUTH-PCS-Sharing",
	"X-APPLE-WEBAUTH-LOGIN",
	"X-APPLE-DS-WEB-SESSION-TOKEN",
	"X-APPLE-WEB-ID",
	"X-APPLE-WEBAUTH-VALIDATE",
	"X-APPLE-WEBAUTH-TOKEN",
}

func cookieDomainMatch(host, domain string) bool {
	domain = strings.TrimPrefix(strings.ToLower(strings.TrimSpace(domain)), ".")
	if domain == "" {
		return false
	}
	return host == domain || strings.HasSuffix(host, "."+domain)
}

func trimForError(data []byte) string {
	text := strings.TrimSpace(string(data))
	if len(text) > 240 {
		return text[:240] + "..."
	}
	return text
}

func appleDebugBody(data []byte) string {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 {
		return ""
	}
	var value any
	if err := json.Unmarshal(trimmed, &value); err == nil {
		redacted := redactAppleDebugJSON(value)
		var buf bytes.Buffer
		encoder := json.NewEncoder(&buf)
		encoder.SetEscapeHTML(false)
		if err := encoder.Encode(redacted); err == nil {
			return trimForError(bytes.TrimSpace(buf.Bytes()))
		}
	}
	return trimForError(trimmed)
}

func redactAppleDebugJSON(value any) any {
	switch v := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(v))
		for key, item := range v {
			if appleDebugSecretKey(key) {
				out[key] = "<redacted>"
				continue
			}
			out[key] = redactAppleDebugJSON(item)
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = redactAppleDebugJSON(item)
		}
		return out
	default:
		return value
	}
}

func appleDebugSecretKey(key string) bool {
	key = strings.ToLower(strings.TrimSpace(key))
	for _, marker := range []string{"apikey", "api_key", "token", "secret", "password", "scnt", "session", "email", "accountname", "appleid", "dsid"} {
		if strings.Contains(key, marker) {
			return true
		}
	}
	return false
}

func setICloudFetchHeaders(req *http.Request, session ICloudSession, accept, contentType string) {
	if strings.TrimSpace(accept) == "" {
		accept = "*/*"
	}
	req.Header.Set("Accept", accept)
	if strings.TrimSpace(contentType) != "" {
		req.Header.Set("Content-Type", contentType)
	}
	origin := iCloudOrigin(session)
	req.Header.Set("Origin", origin)
	req.Header.Set("Referer", origin+"/")
	req.Header.Set("Sec-Fetch-Site", "same-site")
	req.Header.Set("Sec-Fetch-Mode", "cors")
	req.Header.Set("Sec-Fetch-Dest", "empty")
	req.Header.Set("Sec-CH-UA-Platform", `"Windows"`)
	req.Header.Set("Sec-CH-UA", `"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"`)
	req.Header.Set("Sec-CH-UA-Mobile", "?0")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
}

func iCloudOrigin(session ICloudSession) string {
	host := strings.ToLower(session.Host)
	if strings.Contains(host, "icloud.com.cn") {
		return "https://www.icloud.com.cn"
	}
	return "https://www.icloud.com"
}
