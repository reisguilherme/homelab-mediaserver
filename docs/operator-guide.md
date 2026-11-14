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

O apply mantém três perfis em cada Arr: `Max Quality`, `Standard` e `Dublado`.
Escolha o perfil nas opções avançadas do pedido no Seerr ou no cadastro do filme
no Radarr ou da série no Sonarr. Todas as temporadas de uma série compartilham
o mesmo perfil, inclusive em pedidos simultâneos. `Standard` aceita FHD Blu-ray e FHD WEB-DL;
`Max Quality` também inclui UHD e Remux, na ordem documentada em
[configuração](configuration.md#qualidade-e-fontes-lentas). `Dublado` usa a mesma
qualidade visual do Standard, priorizando dual pt-BR + original, depois pt-BR,
depois original, antes de comparar a qualidade.

Para mudar o padrão dos novos pedidos, edite
`HOMESERVER_DOWNLOAD_DEFAULT_PROFILE="Standard"`. Ajuste a ordem de áudio do
Dublado em `HOMESERVER_DUBBED_AUDIO_PREFERENCES="dual,pt-BR,original"` e o mínimo
de seeds de todos os perfis em `HOMESERVER_RELEASE_MIN_SEEDERS="5"`.
Siga o procedimento de aplicação acima. A escolha afeta buscas futuras e
trocas solicitadas manualmente, preservando downloads e mídia existentes;
trocar o perfil não inicia upgrades ou substituições automáticas.

Na primeira aplicação, cadastros no antigo `HomeServer Animation` migram para
`Dublado`; os demais perfis obsoletos migram para o padrão configurado,
inicialmente `Standard`. Novas animações
respeitam o perfil escolhido. Dublado e animações sempre seguem a busca de
legendas, qualquer que seja o áudio. No Radarr, confirme idioma `Any` no
Dublado e original nos outros perfis; `config verify` verifica a configuração.

No Dublado, buscas por títulos brasileiros e marcadores de áudio complementam
os resultados do UIndex/1337x. A classificação usa declarações de áudio, sem
confundir legendas ou o idioma original cadastrado com uma faixa do arquivo.
Se um indexador estiver bloqueado, ou houver descrições ainda por consultar,
o worker registra `PT-BR discovery incomplete` e adia o fallback apenas original.
Downloads já escolhidos continuam normalmente. Confira os logs com
`docker compose logs --tail=100 control-worker`; os próximos ciclos reutilizam
consultas concluídas e retomam inspeções pendentes. Não é necessário limpar a fila.
As [regras de descoberta e confirmação de áudio](configuration.md) detalham
os limites, a prioridade Dublado e a auditoria depois do download.

Contas nativas existentes preservam nomes/senhas. Alterar `ADMIN_PASSWORD`
não faz reset silencioso. Jellyfin/Seerr podem ter suas API keys descobertas
autenticadamente e gravadas no `.env`. A permissão opcional
`JELLYFIN_ENABLE_MEDIA_DELETION` vazia preserva a política existente;
`true`/`false` altera a permissão de exclusão do usuário configurado.

No Seerr, a permissão individual `Gerenciar pedidos` (`MANAGE_REQUESTS`, bit 16)
permite aprovar e recusar pedidos de qualquer usuário. A entrada pública aceita
essa função e somente os POSTs nativos `/api/v1/request/{id}/approve` e
`/api/v1/request/{id}/decline`, com sessão e proteção CSRF. O próprio Seerr
verifica a permissão; contas comuns continuam sem autorização para moderar.
Gestão de usuários, serviços e configurações administrativas permanece privada.

## Acompanhar downloads

Abra o painel em `http://IP_DO_SERVIDOR/status/` ou apenas a raiz; os cards
abrem os serviços por nome, como `/radarr/` e `/qbittorrent/`. O mesmo vale
para o IP Tailscale e o hostname MagicDNS. `/seer` redireciona para `:5055`
no mesmo host porque o Seerr não suporta Base URL.
Use Sonarr/Radarr para fontes e importação, Prowlarr para indexadores e o
monitor qBit para progresso, velocidade e peers.

O card **Downloads** abre `/status/ui/downloads` no mesmo painel. Entre com
`ADMIN_USERNAME` e `ADMIN_PASSWORD` do `.env` para acompanhar progresso, seeds,
velocidade e solicitar **Trocar fonte**. Sem uma candidata compatível e espaço
suficiente, a fonte atual permanece. Por padrão não há troca automática por
lentidão nem por falta de peers após 300 segundos.

Os cards dos discos mostram o **Restante da fila** dos downloads ativos.
Fontes incompletas paradas continuam comprometendo capacidade internamente
até a remoção física ser comprovada; esses bytes não representam arquivos
já ocupando todo esse espaço. A rotina periódica limpa somente fontes
incompletas paradas com identidade e destino exclusivo comprovados.
Torrents completos e seedings permanecem.

Requested no Seerr pode significar espera por fonte, espaço ou importação.
qBit em 100% ainda pode aguardar validação e importação; filmes podem aparecer
no Jellyfin enquanto a busca de legenda continua. Séries também podem aguardar
legenda ou a janela de episódios.
Séries podem ter episódios baixando em paralelo, mas um episódio pronto espera
os anteriores serem importados. Por exemplo, E7 aguarda E5/E6 e S2 aguarda S1
para aparecer no Jellyfin. A janela por série é editável no `.env`.
O painel qBit oferece controle completo após login, incluindo pausar, retomar,
remover torrents e alterar preferências. O serviço `qbit-monitor` mantém o
acesso legado em loopback/Tailscale; `/qbittorrent/` chega a ele pelo proxy
HTTP da rede doméstica/tailnet. Downloads automatizados continuam passando pelo gateway.
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

O administrador também pode editar metadados, imagens, legendas e coleções pelo
Jellyfin. Remover uma coleção ou tirar um filme de seus vínculos preserva os
arquivos dos filmes. Coleções automáticas são recriadas pela varredura conforme
os metadados TMDb enquanto `HOMESERVER_JELLYFIN_AUTO_COLLECTIONS` estiver ativo.

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

## Limpeza de downloads incompletos parados

O worker verifica a fila a cada minuto e remove até dez fontes gerenciadas por
rodada quando estão em `stoppedDL` ou `pausedDL`, com progresso menor que 100%
e bytes restantes. Parar um download incompleto permite essa limpeza automática.
`queuedDL`, `stalledDL`, downloads ativos e torrents completos são preservados,
incluindo os completos parados ou aguardando seeding.

Pausas temporárias feitas pelo controlador para respeitar a janela de episódios
ou aguardar espaço ficam registradas e sobrevivem a reinícios. A limpeza as
preserva, e o controlador só retoma as fontes que ele próprio pausou. Uma fonte
já parada manualmente não é reclassificada como pausa do controlador.

A limpeza cancela a geração interna do filme ou somente o episódio afetado.
Cancelar um episódio não cancela o pedido da temporada nem seus outros downloads.
Os registros de catálogo e os arquivos publicados no Jellyfin permanecem. A rotina
exige metadata verificada, montagem correta e uma pasta exclusiva; uma troca
manual em andamento, proteção do operador ou importação iniciada bloqueia a remoção.
Pacotes atuais de temporada ficam para revisão, pois podem atender vários episódios.

O bloqueio de novas tentativas é persistido antes da exclusão no qBit. A reserva
só é liberada depois de confirmar que o torrent desapareceu e que seus arquivos
de origem foram removidos do disco; o qBit pode concluir essa etapa depois da
resposta HTTP. Os logs de `control-worker` informam os motivos conhecidos de
adiamento sem expor credenciais. Fontes históricas rejeitadas ou substituídas
continuam sujeitas às mesmas verificações de estado e propriedade.
