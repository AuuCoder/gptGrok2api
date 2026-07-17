package app

import (
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	appleAuthUserAgent              = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.1 Safari/605.1.15"
	defaultAppleOAuthClientID       = "d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d"
	appleAccountManageOAuthClientID = "af1139274f266b22b68c2a3e7ad932cb3c0bbe854e13a79af78dcc73136882c3"
	appleHashcashMaxBits            = 24
	appleHashcashMaxAttempts        = 1 << 24
)

const (
	appleTwoFactorMethodTrustedDevice = "trusted_device"
	appleTwoFactorMethodPhone         = "phone"
)

func normalizeAppleTwoFactorMethod(method string) string {
	switch strings.ToLower(strings.TrimSpace(method)) {
	case appleTwoFactorMethodPhone, "sms", "phone_sms", "trusted_phone", "trusted_phone_number":
		return appleTwoFactorMethodPhone
	default:
		return appleTwoFactorMethodTrustedDevice
	}
}

type AppleAuthClient struct {
	httpClient *http.Client
}

type appleAuthEndpoints struct {
	Home  string
	Setup string
	Auth  string
	Host  string
}

type appleAuthSession struct {
	Endpoints           appleAuthEndpoints
	AppleID             string
	ClientID            string
	FrameID             string
	UserAgent           string
	SessionToken        string
	Scnt                string
	ManageScnt          string
	SessionID           string
	AccountCountry      string
	TrustToken          string
	AuthAttributes      string
	HCBits              int
	HCChallenge         string
	CompleteHCBits      int
	CompleteHCChallenge string
	TwoFactorPhone      json.RawMessage
	TwoFactorMethod     string
	Cookies             []SessionCookie
}

type appleSRPInitResponse struct {
	Iteration int    `json:"iteration"`
	Salt      string `json:"salt"`
	Protocol  string `json:"protocol"`
	B         string `json:"b"`
	C         string `json:"c"`
}

type appleAuthStartResult struct {
	Session   ICloudSession
	PendingID string
	Needs2FA  bool
	Message   string
	AppleID   string
	ExpiresAt time.Time
}

type appleDomainRedirectError struct {
	DomainToUse string
	Host        string
}

func (e appleDomainRedirectError) Error() string {
	if e.Host != "" {
		return "Apple 要求切换 iCloud 域：" + e.Host
	}
	return "Apple 要求切换 iCloud 域：" + e.DomainToUse
}

type appleAccountInfo struct {
	DSInfo struct {
		DSID                            string `json:"dsid"`
		AppleID                         string `json:"appleId"`
		PrimaryEmail                    string `json:"primaryEmail"`
		IsHideMyEmailSubscriptionActive bool   `json:"isHideMyEmailSubscriptionActive"`
		IsHideMyEmailFeatureAvailable   bool   `json:"isHideMyEmailFeatureAvailable"`
		HsaVersion                      int    `json:"hsaVersion"`
	} `json:"dsInfo"`
	Webservices map[string]struct {
		URL         string `json:"url"`
		Status      string `json:"status"`
		PcsRequired bool   `json:"pcsRequired"`
	} `json:"webservices"`
	HsaChallengeRequired bool `json:"hsaChallengeRequired"`
}

type appleAuthPending struct {
	ID        string
	Session   *appleAuthSession
	CreatedAt time.Time
	ExpiresAt time.Time
}

type appleAuthPendingStore struct {
	mu    sync.Mutex
	items map[string]appleAuthPending
}

func NewAppleAuthClient() *AppleAuthClient {
	return &AppleAuthClient{httpClient: &http.Client{Timeout: 30 * time.Second}}
}

func newAppleAuthPendingStore() *appleAuthPendingStore {
	return &appleAuthPendingStore{items: make(map[string]appleAuthPending)}
}

func (s *appleAuthPendingStore) put(session *appleAuthSession) (appleAuthPending, error) {
	id, err := randomToken(18)
	if err != nil {
		return appleAuthPending{}, err
	}
	now := time.Now()
	pending := appleAuthPending{
		ID:        id,
		Session:   session,
		CreatedAt: now,
		ExpiresAt: now.Add(10 * time.Minute),
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cleanupLocked(now)
	s.items[id] = pending
	return pending, nil
}

func (s *appleAuthPendingStore) get(id string) (appleAuthPending, bool) {
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cleanupLocked(now)
	pending, ok := s.items[strings.TrimSpace(id)]
	return pending, ok
}

func (s *appleAuthPendingStore) delete(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.items, strings.TrimSpace(id))
}

func (s *appleAuthPendingStore) cleanupLocked(now time.Time) {
	for id, pending := range s.items {
		if now.After(pending.ExpiresAt) {
			delete(s.items, id)
		}
	}
}

func (c *AppleAuthClient) StartLogin(ctx context.Context, appleID, password, defaultHost, clientID string, pendingStore *appleAuthPendingStore, twoFactorMethod string) (appleAuthStartResult, error) {
	appleID = strings.ToLower(strings.TrimSpace(appleID))
	if appleID == "" || strings.TrimSpace(password) == "" {
		return appleAuthStartResult{}, errCode("apple_credentials_missing", "缺少 Apple ID 或密码", false)
	}
	method := normalizeAppleTwoFactorMethod(twoFactorMethod)
	result, err := c.startLoginOnHost(ctx, appleID, password, defaultHost, clientID, pendingStore, method)
	if err == nil {
		return result, nil
	}
	var redirect appleDomainRedirectError
	if !errors.As(err, &redirect) || redirect.Host == "" {
		return appleAuthStartResult{}, err
	}
	currentHost := appleAuthEndpointsForHost(defaultHost).Host
	nextHost := appleAuthEndpointsForHost(redirect.Host).Host
	if currentHost == nextHost {
		return appleAuthStartResult{}, err
	}
	return c.startLoginOnHost(ctx, appleID, password, nextHost, clientID, pendingStore, method)
}

func (c *AppleAuthClient) StartAppleAccountManageLogin(ctx context.Context, appleID, password string, pendingStore *appleAuthPendingStore, twoFactorMethod string) (appleAuthStartResult, error) {
	appleID = strings.ToLower(strings.TrimSpace(appleID))
	if appleID == "" || strings.TrimSpace(password) == "" {
		return appleAuthStartResult{}, errCode("apple_credentials_missing", "缺少 Apple ID 或密码", false)
	}
	method := normalizeAppleTwoFactorMethod(twoFactorMethod)
	frameID, err := randomUUID()
	if err != nil {
		return appleAuthStartResult{}, err
	}
	session := &appleAuthSession{
		Endpoints: appleAccountManageAuthEndpoints(),
		AppleID:   appleID,
		ClientID:  appleAccountManageOAuthClientID,
		FrameID:   strings.ToLower(frameID),
		UserAgent: appleAccountManageUserAgent,
	}
	if err := c.primeAppleAccountManageState(ctx, session); err != nil {
		return appleAuthStartResult{}, err
	}
	if err := c.authStart(ctx, session); err != nil {
		return appleAuthStartResult{}, err
	}
	if err := c.authDeviceKeyChallenge(ctx, session); err != nil {
		return appleAuthStartResult{}, err
	}
	if err := c.authFederate(ctx, session); err != nil {
		return appleAuthStartResult{}, err
	}
	needs2FA, err := c.authSRP(ctx, session, password)
	if err != nil {
		return appleAuthStartResult{}, err
	}
	if needs2FA {
		session.TwoFactorMethod = method
		message := "Apple Account 已向受信任设备发送验证码；收到 6 位验证码后提交"
		if err := c.refreshAuthState(ctx, session); err != nil {
			message = "Apple Account 已要求 2FA；请查看受信任设备验证码"
		}
		if method == appleTwoFactorMethodPhone {
			if err := c.requestPhoneSecurityCode(ctx, session, nil); err != nil {
				return appleAuthStartResult{}, err
			}
			message = "Apple Account 已向受信任手机号发送短信验证码；收到 6 位验证码后提交"
		}
		pending, err := pendingStore.put(session)
		if err != nil {
			return appleAuthStartResult{}, err
		}
		return appleAuthStartResult{
			PendingID: pending.ID,
			Needs2FA:  true,
			Message:   message,
			AppleID:   strings.TrimSpace(appleID),
			ExpiresAt: pending.ExpiresAt,
		}, nil
	}
	icloudSession, err := c.authWithAppleAccountManage(ctx, session)
	if err != nil {
		return appleAuthStartResult{}, err
	}
	return appleAuthStartResult{
		Session:  icloudSession,
		Needs2FA: false,
		Message:  "Apple Account 管理态登录成功",
		AppleID:  strings.TrimSpace(appleID),
	}, nil
}

func (c *AppleAuthClient) startLoginOnHost(ctx context.Context, appleID, password, host, clientID string, pendingStore *appleAuthPendingStore, twoFactorMethod string) (appleAuthStartResult, error) {
	frameID, err := randomUUID()
	if err != nil {
		return appleAuthStartResult{}, err
	}
	session := &appleAuthSession{
		Endpoints: appleAuthEndpointsForHost(host),
		AppleID:   appleID,
		ClientID:  firstNonEmpty(clientID, defaultAppleOAuthClientID),
		FrameID:   strings.ToLower(frameID),
	}
	if err := c.authStart(ctx, session); err != nil {
		return appleAuthStartResult{}, err
	}
	if err := c.authFederate(ctx, session); err != nil {
		return appleAuthStartResult{}, err
	}
	needs2FA, err := c.authSRP(ctx, session, password)
	if err != nil {
		return appleAuthStartResult{}, err
	}
	if redirect, ok := session.redirectForAccountCountry(); ok {
		return appleAuthStartResult{}, redirect
	}
	if session.SessionToken == "" {
		return appleAuthStartResult{}, errCode("apple_session_token_missing", "Apple 登录未返回 Session Token，请重新协议登录或检查账号安全状态", true)
	}
	if needs2FA {
		session.TwoFactorMethod = normalizeAppleTwoFactorMethod(twoFactorMethod)
		message := "已触发 Apple 2FA，请在受信任设备允许后输入 6 位验证码"
		if session.TwoFactorMethod == appleTwoFactorMethodPhone {
			_ = c.refreshAuthState(ctx, session)
			if err := c.requestPhoneSecurityCode(ctx, session, nil); err != nil {
				return appleAuthStartResult{}, err
			}
			message = "已向受信任手机号发送短信验证码，请输入 6 位验证码"
		} else if err := c.requestTrustedDeviceCode(ctx, session); err != nil {
			var redirect appleDomainRedirectError
			if errors.As(err, &redirect) {
				return appleAuthStartResult{}, err
			}
			message = "Apple 已要求 2FA；自动触发验证码未确认，请查看受信任设备后输入验证码"
		}
		pending, err := pendingStore.put(session)
		if err != nil {
			return appleAuthStartResult{}, err
		}
		return appleAuthStartResult{
			PendingID: pending.ID,
			Needs2FA:  true,
			Message:   message,
			AppleID:   strings.TrimSpace(appleID),
			ExpiresAt: pending.ExpiresAt,
		}, nil
	}

	icloudSession, err := c.authWithTokenAndValidate(ctx, session)
	if err != nil {
		var redirect appleDomainRedirectError
		if errors.As(err, &redirect) {
			return appleAuthStartResult{}, err
		}
		pending, putErr := pendingStore.put(session)
		if putErr != nil {
			return appleAuthStartResult{}, putErr
		}
		return appleAuthStartResult{
			PendingID: pending.ID,
			Needs2FA:  true,
			Message:   "登录已进入二次验证；如果设备已弹码，请输入验证码继续",
			AppleID:   strings.TrimSpace(appleID),
			ExpiresAt: pending.ExpiresAt,
		}, nil
	}
	return appleAuthStartResult{
		Session:  icloudSession,
		Needs2FA: false,
		Message:  "Apple 协议登录成功，登录态已生成",
		AppleID:  strings.TrimSpace(appleID),
	}, nil
}

func (c *AppleAuthClient) Submit2FA(ctx context.Context, pending appleAuthPending, code string) (ICloudSession, error) {
	code = strings.TrimSpace(code)
	if len(code) != 6 {
		return ICloudSession{}, errCode("invalid_2fa_code", "2FA 验证码必须是 6 位", false)
	}
	session := pending.Session
	for attempt := 0; attempt < 2; attempt++ {
		icloudSession, err := c.submit2FAWithSession(ctx, session, code)
		if err == nil {
			return icloudSession, nil
		}
		var redirect appleDomainRedirectError
		if !errors.As(err, &redirect) || !session.switchHost(redirect.Host) {
			return ICloudSession{}, err
		}
	}
	return ICloudSession{}, errCode("apple_domain_switch_failed", "Apple 登录域切换后仍未完成 2FA，请重新发起协议登录", true)
}

func (c *AppleAuthClient) SubmitAppleAccountManage2FA(ctx context.Context, pending appleAuthPending, code string, phoneNumber json.RawMessage) (ICloudSession, error) {
	code = strings.TrimSpace(code)
	if len(code) != 6 {
		return ICloudSession{}, errCode("invalid_2fa_code", "2FA 验证码必须是 6 位", false)
	}
	session := pending.Session
	switch session.TwoFactorMethod {
	case appleTwoFactorMethodPhone:
		if err := c.validatePhoneSecurityCode(ctx, session, code, phoneNumber); err != nil {
			return ICloudSession{}, err
		}
	default:
		if err := c.validateTrustedDeviceCode(ctx, session, code); err != nil {
			return ICloudSession{}, err
		}
	}
	if err := c.trustSession(ctx, session); err != nil && os.Getenv("IPM_DEBUG_APPLE_ACCOUNT") == "1" {
		fmt.Fprintf(os.Stderr, "APPLE_ACCOUNT_TRUST_DEBUG status=skipped err=%s\n", err.Error())
	}
	return c.authWithAppleAccountManage(ctx, session)
}

func (c *AppleAuthClient) submit2FAWithSession(ctx context.Context, session *appleAuthSession, code string) (ICloudSession, error) {
	switch session.TwoFactorMethod {
	case appleTwoFactorMethodPhone:
		if err := c.validatePhoneSecurityCode(ctx, session, code, nil); err != nil {
			return ICloudSession{}, err
		}
	default:
		if err := c.validateTrustedDeviceCode(ctx, session, code); err != nil {
			return ICloudSession{}, err
		}
	}
	if err := c.trustSession(ctx, session); err != nil {
		return ICloudSession{}, err
	}
	return c.authWithTokenAndValidate(ctx, session)
}

func appleAuthEndpointsForHost(host string) appleAuthEndpoints {
	host = strings.ToLower(strings.TrimSpace(host))
	if strings.Contains(host, "icloud.com.cn") {
		return appleAuthEndpoints{
			Home:  "https://www.icloud.com.cn",
			Setup: "https://setup.icloud.com.cn/setup/ws/1",
			Auth:  "https://idmsa.apple.com.cn/appleauth/auth",
			Host:  "www.icloud.com.cn",
		}
	}
	return appleAuthEndpoints{
		Home:  "https://www.icloud.com",
		Setup: "https://setup.icloud.com/setup/ws/1",
		Auth:  "https://idmsa.apple.com/appleauth/auth",
		Host:  "www.icloud.com",
	}
}

func appleAccountManageAuthEndpoints() appleAuthEndpoints {
	return appleAuthEndpoints{
		Home: "https://account.apple.com",
		Auth: "https://idmsa.apple.com/appleauth/auth",
		Host: "appleid.apple.com",
	}
}

func appleDomainToHost(domain string) string {
	domain = strings.ToLower(strings.TrimSpace(domain))
	domain = strings.TrimPrefix(domain, "https://")
	domain = strings.TrimPrefix(domain, "http://")
	domain = strings.TrimPrefix(domain, "www.")
	domain = strings.TrimSuffix(domain, "/")
	if strings.Contains(domain, "icloud.com.cn") {
		return "www.icloud.com.cn"
	}
	if strings.Contains(domain, "icloud.com") {
		return "www.icloud.com"
	}
	return ""
}

func appleHostForAccountCountry(country string) string {
	country = strings.ToUpper(strings.TrimSpace(country))
	if country == "" {
		return ""
	}
	if country == "CHN" || country == "CN" {
		return "www.icloud.com.cn"
	}
	return "www.icloud.com"
}

func parseAppleDomainRedirect(status int, data []byte) (appleDomainRedirectError, bool) {
	if status < 300 || status >= 400 {
		return appleDomainRedirectError{}, false
	}
	var payload struct {
		DomainToUse string `json:"domainToUse"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(data), &payload); err != nil {
		return appleDomainRedirectError{}, false
	}
	host := appleDomainToHost(payload.DomainToUse)
	if host == "" {
		return appleDomainRedirectError{}, false
	}
	return appleDomainRedirectError{DomainToUse: payload.DomainToUse, Host: host}, true
}

func (c *AppleAuthClient) authStart(ctx context.Context, session *appleAuthSession) error {
	frameTag := "auth-" + session.FrameID
	u, err := url.Parse(session.Endpoints.Auth + "/authorize/signin")
	if err != nil {
		return err
	}
	q := u.Query()
	q.Set("frame_id", frameTag)
	q.Set("skVersion", "7")
	q.Set("iframeId", frameTag)
	q.Set("client_id", session.ClientID)
	q.Set("redirect_uri", session.Endpoints.Home)
	q.Set("response_type", "code")
	q.Set("response_mode", "web_message")
	q.Set("state", frameTag)
	headers := map[string]string{"Accept": "*/*"}
	if session.isAppleAccountManage() {
		q.Set("authVersion", "8.0.2")
		headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
		headers["Referer"] = session.Endpoints.Home + "/"
		applyAppleAccountBrowserHints(headers)
		headers["Sec-Fetch-Dest"] = "iframe"
		headers["Sec-Fetch-Mode"] = "navigate"
		headers["Sec-Fetch-Site"] = "same-site"
	} else {
		q.Set("language", "zh_CN")
		q.Set("authVersion", "latest")
	}
	u.RawQuery = q.Encode()
	_, _, err = c.doWithTransientRetry(ctx, session, http.MethodGet, u.String(), headers, nil, nil, false)
	if err != nil {
		return err
	}
	if session.isAppleAccountManage() {
		session.rememberCompleteHashcashChallenge()
	}
	return nil
}

func (c *AppleAuthClient) authDeviceKeyChallenge(ctx context.Context, session *appleAuthSession) error {
	if !session.isAppleAccountManage() {
		return nil
	}
	body := map[string]bool{"passkeyAutofill": false}
	headers := session.srpHeaders()
	delete(headers, "scnt")
	delete(headers, "X-Apple-ID-Session-Id")
	delete(headers, "X-Apple-App-Id")
	_, _, err := c.doWithTransientRetry(ctx, session, http.MethodPost, session.Endpoints.Auth+"/verify/device/key/challenge", headers, body, nil, false)
	return err
}

func (c *AppleAuthClient) authFederate(ctx context.Context, session *appleAuthSession) error {
	u := session.Endpoints.Auth + "/federate?isRememberMeEnabled=true"
	body := map[string]any{"accountName": session.AppleID, "rememberMe": true}
	_, _, err := c.doWithTransientRetry(ctx, session, http.MethodPost, u, session.srpHeaders(), body, nil, false)
	return err
}

func (c *AppleAuthClient) authSRP(ctx context.Context, session *appleAuthSession, password string) (bool, error) {
	srp, err := newSRPClient()
	if err != nil {
		return false, err
	}
	var initResp appleSRPInitResponse
	initBody := map[string]any{
		"a":           base64.StdEncoding.EncodeToString(srp.ABytes()),
		"accountName": session.AppleID,
		"protocols":   []string{"s2k", "s2k_fo"},
	}
	if _, _, err := c.doWithTransientRetry(ctx, session, http.MethodPost, session.Endpoints.Auth+"/signin/init", session.srpHeaders(), initBody, &initResp, false); err != nil {
		return false, err
	}
	serverB, err := base64.StdEncoding.DecodeString(initResp.B)
	if err != nil {
		return false, fmt.Errorf("decode SRP B: %w", err)
	}
	salt, err := base64.StdEncoding.DecodeString(initResp.Salt)
	if err != nil {
		return false, fmt.Errorf("decode SRP salt: %w", err)
	}
	derived, err := deriveAppleSRPPassword(password, salt, initResp.Iteration, initResp.Protocol)
	if err != nil {
		return false, err
	}
	if err := srp.processChallenge([]byte(session.AppleID), derived, salt, serverB); err != nil {
		return false, err
	}
	completeBody := map[string]any{
		"accountName": session.AppleID,
		"m1":          base64.StdEncoding.EncodeToString(srp.M1),
		"m2":          base64.StdEncoding.EncodeToString(srp.M2),
		"c":           initResp.C,
		"rememberMe":  true,
	}
	if !session.isAppleAccountManage() {
		completeBody["trustTokens"] = []string{}
		if session.TrustToken != "" {
			completeBody["trustTokens"] = []string{session.TrustToken}
		}
	}
	headers := session.srpHeaders()
	if session.isAppleAccountManage() {
		bits, challenge := session.completeHashcashChallenge()
		hc, err := generateAppleHashcash(bits, challenge, time.Now())
		if err != nil {
			return false, err
		}
		headers["X-Apple-HC"] = hc
	}
	status, _, err := c.doWithTransientRetry(ctx, session, http.MethodPost, session.Endpoints.Auth+"/signin/complete?isRememberMeEnabled=true", headers, completeBody, nil, true)
	if status == http.StatusUnauthorized {
		return false, errCode("apple_credentials_invalid", "Apple ID 或密码错误，请检查后重新协议登录", false)
	}
	return status == http.StatusConflict, err
}

func generateAppleHashcash(bits int, challenge string, now time.Time) (string, error) {
	challenge = strings.TrimSpace(challenge)
	if bits <= 0 || challenge == "" {
		return "", errCode("apple_hc_missing", "Apple Account 缺少动态验证挑战，请重新协议登录", true)
	}
	if bits > appleHashcashMaxBits {
		return "", errCode("apple_hc_too_hard", "Apple Account 动态验证难度过高，请稍后重试", true)
	}
	prefix := fmt.Sprintf("1:%d:%s:%s::", bits, now.UTC().Format("20060102150405"), challenge)
	for counter := int64(0); counter < appleHashcashMaxAttempts; counter++ {
		value := prefix + strconv.FormatInt(counter, 36)
		sum := sha1.Sum([]byte(value))
		if leadingZeroBits(sum[:]) >= bits {
			return value, nil
		}
	}
	return "", errCode("apple_hc_failed", "Apple Account 动态验证生成失败，请稍后重试", true)
}

func leadingZeroBits(data []byte) int {
	total := 0
	for _, b := range data {
		for bit := 7; bit >= 0; bit-- {
			if b&(1<<bit) != 0 {
				return total
			}
			total++
		}
	}
	return total
}

func (c *AppleAuthClient) requestTrustedDeviceCode(ctx context.Context, session *appleAuthSession) error {
	_, _, err := c.do(ctx, session, http.MethodPut, session.Endpoints.Auth+"/verify/trusteddevice/securitycode", session.twoFactorHeaders(), nil, nil, false)
	return err
}

func (c *AppleAuthClient) requestPhoneSecurityCode(ctx context.Context, session *appleAuthSession, phoneNumber json.RawMessage) error {
	phone, err := appleAccountPhoneNumberPayload(appleAccountFallbackPhoneNumber(phoneNumber, session.TwoFactorPhone), false)
	if err != nil {
		return err
	}
	body := map[string]any{
		"phoneNumber": phone,
		"mode":        "sms",
	}
	_, _, err = c.do(ctx, session, http.MethodPut, session.Endpoints.Auth+"/verify/phone", session.twoFactorHeaders(), body, nil, false)
	return err
}

func (c *AppleAuthClient) refreshAuthState(ctx context.Context, session *appleAuthSession) error {
	headers := session.authHeaders()
	headers["Accept"] = "text/html"
	headers["Referer"] = strings.TrimSuffix(session.Endpoints.Auth, "/appleauth/auth") + "/"
	headers["Sec-Fetch-Dest"] = "empty"
	headers["Sec-Fetch-Mode"] = "cors"
	headers["Sec-Fetch-Site"] = "same-origin"
	delete(headers, "Origin")
	delete(headers, "X-Apple-App-Id")
	_, data, err := c.do(ctx, session, http.MethodGet, session.Endpoints.Auth, headers, nil, nil, false)
	if err == nil {
		session.rememberTwoFactorPhoneNumber(data)
	}
	return err
}

func (c *AppleAuthClient) primeAppleAccountManageState(ctx context.Context, session *appleAuthSession) error {
	if session == nil || !session.isAppleAccountManage() {
		return nil
	}
	state := LoginState{
		Kind:      LoginStateAppleAccount,
		Host:      "appleid.apple.com",
		Origin:    appleAccountManageOrigin,
		UserAgent: firstNonEmpty(session.UserAgent, appleAccountManageUserAgent),
	}
	client := &ICloudClient{client: c.httpClient}
	if err := client.warmAppleAccountPortal(ctx, &state); err != nil {
		return err
	}
	var token struct {
		TimeOutInterval int `json:"timeOutInterval"`
	}
	tokenState := state
	tokenState.Scnt = ""
	scnt, err := client.fetchAppleAccountManageTokenScnt(ctx, tokenState, &token)
	if err != nil {
		if strings.TrimSpace(scnt) == "" {
			return err
		}
	}
	if scnt := strings.TrimSpace(scnt); scnt != "" {
		session.ManageScnt = scnt
	}
	return nil
}

func (c *AppleAuthClient) validateTrustedDeviceCode(ctx context.Context, session *appleAuthSession, code string) error {
	body := map[string]any{"securityCode": map[string]string{"code": code}}
	_, _, err := c.do(ctx, session, http.MethodPost, session.Endpoints.Auth+"/verify/trusteddevice/securitycode", session.twoFactorHeaders(), body, nil, false)
	if err != nil {
		var redirect appleDomainRedirectError
		if errors.As(err, &redirect) {
			return err
		}
		return errCode("apple_2fa_failed", "Apple 2FA 验证失败："+err.Error(), true)
	}
	return nil
}

func (c *AppleAuthClient) validatePhoneSecurityCode(ctx context.Context, session *appleAuthSession, code string, phoneNumber json.RawMessage) error {
	phone, err := appleAccountPhoneNumberPayload(appleAccountFallbackPhoneNumber(phoneNumber, session.TwoFactorPhone), true)
	if err != nil {
		return err
	}
	body := map[string]any{
		"phoneNumber":  phone,
		"securityCode": map[string]string{"code": code},
		"mode":         "sms",
	}
	_, _, err = c.do(ctx, session, http.MethodPost, session.Endpoints.Auth+"/verify/phone/securitycode", session.twoFactorHeaders(), body, nil, false)
	if err != nil {
		return errCode("apple_2fa_failed", "Apple 短信 2FA 验证失败："+err.Error(), true)
	}
	return nil
}

func appleAccountFallbackPhoneNumber(phoneNumber json.RawMessage, fallbacks ...json.RawMessage) json.RawMessage {
	phoneNumber = bytes.TrimSpace(phoneNumber)
	if len(phoneNumber) > 0 && string(phoneNumber) != "null" {
		return phoneNumber
	}
	for _, fallback := range fallbacks {
		fallback = bytes.TrimSpace(fallback)
		if len(fallback) > 0 && string(fallback) != "null" {
			return fallback
		}
	}
	return json.RawMessage(`{"id":1,"nonFTEU":true}`)
}

func appleAccountPhoneNumberPayload(phoneNumber json.RawMessage, includeNonFTEU bool) (map[string]any, error) {
	var phone map[string]any
	if err := json.Unmarshal(phoneNumber, &phone); err != nil {
		return nil, errCode("invalid_phone_number_payload", "短信验证码缺少有效 phoneNumber 参数", false)
	}
	if _, ok := phone["id"]; !ok {
		return nil, errCode("invalid_phone_number_payload", "短信验证码缺少有效 phoneNumber 参数", false)
	}
	if !includeNonFTEU {
		delete(phone, "nonFTEU")
	}
	return phone, nil
}

func (s *appleAuthSession) rememberTwoFactorPhoneNumber(data []byte) {
	if s == nil || len(bytes.TrimSpace(s.TwoFactorPhone)) > 0 {
		return
	}
	payload := extractAppleAppConfigJSON(data)
	if len(payload) == 0 {
		payload = bytes.TrimSpace(data)
	}
	var root any
	if err := json.Unmarshal(payload, &root); err != nil {
		return
	}
	if phone, ok := firstApplePhoneNumber(root, 0); ok {
		if data, err := json.Marshal(phone); err == nil {
			s.TwoFactorPhone = data
		}
	}
}

func extractAppleAppConfigJSON(data []byte) []byte {
	text := string(data)
	for _, marker := range []string{`id="app_config"`, `id='app_config'`} {
		idx := strings.Index(text, marker)
		if idx < 0 {
			continue
		}
		open := strings.Index(text[idx:], ">")
		if open < 0 {
			continue
		}
		start := idx + open + 1
		end := strings.Index(text[start:], "</script>")
		if end < 0 {
			continue
		}
		return []byte(strings.TrimSpace(text[start : start+end]))
	}
	return nil
}

func firstApplePhoneNumber(value any, depth int) (map[string]any, bool) {
	if depth > 16 {
		return nil, false
	}
	switch v := value.(type) {
	case map[string]any:
		if numbers, ok := v["trustedPhoneNumbers"].([]any); ok {
			for _, item := range numbers {
				if phone, ok := normalizeApplePhoneNumber(item); ok {
					return phone, true
				}
			}
		}
		if phone, ok := normalizeApplePhoneNumber(v["phoneNumber"]); ok {
			return phone, true
		}
		for _, item := range v {
			if phone, ok := firstApplePhoneNumber(item, depth+1); ok {
				return phone, true
			}
		}
	case []any:
		for _, item := range v {
			if phone, ok := firstApplePhoneNumber(item, depth+1); ok {
				return phone, true
			}
		}
	}
	return nil, false
}

func normalizeApplePhoneNumber(value any) (map[string]any, bool) {
	phone, ok := value.(map[string]any)
	if !ok {
		return nil, false
	}
	id, ok := phone["id"]
	if !ok {
		return nil, false
	}
	out := map[string]any{"id": id}
	if nonFTEU, ok := phone["nonFTEU"]; ok {
		out["nonFTEU"] = nonFTEU
	}
	return out, true
}

func (c *AppleAuthClient) trustSession(ctx context.Context, session *appleAuthSession) error {
	return retryAppleTransient(ctx, func() error {
		_, _, err := c.do(ctx, session, http.MethodGet, session.Endpoints.Auth+"/2sv/trust", session.authHeaders(), nil, nil, false)
		return err
	})
}

func (c *AppleAuthClient) authWithAppleAccountManage(ctx context.Context, session *appleAuthSession) (ICloudSession, error) {
	now := time.Now()
	loginState := LoginState{
		Kind:      LoginStateAppleAccount,
		Host:      "appleid.apple.com",
		Origin:    appleAccountManageOrigin,
		SavedAt:   now,
		Cookies:   session.cloneCookies(),
		Scnt:      firstNonEmpty(session.Scnt, session.ManageScnt),
		SessionID: session.SessionID,
		UserAgent: appleAccountManageUserAgent,
		Note:      "Apple Account management login state",
	}
	refreshed, err := (&ICloudClient{client: c.httpClient}).RefreshAppleAccountManageState(ctx, loginState)
	if err != nil {
		return ICloudSession{}, err
	}
	return ICloudSession{
		SavedAt:     now,
		AppleID:     session.AppleID,
		Host:        "appleid.apple.com",
		LoginStates: []LoginState{refreshed},
		Note:        "saved from Apple Account management protocol login",
	}, nil
}

func (c *AppleAuthClient) authWithTokenAndValidate(ctx context.Context, session *appleAuthSession) (ICloudSession, error) {
	if session.SessionToken == "" {
		return ICloudSession{}, errCode("apple_session_token_missing", "Apple Session Token 缺失，无法换取 iCloud 登录态", true)
	}
	var account appleAccountInfo
	body := map[string]any{
		"accountCountryCode": session.AccountCountry,
		"dsWebAuthToken":     session.SessionToken,
		"extended_login":     true,
		"trustToken":         session.TrustToken,
	}
	headers := session.commonHeaders(map[string]string{})
	if err := retryAppleTransient(ctx, func() error {
		_, _, err := c.do(ctx, session, http.MethodPost, session.Endpoints.Setup+"/accountLogin", headers, body, &account, false)
		return err
	}); err != nil {
		return ICloudSession{}, err
	}
	cookies := session.cloneCookies()
	validate, err := NewICloudSessionValidator().Validate(ctx, cookies, session.Endpoints.Host)
	if err != nil {
		return ICloudSession{}, err
	}
	savedAt := time.Now()
	return ICloudSession{
		SavedAt:            savedAt,
		AppleID:            firstNonEmpty(validate.AppleID, account.DSInfo.AppleID, account.DSInfo.PrimaryEmail, session.AppleID),
		DSID:               validate.DSID,
		ClientID:           validate.ClientID,
		ClientBuildNumber:  validate.ClientBuildNumber,
		MasteringNumber:    validate.MasteringNumber,
		PremiumMailBaseURL: strings.TrimRight(validate.PremiumMailBaseURL, "/"),
		MailGatewayBaseURL: strings.TrimRight(validate.MailGatewayBaseURL, "/"),
		MailBaseURL:        strings.TrimRight(validate.MailBaseURL, "/"),
		Host:               session.Endpoints.Host,
		IsICloudPlus:       validate.IsICloudPlus,
		CanCreateHME:       validate.CanCreateHME,
		Cookies:            cookies,
		LoginStates: []LoginState{
			{
				Kind:      LoginStateICloudWeb,
				Host:      session.Endpoints.Host,
				Origin:    session.Endpoints.Home,
				SavedAt:   savedAt,
				Cookies:   append([]SessionCookie(nil), cookies...),
				UserAgent: appleAuthUserAgent,
				Note:      "iCloud webservices login state",
			},
		},
		Note: "saved from Go Apple SRP protocol login",
	}, nil
}

func (c *AppleAuthClient) doWithTransientRetry(ctx context.Context, session *appleAuthSession, method, rawURL string, headers map[string]string, body any, out any, allow409 bool) (int, []byte, error) {
	var status int
	var data []byte
	err := retryAppleTransient(ctx, func() error {
		var err error
		status, data, err = c.do(ctx, session, method, rawURL, headers, body, out, allow409)
		return err
	})
	return status, data, err
}

func (c *AppleAuthClient) do(ctx context.Context, session *appleAuthSession, method, rawURL string, headers map[string]string, body any, out any, allow409 bool) (int, []byte, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return 0, nil, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, rawURL, reader)
	if err != nil {
		return 0, nil, err
	}
	userAgent := firstNonEmpty(session.UserAgent, appleAuthUserAgent)
	req.Header.Set("User-Agent", userAgent)
	for key, value := range headers {
		if strings.TrimSpace(value) != "" {
			req.Header.Set(key, value)
		}
	}
	if body != nil && req.Header.Get("Content-Type") == "" {
		req.Header.Set("Content-Type", "application/json")
	}
	if cookie := cookieHeader(session.Cookies, rawURL); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	session.extract(resp)
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return resp.StatusCode, nil, err
	}
	if os.Getenv("IPM_DEBUG_APPLE_AUTH") == "1" {
		u, _ := url.Parse(rawURL)
		path := rawURL
		if u != nil {
			path = u.Path
		}
		fmt.Fprintf(os.Stderr, "APPLE_AUTH_DEBUG method=%s path=%s status=%d req_scnt=%s res_scnt=%s res_session=%s body=%q\n",
			method,
			path,
			resp.StatusCode,
			appleDebugFingerprint(req.Header.Get("scnt")),
			appleDebugFingerprint(resp.Header.Get("scnt")),
			appleDebugFingerprint(resp.Header.Get("X-Apple-ID-Session-Id")),
			appleDebugBody(data),
		)
	}
	if redirect, ok := parseAppleDomainRedirect(resp.StatusCode, data); ok {
		return resp.StatusCode, data, redirect
	}
	if resp.StatusCode == http.StatusForbidden {
		return resp.StatusCode, nil, errCode("apple_login_forbidden", "Apple ID 或密码错误，或当前账号被限制登录", true)
	}
	if allow409 && resp.StatusCode == http.StatusConflict {
		return resp.StatusCode, data, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		code := "apple_protocol_http_error"
		if isAppleTransientHTTPStatus(resp.StatusCode) {
			code = "apple_protocol_http_transient"
		}
		path := rawURL
		if parsed, parseErr := url.Parse(rawURL); parseErr == nil && parsed.Path != "" {
			path = parsed.Path
		}
		if code == "apple_protocol_http_transient" {
			return resp.StatusCode, nil, errCode(
				code,
				fmt.Sprintf("Apple 服务暂时不可用（HTTP %d，%s），已自动重试，请稍后再试", resp.StatusCode, path),
				true,
			)
		}
		return resp.StatusCode, nil, errCode(code, fmt.Sprintf("Apple 协议 HTTP %d（%s）: %s", resp.StatusCode, path, trimForError(data)), true)
	}
	if out != nil && len(bytes.TrimSpace(data)) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			return resp.StatusCode, nil, errCode("apple_protocol_bad_json", "Apple 协议返回无法解析", true)
		}
	}
	return resp.StatusCode, data, nil
}

func appleDebugFingerprint(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "-"
	}
	sum := sha1.Sum([]byte(value))
	return fmt.Sprintf("%x/%d", sum[:4], len(value))
}

func retryAppleTransient(ctx context.Context, fn func() error) error {
	var last error
	for attempt := 0; attempt < 3; attempt++ {
		if err := fn(); err != nil {
			if !isAppleTransientNetworkError(err) {
				return err
			}
			last = err
			if attempt == 2 {
				break
			}
			timer := time.NewTimer(time.Duration(attempt+1) * 800 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
			}
			continue
		}
		return nil
	}
	return last
}

func isAppleTransientHTTPStatus(status int) bool {
	switch status {
	case http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	default:
		return false
	}
}

func isAppleTransientNetworkError(err error) bool {
	if err == nil || errors.Is(err, context.Canceled) {
		return false
	}
	var coded codedError
	if errors.As(err, &coded) {
		return coded.code == "apple_protocol_http_transient" || coded.code == "apple_account_http_transient"
	}
	if errors.Is(err, io.EOF) || errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}
	text := strings.ToLower(err.Error())
	for _, marker := range []string{
		"eof",
		"timeout",
		"i/o timeout",
		"no such host",
		"temporary failure in name resolution",
		"network is unreachable",
		"no route to host",
		"connection reset",
		"connection refused",
		"connection aborted",
		"server closed idle connection",
		"tls handshake timeout",
	} {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

func (s *appleAuthSession) extract(resp *http.Response) {
	s.mergeCookies(resp.Request.URL, resp.Cookies())
	if v := resp.Header.Get("X-Apple-ID-Account-Country"); v != "" {
		s.AccountCountry = v
	}
	if v := resp.Header.Get("X-Apple-ID-Session-Id"); v != "" {
		s.SessionID = v
	}
	if v := resp.Header.Get("X-Apple-Session-Token"); v != "" {
		s.SessionToken = v
	}
	if v := resp.Header.Get("X-Apple-TwoSV-Trust-Token"); v != "" {
		s.TrustToken = v
	}
	if v := resp.Header.Get("scnt"); v != "" {
		s.Scnt = v
	}
	if v := resp.Header.Get("X-Apple-Auth-Attributes"); v != "" {
		s.AuthAttributes = v
	}
	if v := strings.TrimSpace(resp.Header.Get("X-Apple-HC-Bits")); v != "" {
		if bits, err := strconv.Atoi(v); err == nil && bits > 0 {
			s.HCBits = bits
		}
	}
	if v := strings.TrimSpace(resp.Header.Get("X-Apple-HC-Challenge")); v != "" {
		s.HCChallenge = v
	}
}

func (s *appleAuthSession) rememberCompleteHashcashChallenge() {
	if s == nil || s.CompleteHCBits > 0 || s.CompleteHCChallenge != "" {
		return
	}
	if s.HCBits > 0 && strings.TrimSpace(s.HCChallenge) != "" {
		s.CompleteHCBits = s.HCBits
		s.CompleteHCChallenge = s.HCChallenge
	}
}

func (s *appleAuthSession) completeHashcashChallenge() (int, string) {
	if s == nil {
		return 0, ""
	}
	if s.CompleteHCBits > 0 && strings.TrimSpace(s.CompleteHCChallenge) != "" {
		return s.CompleteHCBits, s.CompleteHCChallenge
	}
	return s.HCBits, s.HCChallenge
}

func (s *appleAuthSession) switchHost(host string) bool {
	next := appleAuthEndpointsForHost(host)
	if next.Host == "" || next.Host == s.Endpoints.Host {
		return false
	}
	s.Endpoints = next
	return true
}

func (s *appleAuthSession) redirectForAccountCountry() (appleDomainRedirectError, bool) {
	host := appleHostForAccountCountry(s.AccountCountry)
	if host == "" {
		return appleDomainRedirectError{}, false
	}
	next := appleAuthEndpointsForHost(host)
	if next.Host == "" || next.Host == s.Endpoints.Host {
		return appleDomainRedirectError{}, false
	}
	domain := "iCloud.com"
	if strings.Contains(next.Host, "icloud.com.cn") {
		domain = "iCloud.com.cn"
	}
	return appleDomainRedirectError{DomainToUse: domain, Host: next.Host}, true
}

func (s *appleAuthSession) mergeCookies(requestURL *url.URL, cookies []*http.Cookie) {
	mergeSessionCookies(&s.Cookies, requestURL, cookies)
}

func (s *appleAuthSession) cloneCookies() []SessionCookie {
	return append([]SessionCookie(nil), s.Cookies...)
}

func (s *appleAuthSession) srpHeaders() map[string]string {
	frameTag := "auth-" + s.FrameID
	origin := strings.TrimSuffix(s.Endpoints.Auth, "/appleauth/auth")
	userAgent := firstNonEmpty(s.UserAgent, appleAuthUserAgent)
	headers := map[string]string{
		"Accept":                           "application/json",
		"Content-Type":                     "application/json",
		"Origin":                           origin,
		"Referer":                          origin + "/",
		"X-Apple-Widget-Key":               s.ClientID,
		"X-Apple-OAuth-Client-Id":          s.ClientID,
		"X-Apple-OAuth-Client-Type":        "firstPartyAuth",
		"X-Apple-OAuth-Redirect-URI":       s.Endpoints.Home,
		"X-Apple-OAuth-Require-Grant-Code": "true",
		"X-Apple-OAuth-Response-Mode":      "web_message",
		"X-Apple-OAuth-Response-Type":      "code",
		"X-Apple-OAuth-State":              frameTag,
		"X-Apple-Frame-Id":                 frameTag,
		"X-Requested-With":                 "XMLHttpRequest",
		"X-Apple-Mandate-Security-Upgrade": "0",
		"X-Apple-I-Require-UE":             "true",
		"X-Apple-I-FD-Client-Info":         `{"U":"` + userAgent + `","L":"zh-CN","Z":"GMT+08:00","V":"1.1","F":""}`,
	}
	if s.isAppleAccountManage() {
		headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
		headers["X-Apple-I-FD-Client-Info"] = appleAccountFDClientInfo(userAgent)
		applyAppleAccountBrowserHints(headers)
		headers["Sec-Fetch-Dest"] = "empty"
		headers["Sec-Fetch-Mode"] = "cors"
		headers["Sec-Fetch-Site"] = "same-origin"
		delete(headers, "X-Apple-OAuth-Require-Grant-Code")
		delete(headers, "X-Apple-Mandate-Security-Upgrade")
		delete(headers, "X-Apple-I-Require-UE")
	}
	if s.AuthAttributes != "" {
		headers["X-Apple-Auth-Attributes"] = s.AuthAttributes
	}
	if s.Scnt != "" {
		headers["scnt"] = s.Scnt
	}
	if s.SessionID != "" {
		headers["X-Apple-ID-Session-Id"] = s.SessionID
	}
	if s.SessionToken != "" {
		headers["X-Apple-Session-Token"] = s.SessionToken
	}
	if s.isAppleAccountManage() {
		headers["X-Apple-Domain-Id"] = "11"
		headers["X-Apple-Privacy-Consent"] = "true"
		headers["X-Apple-Privacy-Consent-Accepted"] = "true"
	}
	return headers
}

func (s *appleAuthSession) authHeaders() map[string]string {
	return s.srpHeaders()
}

func (s *appleAuthSession) twoFactorHeaders() map[string]string {
	headers := s.authHeaders()
	if s.isAppleAccountManage() {
		headers["Accept"] = "application/json, text/plain, */*"
		headers["X-Apple-App-Id"] = s.ClientID
		delete(headers, "X-Requested-With")
	}
	return headers
}

func applyAppleAccountBrowserHints(headers map[string]string) {
	headers["Accept-Language"] = appleAccountManageLanguage + ",en;q=0.9"
	headers["Sec-CH-UA"] = `"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"`
	headers["Sec-CH-UA-Mobile"] = "?0"
	headers["Sec-CH-UA-Platform"] = appleAccountManagePlatform
}

func (s *appleAuthSession) isAppleAccountManage() bool {
	if s == nil {
		return false
	}
	return strings.EqualFold(strings.TrimRight(s.Endpoints.Home, "/"), "https://account.apple.com")
}

func (s *appleAuthSession) commonHeaders(overwrite map[string]string) map[string]string {
	userAgent := firstNonEmpty(s.UserAgent, appleAuthUserAgent)
	headers := map[string]string{
		"Accept":       "application/json",
		"Content-Type": "application/json",
		"Origin":       s.Endpoints.Home,
		"Referer":      s.Endpoints.Home + "/",
		"User-Agent":   userAgent,
	}
	for key, value := range overwrite {
		headers[key] = value
	}
	return headers
}
