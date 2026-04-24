const { setTimeout: delay } = require('timers/promises');
const zmq = require('zeromq');
const { pack, unpack } = require('msgpackr');

const FRONTEND_ENDPOINT = process.env.FRONTEND_ENDPOINT || 'tcp://broker:5555';
const PUBSUB_SUB_ENDPOINT = process.env.PUBSUB_SUB_ENDPOINT || 'tcp://pubsub_proxy:5558';
const USERNAME = process.env.USERNAME || 'js_bot';

const MESSAGE_BANK = [
  'checando status',
  'mensagem de teste',
  'publicacao automatica',
  'sincronizando canal',
  'novo evento recebido',
  'validando fluxo pubsub',
];

let logicalClock = 0;

function nowIso() {
  return new Date().toISOString();
}

function logicalClockReceive(receivedValue) {
  const incoming = Number.parseInt(receivedValue, 10);
  logicalClock = Math.max(logicalClock, Number.isNaN(incoming) ? 0 : incoming);
}

function logicalClockSend() {
  logicalClock += 1;
  return logicalClock;
}

function randomItem(list) {
  return list[Math.floor(Math.random() * list.length)];
}

async function sendRequest(socket, payload) {
  payload.timestamp = nowIso();
  payload.logical_clock = logicalClockSend();

  console.log(`[JS-CLIENT:${USERNAME}] TX`, payload);
  await socket.send(pack(payload));
  const [raw] = await socket.receive();
  const response = unpack(raw);
  logicalClockReceive(response.logical_clock);
  console.log(`[JS-CLIENT:${USERNAME}] RX`, response);
  return response;
}

async function loginWithRetry(socket) {
  while (true) {
    const response = await sendRequest(socket, {
      type: 'request',
      action: 'login',
      username: USERNAME,
    });
    if (response.status === 'ok') {
      return;
    }
    await delay(2000);
  }
}

async function listChannels(socket) {
  const response = await sendRequest(socket, { type: 'request', action: 'list_channels' });
  return response?.payload?.channels || [];
}

async function createChannelsUntilFive(socket, channels) {
  let updated = [...channels];
  while (updated.length < 5) {
    const candidate = `auto_${updated.length + 1}_${Math.floor(Math.random() * 900 + 100)}`.slice(0, 24);
    const response = await sendRequest(socket, {
      type: 'request',
      action: 'create_channel',
      channel: candidate,
    });
    if (response.status === 'ok') {
      updated = await listChannels(socket);
    } else {
      await delay(1000);
    }
  }
  return updated;
}

function subscribeUpToThree(subSocket, subscribed, channels) {
  const available = channels.filter((channel) => !subscribed.has(channel));
  while (subscribed.size < 3 && available.length > 0) {
    const channel = randomItem(available);
    subSocket.subscribe(channel);
    subscribed.add(channel);
    available.splice(available.indexOf(channel), 1);
    console.log(`[JS-CLIENT:${USERNAME}] SUBSCRIBED ${channel}`);
  }
}

async function listenPublications(subSocket, isRunning) {
  try {
    for await (const [topic, raw] of subSocket) {
      if (!isRunning()) {
        break;
      }
      const publication = unpack(raw);
      logicalClockReceive(publication.logical_clock);
      console.log(
        `[JS-CLIENT:${USERNAME}] PUB channel=${topic.toString()} message=${publication.message} sent=${publication.published_timestamp} received=${nowIso()}`,
      );
    }
  } catch (error) {
    if (isRunning()) {
      console.error(`[JS-CLIENT:${USERNAME}] publication listener error`, error);
    }
  }
}

async function publishMessage(socket, channel) {
  const text = `${randomItem(MESSAGE_BANK)} #${Math.floor(Math.random() * 9000 + 1000)}`;
  await sendRequest(socket, {
    type: 'request',
    action: 'publish_message',
    username: USERNAME,
    channel,
    message: text,
  });
}

async function main() {
  const reqSocket = new zmq.Request();
  await reqSocket.connect(FRONTEND_ENDPOINT);

  const subSocket = new zmq.Subscriber();
  await subSocket.connect(PUBSUB_SUB_ENDPOINT);

  console.log(
    `[JS-CLIENT:${USERNAME}] started frontend=${FRONTEND_ENDPOINT} pubsub=${PUBSUB_SUB_ENDPOINT}`,
  );

  let running = true;
  const listenerPromise = listenPublications(subSocket, () => running);

  try {
    await loginWithRetry(reqSocket);

    let channels = await listChannels(reqSocket);
    channels = await createChannelsUntilFive(reqSocket, channels);

    const subscribed = new Set();
    subscribeUpToThree(subSocket, subscribed, channels);

    while (true) {
      if (channels.length === 0) {
        channels = await listChannels(reqSocket);
      }

      const channel = randomItem(channels);
      for (let i = 0; i < 10; i += 1) {
        await publishMessage(reqSocket, channel);
        await delay(1000);
      }

      channels = await listChannels(reqSocket);
      channels = await createChannelsUntilFive(reqSocket, channels);
      subscribeUpToThree(subSocket, subscribed, channels);
    }
  } catch (error) {
    console.error(`[JS-CLIENT:${USERNAME}] fatal`, error);
  } finally {
    running = false;
    subSocket.close();
    reqSocket.close();
    await listenerPromise.catch(() => {});
  }
}

main().catch((error) => {
  console.error(`[JS-CLIENT:${USERNAME}] fatal`, error);
  process.exit(1);
});