const zmq = require('zeromq');
const { pack, unpack } = require('msgpackr');

const FRONTEND_ENDPOINT = process.env.FRONTEND_ENDPOINT || 'tcp://broker:5555';
const USERNAME = process.env.USERNAME || 'bot_js';
const CHANNEL_CANDIDATES = [
  process.env.CHANNEL_1 || 'geral',
  process.env.CHANNEL_2 || 'duvidas',
  process.env.CHANNEL_3 || 'avisos',
];

function nowIso() {
  return new Date().toISOString();
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

    await wait(2000);
  }
}

async function main() {
  const socket = new zmq.Request();
  await socket.connect(FRONTEND_ENDPOINT);
  console.log(`[JS-CLIENT:${USERNAME}] Connected to ${FRONTEND_ENDPOINT}`);

  await loginWithRetry(socket);

  let step = 0;
  while (true) {
    if (step % 3 === 0) {
      const index = Math.floor(step / 3) % CHANNEL_CANDIDATES.length;
      const channel = CHANNEL_CANDIDATES[index];
      await sendRequest(socket, {
        type: 'request',
        action: 'create_channel',
        channel,
      });
    } else {
      await sendRequest(socket, {
        type: 'request',
        action: 'list_channels',
      });
    }

    step += 1;
    await wait(5000);
  }
}

main().catch((error) => {
  console.error(`[JS-CLIENT:${USERNAME}] Fatal error`, error);
  process.exit(1);
});
