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

Requested pode aguardar fonte, espaço ou importação; séries também podem aguardar
a janela de episódios e legenda. Filmes são publicados individualmente enquanto
a busca de legenda continua, quando necessária.
Veja Arr, monitor qBit e estado do controlador antes de repetir um pedido.
O painel qBit permite administração completa com login nativo, por
loopback/Tailscale. Comandos manuais são diretos; downloads automatizados
continuam sujeitos ao gateway e à capacidade real.

## Exclusão coordenada

Habilite `HOMESERVER_JELLYFIN_ENABLE_MEDIA_DELETION="true"` e aplique a
configuração para permitir exclusão pelo usuário administrado no Jellyfin.
Jellyfin pode escrever metadados e legendas. Nas exclusões de filmes, episódios
e temporadas, o proxy cria um job durável e o worker coordena torrent, registros
e arquivo. Solicitações em lote são tratadas por ID conforme o tipo da mídia.
Coleções, playlists e seus vínculos são administrados nativamente, preservando
os arquivos vinculados. Outros tipos, inclusive uma série inteira, também
aceitam a ação administrativa nativa, sem depender do worker; essa exclusão não
coordena a limpeza em Arr/qBit/Seerr. Para limpar uma série entre os serviços,
exclua suas temporadas pelo fluxo coordenado. Assistir não apaga.

`GET /api/v1/deletions/jobs`, autenticado com `X-Admin-Token`, informa
complete/blocked/retry. No fluxo coordenado, arquivos ambíguos ou compartilhados
exigem preservar episódios fora da exclusão solicitada. Tombstones impedem
reaquisição automática da mídia explicitamente excluída.

Não apague pastas ou retome torrents como atalho para liberar espaço.
Confira o filesystem e os registros responsáveis pela mídia antes de agir.
