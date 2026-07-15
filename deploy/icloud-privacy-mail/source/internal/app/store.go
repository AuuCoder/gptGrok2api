package app

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type FileStore struct {
	mu    sync.Mutex
	path  string
	state State
}

type DeleteUserResult struct {
	UserID         string `json:"user_id"`
	Username       string `json:"username"`
	Accounts       int    `json:"accounts"`
	Mailboxes      int    `json:"mailboxes"`
	Messages       int    `json:"messages"`
	ICloudSessions int    `json:"icloud_sessions"`
	WebSessions    int    `json:"web_sessions"`
}

func NewFileStore(path string) (*FileStore, error) {
	if path == "" {
		path = filepath.Join("data", "state.json")
	}
	s := &FileStore{path: path, state: State{NextID: 1}}
	if err := s.load(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *FileStore) load() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	data, err := os.ReadFile(s.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return s.saveLocked()
		}
		return err
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return s.saveLocked()
	}
	if err := json.Unmarshal(data, &s.state); err != nil {
		return err
	}
	if s.state.NextID <= 0 {
		s.state.NextID = 1
	}
	changed := s.migrateLegacyICloudSessionToAdminLocked()
	if s.migrateLegacyICloudSessionAccountIDsLocked() {
		changed = true
	}
	if s.migrateLegacyMailboxAccountIDsLocked() {
		changed = true
	}
	if s.migrateLegacyMailboxClaimsLocked() {
		changed = true
	}
	if changed {
		return s.saveLocked()
	}
	return nil
}

func (s *FileStore) Snapshot() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return cloneState(s.state)
}

func (s *FileStore) SnapshotForOwner(ownerID string) State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return filterStateByOwnerLocked(s.state, strings.TrimSpace(ownerID))
}

func (s *FileStore) CreateSettingsForOwner(ownerID string) CreateSettings {
	s.mu.Lock()
	defer s.mu.Unlock()
	return createSettingsForOwnerLocked(s.state, strings.TrimSpace(ownerID))
}

func (s *FileStore) SaveCreateSettingsForOwner(ownerID string, settings CreateSettings) (CreateSettings, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	ownerID = strings.TrimSpace(ownerID)
	settings = normalizeCreateSettings(ownerID, settings)
	settings.UpdatedAt = time.Now()
	for i := range s.state.CreateSettings {
		if constantTimeEqual(ownerID, s.state.CreateSettings[i].OwnerID) {
			s.state.CreateSettings[i] = settings
			return settings, s.saveLocked()
		}
	}
	s.state.CreateSettings = append(s.state.CreateSettings, settings)
	return settings, s.saveLocked()
}

func (s *FileStore) Users() []User {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]User(nil), s.state.Users...)
}

func (s *FileStore) CreateUser(username, password string) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	username = normalizeUsername(username)
	if err := validateUsername(username); err != nil {
		return User{}, err
	}
	if err := validatePassword(password); err != nil {
		return User{}, err
	}
	for _, user := range s.state.Users {
		if strings.EqualFold(user.Username, username) {
			return User{}, errCode("user_exists", "账号已存在", false)
		}
	}
	passwordHash, err := hashPassword(password)
	if err != nil {
		return User{}, err
	}
	now := time.Now()
	user := User{
		ID:           s.nextIDLocked("usr"),
		Username:     username,
		PasswordHash: passwordHash,
		IsAdmin:      len(s.state.Users) == 0,
		Status:       StatusActive,
		CreatedAt:    now,
		UpdatedAt:    now,
	}
	s.state.Users = append(s.state.Users, user)
	return user, s.saveLocked()
}

func (s *FileStore) AuthenticateUser(username, password string) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	username = normalizeUsername(username)
	for i, user := range s.state.Users {
		if !strings.EqualFold(user.Username, username) {
			continue
		}
		if user.Status != StatusActive {
			return User{}, errCode("user_disabled", "账号已停用", false)
		}
		if !verifyPassword(password, user.PasswordHash) {
			return User{}, errCode("invalid_login", "账号或密码错误", false)
		}
		now := time.Now()
		s.state.Users[i].LastLoginAt = now
		s.state.Users[i].UpdatedAt = now
		return s.state.Users[i], s.saveLocked()
	}
	return User{}, errCode("invalid_login", "账号或密码错误", false)
}

func (s *FileStore) UserByID(id string) (User, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.userByIDLocked(id)
}

func (s *FileStore) DeleteUser(id string) (DeleteUserResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	id = strings.TrimSpace(id)
	if id == "" {
		return DeleteUserResult{}, errCode("user_not_found", "账号不存在", false)
	}
	idx := -1
	var user User
	for i, candidate := range s.state.Users {
		if candidate.ID == id {
			idx = i
			user = candidate
			break
		}
	}
	if idx < 0 {
		return DeleteUserResult{}, errCode("user_not_found", "账号不存在", false)
	}
	if user.IsAdmin {
		return DeleteUserResult{}, errCode("cannot_delete_admin_user", "不能删除管理员账号", false)
	}

	result := DeleteUserResult{
		UserID:   user.ID,
		Username: user.Username,
	}
	s.state.Users = append(s.state.Users[:idx], s.state.Users[idx+1:]...)

	accounts := s.state.Accounts[:0]
	for _, account := range s.state.Accounts {
		if constantTimeEqual(id, account.OwnerID) {
			result.Accounts++
			continue
		}
		accounts = append(accounts, account)
	}
	s.state.Accounts = accounts

	deletedMailboxIDs := make(map[string]struct{})
	mailboxes := s.state.Mailboxes[:0]
	for _, mailbox := range s.state.Mailboxes {
		if constantTimeEqual(id, mailbox.OwnerID) {
			result.Mailboxes++
			deletedMailboxIDs[mailbox.ID] = struct{}{}
			continue
		}
		mailboxes = append(mailboxes, mailbox)
	}
	s.state.Mailboxes = mailboxes

	messages := s.state.Messages[:0]
	for _, msg := range s.state.Messages {
		_, mailboxDeleted := deletedMailboxIDs[msg.MailboxID]
		if mailboxDeleted || constantTimeEqual(id, msg.OwnerID) {
			result.Messages++
			continue
		}
		messages = append(messages, msg)
	}
	s.state.Messages = messages

	icloudSessions := s.state.ICloudSessions[:0]
	for _, session := range s.state.ICloudSessions {
		if constantTimeEqual(id, session.OwnerID) {
			result.ICloudSessions++
			continue
		}
		icloudSessions = append(icloudSessions, session)
	}
	s.state.ICloudSessions = icloudSessions

	webSessions := s.state.WebSessions[:0]
	for _, session := range s.state.WebSessions {
		if constantTimeEqual(id, session.UserID) {
			result.WebSessions++
			continue
		}
		webSessions = append(webSessions, session)
	}
	s.state.WebSessions = webSessions

	return result, s.saveLocked()
}

func (s *FileStore) CreateWebSession(userID string, isAdmin bool, ttl time.Duration) (string, WebSession, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	userID = strings.TrimSpace(userID)
	if _, ok := s.userByIDLocked(userID); !ok {
		return "", WebSession{}, errCode("user_not_found", "账号不存在", false)
	}
	token, err := randomToken(32)
	if err != nil {
		return "", WebSession{}, err
	}
	now := time.Now()
	session := WebSession{
		TokenHash:  sessionTokenHash(token),
		UserID:     userID,
		IsAdmin:    isAdmin,
		CreatedAt:  now,
		LastSeenAt: now,
		ExpiresAt:  now.Add(ttl),
	}
	s.state.WebSessions = append(s.state.WebSessions, session)
	return token, session, s.saveLocked()
}

func (s *FileStore) WebSessionByToken(token string) (WebSession, User, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	tokenHash := sessionTokenHash(token)
	if strings.TrimSpace(token) == "" {
		return WebSession{}, User{}, false
	}
	now := time.Now()
	for _, session := range s.state.WebSessions {
		if !constantTimeEqual(tokenHash, session.TokenHash) || !session.ExpiresAt.After(now) {
			continue
		}
		user, ok := s.userByIDLocked(session.UserID)
		if !ok || user.Status != StatusActive {
			return WebSession{}, User{}, false
		}
		return session, user, true
	}
	return WebSession{}, User{}, false
}

func (s *FileStore) DeleteWebSession(token string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	tokenHash := sessionTokenHash(token)
	filtered := s.state.WebSessions[:0]
	for _, session := range s.state.WebSessions {
		if constantTimeEqual(tokenHash, session.TokenHash) {
			continue
		}
		filtered = append(filtered, session)
	}
	s.state.WebSessions = filtered
	return s.saveLocked()
}

func (s *FileStore) Path() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.path
}

func (s *FileStore) SetPath(path string) (State, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	path = strings.TrimSpace(path)
	if path == "" {
		path = filepath.Join("data", "state.json")
	}
	if strings.EqualFold(filepath.Ext(path), ".json") {
		path = filepath.Clean(path)
	} else {
		path = filepath.Join(path, "state.json")
	}
	current := cloneState(s.state)
	data, err := os.ReadFile(path)
	switch {
	case err == nil && len(strings.TrimSpace(string(data))) > 0:
		var next State
		if err := json.Unmarshal(data, &next); err != nil {
			return State{}, err
		}
		if next.NextID <= 0 {
			next.NextID = 1
		}
		s.state = next
	case err == nil:
		s.state = current
	case errors.Is(err, os.ErrNotExist):
		s.state = current
	default:
		return State{}, err
	}
	s.path = path
	if err := s.saveLocked(); err != nil {
		return State{}, err
	}
	return cloneState(s.state), nil
}

func (s *FileStore) AddAccount(label, appleID, note string) (Account, error) {
	return s.AddAccountForOwner("", label, appleID, note)
}

func (s *FileStore) AddAccountForOwner(ownerID, label, appleID, note string) (Account, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := time.Now()
	account := Account{
		ID:           s.nextIDLocked("acc"),
		OwnerID:      strings.TrimSpace(ownerID),
		Label:        strings.TrimSpace(label),
		AppleID:      strings.TrimSpace(appleID),
		Status:       StatusActive,
		ICloudStatus: ICloudStatusNeedLogin,
		Note:         strings.TrimSpace(note),
		CreatedAt:    now,
		UpdatedAt:    now,
	}
	if account.Label == "" {
		account.Label = account.ID
	}
	s.state.Accounts = append(s.state.Accounts, account)
	return account, s.saveLocked()
}

func (s *FileStore) AddMailbox(accountID, label, email string) (Mailbox, error) {
	return s.AddMailboxForOwner("", accountID, label, email)
}

func (s *FileStore) AddMailboxForOwner(ownerID, accountID, label, email string) (Mailbox, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	email = strings.ToLower(strings.TrimSpace(email))
	if email == "" {
		return Mailbox{}, errCode("provider_not_configured", "当前 MVP 需要先手动填入已创建的隐私邮箱；自动创建接口留给后续 iCloud Provider 接入", false)
	}
	for _, mailbox := range s.state.Mailboxes {
		if strings.EqualFold(mailbox.Email, email) {
			return Mailbox{}, errCode("mailbox_exists", "邮箱已存在", false)
		}
	}

	now := time.Now()
	token, err := randomToken(24)
	if err != nil {
		return Mailbox{}, err
	}
	if strings.TrimSpace(label) == "" {
		label = fmt.Sprintf("UPI-%s", time.Now().Format("0102-150405"))
	}
	mailbox := Mailbox{
		ID:           s.nextIDLocked("mbx"),
		OwnerID:      strings.TrimSpace(ownerID),
		AccountID:    strings.TrimSpace(accountID),
		Label:        strings.TrimSpace(label),
		Email:        email,
		APIToken:     token,
		APIActive:    true,
		ICloudActive: true,
		Status:       StatusAvailable,
		CreatedAt:    now,
		UpdatedAt:    now,
	}
	s.state.Mailboxes = append(s.state.Mailboxes, mailbox)
	return mailbox, s.saveLocked()
}

func (s *FileStore) UpsertMailboxFromRemote(ownerID, accountID string, remote ICloudRemoteMailbox, defaultNote string) (Mailbox, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	ownerID = strings.TrimSpace(ownerID)
	accountID = strings.TrimSpace(accountID)
	email := strings.ToLower(strings.TrimSpace(remote.Email))
	if email == "" {
		return Mailbox{}, false, errCode("mailbox_email_missing", "iCloud 返回的邮箱地址为空", false)
	}
	now := time.Now()
	for i, mailbox := range s.state.Mailboxes {
		if !strings.EqualFold(mailbox.Email, email) {
			continue
		}
		if strings.TrimSpace(mailbox.OwnerID) != ownerID {
			return Mailbox{}, false, errCode("mailbox_exists_other_owner", "邮箱已存在于其他登录账号的数据中，已跳过导入", false)
		}
		if strings.TrimSpace(remote.Label) != "" {
			s.state.Mailboxes[i].Label = strings.TrimSpace(remote.Label)
		}
		if accountID != "" && strings.TrimSpace(s.state.Mailboxes[i].AccountID) != accountID {
			s.state.Mailboxes[i].AccountID = accountID
		}
		s.state.Mailboxes[i].ICloudActive = remote.IsActive
		note := strings.TrimSpace(remote.Note)
		if note == "" {
			note = strings.TrimSpace(defaultNote)
		}
		if note != "" && strings.TrimSpace(s.state.Mailboxes[i].Note) == "" {
			s.state.Mailboxes[i].Note = note
		}
		s.state.Mailboxes[i].UpdatedAt = now
		return s.state.Mailboxes[i], false, s.saveLocked()
	}

	token, err := randomToken(24)
	if err != nil {
		return Mailbox{}, false, err
	}
	label := strings.TrimSpace(remote.Label)
	if label == "" {
		label = fmt.Sprintf("HME-%s", now.Format("0102-150405"))
	}
	note := strings.TrimSpace(remote.Note)
	if note == "" {
		note = strings.TrimSpace(defaultNote)
	}
	status := StatusAvailable
	if !remote.IsActive {
		status = StatusDisabled
	}
	mailbox := Mailbox{
		ID:           s.nextIDLocked("mbx"),
		OwnerID:      ownerID,
		AccountID:    accountID,
		Label:        label,
		Email:        email,
		APIToken:     token,
		APIActive:    true,
		ICloudActive: remote.IsActive,
		Status:       status,
		Note:         note,
		CreatedAt:    now,
		UpdatedAt:    now,
	}
	s.state.Mailboxes = append(s.state.Mailboxes, mailbox)
	return mailbox, true, s.saveLocked()
}

func (s *FileStore) ClaimAvailableMailbox(project, note string) (Mailbox, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	project = normalizeMailboxClaimProject(project)
	for i, mailbox := range s.state.Mailboxes {
		if !mailbox.APIActive || !mailbox.ICloudActive {
			continue
		}
		switch project {
		case "openai":
			if mailbox.OpenAIClaimed || (mailbox.Status != StatusAvailable && !mailbox.GrokClaimed) {
				continue
			}
			s.state.Mailboxes[i].OpenAIClaimed = true
		case "grok":
			if mailbox.GrokClaimed || (mailbox.Status != StatusAvailable && !mailbox.OpenAIClaimed) {
				continue
			}
			s.state.Mailboxes[i].GrokClaimed = true
		default:
			if mailbox.Status != StatusAvailable || mailbox.OpenAIClaimed || mailbox.GrokClaimed {
				continue
			}
			s.state.Mailboxes[i].Status = StatusUsed
		}
		if project == "openai" || project == "grok" {
			if s.state.Mailboxes[i].OpenAIClaimed && s.state.Mailboxes[i].GrokClaimed {
				s.state.Mailboxes[i].Status = StatusUsed
			} else {
				s.state.Mailboxes[i].Status = StatusAvailable
			}
		}
		if strings.TrimSpace(note) != "" {
			s.state.Mailboxes[i].Note = mergeMailboxClaimNote(s.state.Mailboxes[i].Note, note)
		}
		s.state.Mailboxes[i].UpdatedAt = time.Now()
		return s.state.Mailboxes[i], s.saveLocked()
	}
	return Mailbox{}, errCode("no_available_mailbox", "没有可用隐私邮箱", false)
}

func normalizeMailboxClaimProject(project string) string {
	switch strings.ToLower(strings.TrimSpace(project)) {
	case "openai", "chatgpt", "gpt":
		return "openai"
	case "grok", "xai", "x.ai":
		return "grok"
	default:
		return ""
	}
}

func (s *FileStore) SetMailboxClaimed(project string, emails []string, claimed bool) (int, []string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	project = normalizeMailboxClaimProject(project)
	if project == "" {
		return 0, nil, errCode("invalid_project", "project 必须是 openai 或 grok", false)
	}
	seen := make(map[string]struct{}, len(emails))
	missing := make([]string, 0)
	updated := 0
	for _, rawEmail := range emails {
		email := strings.ToLower(strings.TrimSpace(rawEmail))
		if email == "" {
			continue
		}
		if _, ok := seen[email]; ok {
			continue
		}
		seen[email] = struct{}{}
		index := -1
		for i := range s.state.Mailboxes {
			if strings.EqualFold(s.state.Mailboxes[i].Email, email) {
				index = i
				break
			}
		}
		if index < 0 {
			missing = append(missing, email)
			continue
		}
		mailbox := &s.state.Mailboxes[index]
		if project == "openai" {
			mailbox.OpenAIClaimed = claimed
		} else {
			mailbox.GrokClaimed = claimed
		}
		if mailbox.OpenAIClaimed && mailbox.GrokClaimed {
			mailbox.Status = StatusUsed
		} else if mailbox.OpenAIClaimed || mailbox.GrokClaimed {
			mailbox.Status = StatusAvailable
		} else if mailbox.Status == StatusUsed {
			mailbox.Status = StatusAvailable
		}
		mailbox.UpdatedAt = time.Now()
		updated++
	}
	if updated > 0 {
		if err := s.saveLocked(); err != nil {
			return 0, missing, err
		}
	}
	return updated, missing, nil
}

func mergeMailboxClaimNote(existing, incoming string) string {
	existing = strings.TrimSpace(existing)
	incoming = strings.TrimSpace(incoming)
	if existing == "" || !strings.HasPrefix(existing, "外部 API 已领取") {
		return incoming
	}
	label := strings.TrimSpace(strings.TrimPrefix(incoming, "外部 API 已领取："))
	if label == "" || strings.Contains(strings.ToLower(existing), strings.ToLower(label)) {
		return existing
	}
	return existing + "；" + label
}

func (s *FileStore) SaveICloudSession(session ICloudSession) error {
	return s.SaveICloudSessionForOwner("", session)
}

func (s *FileStore) SaveICloudSessionForOwner(ownerID string, session ICloudSession) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	ownerID = strings.TrimSpace(ownerID)
	session.OwnerID = ownerID
	if strings.TrimSpace(session.AccountID) == "" && shouldCreateICloudAccount(session) {
		session.AccountID = s.ensureICloudAccountLocked(ownerID, session)
	}
	if session.SavedAt.IsZero() {
		session.SavedAt = time.Now()
	}
	if ownerID != "" {
		if strings.TrimSpace(session.AccountID) == "" {
			session.AccountID = s.ensureICloudAccountLocked(ownerID, session)
		} else {
			s.touchICloudAccountLocked(ownerID, session.AccountID, session)
		}
		for i, existing := range s.state.ICloudSessions {
			if constantTimeEqual(ownerID, existing.OwnerID) && sameICloudSessionIdentity(existing, session) {
				merged := mergeICloudSession(existing, session)
				s.state.ICloudSessions[i] = merged
				if strings.TrimSpace(merged.AccountID) != "" {
					s.touchICloudAccountLocked(ownerID, merged.AccountID, merged)
				}
				s.pruneDuplicateIMAPOnlySessionsLocked(ownerID, merged, i)
				return s.saveLocked()
			}
		}
		s.state.ICloudSessions = append(s.state.ICloudSessions, session)
		s.pruneDuplicateIMAPOnlySessionsLocked(ownerID, session, len(s.state.ICloudSessions)-1)
		return s.saveLocked()
	}
	if s.state.ICloudSession != nil && sameICloudSessionIdentity(*s.state.ICloudSession, session) {
		session = mergeICloudSession(*s.state.ICloudSession, session)
	}
	s.state.ICloudSession = &session
	if strings.TrimSpace(session.AccountID) != "" {
		s.migrateLegacyMailboxAccountIDsLocked()
	}
	return s.saveLocked()
}

func (s *FileStore) ICloudSession() (ICloudSession, bool) {
	return s.ICloudSessionForOwner("")
}

func (s *FileStore) ICloudSessionForOwner(ownerID string) (ICloudSession, bool) {
	sessions := s.ICloudSessionsForOwner(ownerID)
	if len(sessions) == 0 {
		return ICloudSession{}, false
	}
	return sessions[0], true
}

func (s *FileStore) ICloudSessionsForOwner(ownerID string) []ICloudSession {
	s.mu.Lock()
	defer s.mu.Unlock()

	ownerID = strings.TrimSpace(ownerID)
	if ownerID != "" {
		out := make([]ICloudSession, 0, 2)
		for _, session := range s.state.ICloudSessions {
			if constantTimeEqual(ownerID, session.OwnerID) {
				out = append(out, cloneICloudSession(session))
			}
		}
		return out
	}
	out := make([]ICloudSession, 0, len(s.state.ICloudSessions)+1)
	if s.state.ICloudSession != nil {
		out = append(out, cloneICloudSession(*s.state.ICloudSession))
	}
	for _, session := range s.state.ICloudSessions {
		if strings.TrimSpace(session.OwnerID) == "" {
			out = append(out, cloneICloudSession(session))
		}
	}
	return out
}

func (s *FileStore) ICloudSessionForOwnerAccount(ownerID, accountID string) (ICloudSession, bool) {
	accountID = strings.TrimSpace(accountID)
	if accountID == "" {
		return s.ICloudSessionForOwner(ownerID)
	}
	for _, session := range s.ICloudSessionsForOwner(ownerID) {
		if constantTimeEqual(accountID, session.AccountID) {
			return session, true
		}
	}
	return ICloudSession{}, false
}

func (s *FileStore) FindAccountByID(id string) (Account, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	id = strings.TrimSpace(id)
	for _, account := range s.state.Accounts {
		if account.ID == id {
			return account, true
		}
	}
	return Account{}, false
}

func (s *FileStore) AddMessage(mailboxID, subject, from, body string, receivedAt time.Time) (Message, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	idx := s.mailboxIndexLocked(mailboxID)
	if idx < 0 {
		return Message{}, errCode("mailbox_not_found", "邮箱不存在", false)
	}
	if receivedAt.IsZero() {
		receivedAt = time.Now()
	}
	msg := Message{
		ID:         s.nextIDLocked("msg"),
		OwnerID:    s.state.Mailboxes[idx].OwnerID,
		MailboxID:  mailboxID,
		Subject:    strings.TrimSpace(subject),
		From:       strings.TrimSpace(from),
		Body:       body,
		ReceivedAt: receivedAt,
		CreatedAt:  time.Now(),
	}
	s.state.Messages = append(s.state.Messages, msg)
	s.state.Mailboxes[idx].ReceiveCount++
	s.state.Mailboxes[idx].UpdatedAt = time.Now()
	return msg, s.saveLocked()
}

func (s *FileStore) UpsertMessage(mailboxID, remoteID, source, subject, from, body string, receivedAt time.Time) (Message, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	idx := s.mailboxIndexLocked(mailboxID)
	if idx < 0 {
		return Message{}, false, errCode("mailbox_not_found", "邮箱不存在", false)
	}
	remoteID = strings.TrimSpace(remoteID)
	if remoteID != "" {
		for i, msg := range s.state.Messages {
			if msg.MailboxID == mailboxID && msg.RemoteID == remoteID {
				s.state.Messages[i].OwnerID = s.state.Mailboxes[idx].OwnerID
				s.state.Messages[i].Source = strings.TrimSpace(source)
				s.state.Messages[i].Subject = strings.TrimSpace(subject)
				s.state.Messages[i].From = strings.TrimSpace(from)
				s.state.Messages[i].Body = body
				if !receivedAt.IsZero() {
					s.state.Messages[i].ReceivedAt = receivedAt
				}
				s.state.Messages[i].CreatedAt = firstNonZeroTime(s.state.Messages[i].CreatedAt, time.Now())
				s.state.Mailboxes[idx].UpdatedAt = time.Now()
				return s.state.Messages[i], false, s.saveLocked()
			}
		}
	}
	if receivedAt.IsZero() {
		receivedAt = time.Now()
	}
	msg := Message{
		ID:         s.nextIDLocked("msg"),
		OwnerID:    s.state.Mailboxes[idx].OwnerID,
		MailboxID:  mailboxID,
		RemoteID:   remoteID,
		Source:     strings.TrimSpace(source),
		Subject:    strings.TrimSpace(subject),
		From:       strings.TrimSpace(from),
		Body:       body,
		ReceivedAt: receivedAt,
		CreatedAt:  time.Now(),
	}
	s.state.Messages = append(s.state.Messages, msg)
	s.state.Mailboxes[idx].ReceiveCount++
	s.state.Mailboxes[idx].UpdatedAt = time.Now()
	return msg, true, s.saveLocked()
}

func (s *FileStore) SetMailboxStatus(id string, apiActive *bool, icloudActive *bool, status, note string) (Mailbox, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	idx := s.mailboxIndexLocked(id)
	if idx < 0 {
		return Mailbox{}, errCode("mailbox_not_found", "邮箱不存在", false)
	}
	if apiActive != nil {
		s.state.Mailboxes[idx].APIActive = *apiActive
	}
	if icloudActive != nil {
		s.state.Mailboxes[idx].ICloudActive = *icloudActive
	}
	if strings.TrimSpace(status) != "" {
		s.state.Mailboxes[idx].Status = strings.TrimSpace(status)
	}
	if strings.TrimSpace(note) != "" {
		s.state.Mailboxes[idx].Note = strings.TrimSpace(note)
	}
	s.state.Mailboxes[idx].UpdatedAt = time.Now()
	return s.state.Mailboxes[idx], s.saveLocked()
}

func (s *FileStore) SetMailboxSyncCursor(id string, syncedAt time.Time, lastUID string) (Mailbox, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	idx := s.mailboxIndexLocked(id)
	if idx < 0 {
		return Mailbox{}, errCode("mailbox_not_found", "邮箱不存在", false)
	}
	if syncedAt.IsZero() {
		syncedAt = time.Now()
	}
	s.state.Mailboxes[idx].LastSyncAt = syncedAt
	if strings.TrimSpace(lastUID) != "" {
		s.state.Mailboxes[idx].LastSyncUID = strings.TrimSpace(lastUID)
	}
	s.state.Mailboxes[idx].UpdatedAt = time.Now()
	return s.state.Mailboxes[idx], s.saveLocked()
}

func (s *FileStore) SetICloudIMAPSyncCursor(ownerID, accountID, stateKey string, syncedAt time.Time, lastUID string) (ICloudSession, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	ownerID = strings.TrimSpace(ownerID)
	accountID = strings.TrimSpace(accountID)
	stateKey = strings.TrimSpace(stateKey)
	if syncedAt.IsZero() {
		syncedAt = time.Now()
	}
	updateSession := func(session *ICloudSession) bool {
		if session == nil {
			return false
		}
		if ownerID != "" && !constantTimeEqual(ownerID, session.OwnerID) {
			return false
		}
		if accountID != "" && !constantTimeEqual(accountID, session.AccountID) {
			return false
		}
		for i, state := range session.LoginStates {
			if state.Kind != LoginStateICloudIMAP {
				continue
			}
			if accountID == "" && stateKey != "" && imapStateKey(state) != stateKey {
				continue
			}
			session.LoginStates[i].IMAPLastSyncAt = syncedAt
			if strings.TrimSpace(lastUID) != "" {
				session.LoginStates[i].IMAPLastSyncUID = strings.TrimSpace(lastUID)
			}
			return true
		}
		return false
	}
	if ownerID == "" && s.state.ICloudSession != nil && updateSession(s.state.ICloudSession) {
		return cloneICloudSession(*s.state.ICloudSession), s.saveLocked()
	}
	for i := range s.state.ICloudSessions {
		if updateSession(&s.state.ICloudSessions[i]) {
			updated := cloneICloudSession(s.state.ICloudSessions[i])
			return updated, s.saveLocked()
		}
	}
	return ICloudSession{}, errCode("imap_session_missing", "未找到取码登录态，无法保存 IMAP 同步游标", true)
}

func (s *FileStore) SetMailboxLastCode(id string, messageID string, servedAt time.Time) (Mailbox, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	idx := s.mailboxIndexLocked(id)
	if idx < 0 {
		return Mailbox{}, errCode("mailbox_not_found", "邮箱不存在", false)
	}
	messageID = strings.TrimSpace(messageID)
	if messageID == "" {
		return Mailbox{}, errCode("message_id_missing", "验证码邮件 ID 为空", false)
	}
	if servedAt.IsZero() {
		servedAt = time.Now()
	}
	s.state.Mailboxes[idx].LastCodeMessageID = messageID
	s.state.Mailboxes[idx].LastCodeAt = servedAt
	s.state.Mailboxes[idx].UpdatedAt = time.Now()
	return s.state.Mailboxes[idx], s.saveLocked()
}

func (s *FileStore) DeleteMailbox(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	idx := s.mailboxIndexLocked(id)
	if idx < 0 {
		return errCode("mailbox_not_found", "邮箱不存在", false)
	}
	s.state.Mailboxes = append(s.state.Mailboxes[:idx], s.state.Mailboxes[idx+1:]...)
	filtered := s.state.Messages[:0]
	for _, msg := range s.state.Messages {
		if msg.MailboxID != id {
			filtered = append(filtered, msg)
		}
	}
	s.state.Messages = filtered
	return s.saveLocked()
}

func (s *FileStore) FindMailboxByID(id string) (Mailbox, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	idx := s.mailboxIndexLocked(id)
	if idx < 0 {
		return Mailbox{}, false
	}
	return s.state.Mailboxes[idx], true
}

func (s *FileStore) FindMailboxByEmail(email string) (Mailbox, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	for _, mailbox := range s.state.Mailboxes {
		if strings.EqualFold(mailbox.Email, email) {
			return mailbox, true
		}
	}
	return Mailbox{}, false
}

func (s *FileStore) MessagesForMailbox(mailboxID string) []Message {
	s.mu.Lock()
	defer s.mu.Unlock()

	var out []Message
	for _, msg := range s.state.Messages {
		if msg.MailboxID == mailboxID {
			out = append(out, msg)
		}
	}
	return out
}

func (s *FileStore) nextIDLocked(prefix string) string {
	id := fmt.Sprintf("%s_%06d", prefix, s.state.NextID)
	s.state.NextID++
	return id
}

func (s *FileStore) ensureICloudAccountLocked(ownerID string, session ICloudSession) string {
	ownerID = strings.TrimSpace(ownerID)
	for _, existing := range s.state.ICloudSessions {
		if constantTimeEqual(ownerID, existing.OwnerID) && sameICloudSessionIdentity(existing, session) && strings.TrimSpace(existing.AccountID) != "" {
			s.touchICloudAccountLocked(ownerID, existing.AccountID, session)
			return existing.AccountID
		}
	}

	appleID := strings.TrimSpace(session.AppleID)
	if appleID != "" {
		for i, account := range s.state.Accounts {
			if constantTimeEqual(ownerID, account.OwnerID) && strings.EqualFold(strings.TrimSpace(account.AppleID), appleID) {
				s.updateICloudAccountFromSessionLocked(i, session)
				return account.ID
			}
		}
	}

	now := time.Now()
	label := appleID
	if label == "" && strings.TrimSpace(session.DSID) != "" {
		label = "iCloud " + maskSecret(session.DSID, 4)
	}
	if label == "" {
		label = "iCloud " + now.Format("0102-150405")
	}
	account := Account{
		ID:           s.nextIDLocked("acc"),
		OwnerID:      ownerID,
		Label:        label,
		AppleID:      appleID,
		Status:       StatusActive,
		ICloudStatus: iCloudStatusFromSession(session),
		CreatedAt:    now,
		UpdatedAt:    now,
	}
	s.state.Accounts = append(s.state.Accounts, account)
	return account.ID
}

func (s *FileStore) touchICloudAccountLocked(ownerID, accountID string, session ICloudSession) {
	ownerID = strings.TrimSpace(ownerID)
	accountID = strings.TrimSpace(accountID)
	if accountID == "" {
		return
	}
	for i, account := range s.state.Accounts {
		if account.ID == accountID && constantTimeEqual(ownerID, account.OwnerID) {
			s.updateICloudAccountFromSessionLocked(i, session)
			return
		}
	}
}

func (s *FileStore) updateICloudAccountFromSessionLocked(index int, session ICloudSession) {
	if index < 0 || index >= len(s.state.Accounts) {
		return
	}
	account := &s.state.Accounts[index]
	if appleID := strings.TrimSpace(session.AppleID); appleID != "" {
		account.AppleID = appleID
		if strings.TrimSpace(account.Label) == "" || strings.HasPrefix(strings.TrimSpace(account.Label), "iCloud ") {
			account.Label = appleID
		}
	}
	account.Status = StatusActive
	account.ICloudStatus = iCloudStatusFromSession(session)
	account.UpdatedAt = time.Now()
}

func sameICloudSessionIdentity(a, b ICloudSession) bool {
	if strings.TrimSpace(a.AccountID) != "" && constantTimeEqual(a.AccountID, b.AccountID) {
		return true
	}
	if strings.TrimSpace(a.DSID) != "" && constantTimeEqual(a.DSID, b.DSID) {
		return true
	}
	if strings.TrimSpace(a.AppleID) != "" && strings.EqualFold(strings.TrimSpace(a.AppleID), strings.TrimSpace(b.AppleID)) {
		return true
	}
	return false
}

func (s *FileStore) pruneDuplicateIMAPOnlySessionsLocked(ownerID string, target ICloudSession, targetIndex int) {
	ownerID = strings.TrimSpace(ownerID)
	targetIMAPEmail := sessionIMAPEmail(target)
	if targetIMAPEmail == "" {
		return
	}
	targetLocal := emailLocalPart(targetIMAPEmail)
	removedAccountIDs := map[string]struct{}{}
	next := s.state.ICloudSessions[:0]
	for i, session := range s.state.ICloudSessions {
		if i == targetIndex || !constantTimeEqual(ownerID, session.OwnerID) {
			next = append(next, session)
			continue
		}
		if hasCreateLoginState(session) {
			next = append(next, session)
			continue
		}
		imapEmail := sessionIMAPEmail(session)
		sameIMAPEmail := targetIMAPEmail != "" && strings.EqualFold(imapEmail, targetIMAPEmail)
		sameLocalPart := targetLocal != "" && strings.EqualFold(emailLocalPart(imapEmail), targetLocal)
		if sameIMAPEmail || sameLocalPart {
			if accountID := strings.TrimSpace(session.AccountID); accountID != "" {
				removedAccountIDs[accountID] = struct{}{}
			}
			continue
		}
		next = append(next, session)
	}
	s.state.ICloudSessions = next
	if len(removedAccountIDs) > 0 {
		s.removeAccountIDsFromCreateSettingsLocked(ownerID, removedAccountIDs)
		s.pruneRemovedIMAPOnlyAccountsLocked(ownerID, removedAccountIDs)
	}
}

func (s *FileStore) removeAccountIDsFromCreateSettingsLocked(ownerID string, accountIDs map[string]struct{}) {
	for i, settings := range s.state.CreateSettings {
		if !constantTimeEqual(ownerID, settings.OwnerID) {
			continue
		}
		next := settings.AccountIDs[:0]
		for _, accountID := range settings.AccountIDs {
			if _, remove := accountIDs[strings.TrimSpace(accountID)]; remove {
				continue
			}
			next = append(next, accountID)
		}
		s.state.CreateSettings[i].AccountIDs = normalizeAccountIDSelection("", next)
		s.state.CreateSettings[i].UpdatedAt = time.Now()
	}
}

func (s *FileStore) pruneRemovedIMAPOnlyAccountsLocked(ownerID string, accountIDs map[string]struct{}) {
	referenced := map[string]struct{}{}
	for _, session := range s.state.ICloudSessions {
		if constantTimeEqual(ownerID, session.OwnerID) {
			if accountID := strings.TrimSpace(session.AccountID); accountID != "" {
				referenced[accountID] = struct{}{}
			}
		}
	}
	for _, mailbox := range s.state.Mailboxes {
		if constantTimeEqual(ownerID, mailbox.OwnerID) {
			if accountID := strings.TrimSpace(mailbox.AccountID); accountID != "" {
				referenced[accountID] = struct{}{}
			}
		}
	}
	next := s.state.Accounts[:0]
	for _, account := range s.state.Accounts {
		accountID := strings.TrimSpace(account.ID)
		if constantTimeEqual(ownerID, account.OwnerID) {
			if _, wasRemovedSessionAccount := accountIDs[accountID]; wasRemovedSessionAccount {
				if _, stillReferenced := referenced[accountID]; !stillReferenced {
					continue
				}
			}
		}
		next = append(next, account)
	}
	s.state.Accounts = next
}

func hasCreateLoginState(session ICloudSession) bool {
	if len(session.Cookies) > 0 {
		return true
	}
	for _, state := range session.LoginStates {
		if state.Kind == LoginStateICloudWeb && len(state.Cookies) > 0 {
			return true
		}
		if state.Kind == LoginStateAppleAccount && strings.TrimSpace(state.Scnt) != "" {
			return true
		}
	}
	return false
}

func sessionIMAPEmail(session ICloudSession) string {
	for _, state := range session.LoginStates {
		if state.Kind != LoginStateICloudIMAP {
			continue
		}
		email := normalizeICloudIMAPEmail(firstNonEmpty(state.IMAPEmail, state.IMAPUsername, session.AppleID))
		if email != "" && strings.TrimSpace(state.IMAPAppPassword) != "" {
			return email
		}
	}
	return ""
}

func iCloudStatusFromSession(session ICloudSession) string {
	if len(session.Cookies) == 0 {
		return ICloudStatusNeedLogin
	}
	if !session.IsICloudPlus {
		return ICloudStatusNoICloudPlus
	}
	if !session.CanCreateHME {
		return ICloudStatusFailed
	}
	return ICloudStatusActive
}

func (s *FileStore) mailboxIndexLocked(id string) int {
	for i, mailbox := range s.state.Mailboxes {
		if mailbox.ID == id {
			return i
		}
	}
	return -1
}

func (s *FileStore) userByIDLocked(id string) (User, bool) {
	id = strings.TrimSpace(id)
	for _, user := range s.state.Users {
		if user.ID == id {
			return user, true
		}
	}
	return User{}, false
}

func (s *FileStore) saveLocked() error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(s.state, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func (s *FileStore) migrateLegacyMailboxAccountIDsLocked() bool {
	accountsByOwner := make(map[string][]Account)
	for _, account := range s.state.Accounts {
		ownerID := strings.TrimSpace(account.OwnerID)
		accountsByOwner[ownerID] = append(accountsByOwner[ownerID], account)
	}

	changed := false
	now := time.Now()
	for i := range s.state.Mailboxes {
		if strings.TrimSpace(s.state.Mailboxes[i].AccountID) != "" {
			continue
		}
		ownerID := strings.TrimSpace(s.state.Mailboxes[i].OwnerID)
		accounts := accountsByOwner[ownerID]
		if len(accounts) != 1 {
			continue
		}
		s.state.Mailboxes[i].AccountID = accounts[0].ID
		if s.state.Mailboxes[i].UpdatedAt.IsZero() {
			s.state.Mailboxes[i].UpdatedAt = now
		}
		changed = true
	}
	return changed
}

func (s *FileStore) migrateLegacyICloudSessionAccountIDsLocked() bool {
	changed := false
	if s.state.ICloudSession != nil && strings.TrimSpace(s.state.ICloudSession.AccountID) == "" && shouldCreateICloudAccount(*s.state.ICloudSession) {
		s.state.ICloudSession.AccountID = s.ensureICloudAccountLocked("", *s.state.ICloudSession)
		changed = strings.TrimSpace(s.state.ICloudSession.AccountID) != ""
	}
	for i := range s.state.ICloudSessions {
		session := &s.state.ICloudSessions[i]
		if strings.TrimSpace(session.AccountID) != "" || !shouldCreateICloudAccount(*session) {
			continue
		}
		session.AccountID = s.ensureICloudAccountLocked(session.OwnerID, *session)
		if strings.TrimSpace(session.AccountID) != "" {
			changed = true
		}
	}
	return changed
}

func (s *FileStore) migrateLegacyICloudSessionToAdminLocked() bool {
	if s.state.ICloudSession == nil {
		return false
	}
	adminID := singleActiveAdminID(s.state.Users)
	if adminID == "" {
		return false
	}

	legacy := cloneICloudSession(*s.state.ICloudSession)
	legacyAccountID := strings.TrimSpace(legacy.AccountID)
	targetIndex := -1
	for i, session := range s.state.ICloudSessions {
		if constantTimeEqual(adminID, session.OwnerID) && sameICloudSessionIdentity(session, legacy) {
			targetIndex = i
			break
		}
	}

	targetAccountID := ""
	if targetIndex >= 0 {
		existing := s.state.ICloudSessions[targetIndex]
		targetAccountID = strings.TrimSpace(existing.AccountID)
		legacy.OwnerID = adminID
		legacy.AccountID = targetAccountID
		merged := mergeICloudSession(existing, legacy)
		merged.OwnerID = adminID
		merged.AccountID = targetAccountID
		s.state.ICloudSessions[targetIndex] = merged
		if targetAccountID != "" {
			s.touchICloudAccountLocked(adminID, targetAccountID, merged)
		}
	} else {
		legacy.OwnerID = adminID
		if legacyAccountID != "" && s.reassignAccountOwnerLocked(legacyAccountID, adminID) {
			targetAccountID = legacyAccountID
		} else {
			legacy.AccountID = ""
			targetAccountID = s.ensureICloudAccountLocked(adminID, legacy)
		}
		legacy.AccountID = targetAccountID
		s.state.ICloudSessions = append(s.state.ICloudSessions, legacy)
	}

	migratedMailboxIDs := make(map[string]struct{})
	for i := range s.state.Mailboxes {
		mailbox := &s.state.Mailboxes[i]
		if strings.TrimSpace(mailbox.OwnerID) != "" || legacyAccountID == "" || mailbox.AccountID != legacyAccountID {
			continue
		}
		mailbox.OwnerID = adminID
		if targetAccountID != "" {
			mailbox.AccountID = targetAccountID
		}
		migratedMailboxIDs[mailbox.ID] = struct{}{}
	}
	for i := range s.state.Messages {
		if _, ok := migratedMailboxIDs[s.state.Messages[i].MailboxID]; ok {
			s.state.Messages[i].OwnerID = adminID
		}
	}
	s.migrateLegacyCreateSettingsLocked(adminID, legacyAccountID, targetAccountID)
	if legacyAccountID != "" && legacyAccountID != targetAccountID {
		s.removeUnreferencedAccountLocked(legacyAccountID)
	}
	s.state.ICloudSession = nil
	return true
}

func singleActiveAdminID(users []User) string {
	adminID := ""
	for _, user := range users {
		if !user.IsAdmin || (user.Status != "" && user.Status != StatusActive) || strings.TrimSpace(user.ID) == "" {
			continue
		}
		if adminID != "" {
			return ""
		}
		adminID = strings.TrimSpace(user.ID)
	}
	return adminID
}

func (s *FileStore) reassignAccountOwnerLocked(accountID, ownerID string) bool {
	for i := range s.state.Accounts {
		if s.state.Accounts[i].ID == accountID {
			s.state.Accounts[i].OwnerID = ownerID
			s.state.Accounts[i].UpdatedAt = time.Now()
			return true
		}
	}
	return false
}

func (s *FileStore) migrateLegacyCreateSettingsLocked(ownerID, legacyAccountID, targetAccountID string) {
	hasOwnerSettings := false
	for _, settings := range s.state.CreateSettings {
		if constantTimeEqual(ownerID, settings.OwnerID) {
			hasOwnerSettings = true
			break
		}
	}
	next := s.state.CreateSettings[:0]
	for _, settings := range s.state.CreateSettings {
		if strings.TrimSpace(settings.OwnerID) != "" {
			next = append(next, settings)
			continue
		}
		if hasOwnerSettings {
			continue
		}
		settings.OwnerID = ownerID
		for i, accountID := range settings.AccountIDs {
			if accountID == legacyAccountID && targetAccountID != "" {
				settings.AccountIDs[i] = targetAccountID
			}
		}
		next = append(next, settings)
		hasOwnerSettings = true
	}
	s.state.CreateSettings = next
}

func (s *FileStore) removeUnreferencedAccountLocked(accountID string) {
	for _, session := range s.state.ICloudSessions {
		if session.AccountID == accountID {
			return
		}
	}
	for _, mailbox := range s.state.Mailboxes {
		if mailbox.AccountID == accountID {
			return
		}
	}
	next := s.state.Accounts[:0]
	for _, account := range s.state.Accounts {
		if account.ID != accountID {
			next = append(next, account)
		}
	}
	s.state.Accounts = next
}

func shouldCreateICloudAccount(session ICloudSession) bool {
	if strings.TrimSpace(session.AccountID) != "" {
		return false
	}
	if strings.TrimSpace(session.AppleID) == "" {
		return false
	}
	if strings.TrimSpace(session.DSID) != "" || len(session.Cookies) > 0 {
		return true
	}
	for _, state := range session.LoginStates {
		if state.Kind == LoginStateAppleAccount || state.Kind == LoginStateICloudWeb {
			return true
		}
	}
	return false
}

func (s *FileStore) migrateLegacyMailboxClaimsLocked() bool {
	changed := false
	for i := range s.state.Mailboxes {
		mailbox := &s.state.Mailboxes[i]
		if mailbox.Status != StatusUsed || mailbox.OpenAIClaimed || mailbox.GrokClaimed {
			continue
		}
		note := strings.ToLower(strings.TrimSpace(mailbox.Note))
		openAIClaimed := strings.Contains(note, "openai register") || strings.Contains(note, "chatgpt register")
		grokClaimed := strings.Contains(note, "grok register") || strings.Contains(note, "xai register")
		if !openAIClaimed && !grokClaimed {
			continue
		}
		mailbox.OpenAIClaimed = openAIClaimed
		mailbox.GrokClaimed = grokClaimed
		if mailbox.OpenAIClaimed != mailbox.GrokClaimed {
			mailbox.Status = StatusAvailable
		}
		changed = true
	}
	return changed
}

func cloneState(in State) State {
	out := in
	out.Users = append([]User(nil), in.Users...)
	out.WebSessions = append([]WebSession(nil), in.WebSessions...)
	out.Accounts = append([]Account(nil), in.Accounts...)
	out.Mailboxes = append([]Mailbox(nil), in.Mailboxes...)
	out.Messages = append([]Message(nil), in.Messages...)
	if in.ICloudSession != nil {
		session := cloneICloudSession(*in.ICloudSession)
		out.ICloudSession = &session
	}
	out.ICloudSessions = cloneICloudSessions(in.ICloudSessions)
	out.CreateSettings = cloneCreateSettings(in.CreateSettings)
	return out
}

func cloneICloudSession(in ICloudSession) ICloudSession {
	out := in
	out.Cookies = append([]SessionCookie(nil), in.Cookies...)
	out.LoginStates = cloneLoginStates(in.LoginStates)
	return out
}

func mergeICloudSession(existing, incoming ICloudSession) ICloudSession {
	out := incoming
	out.OwnerID = firstNonEmpty(incoming.OwnerID, existing.OwnerID)
	out.AccountID = firstNonEmpty(incoming.AccountID, existing.AccountID)
	if out.SavedAt.IsZero() {
		out.SavedAt = existing.SavedAt
	}
	out.AppleID = firstNonEmpty(incoming.AppleID, existing.AppleID)
	out.DSID = firstNonEmpty(incoming.DSID, existing.DSID)
	out.ClientID = firstNonEmpty(incoming.ClientID, existing.ClientID)
	out.ClientBuildNumber = firstNonEmpty(incoming.ClientBuildNumber, existing.ClientBuildNumber)
	out.MasteringNumber = firstNonEmpty(incoming.MasteringNumber, existing.MasteringNumber)
	out.PremiumMailBaseURL = firstNonEmpty(incoming.PremiumMailBaseURL, existing.PremiumMailBaseURL)
	out.MailGatewayBaseURL = firstNonEmpty(incoming.MailGatewayBaseURL, existing.MailGatewayBaseURL)
	out.MailBaseURL = firstNonEmpty(incoming.MailBaseURL, existing.MailBaseURL)
	out.Host = firstNonEmpty(incoming.Host, existing.Host)
	out.IsICloudPlus = incoming.IsICloudPlus || existing.IsICloudPlus
	out.CanCreateHME = incoming.CanCreateHME || existing.CanCreateHME
	if len(out.Cookies) == 0 {
		out.Cookies = append([]SessionCookie(nil), existing.Cookies...)
	}
	out.LoginStates = mergeLoginStates(existing.LoginStates, incoming.LoginStates)
	out.Note = firstNonEmpty(incoming.Note, existing.Note)
	if out.LastCheckedAt.IsZero() {
		out.LastCheckedAt = existing.LastCheckedAt
	}
	if !incoming.LastCheckOK {
		out.LastCheckOK = existing.LastCheckOK
	}
	out.LastStatusMessage = firstNonEmpty(incoming.LastStatusMessage, existing.LastStatusMessage)
	return out
}

func mergeLoginStates(existing, incoming []LoginState) []LoginState {
	out := cloneLoginStates(existing)
	for _, state := range incoming {
		replaced := false
		for i, current := range out {
			if current.Kind == state.Kind {
				next := state
				next.Cookies = append([]SessionCookie(nil), state.Cookies...)
				out[i] = next
				replaced = true
				break
			}
		}
		if !replaced {
			next := state
			next.Cookies = append([]SessionCookie(nil), state.Cookies...)
			out = append(out, next)
		}
	}
	return out
}

func cloneLoginStates(in []LoginState) []LoginState {
	out := make([]LoginState, 0, len(in))
	for _, state := range in {
		next := state
		next.Cookies = append([]SessionCookie(nil), state.Cookies...)
		out = append(out, next)
	}
	return out
}

func cloneICloudSessions(in []ICloudSession) []ICloudSession {
	out := make([]ICloudSession, 0, len(in))
	for _, session := range in {
		out = append(out, cloneICloudSession(session))
	}
	return out
}

func cloneCreateSettings(in []CreateSettings) []CreateSettings {
	out := make([]CreateSettings, 0, len(in))
	for _, settings := range in {
		next := settings
		next.AccountIDs = append([]string(nil), settings.AccountIDs...)
		out = append(out, next)
	}
	return out
}

func filterStateByOwnerLocked(in State, ownerID string) State {
	if ownerID == "" {
		return cloneState(in)
	}
	out := State{NextID: in.NextID}
	for _, user := range in.Users {
		if user.ID == ownerID {
			out.Users = append(out.Users, user)
			break
		}
	}
	for _, account := range in.Accounts {
		if constantTimeEqual(ownerID, account.OwnerID) {
			out.Accounts = append(out.Accounts, account)
		}
	}
	allowedMailboxes := make(map[string]struct{})
	for _, mailbox := range in.Mailboxes {
		if constantTimeEqual(ownerID, mailbox.OwnerID) {
			out.Mailboxes = append(out.Mailboxes, mailbox)
			allowedMailboxes[mailbox.ID] = struct{}{}
		}
	}
	for _, msg := range in.Messages {
		if _, ok := allowedMailboxes[msg.MailboxID]; ok || constantTimeEqual(ownerID, msg.OwnerID) {
			out.Messages = append(out.Messages, msg)
		}
	}
	for _, session := range in.ICloudSessions {
		if constantTimeEqual(ownerID, session.OwnerID) {
			cloned := cloneICloudSession(session)
			if out.ICloudSession == nil {
				first := cloneICloudSession(session)
				out.ICloudSession = &first
			}
			out.ICloudSessions = append(out.ICloudSessions, cloned)
		}
	}
	for _, settings := range in.CreateSettings {
		if constantTimeEqual(ownerID, settings.OwnerID) {
			next := settings
			next.AccountIDs = append([]string(nil), settings.AccountIDs...)
			out.CreateSettings = append(out.CreateSettings, next)
		}
	}
	return out
}

func createSettingsForOwnerLocked(state State, ownerID string) CreateSettings {
	ownerID = strings.TrimSpace(ownerID)
	for _, settings := range state.CreateSettings {
		if constantTimeEqual(ownerID, settings.OwnerID) {
			return normalizeCreateSettings(ownerID, settings)
		}
	}
	return defaultCreateSettings(ownerID)
}

func defaultCreateSettings(ownerID string) CreateSettings {
	return CreateSettings{
		OwnerID:                       strings.TrimSpace(ownerID),
		CreateChannel:                 string(mailboxCreateChannelAuto),
		SchedulerCreateChannel:        string(mailboxCreateChannelAuto),
		AppleAccountTwoFactorMethod:   appleTwoFactorMethodTrustedDevice,
		ICloudWebTwoFactorMethod:      appleTwoFactorMethodTrustedDevice,
		SchedulerIntervalMinutes:      int(defaultMailboxSchedulerInterval.Round(time.Minute).Minutes()),
		SchedulerRoundIntervalSeconds: int(defaultMailboxSchedulerRoundInterval.Round(time.Second).Seconds()),
		MailboxPageSize:               10,
	}
}

func normalizeCreateSettings(ownerID string, settings CreateSettings) CreateSettings {
	defaults := defaultCreateSettings(ownerID)
	out := settings
	out.OwnerID = strings.TrimSpace(ownerID)
	out.Label = strings.TrimSpace(settings.Label)
	out.Note = strings.TrimSpace(settings.Note)
	out.AccountIDs = normalizeAccountIDSelection("", settings.AccountIDs)
	out.CreateChannel = string(normalizeMailboxCreateChannel(mailboxCreateChannel(strings.ToLower(strings.TrimSpace(settings.CreateChannel)))))
	out.SchedulerCreateChannel = string(normalizeMailboxCreateChannel(mailboxCreateChannel(strings.ToLower(strings.TrimSpace(settings.SchedulerCreateChannel)))))
	out.AppleAccountTwoFactorMethod = normalizeAppleTwoFactorMethod(settings.AppleAccountTwoFactorMethod)
	out.ICloudWebTwoFactorMethod = normalizeAppleTwoFactorMethod(settings.ICloudWebTwoFactorMethod)
	if out.SchedulerIntervalMinutes < 1 {
		out.SchedulerIntervalMinutes = defaults.SchedulerIntervalMinutes
	}
	if out.SchedulerIntervalMinutes > 1440 {
		out.SchedulerIntervalMinutes = 1440
	}
	if out.SchedulerRoundIntervalSeconds < 1 {
		out.SchedulerRoundIntervalSeconds = defaults.SchedulerRoundIntervalSeconds
	}
	if out.SchedulerRoundIntervalSeconds > 600 {
		out.SchedulerRoundIntervalSeconds = 600
	}
	if out.MailboxPageSize < 1 {
		out.MailboxPageSize = defaults.MailboxPageSize
	}
	if out.MailboxPageSize > 500 {
		out.MailboxPageSize = 500
	}
	return out
}

type codedError struct {
	code      string
	message   string
	retryable bool
}

func (e codedError) Error() string { return e.message }

func errCode(code, message string, retryable bool) error {
	return codedError{code: code, message: message, retryable: retryable}
}

func firstNonZeroTime(values ...time.Time) time.Time {
	for _, value := range values {
		if !value.IsZero() {
			return value
		}
	}
	return time.Time{}
}
