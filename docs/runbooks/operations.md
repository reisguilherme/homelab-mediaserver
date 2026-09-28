# Operação diária

O [guia do operador](../operator-guide.md) mostra como alterar `.env`,
aplicar preferências, acompanhar downloads e atualizar a stack. Use
[diagnóstico](../troubleshooting.md) para estados da fila e
[serviços](service-setup.md) para conexões/provedores.

Capacidade usa bytes reais, sem reserva fixa por filme. Filmes elegíveis
podem ser priorizados por seeds; séries baixam em paralelo na janela configurada,
respeitando o limite global. A importação mantém temporada/episódio em ordem:
E7 já baixado aguarda E5/E6 e S2 aguarda S1 para aparecer no Jellyfin.
Fonte lenta por cinco minutos dispara busca/medição mantendo a atual,
com qualidade/edição e previsão de término verificadas.

Requested pode aguardar fonte, espaço, janela de episódios, legenda ou importação.
Veja Arr, monitor qBit e estado do controlador antes de repetir um pedido.
O monitor qBit permite leitura; mudanças pelo proxy são recusadas.

## Exclusão coordenada

Habilite `HOMESERVER_JELLYFIN_ENABLE_MEDIA_DELETION="true"` e aplique a
configuração para permitir exclusão pelo usuário administrado no Jellyfin.
A mídia continua sem escrita direta pelo Jellyfin: o proxy cria um job durável
e o worker coordena torrent, registros e arquivo. Assistir não apaga.

`GET /api/v1/deletions/jobs`, autenticado com `X-Admin-Token`, informa
complete/blocked/retry. Série/temporada inteira ou pacote compartilhado é
bloqueado quando a operação afetaria outros episódios. Tombstones impedem
reaquisição automática da mídia explicitamente excluída.

Não apague pastas ou retome torrents como atalho para liberar espaço.
Confira o filesystem e os registros responsáveis pela mídia antes de agir.
