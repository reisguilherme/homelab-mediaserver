# Diagnóstico

Comece pelos containers, logs e validação do `.env`:

```bash
docker compose ps
docker compose logs --tail=100
docker compose run --rm operator config validate --env-file /project/.env
df -hT /srv/data
tailscale status
```

Revise saídas nativas antes de compartilhar: logs podem conter informações
privadas. Reiniciar sem corrigir a causa costuma repetir o erro.

| Sintoma | Verificação e ação |
|---|---|
| Configuração inválida | Corrigir a chave/tipo/lista indicada; não usar `source .env`. |
| Diretório ausente ou sem escrita | Conferir /srv e UID/GID 1000; conferir o filesystem montado com `findmnt --target /srv/data`. |
| Native HTTP 401/403 | Importar a chave/senha real no .env; init/apply não resetam contas existentes. |
| Native unsupported | Conferir versão, definição, fields e IDs reais; não tratar capacidade não suportada como sucesso. |
| Read-back drift | Conferir preferências nativas e configurações concorrentes; repetir plan/apply/verify. |
| Container encerrado/unhealthy | Ver logs, memória e mounts; repetir a inicialização após corrigir a causa. |
| Requested por horas | Conferir Arr/qBit/controlador: pode aguardar espaço, fonte, janela de episódios, legenda ou importação. |
| Avisos nativos de RSS, busca automática ou Completed Download Handling | Essas funções ficam desativadas nos Arr porque o worker controla a aquisição e a importação. Confira buscas interativas habilitadas, gateway e saúde do worker. |
| Fonte aparece no site, mas Arr não encontra | Conferir indexador, categorias/caps e sincronização Prowlarr; proxy Byparr deve compartilhar a tag gerenciada com a fonte. |
| Indexador responde 429 | Revisar seleção do proxy e limitação da fonte; respeitar o retry configurado. |
| Seeds no UIndex, mas nenhum conectado no qBit | Conferir a release/hash e os trackers; magnets precisam manter os `tr` ao usar cache de metadados. Seeds anunciados não são peers conectados. |
| Pack de temporada recusado | Conferir cobertura completa e piso MiB/min de cada episódio; mais seeds não permitem baixar vídeos abaixo da qualidade definida. |
| Busca de temporada demora verificando episódios avulsos | Conferir o filtro `fullSeason` do Sonarr; a busca nativa também retorna episódios de outras temporadas. Esses resultados são descartados antes de buscar metadados do pacote. |
| Torrent pequeno anunciado como 1080p | Conferir bytes do vídeo principal e duração contra o piso MiB/min; samples/sidecars não contam. |
| qBit 100%, mas nada no Jellyfin | Ver validação, legenda e importação worker; episódio pronto aguarda anteriores importados. Depois conferir sync Arr/Jellyfin/Seerr; manter CDH desabilitado e hardlinks habilitados. |
| Episódio lento bloqueia novos downloads após os seguintes terminarem | A janela deve contar episódios ainda não baixados. Confira evidência atual do gateway/qBit: torrents confirmados com zero bytes restantes liberam vagas mesmo antes da importação; sem essa evidência, permanecem na janela. |
| Fonte lenta sem troca | Conferir janela, qualidade/edição, peers medidos, capacidade conjunta e ETA; pausa/completo ou candidata inferior não deve ser promovida. |
| Preferência alterada sem efeito | Rodar operator apply e reiniciar os cinco consumidores conforme o guia do operador. |
| Painel sem métricas | Ver logs de host-metrics/telemetry e idade dos snapshots; primeira amostra não tem taxa. |
| Serviço inacessível pelo Tailscale | Conferir autenticação/status, IP atual e acesso na mesma tailnet; usar a porta do serviço. |
| Monitor qBit inacessível | Conferir loopback `18080` e `tailscale serve status`; usar encaminhamento TCP para abrir pelo IP Tailscale ou MagicDNS na porta `18080`. |
| Sem opção de excluir episódio/temporada no Jellyfin | Usar Jellyfin Web pelo endereço publicado; conferir conta administradora, permissão de exclusão e `CanDelete`. Abrir o menu do episódio ou do card da temporada, conforme o alcance desejado. |
| Exclusão aceita, mas mídia ainda aparece | Conferir `stage` e `error` em `GET /api/v1/deletions/jobs` com `X-Admin-Token` e logs de `control-worker`; a limpeza e sincronização dos catálogos são assíncronas. |
| Exclusão bloqueada pelo gateway | O erro informa o HTTP e motivos conhecidos, como manifesto ausente, identidade alterada ou outra mídia no torrent. Auxiliares pequenos conhecidos (TXT, NFO, legendas e imagens) são aceitos somente quando pertencem ao manifesto verificado; vídeos extras continuam bloqueados. Corrigir a causa antes de retomar o job. |
| Exclusão de temporada bloqueada | Conferir correspondência dos episódios locais com Sonarr e identidade dos arquivos. Arquivos com vários episódios, caminhos compartilhados ou identidade alterada exigem resolver a ambiguidade antes de repetir; não apagar diretórios manualmente para contornar o job. |
| Episódio removido, mas disco não liberou espaço | Conferir se a fonte vem de pacote ainda usado por outros episódios; hardlinks e pacote preservado mantêm os bytes físicos. O torrent só é removido quando todos os vínculos foram explicitamente excluídos. |
| Pedido continua no Seerr após excluir temporada | Pedidos com outras temporadas permanecem para preservá-las; conferir a sincronização da biblioteca. Só um pedido exclusivo da temporada removida é excluído. |
| Intel inacessível | Conferir renderD128 e GIDs reais no override; consultar o guia de hardware e testar reprodução. |

## Comandos por serviço

```bash
docker compose logs --tail=100 control-worker download-gateway
docker compose logs --tail=100 sonarr radarr prowlarr
docker compose logs --tail=100 jellyfin seerr bazarr
docker compose logs --tail=100 host-metrics telemetry
docker compose run --rm operator config plan --env-file /project/.env --in-container
docker compose run --rm operator config verify --env-file /project/.env --in-container
```

O healthcheck do gateway verifica processo HTTP; autorização de downloads
também exige capacidade recente, reservas válidas e estado consistente.
Não contorne o gateway para fazer um pedido começar.

## Evidência

Registre comando, horário, versão e resultado sanitizado. Separe declarado,
verificado, ausente e desconhecido. Fixtures Linux/WSL comprovam contratos;
rede, reinício, montagem física, GPU e reprodução precisam da máquina real.
Nunca adicione bancos, mídia, credenciais ou inventário bruto ao Git.

Veja [operação](operator-guide.md), [serviços](runbooks/service-setup.md) e
[hardware Jellyfin](runbooks/jellyfin-hardware.md).
