package main

import (
	"embed"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
)

//go:embed web/*
var assets embed.FS

type metrics struct {
	Hostname       string  `json:"hostname"`
	OS             string  `json:"os"`
	Arch           string  `json:"arch"`
	CPUs           int     `json:"cpus"`
	UptimeSeconds  float64 `json:"uptimeSeconds"`
	Load1           *float64 `json:"load1"`
	Load5           *float64 `json:"load5"`
	Load15          *float64 `json:"load15"`
	MemTotalBytes  uint64  `json:"memTotalBytes"`
	MemAvailBytes  uint64  `json:"memAvailBytes"`
	ProcessAlloc   uint64  `json:"processAllocBytes"`
	ProcessSys     uint64  `json:"processSysBytes"`
	Goroutines     int     `json:"goroutines"`
	GoVersion      string  `json:"goVersion"`
	CollectedAt    string  `json:"collectedAt"`
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = io.WriteString(w, `{"status":"ok"}`+"\n")
	})
	mux.HandleFunc("/api/metrics", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		_ = json.NewEncoder(w).Encode(collectMetrics())
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-cache")
		f, err := assets.Open("web/index.html")
		if err != nil {
			http.Error(w, "dashboard unavailable", http.StatusInternalServerError)
			return
		}
		defer f.Close()
		_, _ = io.Copy(w, f)
	})
	for _, path := range []string{"/styles.css", "/app.js"} {
		file := strings.TrimPrefix(path, "/")
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet && r.Method != http.MethodHead {
				http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
				return
			}
			contentType := "text/css; charset=utf-8"
			if strings.HasSuffix(file, ".js") {
				contentType = "text/javascript; charset=utf-8"
			}
			w.Header().Set("Content-Type", contentType)
			w.Header().Set("Cache-Control", "public, max-age=300")
			data, err := assets.ReadFile("web/" + file)
			if err != nil {
				http.NotFound(w, r)
				return
			}
			_, _ = w.Write(data)
		})
	}

	port := strings.TrimSpace(os.Getenv("PORT"))
	if port == "" {
		port = "8080"
	}
	if _, err := strconv.Atoi(port); err != nil {
		log.Fatalf("invalid PORT %q: expected a number", port)
	}
	server := &http.Server{
		Addr:              ":" + port,
		Handler:           securityHeaders(mux),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("Tinybox listening on :%s", port)
	log.Fatal(server.ListenAndServe())
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
		next.ServeHTTP(w, r)
	})
}

func collectMetrics() metrics {
	var m metrics
	m.OS, m.Arch, m.CPUs, m.GoVersion = runtime.GOOS, runtime.GOARCH, runtime.NumCPU(), runtime.Version()
	m.CollectedAt = time.Now().UTC().Format(time.RFC3339)
	m.Hostname, _ = os.Hostname()
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	m.ProcessAlloc, m.ProcessSys, m.Goroutines = mem.Alloc, mem.Sys, runtime.NumGoroutine()

	if data, err := os.ReadFile("/proc/uptime"); err == nil {
		fields := strings.Fields(string(data))
		if len(fields) > 0 {
			m.UptimeSeconds, _ = strconv.ParseFloat(fields[0], 64)
		}
	}
	if data, err := os.ReadFile("/proc/loadavg"); err == nil {
		fields := strings.Fields(string(data))
		if len(fields) >= 3 {
			m.Load1, m.Load5, m.Load15 = parseFloat(fields[0]), parseFloat(fields[1]), parseFloat(fields[2])
		}
	}
	if data, err := os.ReadFile("/proc/meminfo"); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			fields := strings.Fields(line)
			if len(fields) < 2 {
				continue
			}
			value, err := strconv.ParseUint(fields[1], 10, 64)
			if err != nil {
				continue
			}
			switch strings.TrimSuffix(fields[0], ":") {
			case "MemTotal":
				m.MemTotalBytes = value * 1024
			case "MemAvailable":
				m.MemAvailBytes = value * 1024
			}
		}
	}
	return m
}

func parseFloat(s string) *float64 {
	value, err := strconv.ParseFloat(s, 64)
	if err != nil || errors.Is(err, strconv.ErrSyntax) {
		return nil
	}
	return &value
}

