package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	updateCacheTTL         = 10 * time.Minute
	updateHTTPTimeout      = 45 * time.Second
	updateDownloadMaxBytes = 256 << 20
)

var updateGitHubAPIBaseURL = "https://api.github.com"

type publicUpdateStatus struct {
	Enabled         bool              `json:"enabled"`
	Current         publicVersionInfo `json:"current"`
	LatestVersion   string            `json:"latest_version,omitempty"`
	LatestName      string            `json:"latest_name,omitempty"`
	LatestNotes     string            `json:"latest_notes,omitempty"`
	PublishedAt     string            `json:"published_at,omitempty"`
	AssetName       string            `json:"asset_name,omitempty"`
	AssetAvailable  bool              `json:"asset_available"`
	UpdateAvailable bool              `json:"update_available"`
	CheckedAt       string            `json:"checked_at,omitempty"`
	Error           string            `json:"error,omitempty"`
}

type updateCandidate struct {
	Status      publicUpdateStatus
	DownloadURL string
	SHA256      string
}

type updateManifest struct {
	Version     string                `json:"version"`
	Name        string                `json:"name"`
	Notes       string                `json:"notes"`
	PublishedAt string                `json:"published_at"`
	Assets      []updateManifestAsset `json:"assets"`
}

type updateManifestAsset struct {
	Name   string `json:"name"`
	OS     string `json:"os"`
	Arch   string `json:"arch"`
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
}

type githubReleaseAsset struct {
	Name               string `json:"name"`
	BrowserDownloadURL string `json:"browser_download_url"`
	Size               int64  `json:"size"`
}

type httpStatusError struct {
	StatusCode int
	Body       string
}

func (e *httpStatusError) Error() string {
	if strings.TrimSpace(e.Body) == "" {
		return fmt.Sprintf("HTTP %d", e.StatusCode)
	}
	return fmt.Sprintf("HTTP %d：%s", e.StatusCode, strings.TrimSpace(e.Body))
}

func (s *Server) handleUpdateStatus(w http.ResponseWriter, r *http.Request) {
	status := s.updateStatus(r.Context(), r.URL.Query().Get("force") == "1")
	writeJSON(w, http.StatusOK, map[string]any{
		"success": true,
		"update":  status,
	})
}

func (s *Server) handleApplyUpdate(w http.ResponseWriter, r *http.Request) {
	if !s.isAdminRequest(r) {
		writeError(w, http.StatusForbidden, errCode("admin_required", "需要管理员权限", false))
		return
	}
	result, err := s.applyUpdate(r.Context())
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"success": true,
		"update":  result,
	})
}

func (s *Server) updateStatus(ctx context.Context, force bool) publicUpdateStatus {
	candidate, err := s.latestUpdateCandidate(ctx, force)
	if err != nil {
		status := publicUpdateStatus{
			Enabled:   s.cfg.UpdateEnabled,
			Current:   currentVersionInfo(),
			CheckedAt: formatTime(time.Now()),
			Error:     err.Error(),
		}
		return status
	}
	return candidate.Status
}

func (s *Server) latestUpdateCandidate(ctx context.Context, force bool) (updateCandidate, error) {
	now := time.Now()
	s.updateMu.Lock()
	if !force && !s.updateCacheAt.IsZero() && now.Sub(s.updateCacheAt) < updateCacheTTL {
		cached := s.updateCache
		s.updateMu.Unlock()
		return cached, nil
	}
	s.updateMu.Unlock()

	candidate, err := s.fetchLatestUpdateCandidate(ctx, now)
	if err != nil {
		return updateCandidate{}, err
	}

	s.updateMu.Lock()
	s.updateCache = candidate
	s.updateCacheAt = now
	s.updateMu.Unlock()
	return candidate, nil
}

func (s *Server) fetchLatestUpdateCandidate(ctx context.Context, checkedAt time.Time) (updateCandidate, error) {
	status := publicUpdateStatus{
		Enabled:   s.cfg.UpdateEnabled,
		Current:   currentVersionInfo(),
		CheckedAt: formatTime(checkedAt),
	}
	if !s.cfg.UpdateEnabled {
		return updateCandidate{Status: status}, nil
	}
	if manifestURL := strings.TrimSpace(s.cfg.UpdateManifestURL); manifestURL != "" {
		return s.fetchManifestUpdateCandidate(ctx, manifestURL, status)
	}
	if strings.TrimSpace(s.cfg.UpdateRepository) == "" {
		return updateCandidate{Status: status}, nil
	}
	return s.fetchGitHubReleaseUpdateCandidate(ctx, status)
}

func (s *Server) fetchManifestUpdateCandidate(ctx context.Context, manifestURL string, status publicUpdateStatus) (updateCandidate, error) {
	var manifest updateManifest
	if err := getJSON(ctx, manifestURL, &manifest); err != nil {
		return updateCandidate{}, fmt.Errorf("检查更新失败：%w", err)
	}
	asset, ok := selectManifestAsset(manifest.Assets, runtime.GOOS, runtime.GOARCH, s.cfg.UpdateAssetName)
	status.LatestVersion = strings.TrimSpace(manifest.Version)
	status.LatestName = strings.TrimSpace(firstNonEmptyString(manifest.Name, manifest.Version))
	status.LatestNotes = strings.TrimSpace(manifest.Notes)
	status.PublishedAt = strings.TrimSpace(manifest.PublishedAt)
	status.AssetAvailable = ok
	status.UpdateAvailable = versionIsNewer(status.Current.Version, status.LatestVersion)
	if ok {
		status.AssetName = strings.TrimSpace(asset.Name)
	}
	return updateCandidate{
		Status:      status,
		DownloadURL: strings.TrimSpace(asset.URL),
		SHA256:      strings.ToLower(strings.TrimSpace(asset.SHA256)),
	}, nil
}

func (s *Server) fetchGitHubReleaseUpdateCandidate(ctx context.Context, status publicUpdateStatus) (updateCandidate, error) {
	repo := strings.Trim(strings.TrimSpace(s.cfg.UpdateRepository), "/")
	if repo == "" {
		return updateCandidate{}, errors.New("检查更新失败：未配置 update_repository")
	}
	apiURL := githubAPIURL("/repos/" + repo + "/releases/latest")
	var release struct {
		TagName     string               `json:"tag_name"`
		Name        string               `json:"name"`
		Body        string               `json:"body"`
		PublishedAt string               `json:"published_at"`
		Assets      []githubReleaseAsset `json:"assets"`
	}
	if err := getJSON(ctx, apiURL, &release); err != nil {
		var statusErr *httpStatusError
		if errors.As(err, &statusErr) && statusErr.StatusCode == http.StatusNotFound {
			return s.fetchGitHubDefaultBranchUpdateCandidate(ctx, repo, status)
		}
		return updateCandidate{}, fmt.Errorf("检查更新失败：%w", err)
	}
	asset, ok := selectGitHubReleaseAsset(release.Assets, runtime.GOOS, runtime.GOARCH, s.cfg.UpdateAssetName)
	status.LatestVersion = strings.TrimSpace(release.TagName)
	status.LatestName = strings.TrimSpace(firstNonEmptyString(release.Name, release.TagName))
	status.LatestNotes = strings.TrimSpace(release.Body)
	status.PublishedAt = strings.TrimSpace(release.PublishedAt)
	status.AssetAvailable = ok
	status.UpdateAvailable = versionIsNewer(status.Current.Version, status.LatestVersion)
	if ok {
		status.AssetName = strings.TrimSpace(asset.Name)
	}
	return updateCandidate{
		Status:      status,
		DownloadURL: strings.TrimSpace(asset.BrowserDownloadURL),
	}, nil
}

func (s *Server) fetchGitHubDefaultBranchUpdateCandidate(ctx context.Context, repo string, status publicUpdateStatus) (updateCandidate, error) {
	var repoInfo struct {
		DefaultBranch string `json:"default_branch"`
	}
	if err := getJSON(ctx, githubAPIURL("/repos/"+repo), &repoInfo); err != nil {
		return updateCandidate{}, fmt.Errorf("检查更新失败：GitHub 仓库没有 Release，且读取默认分支失败：%w", err)
	}
	branch := strings.TrimSpace(repoInfo.DefaultBranch)
	if branch == "" {
		branch = "master"
	}
	var commitInfo struct {
		SHA    string `json:"sha"`
		Commit struct {
			Message   string `json:"message"`
			Committer struct {
				Date string `json:"date"`
			} `json:"committer"`
		} `json:"commit"`
	}
	if err := getJSON(ctx, githubAPIURL("/repos/"+repo+"/commits/"+url.PathEscape(branch)), &commitInfo); err != nil {
		return updateCandidate{}, fmt.Errorf("检查更新失败：GitHub 仓库没有 Release，且读取最新提交失败：%w", err)
	}
	latestSHA := strings.TrimSpace(commitInfo.SHA)
	shortSHA := shortCommit(latestSHA)
	currentCommit := strings.TrimSpace(status.Current.Commit)
	matchesCurrent := currentCommit != "" && currentCommit != "unknown" && strings.HasPrefix(latestSHA, currentCommit)
	status.LatestName = "GitHub 最新源码 " + shortSHA
	status.LatestVersion = strings.TrimSpace(status.Current.Version)
	if shortSHA != "" {
		if status.LatestVersion == "" {
			status.LatestVersion = shortSHA
		} else {
			status.LatestVersion += " / " + shortSHA
		}
	}
	status.PublishedAt = strings.TrimSpace(commitInfo.Commit.Committer.Date)
	status.AssetAvailable = false
	status.UpdateAvailable = currentCommit != "" && currentCommit != "unknown" && !matchesCurrent
	if matchesCurrent {
		status.LatestNotes = "GitHub 仓库还没有发布 Release 更新包；已按默认分支最新提交确认，当前服务已经是最新源码。"
	} else if currentCommit == "" || currentCommit == "unknown" {
		status.LatestNotes = "GitHub 仓库还没有发布 Release 更新包；当前程序没有写入提交号，无法判断是否为最新源码。"
	} else {
		status.LatestNotes = "GitHub 仓库还没有发布 Release 更新包；默认分支已有新提交，但暂时没有可一键更新的二进制包。"
	}
	if msg := strings.TrimSpace(commitInfo.Commit.Message); msg != "" {
		status.LatestNotes += "\n最新提交：" + firstLine(msg)
	}
	return updateCandidate{Status: status}, nil
}

func (s *Server) applyUpdate(ctx context.Context) (publicUpdateStatus, error) {
	s.updateApplyMu.Lock()
	defer s.updateApplyMu.Unlock()

	if runtime.GOOS == "windows" {
		return publicUpdateStatus{}, errCode("update_unsupported", "Windows 运行中的 exe 不能安全自替换，请下载新版后手动重启", false)
	}
	candidate, err := s.latestUpdateCandidate(ctx, true)
	if err != nil {
		return publicUpdateStatus{}, err
	}
	if !candidate.Status.UpdateAvailable {
		return candidate.Status, errCode("no_update_available", "当前已经是最新版本", false)
	}
	if strings.TrimSpace(candidate.DownloadURL) == "" || !candidate.Status.AssetAvailable {
		return candidate.Status, errCode("update_asset_missing", "最新版本没有匹配当前系统架构的二进制包", false)
	}
	if isArchiveAssetName(candidate.Status.AssetName) {
		return candidate.Status, errCode("update_asset_unsupported", "最新版本匹配到的是压缩包，在线更新需要发布裸二进制文件", false)
	}
	exePath, err := os.Executable()
	if err != nil {
		return candidate.Status, fmt.Errorf("读取当前程序路径失败：%w", err)
	}
	exePath, err = filepath.EvalSymlinks(exePath)
	if err != nil {
		return candidate.Status, fmt.Errorf("解析当前程序路径失败：%w", err)
	}
	if err := downloadAndReplaceExecutable(ctx, candidate.DownloadURL, candidate.SHA256, exePath); err != nil {
		return candidate.Status, err
	}
	candidate.Status.CheckedAt = formatTime(time.Now())
	go func() {
		time.Sleep(1200 * time.Millisecond)
		os.Exit(2)
	}()
	return candidate.Status, nil
}

func getJSON(ctx context.Context, url string, out any) error {
	reqCtx, cancel := context.WithTimeout(ctx, updateHTTPTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "iCloud-Privacy-Mail-Updater/"+AppVersion)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(res.Body, 2048))
		return &httpStatusError{StatusCode: res.StatusCode, Body: strings.TrimSpace(string(body))}
	}
	return json.NewDecoder(io.LimitReader(res.Body, 4<<20)).Decode(out)
}

func githubAPIURL(path string) string {
	base := strings.TrimRight(strings.TrimSpace(updateGitHubAPIBaseURL), "/")
	if base == "" {
		base = "https://api.github.com"
	}
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	return base + path
}

func shortCommit(sha string) string {
	sha = strings.TrimSpace(sha)
	if len(sha) <= 7 {
		return sha
	}
	return sha[:7]
}

func firstLine(text string) string {
	text = strings.TrimSpace(text)
	if text == "" {
		return ""
	}
	if idx := strings.IndexAny(text, "\r\n"); idx >= 0 {
		return strings.TrimSpace(text[:idx])
	}
	return text
}

func downloadAndReplaceExecutable(ctx context.Context, downloadURL, wantSHA256, exePath string) error {
	reqCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, downloadURL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "iCloud-Privacy-Mail-Updater/"+AppVersion)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("下载更新失败：%w", err)
	}
	defer res.Body.Close()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(res.Body, 2048))
		return fmt.Errorf("下载更新失败：HTTP %d：%s", res.StatusCode, strings.TrimSpace(string(body)))
	}
	dir := filepath.Dir(exePath)
	tmp, err := os.CreateTemp(dir, ".panel-update-*")
	if err != nil {
		return fmt.Errorf("创建更新临时文件失败：%w", err)
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()

	hasher := sha256.New()
	limited := &io.LimitedReader{R: res.Body, N: updateDownloadMaxBytes + 1}
	written, copyErr := io.Copy(io.MultiWriter(tmp, hasher), limited)
	closeErr := tmp.Close()
	if copyErr != nil {
		return fmt.Errorf("保存更新文件失败：%w", copyErr)
	}
	if closeErr != nil {
		return fmt.Errorf("关闭更新文件失败：%w", closeErr)
	}
	if written > updateDownloadMaxBytes {
		return fmt.Errorf("更新文件超过 %d MB，已拒绝", updateDownloadMaxBytes>>20)
	}
	gotSHA256 := hex.EncodeToString(hasher.Sum(nil))
	if wantSHA256 != "" && !strings.EqualFold(gotSHA256, wantSHA256) {
		return fmt.Errorf("更新文件校验失败：sha256=%s", gotSHA256)
	}
	if err := os.Chmod(tmpPath, 0755); err != nil {
		return fmt.Errorf("设置更新文件权限失败：%w", err)
	}
	backupPath := exePath + ".bak-" + time.Now().Format("20060102150405")
	if err := os.Rename(exePath, backupPath); err != nil {
		return fmt.Errorf("备份当前程序失败：%w", err)
	}
	if err := os.Rename(tmpPath, exePath); err != nil {
		_ = os.Rename(backupPath, exePath)
		return fmt.Errorf("替换当前程序失败：%w", err)
	}
	return nil
}

func selectManifestAsset(assets []updateManifestAsset, goos, goarch, preferred string) (updateManifestAsset, bool) {
	preferred = strings.TrimSpace(preferred)
	for _, asset := range assets {
		if preferred != "" && strings.EqualFold(strings.TrimSpace(asset.Name), preferred) {
			return asset, true
		}
	}
	for _, asset := range assets {
		if !strings.EqualFold(strings.TrimSpace(asset.OS), goos) || !strings.EqualFold(strings.TrimSpace(asset.Arch), goarch) {
			continue
		}
		if strings.TrimSpace(asset.URL) == "" {
			continue
		}
		if isArchiveAssetName(asset.Name) {
			continue
		}
		return asset, true
	}
	return updateManifestAsset{}, false
}

func selectGitHubReleaseAsset(assets []githubReleaseAsset, goos, goarch, preferred string) (githubReleaseAsset, bool) {
	preferred = strings.TrimSpace(preferred)
	for _, asset := range assets {
		if preferred != "" && strings.EqualFold(strings.TrimSpace(asset.Name), preferred) && strings.TrimSpace(asset.BrowserDownloadURL) != "" {
			return asset, true
		}
	}
	for _, asset := range assets {
		name := strings.TrimSpace(asset.Name)
		url := strings.TrimSpace(asset.BrowserDownloadURL)
		lower := strings.ToLower(name)
		if url == "" || strings.Contains(lower, "sha256") || strings.Contains(lower, "checksum") || isArchiveAssetName(name) {
			continue
		}
		if strings.Contains(lower, strings.ToLower(goos)) && strings.Contains(lower, strings.ToLower(goarch)) {
			return asset, true
		}
	}
	return githubReleaseAsset{}, false
}

func isArchiveAssetName(name string) bool {
	lower := strings.ToLower(strings.TrimSpace(name))
	return strings.HasSuffix(lower, ".zip") ||
		strings.HasSuffix(lower, ".tar.gz") ||
		strings.HasSuffix(lower, ".tgz") ||
		strings.HasSuffix(lower, ".gz") ||
		strings.HasSuffix(lower, ".7z") ||
		strings.HasSuffix(lower, ".rar")
}

func versionIsNewer(current, latest string) bool {
	current = normalizeVersion(current)
	latest = normalizeVersion(latest)
	if latest == "" {
		return false
	}
	if current == "" || strings.Contains(current, "dev") || strings.Contains(current, "unknown") {
		return latest != current
	}
	if current == latest {
		return false
	}
	curParts := splitVersionParts(current)
	latParts := splitVersionParts(latest)
	max := len(curParts)
	if len(latParts) > max {
		max = len(latParts)
	}
	for i := 0; i < max; i++ {
		var cur, lat int
		if i < len(curParts) {
			cur = curParts[i]
		}
		if i < len(latParts) {
			lat = latParts[i]
		}
		if lat > cur {
			return true
		}
		if lat < cur {
			return false
		}
	}
	return false
}

func normalizeVersion(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	value = strings.TrimPrefix(value, "version")
	value = strings.TrimSpace(value)
	return strings.TrimPrefix(value, "v")
}

func splitVersionParts(value string) []int {
	fields := strings.FieldsFunc(value, func(r rune) bool {
		return r < '0' || r > '9'
	})
	out := make([]int, 0, len(fields))
	for _, field := range fields {
		if field == "" {
			continue
		}
		var n int
		for _, r := range field {
			n = n*10 + int(r-'0')
		}
		out = append(out, n)
	}
	return out
}
