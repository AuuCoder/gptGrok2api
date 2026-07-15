package app

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Config struct {
	ConfigPath                   string `json:"-"`
	Host                         string `json:"host"`
	Port                         int    `json:"port"`
	DataPath                     string `json:"data_path"`
	APIKey                       string `json:"api_key"`
	PublicBaseURL                string `json:"public_base_url"`
	ICloudDefaultHost            string `json:"icloud_default_host"`
	ICloudClientID               string `json:"icloud_client_id"`
	AppleAccountAPIKey           string `json:"apple_account_api_key"`
	AppleAccountKeepAliveEnabled bool   `json:"apple_account_keep_alive_enabled"`
	AppleAccountKeepAliveMS      int    `json:"apple_account_keep_alive_ms"`
	MailWatcherEnabled           bool   `json:"mail_watcher_enabled"`
	MailWatcherPollMS            int    `json:"mail_watcher_poll_ms"`
	MailWatcherFetchLimit        int    `json:"mail_watcher_fetch_limit"`
	MailWatcherInitialFetchLimit int    `json:"mail_watcher_initial_fetch_limit"`
	MailWatcherLookbackHours     int    `json:"mail_watcher_lookback_hours"`
	PublicFastSyncWaitMS         int    `json:"public_fast_sync_wait_ms"`
	PublicSyncMinIntervalMS      int    `json:"public_sync_min_interval_ms"`
	UpdateEnabled                bool   `json:"update_enabled"`
	UpdateRepository             string `json:"update_repository"`
	UpdateManifestURL            string `json:"update_manifest_url"`
	UpdateAssetName              string `json:"update_asset_name"`
}

func LoadConfig(path string) (Config, error) {
	cfg := Config{
		ConfigPath:                   path,
		Host:                         "127.0.0.1",
		Port:                         8787,
		DataPath:                     filepath.Join("data", "state.json"),
		APIKey:                       strings.TrimSpace(os.Getenv("IPM_API_KEY")),
		PublicBaseURL:                strings.TrimRight(strings.TrimSpace(os.Getenv("IPM_PUBLIC_BASE_URL")), "/"),
		ICloudDefaultHost:            "www.icloud.com.cn",
		ICloudClientID:               defaultAppleOAuthClientID,
		AppleAccountAPIKey:           strings.TrimSpace(os.Getenv("IPM_APPLE_ACCOUNT_API_KEY")),
		AppleAccountKeepAliveEnabled: envBool("APPLE_ACCOUNT_KEEP_ALIVE_ENABLED", true),
		AppleAccountKeepAliveMS:      envPositiveInt("APPLE_ACCOUNT_KEEP_ALIVE_MS", 240000),
		MailWatcherEnabled:           envBool("MAIL_WATCHER_ENABLED", true),
		MailWatcherPollMS:            envPositiveInt("MAIL_WATCHER_POLL_MS", 3000),
		MailWatcherFetchLimit:        envPositiveInt("MAIL_WATCHER_FETCH_LIMIT", 8),
		MailWatcherInitialFetchLimit: envPositiveInt("MAIL_WATCHER_INITIAL_FETCH_LIMIT", 20),
		MailWatcherLookbackHours:     envPositiveInt("MAIL_WATCHER_LOOKBACK_HOURS", 24),
		PublicFastSyncWaitMS:         envPositiveInt("PUBLIC_FAST_SYNC_WAIT_MS", 600),
		PublicSyncMinIntervalMS:      envPositiveInt("PUBLIC_SYNC_MIN_INTERVAL_MS", 3000),
		UpdateEnabled:                envBool("IPM_UPDATE_ENABLED", true),
		UpdateRepository:             strings.TrimSpace(os.Getenv("IPM_UPDATE_REPOSITORY")),
		UpdateManifestURL:            strings.TrimSpace(os.Getenv("IPM_UPDATE_MANIFEST_URL")),
		UpdateAssetName:              strings.TrimSpace(os.Getenv("IPM_UPDATE_ASSET_NAME")),
	}
	if path == "" {
		return cfg, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return cfg, nil
		}
		return Config{}, err
	}
	var fromFile Config
	if err := json.Unmarshal(data, &fromFile); err != nil {
		return Config{}, err
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return Config{}, err
	}
	if fromFile.Host != "" {
		cfg.Host = fromFile.Host
	}
	if fromFile.Port != 0 {
		cfg.Port = fromFile.Port
	}
	if fromFile.DataPath != "" {
		cfg.DataPath = fromFile.DataPath
	}
	if strings.TrimSpace(fromFile.APIKey) != "" {
		cfg.APIKey = strings.TrimSpace(fromFile.APIKey)
	}
	if strings.TrimSpace(fromFile.PublicBaseURL) != "" {
		cfg.PublicBaseURL = strings.TrimRight(strings.TrimSpace(fromFile.PublicBaseURL), "/")
	}
	if strings.TrimSpace(fromFile.ICloudDefaultHost) != "" {
		cfg.ICloudDefaultHost = strings.TrimSpace(fromFile.ICloudDefaultHost)
	}
	if strings.TrimSpace(fromFile.ICloudClientID) != "" {
		cfg.ICloudClientID = strings.TrimSpace(fromFile.ICloudClientID)
	}
	if strings.TrimSpace(fromFile.AppleAccountAPIKey) != "" {
		cfg.AppleAccountAPIKey = strings.TrimSpace(fromFile.AppleAccountAPIKey)
	}
	if rawValue, ok := raw["apple_account_keep_alive_enabled"]; ok {
		var enabled bool
		if err := json.Unmarshal(rawValue, &enabled); err != nil {
			return Config{}, err
		}
		cfg.AppleAccountKeepAliveEnabled = enabled
	}
	if fromFile.AppleAccountKeepAliveMS > 0 {
		cfg.AppleAccountKeepAliveMS = fromFile.AppleAccountKeepAliveMS
	}
	if rawValue, ok := raw["mail_watcher_enabled"]; ok {
		var enabled bool
		if err := json.Unmarshal(rawValue, &enabled); err != nil {
			return Config{}, err
		}
		cfg.MailWatcherEnabled = enabled
	}
	if fromFile.MailWatcherPollMS > 0 {
		cfg.MailWatcherPollMS = fromFile.MailWatcherPollMS
	}
	if fromFile.MailWatcherFetchLimit > 0 {
		cfg.MailWatcherFetchLimit = fromFile.MailWatcherFetchLimit
	}
	if fromFile.MailWatcherInitialFetchLimit > 0 {
		cfg.MailWatcherInitialFetchLimit = fromFile.MailWatcherInitialFetchLimit
	}
	if fromFile.MailWatcherLookbackHours > 0 {
		cfg.MailWatcherLookbackHours = fromFile.MailWatcherLookbackHours
	}
	if fromFile.PublicFastSyncWaitMS > 0 {
		cfg.PublicFastSyncWaitMS = fromFile.PublicFastSyncWaitMS
	}
	if fromFile.PublicSyncMinIntervalMS > 0 {
		cfg.PublicSyncMinIntervalMS = fromFile.PublicSyncMinIntervalMS
	}
	if rawValue, ok := raw["update_enabled"]; ok {
		var enabled bool
		if err := json.Unmarshal(rawValue, &enabled); err != nil {
			return Config{}, err
		}
		cfg.UpdateEnabled = enabled
	}
	if strings.TrimSpace(fromFile.UpdateRepository) != "" {
		cfg.UpdateRepository = strings.TrimSpace(fromFile.UpdateRepository)
	}
	if strings.TrimSpace(fromFile.UpdateManifestURL) != "" {
		cfg.UpdateManifestURL = strings.TrimSpace(fromFile.UpdateManifestURL)
	}
	if strings.TrimSpace(fromFile.UpdateAssetName) != "" {
		cfg.UpdateAssetName = strings.TrimSpace(fromFile.UpdateAssetName)
	}
	return cfg, nil
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func envBool(name string, fallback bool) bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv(name)))
	if value == "" {
		return fallback
	}
	switch value {
	case "1", "true", "yes", "y", "on":
		return true
	case "0", "false", "no", "n", "off":
		return false
	default:
		return fallback
	}
}

func envPositiveInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}
