# Guia do operador

Execute os comandos no checkout que contém `compose.yaml` e `.env`.
O [.env](configuration.md) é a configuração cotidiana de preferências e
credenciais. O operator é um container temporário com acesso às redes internas.

## Aplicar uma mudança

```bash
nano .env
docker compose run --rm operator config validate --env-file /project/.env
docker compose run --rm operator config plan --env-file /project/.env --in-container
docker compose run --rm operator config apply --env-file /project/.env --in-container
docker compose restart control-api control-worker download-gateway telemetry host-metrics
docker compose run --rm operator config verify --env-file /project/.env --in-container
```

Se seu usuário do host não for UID 1000, edite com `sudoedit .env` e preserve
owner 1000/permissão 0600, para os containers conseguirem ler a configuração.

Plan mostra o diff com segredos ocultos. Apply preserva IDs, aplica somente
preferências gerenciadas e confirma valores pela API. Uma aplicação parcial
pode ser repetida após corrigir a dependência. APIs indisponíveis ou capacidade
não suportada não são sucesso.

Contas nativas existentes preservam nomes/senhas. Alterar `ADMIN_PASSWORD`
não faz reset silencioso. Jellyfin/Seerr podem ter suas API keys descobertas
autenticadamente e gravadas no `.env`. A permissão opcional
`JELLYFIN_ENABLE_MEDIA_DELETION` vazia preserva a política existente;
`true`/`false` altera a permissão de exclusão do usuário configurado.

## Acompanhar downloads

Abra o painel em `http://IP_DO_SERVIDOR:8081`; os cards abrem os serviços.
Use Sonarr/Radarr para fontes e importação, Prowlarr para indexadores e o
monitor qBit para progresso, velocidade e peers.

Requested no Seerr pode significar espera por fonte, espaço, janela de episódios,
legenda ou importação. qBit em 100% ainda pode estar aguardando essas etapas.
Séries podem ter episódios baixando em paralelo, mas um episódio pronto espera
os anteriores serem importados. Por exemplo, E7 aguarda E5/E6 e S2 aguarda S1
para aparecer no Jellyfin. A janela por série é editável no `.env`.
O monitor qBit permite leitura e login; botões de mutação retornam HTTP 403.
Adições e retomadas gerenciadas passam pelo gateway com checagem de capacidade.

As [regras de qualidade, seeding, idiomas e troca de fonte](configuration.md)
continuam válidas mesmo com limites de banda maiores. Sonarr/Radarr mantêm
Completed Download Handling desabilitado e hardlinks habilitados para que a
importação passe pelo worker e o torrent continue disponível para seeding.

## Verificar o servidor

```bash
docker compose ps
docker compose logs --tail=100
df -hT /srv/data
tailscale status
```

O painel mostra CPU, memória, rede, armazenamento e idade das medições.
Dados antigos ou ausentes aparecem como indisponíveis. Métricas são coletadas
por container; não há serviço HomeServer separado no systemd.

`/health/live` confirma processo HTTP. Admissão de downloads também depende
do estado do worker, capacidade recente, autenticação e reservas válidas.
Antes de reiniciar um serviço, consulte os logs e corrija a causa.

## Atualizar e parar

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
```

Confira mudanças em `.env.example` e no [histórico](../CHANGELOG.md) antes
de aplicar novos defaults. Para parar mantendo os dados:

```bash
docker compose stop
```

A persistência está em `/srv/appdata` e `/srv/data`; recriar containers não
remove esses diretórios. Não há pipeline CI/CD nem comandos próprios de release,
rollback ou backup.

## Remover mídia assistida

Solicite a exclusão no Jellyfin com a permissão habilitada. O proxy cria um job
durável e o worker coordena arquivo, torrent, Arr e Seerr. Assistir não apaga
mídia automaticamente. Série/temporada inteira ou pacote compartilhado pode
ser bloqueado para preservar outros episódios; consulte
[operação diária](runbooks/operations.md) e [diagnóstico](troubleshooting.md).
