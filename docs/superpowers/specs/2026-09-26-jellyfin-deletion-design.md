# Exclusão coordenada iniciada no Jellyfin

**Objetivo:** o operador exclui manualmente um filme ou episódio no Jellyfin; o HomeServer impede redownload e limpa as referências e os torrents correspondentes sem afetar outra mídia. Assistir ou marcar como assistido nunca inicia exclusão.

## Estado e entrada

O Jellyfin 10.10.7 atual recebe `/srv/data/media` somente para leitura. Uma exclusão de teste retornou HTTP 500 e conservou o arquivo. Um segundo teste retirou somente o vídeo temporário pela montagem autorizada e confirmou que o DELETE nativo então responde HTTP 204 e retira a entrada do catálogo. Manter a montagem do Jellyfin somente para leitura evita que ele apague mídia sem coordenação.

Um proxy HTTP leve preserva a porta 8096, WebSocket e requisições de reprodução. Ele encaminha todos os métodos e caminhos ao Jellyfin, exceto `DELETE /Items/{id}`, que vai ao controlador. O controlador valida a sessão com o Jellyfin, exige administrador e permissão de exclusão, lê o item **antes** de qualquer mutação, cria uma operação persistente idempotente e retorna sucesso ao cliente. O worker executa a limpeza e só depois chama o DELETE interno do Jellyfin. Exclusões provocadas por varreduras da biblioteca não passam por essa rota e não disparam a cascata. Qualquer forma de exclusão em lote não implementada é bloqueada no proxy, nunca encaminhada diretamente.

## Guardas antes de efeitos

O worker executa no máximo uma exclusão por ciclo. Antes de qualquer mutação remota, exige snapshot recente da montagem UUID esperada. A identidade vem do item Jellyfin consultado antes da exclusão e de uma correspondência única com o registro Arr, a reserva do controlador e o arquivo registrado no Arr. O caminho deve ser regular, sem symlink, dentro da biblioteca correta e manter a identidade capturada. Se a mídia mudou de caminho, ganhou uma nova versão, não tem mapeamento inequívoco ou o disco está indisponível, a operação fica bloqueada para revisão.

O worker grava um tombstone persistente antes de desmonitorar ou remover a mídia. Para filme, usa `movie:tmdb:<id>`. Para episódio, usa `episode:tmdb:<series-id>:SxxExx`; o aquisidor de séries deve tratar esse episódio como deliberadamente ausente para manter a fila sequencial. Uma solicitação explícita futura pode revogar o tombstone por operação administrativa separada.

## Efeitos por tipo

**Filme:** localizar exatamente um filme no Radarr pelo TMDb, confirmar seu `movieFile.path`, excluir somente o `moviefile/{id}` capturado e depois remover o registro do filme com `deleteFiles=false` e `addImportExclusion=true`. O `deleteFiles=true` do registro apaga a pasta inteira e, por isso, não é usado. Retirar o pedido correspondente do Seerr e os torrents identificados pelos permits da mesma reserva. O qBittorrent só aceita remoção por uma rota interna autenticada, depois de verificar hash, categoria, destino e seleção de arquivos; a rota Arr continua sem acesso a `torrents/delete`. Retirar sidecars próprios que restarem na pasta e, com o caminho de vídeo ausente, chamar o DELETE interno do Jellyfin.

**Episódio:** resolver a série Jellyfin e Sonarr, temporada/episódio e `episodeFileId` exatos. Desmonitorar somente o episódio; remover o registro de arquivo do Sonarr apenas se nenhum episódio fora da seleção compartilhar o mesmo `episodeFileId`. Remover o torrent do permit com o escopo SxxExx somente se seu manifesto contiver exclusivamente esse vídeo e arquivos auxiliares dele. O pedido de temporada no Seerr permanece quando outros episódios/temporadas ainda existem. Um episódio compartilhado ou torrent de pacote fica bloqueado para limpeza manual do conjunto, sem apagar os outros episódios. O DELETE interno do Jellyfin conclui o item após o arquivo desaparecer.

**Temporada ou série:** exclusão em lote fica bloqueada na entrada. O operador exclui os episódios individualmente. Isso evita retirar episódios não assistidos, arquivos compartilhados e pedidos Seerr de outras temporadas. Uma implementação futura pode capturar todos os descendentes em uma operação única com prévia de escopo.

As etapas são persistidas e repetíveis. Falha parcial deixa a operação visível como pendente ou bloqueada; uma repetição não remove itens diferentes. O endpoint administrativo `GET /api/v1/deletions/jobs` mostra etapa e erro sem expor caminhos ou credenciais. Logs registram IDs, etapa e motivo, sem credenciais ou conteúdo da mídia. Tombstones permanecem até uma intervenção administrativa explícita; um novo pedido do mesmo filme ainda não os revoga automaticamente.

## Implantação e aceite

Implementar e testar proxy, autenticação, guardas, coordenador, Arr/Seerr/qBittorrent e tombstones antes de trocar a porta exposta. Testar com um vídeo temporário e credenciais de operador, verificar WebSocket e reprodução por HTTP Range via proxy, então usar o botão nativo do Jellyfin no fixture. Confirmar que falha da montagem, pedido duplicado, arquivo trocado, pacote compartilhado, usuário sem permissão e exclusão por varredura não causam cascata. Não remover mídia real durante o teste.

Fontes primárias: [Jellyfin DELETE 10.10.7](https://raw.githubusercontent.com/jellyfin/jellyfin/v10.10.7/Jellyfin.Api/Controllers/LibraryController.cs), [Radarr API](https://github.com/Radarr/Radarr/blob/develop/src/Radarr.Api.V3/Movies/MovieController.cs), [Sonarr API](https://github.com/Sonarr/Sonarr/blob/develop/src/Sonarr.Api.V3/EpisodeFiles/EpisodeFileController.cs).
