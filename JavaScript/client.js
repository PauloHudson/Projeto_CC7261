const { setTimeout: delay } = require('timers/promises');
const zmq = require('zeromq');
const { pack, unpack } = require('msgpackr');

const FRONTEND_ENDPOINT = process.env.FRONTEND_ENDPOINT || 'tcp://broker:5555';
const PUBSUB_SUB_ENDPOINT = process.env.PUBSUB_SUB_ENDPOINT || 'tcp://pubsub_proxy:5558';
const USERNAME = process.env.USERNAME || 'bot_js';
const MESSAGE_BANK = [
  'checando status',
  'mensagem de teste',
  'publicacao automatica',
  'sincronizando canal',
  'novo evento recebido',
  'validando fluxo pubsub',
];

function nowIso() {
  return new Date().toISOString();
}

function randomItem(items) {
  return items[Math.floor(Math.random() * items.length)];
}

async function sendRequest(socket, payload) {
  payload.timestamp = nowIso();

  const encoded = pack(payload);
  console.log(`[JS-CLIENT:${USERNAME}] TX`, payload);
  await socket.send(encoded);

  const [raw] = await socket.receive();
  const response = unpack(raw);
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
  const response = await sendRequest(socket, {
    type: 'request',
    action: 'list_channels',
  });

  return response?.payload?.channels || [];
}

async function createNewChannel(socket, currentChannels) {
  let channels = [...currentChannels];

  for (let attempt = 0; attempt < 10; attempt += 1) {
    if (channels.length >= 5) {
      return channels;
    }

    const candidate = `auto_${channels.length + 1}_${Math.floor(Math.random() * 900 + 100)}`.slice(0, 24);
    const response = await sendRequest(socket, {
      type: 'request',
      action: 'create_channel',
      channel: candidate,
    });

    if (response.status === 'ok') {
      channels = await listChannels(socket);
    } else {
      await delay(1000);
    }
  }

  return channels;
}

function ensureSubscriptions(subSocket, subscribedChannels, availableChannels) {
  const remaining = availableChannels.filter((channel) => !subscribedChannels.has(channel));

  while (subscribedChannels.size < 3 && remaining.length > 0) {
    const channel = randomItem(remaining);
    subSocket.subscribe(channel);
    subscribedChannels.add(channel);
    remaining.splice(remaining.indexOf(channel), 1);
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
      console.log(
        `[JS-CLIENT:${USERNAME}] PUB channel=${topic.toString()} message=${publication.message} sent=${publication.published_timestamp} received=${nowIso()}`,
      );
    }
  } catch (error) {
    if (isRunning()) {
      console.error(`[JS-CLIENT:${USERNAME}] Publication listener error`, error);
    }
  }
}

async function publishMessage(socket, channel) {
  const message = `${randomItem(MESSAGE_BANK)} #${Math.floor(Math.random() * 9000 + 1000)}`;
  await sendRequest(socket, {
    type: 'request',
    action: 'publish_message',
    username: USERNAME,
    channel,
    message,
  });
}

async function main() {
  const reqSocket = new zmq.Request();
  await reqSocket.connect(FRONTEND_ENDPOINT);

  const subSocket = new zmq.Subscriber();
  await subSocket.connect(PUBSUB_SUB_ENDPOINT);

  console.log(
    `[JS-CLIENT:${USERNAME}] Connected to ${FRONTEND_ENDPOINT} pubsub=${PUBSUB_SUB_ENDPOINT}`,
  );

  let running = true;
  const isRunning = () => running;
  const listenerPromise = listenPublications(subSocket, isRunning);

  try {
    await loginWithRetry(reqSocket);

    let availableChannels = await listChannels(reqSocket);
    availableChannels = await createNewChannel(reqSocket, availableChannels);

    const subscribedChannels = new Set();
    ensureSubscriptions(subSocket, subscribedChannels, availableChannels);

    while (true) {
      if (availableChannels.length === 0) {
        availableChannels = await listChannels(reqSocket);
      }

      const channel = randomItem(availableChannels);
      for (let index = 0; index < 10; index += 1) {
        await publishMessage(reqSocket, channel);
        await delay(1000);
      }

      availableChannels = await listChannels(reqSocket);
      availableChannels = await createNewChannel(reqSocket, availableChannels);
      ensureSubscriptions(subSocket, subscribedChannels, availableChannels);
    }
  } catch (error) {
    console.error(`[JS-CLIENT:${USERNAME}] Fatal error`, error);
  } finally {
    running = false;
    subSocket.close();
    reqSocket.close();
    await listenerPromise.catch(() => {});
  }
}

main().catch((error) => {
  console.error(`[JS-CLIENT:${USERNAME}] Fatal error`, error);
  process.exit(1);
});