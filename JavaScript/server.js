const fs = require('fs');
const path = require('path');
const zmq = require('zeromq');
const { pack, unpack } = require('msgpackr');

const BACKEND_ENDPOINT = process.env.BACKEND_ENDPOINT || 'tcp://broker:5556';
const SERVICE_NAME = process.env.SERVICE_NAME || 'js_server';
const DATA_FILE = process.env.DATA_FILE || `/app/data/${SERVICE_NAME}.json`;

const USERNAME_REGEX = /^[a-zA-Z0-9_]{3,20}$/;
const CHANNEL_REGEX = /^[a-z0-9_-]{2,24}$/;

function nowIso() {
  return new Date().toISOString();
}

function defaultState() {
  return {
    logins: [],
    channels: ['geral'],
  };
}

function loadState() {
  if (!fs.existsSync(DATA_FILE)) {
    return defaultState();
  }

  const raw = fs.readFileSync(DATA_FILE, 'utf-8');
  return JSON.parse(raw);
}

function saveState(state) {
  fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
  fs.writeFileSync(DATA_FILE, JSON.stringify(state, null, 2), 'utf-8');
}

function okResponse(action, payload = null) {
  const response = {
    type: 'response',
    action,
    status: 'ok',
    server: SERVICE_NAME,
    timestamp: nowIso(),
  };

  if (payload) {
    response.payload = payload;
  }

  return response;
}

function errorResponse(action, reason) {
  return {
    type: 'response',
    action,
    status: 'error',
    reason,
    server: SERVICE_NAME,
    timestamp: nowIso(),
  };
}

function handleLogin(message, state) {
  const username = String(message.username || '').trim();
  if (!USERNAME_REGEX.test(username)) {
    return errorResponse('login', 'invalid_username');
  }

  state.logins.push({
    username,
    timestamp: message.timestamp || nowIso(),
    server: SERVICE_NAME,
  });
  saveState(state);

  return okResponse('login', { username });
}

function handleCreateChannel(message, state) {
  const channel = String(message.channel || '').trim().toLowerCase();

  if (!CHANNEL_REGEX.test(channel)) {
    return errorResponse('create_channel', 'invalid_channel');
  }

  if (state.channels.includes(channel)) {
    return errorResponse('create_channel', 'channel_already_exists');
  }

  state.channels.push(channel);
  saveState(state);
  return okResponse('create_channel', { channel });
}

function handleListChannels(state) {
  const channels = [...state.channels].sort();
  return okResponse('list_channels', { channels });
}

function processRequest(message, state) {
  if (message.action === 'login') {
    return handleLogin(message, state);
  }

  if (message.action === 'create_channel') {
    return handleCreateChannel(message, state);
  }

  if (message.action === 'list_channels') {
    return handleListChannels(state);
  }

  return errorResponse(String(message.action), 'unknown_action');
}

async function main() {
  const socket = new zmq.Reply();
  await socket.connect(BACKEND_ENDPOINT);

  const state = loadState();
  saveState(state);

  console.log(`[JS-SERVER:${SERVICE_NAME}] Connected to ${BACKEND_ENDPOINT} data=${DATA_FILE}`);

  for await (const [raw] of socket) {
    const message = unpack(raw);
    console.log(`[JS-SERVER:${SERVICE_NAME}] RX`, message);

    const response = processRequest(message, state);
    const encoded = pack(response);

    await socket.send(encoded);
    console.log(`[JS-SERVER:${SERVICE_NAME}] TX`, response);
  }
}

main().catch((error) => {
  console.error(`[JS-SERVER:${SERVICE_NAME}] Fatal error`, error);
  process.exit(1);
});
