package app

import "runtime"

var (
	AppVersion = "2026.07.02.2"
	AppCommit  = "unknown"
	AppBuiltAt = ""
)

type publicVersionInfo struct {
	Version string `json:"version"`
	Commit  string `json:"commit"`
	BuiltAt string `json:"built_at"`
	OS      string `json:"os"`
	Arch    string `json:"arch"`
}

func currentVersionInfo() publicVersionInfo {
	return publicVersionInfo{
		Version: AppVersion,
		Commit:  AppCommit,
		BuiltAt: AppBuiltAt,
		OS:      runtime.GOOS,
		Arch:    runtime.GOARCH,
	}
}
