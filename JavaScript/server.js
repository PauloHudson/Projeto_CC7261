const fs = require('fs');
const path = require('path');
const zmq = require('zeromq');
const { pack, unpack } = require('msgpackr');

const BACKEND_ENDPOINT = process.env.BACKEND_ENDPOINT || 'tcp://broker:5556';
const PUBSUB_PUB_ENDPOINT = process.env.PUBSUB_PUB_ENDPOINT || 'tcp://pubsub_proxy:5557';
const PUBSUB_SUB_ENDPOINT = process.env.PUBSUB_SUB_ENDPOINT || 'tcp://pubsub_proxy:5558';
const REFERENCE_ENDPOINT = process.env.REFERENCE_ENDPOINT || 'tcp://reference:5560';
const SERVICE_NAME = process.env.SERVICE_NAME || 'js_server';
const DATA_FILE = process.env.DATA_FILE || '/app/data/state.json';
const SERVER_SYNC_PORT = Number.parseInt(process.env.SERVER_SYNC_PORT || '5561', 10);
const SERVER_SYNC_BIND = `tcp://*:${SERVER_SYNC_PORT}`;
const DEFAULT_SERVER_NAMES = ['py_server_1', 'py_server_2', 'js_server_1', 'js_server_2'];
const ALL_SERVER_NAMES = (process.env.ALL_SERVER_NAMES || DEFAULT_SERVER_NAMES.join(','))
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean);
const HEARTBEAT_INTERVAL_MESSAGES = 10;
const SYNC_INTERVAL_MESSAGES = 15;

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
    coordinator_name: null,
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

function peerEndpoint(serverName) {
  return `tcp://${serverName}:${SERVER_SYNC_PORT}`;
}

async function listKnownServers(referenceSocket, state) {
  const response = await referenceRequest(referenceSocket, 'list_servers');
  if (response.status === 'ok') {
    state.known_servers = response?.payload?.servers || [];
    saveState(state);
  }
  return state.known_servers || [];
}

async function publishCoordinator(publisher, coordinatorName) {
  const announcement = {
    type: 'server_announcement',
    action: 'coordinator_elected',
    coordinator: coordinatorName,
    server: SERVICE_NAME,
    timestamp: nowIso(),
    logical_clock: logicalClockSend(),
  };
  await publisher.send(['servers', pack(announcement)]);
  console.log(`[JS-SERVER:${SERVICE_NAME}] PUB servers`, announcement);
}

async function requestPeer(serverName, action, payload = null) {
  if (!serverName || serverName === SERVICE_NAME) {
    return null;
  }

  const socket = new zmq.Request({ sendTimeout: 1000, receiveTimeout: 1000, linger: 0 });
  try {
    await socket.connect(peerEndpoint(serverName));
    const request = {
      type: 'server_request',
      action,
      server: SERVICE_NAME,
      timestamp: nowIso(),
      logical_clock: logicalClockSend(),
    };
    if (payload) {
      request.payload = payload;
    }

    await socket.send(pack(request));
    const [raw] = await socket.receive();
    const response = unpack(raw);
    logicalClockReceive(response.logical_clock);
    return response;
  } catch {
    return null;
  } finally {
    socket.close();
  }
}

async function electCoordinator(state, publisher, referenceSocket) {
  const knownServers = await listKnownServers(referenceSocket, state);
  const selfRank = Number.parseInt(state.server_rank || 0, 10);

  const higherRankServers = knownServers
    .filter((item) => item.name !== SERVICE_NAME)
    .filter((item) => Number.parseInt(item.rank || 0, 10) > selfRank)
    .sort((a, b) => Number.parseInt(b.rank || 0, 10) - Number.parseInt(a.rank || 0, 10));

  const responders = [];
  for (const item of higherRankServers) {
    const response = await requestPeer(item.name, 'election_request', {
      candidate: SERVICE_NAME,
      candidate_rank: selfRank,
    });
    if (response?.status === 'ok') {
      responders.push({
        name: response.server || item.name,
        rank: Number.parseInt(response?.payload?.rank || item.rank || 0, 10),
      });
    }
  }

  if (responders.length > 0) {
    responders.sort((a, b) => b.rank - a.rank);
    state.coordinator_name = responders[0].name;
    saveState(state);
    console.log(`[JS-SERVER:${SERVICE_NAME}] coordinator set to ${state.coordinator_name} (election response)`);
    return state.coordinator_name;
  }

  state.coordinator_name = SERVICE_NAME;
  saveState(state);
  await publishCoordinator(publisher, SERVICE_NAME);
  console.log(`[JS-SERVER:${SERVICE_NAME}] elected as coordinator`);
  return SERVICE_NAME;
}

async function syncWithCoordinator(state, publisher, referenceSocket) {
  let coordinatorName = String(state.coordinator_name || '').trim();
  if (!coordinatorName) {
    coordinatorName = await electCoordinator(state, publisher, referenceSocket);
  }

  if (coordinatorName === SERVICE_NAME) {
    return;
  }

  const response = await requestPeer(coordinatorName, 'clock_sync_request', {
    requester: SERVICE_NAME,
  });

  if (!response || response.status !== 'ok') {
    console.log(`[JS-SERVER:${SERVICE_NAME}] coordinator ${coordinatorName} unavailable, triggering election`);
    await electCoordinator(state, publisher, referenceSocket);
    return;
  }

  const currentTime = response?.payload?.current_time;
  syncPhysicalClock(currentTime);
  console.log(`[JS-SERVER:${SERVICE_NAME}] synchronized with coordinator=${coordinatorName} at ${currentTime}`);
}

function handlePeerRequest(message, state) {
  if (message.action === 'election_request') {
    return okResponse('election_request', { ok: true, rank: Number.parseInt(state.server_rank || 0, 10) });
  }

  if (message.action === 'clock_sync_request') {
    if (state.coordinator_name !== SERVICE_NAME) {
      return errorResponse('clock_sync_request', 'not_coordinator');
    }
    return okResponse('clock_sync_request', { current_time: nowIso() });
  }

  return errorResponse(String(message.action || 'unknown'), 'unknown_peer_action');
}

function handleServerAnnouncement(raw, state) {
  let message;
  try {
    message = unpack(raw);
  } catch {
    return;
  }

  logicalClockReceive(message.logical_clock);
  if (message.action !== 'coordinator_elected') {
    return;
  }

  const coordinatorName = String(message.coordinator || '').trim();
  if (coordinatorName && coordinatorName !== state.coordinator_name) {
    state.coordinator_name = coordinatorName;
    saveState(state);
    console.log(`[JS-SERVER:${SERVICE_NAME}] coordinator updated from announcement: ${coordinatorName}`);
  }
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
  const clientReply = new zmq.Reply();
  await clientReply.connect(BACKEND_ENDPOINT);

  const peerReply = new zmq.Reply();
  await peerReply.bind(SERVER_SYNC_BIND);

  const publisher = new zmq.Publisher();
  await publisher.connect(PUBSUB_PUB_ENDPOINT);

  const subscriber = new zmq.Subscriber();
  await subscriber.connect(PUBSUB_SUB_ENDPOINT);
  subscriber.subscribe('servers');

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

  if ((state.known_servers || []).length > 0) {
    const sorted = [...state.known_servers].sort((a, b) => Number.parseInt(b.rank || 0, 10) - Number.parseInt(a.rank || 0, 10));
    state.coordinator_name = sorted[0].name;
  } else {
    state.coordinator_name = SERVICE_NAME;
  }

  if (state.coordinator_name === SERVICE_NAME) {
    await publishCoordinator(publisher, SERVICE_NAME);
  }

  saveState(state);

  console.log(
    `[JS-SERVER:${SERVICE_NAME}] started backend=${BACKEND_ENDPOINT} pubsub=${PUBSUB_PUB_ENDPOINT} reference=${REFERENCE_ENDPOINT} rank=${state.server_rank} coordinator=${state.coordinator_name} sync_bind=${SERVER_SYNC_BIND}`,
  );

  let messagesReceived = 0;

  const clientLoop = async () => {
    for await (const [raw] of clientReply) {
      const message = unpack(raw);
      logicalClockReceive(message.logical_clock);
      console.log(`[JS-SERVER:${SERVICE_NAME}] RX`, message);

      const response = await processRequest(message, state, publisher);
      await clientReply.send(pack(response));
      console.log(`[JS-SERVER:${SERVICE_NAME}] TX`, response);

      messagesReceived += 1;
      if (messagesReceived % HEARTBEAT_INTERVAL_MESSAGES === 0) {
        const heartbeatResponse = await referenceRequest(referenceSocket, 'heartbeat');
        if (heartbeatResponse.status === 'ok') {
          state.last_heartbeat = nowIso();
          saveState(state);
        }
      }

      if (messagesReceived % SYNC_INTERVAL_MESSAGES === 0) {
        await syncWithCoordinator(state, publisher, referenceSocket);
      }
    }
  };

  const peerLoop = async () => {
    for await (const [raw] of peerReply) {
      const message = unpack(raw);
      logicalClockReceive(message.logical_clock);
      console.log(`[JS-SERVER:${SERVICE_NAME}] PEER-RX`, message);
      const response = handlePeerRequest(message, state);
      await peerReply.send(pack(response));
      console.log(`[JS-SERVER:${SERVICE_NAME}] PEER-TX`, response);
    }
  };

  const announcementLoop = async () => {
    for await (const [topic, raw] of subscriber) {
      if (topic.toString() === 'servers') {
        handleServerAnnouncement(raw, state);
      }
    }
  };

  try {
    await Promise.all([clientLoop(), peerLoop(), announcementLoop()]);
  } catch (error) {
    console.error(`[JS-SERVER:${SERVICE_NAME}] fatal`, error);
  } finally {
    clientReply.close();
    peerReply.close();
    publisher.close();
    subscriber.close();
    referenceSocket.close();
  }
}

main().catch((error) => {
  console.error(`[JS-SERVER:${SERVICE_NAME}] fatal`, error);
  process.exit(1);
});