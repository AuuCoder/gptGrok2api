package app

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type ICloudSessionValidator struct {
	httpClient *http.Client
}

type validateResult struct {
	AppleID            string
	DSID               string
	ClientID           string
	ClientBuildNumber  string
	MasteringNumber    string
	PremiumMailBaseURL string
	MailGatewayBaseURL string
	MailBaseURL        string
	IsICloudPlus       bool
	CanCreateHME       bool
}

func NewICloudSessionValidator() *ICloudSessionValidator {
	return &ICloudSessionValidator{httpClient: &http.Client{Timeout: 15 * time.Second}}
}

func (c *ICloudSessionValidator) Validate(ctx context.Context, cookies []SessionCookie, defaultHost string) (validateResult, error) {
	host := strings.TrimSpace(defaultHost)
	if host == "" {
		host = "www.icloud.com.cn"
	}
	setupHost := "setup.icloud.com.cn"
	if strings.HasSuffix(host, "icloud.com") && !strings.HasSuffix(host, "icloud.com.cn") {
		setupHost = "setup.icloud.com"
	}

	clientID, err := randomUUID()
	if err != nil {
		return validateResult{}, err
	}
	requestID, err := randomUUID()
	if err != nil {
		return validateResult{}, err
	}
	buildNumber := "2622Build20"
	masteringNumber := buildNumber

	u := url.URL{
		Scheme: "https",
		Host:   setupHost,
		Path:   "/setup/ws/1/validate",
	}
	q := u.Query()
	q.Set("clientBuildNumber", buildNumber)
	q.Set("clientMasteringNumber", masteringNumber)
	q.Set("clientId", clientID)
	q.Set("requestId", requestID)
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u.String(), nil)
	if err != nil {
		return validateResult{}, err
	}
	session := ICloudSession{Host: host, Cookies: cookies}
	setICloudFetchHeaders(req, session, "*/*", "text/plain;charset=UTF-8")
	if cookie := cookieHeader(cookies, u.String()); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return validateResult{}, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return validateResult{}, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return validateResult{}, errCode("icloud_validate_failed", fmt.Sprintf("iCloud 登录态校验失败，HTTP %d: %s", resp.StatusCode, trimForError(data)), true)
	}
	var account struct {
		DSInfo struct {
			DSID                            string `json:"dsid"`
			AppleID                         string `json:"appleId"`
			PrimaryEmail                    string `json:"primaryEmail"`
			IsHideMyEmailSubscriptionActive bool   `json:"isHideMyEmailSubscriptionActive"`
			IsHideMyEmailFeatureAvailable   bool   `json:"isHideMyEmailFeatureAvailable"`
		} `json:"dsInfo"`
		Webservices map[string]struct {
			URL    string `json:"url"`
			Status string `json:"status"`
		} `json:"webservices"`
	}
	if err := json.Unmarshal(data, &account); err != nil {
		return validateResult{}, errCode("icloud_validate_bad_response", "iCloud 登录态校验返回无法解析", true)
	}
	premium := account.Webservices["premiummailsettings"].URL
	mailGateway := account.Webservices["mccgateway"].URL
	mail := account.Webservices["mail"].URL
	appleID := account.DSInfo.AppleID
	if appleID == "" {
		appleID = account.DSInfo.PrimaryEmail
	}
	return validateResult{
		AppleID:            appleID,
		DSID:               account.DSInfo.DSID,
		ClientID:           clientID,
		ClientBuildNumber:  buildNumber,
		MasteringNumber:    masteringNumber,
		PremiumMailBaseURL: premium,
		MailGatewayBaseURL: mailGateway,
		MailBaseURL:        mail,
		IsICloudPlus:       account.DSInfo.IsHideMyEmailSubscriptionActive,
		CanCreateHME:       account.DSInfo.IsHideMyEmailFeatureAvailable,
	}, nil
}
