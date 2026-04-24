const fs = require('fs');
const path = require('path');
const zmq = require('zeromq');
const { pack, unpack } = require('msgpackr');

const BACKEND_ENDPOINT = process.env.BACKEND_ENDPOINT || 'tcp://broker:5556';
const PUBSUB_PUB_ENDPOINT = process.env.PUBSUB_PUB_ENDPOINT || 'tcp://pubsub_proxy:5557';
const REFERENCE_ENDPOINT = process.env.REFERENCE_ENDPOINT || 'tcp://reference:5560';
const SERVICE_NAME = process.env.SERVICE_NAME || 'js_server';
const DATA_FILE = process.env.DATA_FILE || '/app/data/state.json';

let logicalClock = 0;
let clockOffsetMs = 0;

function nowIso() {
  return new Date(Date.now() + clockOffsetMs).toISOString();
}

function logicalClockReceive(receivedValue) {
  const incoming = Number.parseInt(receivedValue, 10);
  logicalClock = Math.max(logicalClock, Number.isNaN(incoming) ? 0 : incoming);
}

function logicalClockSend() {
  logicalClock += 1;
  return logicalClock;
}

function syncPhysicalClock(referenceTime) {
  const referenceMs = Date.parse(referenceTime || '');
  if (!Number.isNaN(referenceMs)) {
    clockOffsetMs = referenceMs - Date.now();
  }
}

function defaultState() {
  return {
    logins: [],
    channels: ['geral'],
    publications: [],
    logical_clock: 0,
    server_rank: null,
    known_servers: [],
    last_heartbeat: null,
  };
}

function loadState() {
  const state = defaultState();
  if (fs.existsSync(DATA_FILE)) {
    const raw = fs.readFileSync(DATA_FILE, 'utf-8');
    Object.assign(state, JSON.parse(raw));
  }
  state.logins = state.logins || [];
  state.channels = state.channels || ['geral'];
  state.publications = state.publications || [];
  state.logical_clock = Number.isInteger(state.logical_clock) ? state.logical_clock : 0;
  return state;
}

function saveState(state) {
  state.logical_clock = logicalClock;
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
    logical_clock: logicalClockSend(),
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
    logical_clock: logicalClockSend(),
  };
}

async function referenceRequest(referenceSocket, action) {
  const request = {
    type: 'request',
    action,
    name: SERVICE_NAME,
    timestamp: nowIso(),
    logical_clock: logicalClockSend(),
  };

  await referenceSocket.send(pack(request));
  const [raw] = await referenceSocket.receive();
  const response = unpack(raw);
  logicalClockReceive(response.logical_clock);
  syncPhysicalClock(response?.payload?.current_time);
  return response;
}

function handleLogin(message, state) {
  const username = String(message.username || '').trim();
  if (!username) {
    return errorResponse('login', 'invalid_username');
  }

  state.logins.push({
    username,
    timestamp: message.timestamp || nowIso(),
    logical_clock: message.logical_clock || 0,
    server: SERVICE_NAME,
  });
  saveState(state);
  return okResponse('login', { username });
}

function handleCreateChannel(message, state) {
  const channel = String(message.channel || '').trim().toLowerCase();
  if (!channel) {
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
  return okResponse('list_channels', { channels: [...state.channels].sort() });
}

async function handlePublishMessage(message, state, publisher) {
  const channel = String(message.channel || '').trim().toLowerCase();
  const text = String(message.message || '').trim();
  const username = String(message.username || '').trim() || 'anon';

  if (!channel) {
    return errorResponse('publish_message', 'invalid_channel');
  }
  if (!text) {
    return errorResponse('publish_message', 'empty_message');
  }

  // Mantem o ajuste da parte 2: publica mesmo se canal nao existia localmente.
  if (!state.channels.includes(channel)) {
    state.channels.push(channel);
  }

  const publication = {
    type: 'publication',
    channel,
    message: text,
    username,
    request_timestamp: message.timestamp || nowIso(),
    request_logical_clock: message.logical_clock || 0,
    published_timestamp: nowIso(),
    logical_clock: logicalClockSend(),
    server: SERVICE_NAME,
  };

  state.publications.push(publication);
  saveState(state);
  await publisher.send([channel, pack(publication)]);

  return okResponse('publish_message', {
    channel,
    message: text,
    published_timestamp: publication.published_timestamp,
  });
}

async function processRequest(message, state, publisher) {
  if (message.action === 'login') {
    return handleLogin(message, state);
  }
  if (message.action === 'create_channel') {
    return handleCreateChannel(message, state);
  }
  if (message.action === 'list_channels') {
    return handleListChannels(state);
  }
  if (message.action === 'publish_message') {
    return handlePublishMessage(message, state, publisher);
  }
  return errorResponse(String(message.action || 'unknown'), 'unknown_action');
}

async function main() {
  const socket = new zmq.Reply();
  await socket.connect(BACKEND_ENDPOINT);

  const publisher = new zmq.Publisher();
  await publisher.connect(PUBSUB_PUB_ENDPOINT);

  const referenceSocket = new zmq.Request();
  await referenceSocket.connect(REFERENCE_ENDPOINT);

  const state = loadState();
  logicalClock = state.logical_clock || 0;

  const registerResponse = await referenceRequest(referenceSocket, 'register_server');
  if (registerResponse.status === 'ok') {
    state.server_rank = registerResponse?.payload?.rank ?? null;
  }

  const listResponse = await referenceRequest(referenceSocket, 'list_servers');
  if (listResponse.status === 'ok') {
    state.known_servers = listResponse?.payload?.servers || [];
  }

  saveState(state);

  console.log(
    `[JS-SERVER:${SERVICE_NAME}] started backend=${BACKEND_ENDPOINT} pubsub=${PUBSUB_PUB_ENDPOINT} reference=${REFERENCE_ENDPOINT} rank=${state.server_rank}`,
  );

  let messagesReceived = 0;
  try {
    for await (const [raw] of socket) {
      const message = unpack(raw);
      logicalClockReceive(message.logical_clock);
      console.log(`[JS-SERVER:${SERVICE_NAME}] RX`, message);

      const response = await processRequest(message, state, publisher);
      await socket.send(pack(response));
      console.log(`[JS-SERVER:${SERVICE_NAME}] TX`, response);

      messagesReceived += 1;
      if (messagesReceived % 10 === 0) {
        const heartbeatResponse = await referenceRequest(referenceSocket, 'heartbeat');
        if (heartbeatResponse.status === 'ok') {
          state.last_heartbeat = heartbeatResponse?.payload?.current_time || null;
          saveState(state);
        }
      }
    }
  } catch (error) {
    console.error(`[JS-SERVER:${SERVICE_NAME}] fatal`, error);
  } finally {
    socket.close();
    publisher.close();
    referenceSocket.close();
  }
}

main().catch((error) => {
  console.error(`[JS-SERVER:${SERVICE_NAME}] fatal`, error);
  process.exit(1);
});