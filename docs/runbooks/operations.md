# Operação diária

O [guia do operador](../operator-guide.md) é o procedimento atual para alterar
`.env`, limites de downloads/seeding, qualidade/idiomas, supervisão, backup,
update e rollback. Use [troubleshooting](../troubleshooting.md) para estados da
fila e [serviços](service-setup.md) para os painéis nativos.

Capacidade usa tamanho real dos arquivos e bytes pendentes, sem reserva fixa
por filme. Filmes elegíveis são ordenados por seeds; séries seguem temporada
e episódio. Fonte lenta por cinco minutos dispara busca/avaliação sem parar
a atual; troca exige qualidade/edição compatíveis e ETA melhor medido.

Solicitações podem aguardar espaço, fonte, legenda, validação ou importação;
Requested no Seerr sozinho não descreve o estado detalhado. Consulte a API de
controle e o painel nativo Arr/qBit antes de repetir o pedido. Monitor qBit
permite leitura; mutações nesse proxy são recusadas para preservar o gateway.

Exclusão é explícita pelo Jellyfin com permissão configurada. Jellyfin continua
sem escrita direta na biblioteca: proxy cria um job durável; worker verifica
origem, remove torrent/registro/arquivo e sincroniza catálogo. Assistir não apaga.
`GET /api/v1/deletions/jobs` com X-Admin-Token informa complete/blocked/retry.
Exclusão de série/temporada inteira ou pacote compartilhado é bloqueada para
não apagar outros episódios. Tombstones impedem reacquisição automática.

Não apague mídia/cache por estimativa, remova recovery ou retome torrents como
atalho. Verifique UUID, capacidade e efeitos externos antes de qualquer operação.
