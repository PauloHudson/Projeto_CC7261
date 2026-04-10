# Projeto CC7261 | Parte 1 - Request-Reply

A primeira parte do projeto relacionado à materia de Sistemas Distribuídos consiste em um sistema de troca de mensagens instantaneas com bots utilizando ZeroMQ + MessagePack.

## Escolhas das linguagens e componentes
- Linguagens: Python e JavaScript
- Serializacao binaria: MessagePack
- Troca de mensagens: ZeroMQ no padrao request/reply
- Persistencia: arquivo JSON em disco por servidor
- Orquestracao: Docker Compose

## Arquitetura da Parte 1
- 1 broker ZeroMQ
- 2 servidores Python
- 2 servidores JavaScript
- 2 clientes Python
- 2 clientes JavaScript

Fluxo:
1. O cliente envia `login`
2. O servidor responde `ok` ou `error`
3. Cliente cria canal com `create_channel`
4. Cliente lista canais com `list_channels`

Todos os pacotes irão trafegar no MessagePack e incluem `timestamp`.

## Regras de validacao implementadas na parte 1
- Username valido: 3 a 20 caracteres (`a-z`, `A-Z`, `0-9`, `_`)
- Channel valido: 2 a 24 caracteres (`a-z`, `0-9`, `_`, `-`)
- Canal duplicado retorna erro `channel_already_exists`

## Persistencia
Cada servidor grava em um arquivo proprio:
- `data/py_server_1/state.json`
- `data/py_server_2/state.json`
- `data/js_server_1/state.json`
- `data/js_server_2/state.json`

Cada arquivo armazena:
- O historico de logins (`username` + `timestamp`)
- A lista de canais criados

## Pré-requisitos
- **Docker** e **Docker Compose** instalados
  - Windows/Mac: https://www.docker.com/products/docker-desktop
  - Linux: `sudo apt install docker.io docker-compose`

## Como executar

### 1. Clone o repositório
```bash
git clone <seu-repositorio>
cd Projeto_CC7261
```

### 2. Inicie os serviços
Na raiz do projeto, execute:

```bash
docker compose up --build
```

Isso vai construir e iniciar todos os containers:
- 1 Broker ZeroMQ
- 1 Proxy Pub/Sub ZeroMQ
- 2 Servidores Python
- 2 Servidores JavaScript
- 2 Clientes Python
- 2 Clientes JavaScript

### 3. Visualização de logs
Para ver todos os logs em tempo real:
```bash
docker compose logs -f
```

Para ver logs de um serviço específico:
```bash
docker compose logs -f py_server_1
docker compose logs -f js_client_1
```

## Parte 2 - Publicacao em canais

A segunda parte adiciona o fluxo Publisher-Subscriber sem alterar o broker da Parte 1.

### Componentes novos
- `pubsub_proxy`: proxy ZeroMQ com `XSUB` em `5557` e `XPUB` em `5558`
- Servidores Python e JavaScript: publicam mensagens no canal escolhido
- Clientes Python e JavaScript: se inscrevem em canais e exibem as publicacoes recebidas

### Fluxo de mensagens
1. Cliente envia `publish_message` ao servidor com `channel`, `message` e `timestamp`
2. Servidor valida a requisicao, persiste a publicacao e publica no tópico do canal
3. Proxy Pub/Sub distribui a mensagem para todos os clientes inscritos naquele canal
4. Cliente exibirá `channel`, `message`, `published_timestamp` e `received_timestamp`

### Comportamento padrao dos bots
- Ao iniciar, cada bot continua fazendo `login`
- Se existirem menos de 5 canais, o bot cria novos canais automaticamente
- Se estiver inscrito em menos de 3 canais, o bot se inscreve em mais um canal
- Em seguida, o bot entra em loop infinito:
  - escolhe um canal disponível
  - envia 10 mensagens aleatórias com intervalo de 1 segundo

### Persistencia da Parte 2
Cada servidor agora persiste também as publicacoes realizadas:
- `logins`
- `channels`
- `publications`

### Exemplo de log da publicacao
```bash
[PY-SERVER:py_server_1] TX {'type': 'response', 'action': 'publish_message', 'status': 'ok', ...}
[PY-CLIENT:py_bot_1] PUB channel=geral message=checando status #1234 sent=2026-04-09T... received=2026-04-09T...
```

## Parar os serviços
Para parar todos os containers:

```bash
docker compose down
```

Para parar e remover volumes (limpar dados persistidos):
```bash
docker compose down -v
```

## Logs esperados
Clientes e servidores exibem sempre:
- mensagem enviada (`TX`) com conteudo completo
- mensagem recebida (`RX`) com conteudo completo

Exemplo de log esperado:
```
[PY-CLIENT:py_bot_1] TX {'type': 'request', 'action': 'login', 'username': 'py_bot_1', 'timestamp': '2026-03-20T...'}
[PY-CLIENT:py_bot_1] RX {'status': 'ok', 'timestamp': '2026-03-20T...'}
```

Isso facilita acompanhar as trocas de mensagens entre os servicos.

## Solucao de problemas

### Caso apareça: "Port already in use"
As portas 5555 ou 5556 já estão em uso:
```bash
docker compose down
# Aguarde 10 segundos
docker compose up --build
```

### Caso os containers não iniciem
Verifique se há erros nos logs:
```bash
docker compose logs
```

### Limpar tudo e começar do zero
```bash
docker compose down -v
docker system prune -f
docker compose up --build
```
