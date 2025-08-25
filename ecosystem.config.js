module.exports = {
  apps: [
    {
      name: "connector-hub-threadless",
      script: "uvicorn",
      args: "app.main:app --host 0.0.0.0 --port 8080",
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      watch: false,
      max_memory_restart: "300M",
      env: {
        LOG_LEVEL: process.env.LOG_LEVEL || "INFO"
      }
    }
  ]
}
