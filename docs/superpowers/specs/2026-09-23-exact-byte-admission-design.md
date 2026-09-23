# Admissão por bytes reais do torrent

## Objetivo

Um pedido não deve ocupar 81 GB por ser filme nem 100 GB por ser temporada. Ele só compromete espaço depois que o worker identifica uma release, lê o metadado completo do torrent e conhece os bytes que o qBittorrent baixará. Não há teto de tamanho por filme, episódio ou temporada, nem margem fixa subtraída do espaço livre da montagem de mídia. Preferências de qualidade, legenda pt-BR, identidade da montagem e passagem exclusiva pelo gateway continuam válidas.

O worker consulta a fila antes de medir o espaço livre para não combinar progresso novo com uma leitura antiga do disco. Quando a release preferida não couber, tenta a próxima elegível por qualidade. Radarr e Sonarr precisam confirmar `copyUsingHardlinks=true` antes do grab, pois a importação não dispõe de uma segunda cota para cópia física.

## Fluxo

1. O worker registra o pedido aprovado com compromisso inicial de zero byte. Isso permite buscar uma fonte sem ocupar disco. Pedidos sem fonte elegível continuam pesquisáveis e não impedem os demais.
2. Para cada release, o worker valida a qualidade, lê o `.torrent` v1 e verifica todos os arquivos e tamanhos. Como o qBittorrent atual baixa o torrent inteiro, o tamanho solicitado é a soma de **todos** os arquivos no metadado, inclusive os que não serão importados. Um magnet só entra após essa inspeção; se não for possível obter metadados verificáveis, não há admissão.
3. Antes do grab, o worker consulta espaço livre atual da montagem validada e estado de todos os torrents no qBittorrent por um endpoint interno autenticado do gateway. O compromisso de um torrent admitido é o número de bytes ainda por baixar (`amount_left`), limitado pelo tamanho inspecionado. Torrent admitido ainda sem metadados no qBittorrent compromete todo o tamanho já inspecionado. Outros torrents na fila também entram na conta pelo restante informado; se algum ainda não tiver tamanho conhecido, a admissão falha fechada. Arquivos já gravados estão refletidos no espaço livre e não são contados outra vez.
4. O worker cria a permissão e registra seu compromisso real numa transação SQLite de escrita. A mesma transação compara `bytes solicitados + bytes pendentes de todos os downloads e permissões` com o espaço livre. Concorrência não pode admitir duas releases sobre os mesmos bytes. Depois da transação, o worker pede o grab ao Arr; o gateway continua exigindo a permissão ligada ao hash e metadado.
5. Se faltar espaço, o pedido fica aguardando e pode ser reconsiderado quando downloads terminarem ou espaço for liberado. Falhas de leitura da montagem ou do gateway bloqueiam somente novas admissões. Permissão autorizada que expira sem despacho libera seu compromisso de forma idempotente; efeitos incertos não são liberados automaticamente.

## Compatibilidade e migração

As reservas persistidas antes da mudança têm orçamentos arbitrários. A migração recalcula cada reserva ativa a partir das permissões existentes: nenhuma permissão implica zero; permissões de episódios usam seus tamanhos já inspecionados. Para permissões antigas de filmes, o tamanho só encolhe após confirmação por metadado ou estado confiável do qBittorrent. Um torrent antigo em `metaDL` sem tamanho verificável permanece conservador até a confirmação ou intervenção do operador. Nada apaga torrents, mídia ou pedidos durante a migração.

## Verificação

Testar 2 GB admitidos onde antes 81 GB bloqueariam; duas admissões concorrentes; torrents na fila, parciais, completos e sem metadados; ausência de fonte; falha do gateway; reinício entre permissão e grab; filme e episódio maiores que os antigos tetos; importação que rejeita arquivo diferente do metadado; migração de reservas antigas. No servidor, validar banco e montagem, fazer backup antes do deploy e acompanhar uma request real até o gateway e a fila sem habilitar grabs nativos dos Arr.
