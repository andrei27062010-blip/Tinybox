(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const formatBytes = (bytes) => {
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0) + " MB";
  };
  const formatDuration = (seconds) => {
    if (!Number.isFinite(seconds) || seconds <= 0) return "—";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor(seconds % 86400 / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    return days ? days + "d " + hours + "h" : hours ? hours + "h " + minutes + "m" : minutes + "m";
  };
  const setText = (id, value) => { const el = $(id); if (el) el.textContent = value; };
  const tickClock = () => setText("clock", new Date().toLocaleTimeString([], {hour12:false}));
  tickClock();
  setInterval(tickClock, 1000);

  async function refresh() {
    try {
      const response = await fetch("/api/metrics", {cache:"no-store", headers:{"Accept":"application/json"}});
      if (!response.ok) throw new Error("metrics request failed");
      const m = await response.json();
      $("status-dot").classList.remove("offline");
      setText("connection-label", "SYSTEM ONLINE");
      setText("updated-at", "Updated " + new Date(m.collectedAt).toLocaleTimeString([], {hour12:false}));
      setText("memory-value", formatBytes(m.memAvailBytes));
      setText("memory-total", "/ " + formatBytes(m.memTotalBytes));
      const used = Math.max(0, m.memTotalBytes - m.memAvailBytes);
      const percent = m.memTotalBytes ? Math.min(100, used / m.memTotalBytes * 100) : 0;
      $("memory-meter").style.width = percent.toFixed(1) + "%";
      setText("memory-detail", m.memTotalBytes ? percent.toFixed(0) + "% used · " + (m.memorySource || "memory") : "Memory metrics unavailable");
      setText("uptime-value", formatDuration(m.uptimeSeconds));
      const loads = [m.load1, m.load5, m.load15].map(v => v == null ? "—" : Number(v).toFixed(2));
      setText("load-value", loads[0] + " / " + loads[1] + " / " + loads[2]);
      setText("cpu-count", m.cpus + (m.cpus === 1 ? " logical CPU" : " logical CPUs"));
      setText("app-memory", formatBytes(m.processRssBytes));
      setText("goroutines", m.threads + " threads");
      setText("hostname", m.hostname || "Unknown");
      setText("platform", m.os || "Unknown");
      setText("architecture", m.arch || "Unknown");
      setText("go-version", m.pythonVersion ? "Python " + m.pythonVersion : "Unknown");
      setText("heartbeat-title", "All systems responding");
      setText("heartbeat-text", "Metrics endpoint answered successfully.");
    } catch (_) {
      $("status-dot").classList.add("offline");
      setText("connection-label", "RECONNECTING");
      setText("heartbeat-title", "Connection interrupted");
      setText("heartbeat-text", "We'll try again automatically in a moment.");
    }
  }
  refresh();
  setInterval(refresh, 3000);
})();
