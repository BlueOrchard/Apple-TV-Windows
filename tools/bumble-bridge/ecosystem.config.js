// PM2 process file for the Siri Remote bridge.
// Start with:  pm2 start ecosystem.config.js
// The bridge injects input with SendInput, so it must run in the logged-in user's
// desktop session (pm2-windows-startup), NOT as a Windows service.
const path = require("path");

module.exports = {
  apps: [
    {
      name: "siri-remote-bridge",
      script: path.join(__dirname, "bridge.py"),
      // Change this if Python lives somewhere else on the machine.
      interpreter: process.env.BRIDGE_PYTHON || "python",
      args: "--no-hex",
      cwd: __dirname,
      autorestart: true,
      restart_delay: 3000,
      max_restarts: 1000,
      exp_backoff_restart_delay: 0,
      watch: false,
      windowsHide: true,
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
