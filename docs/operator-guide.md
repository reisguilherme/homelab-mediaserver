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

Use uma conta administradora com permissão de exclusão habilitada e abra o
Jellyfin Web pelo endereço publicado do servidor:

- Para um episódio: abra seu menu `…`, escolha **Excluir episódio** e confirme.
- Para uma temporada: abra o menu `…` do card da temporada na página da série,
  escolha **Excluir mídia** e confirme. Confira o número da temporada antes de
  confirmar; o menu da série inteira tem outro alcance.

Os nomes dependem do idioma do cliente. O [menu nativo do Jellyfin Web 10.10.7](https://github.com/jellyfin/jellyfin-web/blob/v10.10.7/src/components/itemContextMenu.js#L198)
exibe a opção quando o servidor retorna `CanDelete`. Não é necessário excluir
cada episódio manualmente para remover uma temporada.

O proxy registra a solicitação e o worker coordena arquivos, torrents, Sonarr
e Seerr. Uma temporada gera um job principal e jobs dos episódios, retomáveis
após reiniciar o worker. A operação desmonitora a temporada selecionada e
cancela suas fontes gerenciadas pendentes, preservando as demais temporadas.
Assistir ou marcar como assistido não apaga mídia.

Um torrent de pacote compartilhado fica no qBit enquanto ainda atende algum
episódio preservado. Seus bytes podem continuar ocupando disco mesmo depois
de remover um episódio da biblioteca. A fonte só é removida após a exclusão
explícita de todos os episódios vinculados. Arquivos que representam vários
episódios ou têm identidade ambígua bloqueiam a exclusão para evitar perda de
outros episódios.

No Seerr, um pedido exclusivo da temporada removida é excluído; um pedido que
também abrange outras temporadas permanece e recebe sincronização do estado da
biblioteca. Para filmes gerenciados, a limpeza também remove o registro de
disponibilidade do filme, mesmo quando seu pedido já foi excluído. Se existir
outro pedido do mesmo filme, o job para para revisão antes de remover esse registro.
A atualização dos catálogos e do espaço mostrado no painel é
assíncrona. Consulte os logs de `control-worker` e o endpoint autenticado
`GET /api/v1/deletions/jobs` do controle (porta `8080`, header `X-Admin-Token`)
para acompanhar `stage` e `error`. Veja [diagnóstico](troubleshooting.md).
