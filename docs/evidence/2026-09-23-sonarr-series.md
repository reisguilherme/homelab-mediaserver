# Sonarr: fluxo controlado de séries

Release `e885017e54c079b8e9f799b339f3f5e91e262e6a`, implantada em 23/09/2026 no servidor de testes.

## Diagnóstico

O aviso `No available indexers` no RSS não indicava falha da sincronização com o Prowlarr. O Sonarr tinha quatro indexadores habilitados para busca interativa, mas `enableRss=false` e `enableAutomaticSearch=false` em todos. Essa configuração foi mantida porque o RSS autônomo poderia tentar grabs sem reserva. A busca interativa do episódio 1 da temporada 4 de *Ted Lasso* retornou 43 releases.

O adaptador do Seerr também interpretava o `tmdbId=97546` da série como `tvdbId`. O pedido 3 seleciona apenas a temporada 4; seu identificador passou de `season:tvdb:97546` para `season:tmdb:97546:4`. A reserva antiga, sem permissões de download, foi cancelada. O novo worker criou `season:tmdb:97546:4` com reserva de 100 GB.

## Mudanças e verificações

- O worker cria uma permissão por episódio após inspecionar o `.torrent`, exige um único vídeo até 5 GB e legenda externa identificada explicitamente como pt-BR, e limita a soma das permissões ao orçamento de 100 GB da temporada. Ele ordena remux Blu-ray, Blu-ray e WEB-DL, preferindo 2160p, Dolby Vision e Atmos em seguida. WEBRip não é admitido.
- A importação automática de downloads concluídos no Sonarr foi desativada. O finalizador valida os arquivos, tamanhos, resolução, áudio e conteúdo da legenda antes de solicitar `DownloadedEpisodesScan`; a biblioteca recebe um sidecar `.pt-BR` por hardlink após a importação. Essa etapa passou em testes locais com fixtures, mas ainda não ocorreu com mídia real.
- O cancelamento agora inspeciona todas as permissões da temporada e preserva a reserva se qualquer episódio já começou. A paginação do worker continua mesmo quando uma página do Seerr produz menos objetos após a expansão por temporada.
- Ruff, 52 testes unitários, 35 de contrato, 57 de integração, 3 de sistema e smoke local passaram. O Compose de produção com override, manifesto SHA-256, `homeserver-stack.service`, smoke de produção e migração de banco até a versão 5 passaram no servidor.
- O backup Restic antes e depois do deploy terminou com sucesso. A cópia criptografada em `backup/restic` no desktop passou em `restic check` sem erros.

## Estado atual e limite

O pedido da temporada 4 está reservado e aguardando fonte. Na busca real do episódio 1, 43 releases foram encontradas. Sete passaram pela triagem de qualidade, tamanho e URL; a inspeção dos metadados não encontrou uma legenda externa pt-BR entre elas. Nenhuma permissão de episódio foi emitida e nenhum novo download foi iniciado. O Bazarr pode buscar legendas após a importação, mas não comprova a disponibilidade de uma legenda adequada antes do download; por isso não foi usado para contornar a exigência.

O aviso de RSS ainda aparecerá no Sonarr porque o RSS continua desligado de propósito. A busca controlada usa a API de busca interativa e o gateway, ambos ativos. Para provar o fluxo de ponta a ponta com mídia real, é necessária uma fonte elegível com legenda pt-BR externa, ou um futuro fluxo de aquisição de legenda pré-download que mantenha a validação de idioma. A liberação da reserva não utilizada ao concluir a temporada e a avaliação prévia de todas as releases de temporadas completas ainda precisam de implementação e testes.
