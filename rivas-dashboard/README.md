# Rivas Dashboard

Real-time monitoring dashboard for OpenClaw/Rivas with OpenAI ChatGPT-inspired dark theme.

## Features

- **Real-time Metrics Display**: Live WebSocket connection to OpenClaw gateway
- **Model Usage Tracking**: Monitor tokens, costs, and requests per model
- **Sub-Agent Management**: View active sub-agents with status and activity
- **Cron Job Monitoring**: Track scheduled jobs, last runs, and next executions
- **Daily Spend Overview**: Total tokens and costs for the current day
- **Pending Jobs Queue**: See queued tasks waiting for execution
- **OpenAI Theme**: Beautiful dark mode with blue/white accents inspired by ChatGPT

## Tech Stack

- **Backend**: Node.js + Express + WebSocket (ws)
- **Frontend**: React 18
- **Build**: Webpack + Babel
- **Styling**: Pure CSS (OpenAI theme)

## Prerequisites

- Node.js 16+ (recommended: 18+)
- npm or yarn
- OpenClaw gateway running on `ws://127.0.0.1:18789`

## Quick Start (One Command)

```bash
npm install && npm run build && npm start
```

This will:
1. Install all dependencies
2. Build the React frontend
3. Start the server on http://localhost:3000

## Manual Setup

### 1. Install Dependencies

```bash
npm install
```

### 2. Build Frontend

```bash
npm run build
```

### 3. Start Server

```bash
npm start
```

### 4. Development Mode

```bash
npm run dev
```

## Configuration

### Port

Default port is `3000`. To change:

```bash
PORT=8080 npm start
```

### Gateway URL

The gateway WebSocket URL is configured in `server.js`:

```javascript
const GATEWAY_WS = 'ws://127.0.0.1:18789';
```

Modify this if your OpenClaw gateway runs on a different address.

## API Endpoints

### REST API

- `GET /api/metrics` - Get current metrics snapshot
- `GET /api/health` - Health check (server status, gateway connection, uptime)

### WebSocket

- `ws://localhost:3000/ws` - Real-time metrics stream
- Client can send `{ "action": "refresh" }` to trigger metrics refresh

## Project Structure

```
rivas-dashboard/
├── server.js              # Express + WebSocket server
├── webpack.config.js      # Webpack build configuration
├── package.json           # Dependencies and scripts
├── public/
│   ├── index.html        # HTML template
│   └── bundle.js         # Built React app (generated)
└── src/
    ├── index.js          # React entry point
    ├── App.js            # Main dashboard component
    └── App.css           # OpenAI-inspired styling
```

## Features in Detail

### Real-time Updates

- Automatic reconnection on disconnect
- 10-second polling interval for metrics
- Instant push updates when available

### Responsive Design

- Mobile-friendly layout
- Adaptive grid for stats cards
- Collapsible tables on smaller screens

### Visual Feedback

- Connection status indicator
- Color-coded status badges
- Hover effects and animations
- Smooth transitions

## Troubleshooting

### Dashboard won't connect

1. Ensure OpenClaw gateway is running: `openclaw gateway status`
2. Check gateway is on port 18789: `lsof -i :18789`
3. Verify WebSocket URL in `server.js` matches your gateway

### Build errors

1. Clear node_modules: `rm -rf node_modules package-lock.json`
2. Reinstall: `npm install`
3. Rebuild: `npm run build`

### No data showing

1. Check gateway connection in header (should show "Connected")
2. Click "Refresh" button to manually trigger metrics request
3. Check browser console for errors (F12)
4. Verify OpenClaw gateway API responses

## Production Deployment

### Build optimized bundle

```bash
NODE_ENV=production npm run build
```

### Run with PM2

```bash
npm install -g pm2
pm2 start server.js --name rivas-dashboard
pm2 save
```

### Nginx reverse proxy

```nginx
location / {
  proxy_pass http://localhost:3000;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
}
```

## License

MIT

## Credits

Built for OpenClaw/Rivas monitoring with ❤️ and ⚡
