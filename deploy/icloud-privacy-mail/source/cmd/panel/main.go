package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"icloud-privacy-mail/internal/app"
)

func main() {
	host := flag.String("host", "127.0.0.1", "bind host")
	port := flag.Int("port", 8787, "bind port")
	configPath := flag.String("config", "config.json", "config file path")
	dataPath := flag.String("data", filepath.Join("data", "state.json"), "state file path")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))

	cfg, err := app.LoadConfig(*configPath)
	if err != nil {
		logger.Error("load config", "err", err)
		os.Exit(1)
	}
	if flagWasSet("host") {
		cfg.Host = firstNonEmpty(*host, cfg.Host)
	}
	if flagWasSet("port") && *port != 0 {
		cfg.Port = *port
	}
	if flagWasSet("data") {
		cfg.DataPath = firstNonEmpty(*dataPath, cfg.DataPath)
	}

	store, err := app.NewFileStore(cfg.DataPath)
	if err != nil {
		logger.Error("open store", "err", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	handler := app.NewServer(cfg, store, logger)
	if panel, ok := handler.(*app.Server); ok {
		panel.StartMailWatcher(ctx)
		panel.StartAppleAccountKeepAlive(ctx)
		defer panel.StopMailWatcher()
		defer panel.StopAppleAccountKeepAlive()
	}

	server := &http.Server{
		Addr:              fmt.Sprintf("%s:%d", cfg.Host, cfg.Port),
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      180 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()

	logger.Info("iCloud Privacy Mail panel started", "addr", "http://"+server.Addr)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server stopped", "err", err)
		os.Exit(1)
	}
}

func flagWasSet(name string) bool {
	seen := false
	flag.Visit(func(f *flag.Flag) {
		if f.Name == name {
			seen = true
		}
	})
	return seen
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
