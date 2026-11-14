# Fontes paradas e temporadas

O Seerr acompanha o pedido; qBittorrent mostra a transferência real. Um episódio
concluído pode aguardar a importação porque falta um episódio anterior. Essa
ordem de publicação não limita os downloads a um arquivo por vez.

Se `HOMESERVER_AUDITED_IMPORT_MANIFEST` estiver preenchido, a importação do lote
aprovado é exclusiva por padrão. `HOMESERVER_AUDITED_IMPORT_ALLOW_SEERR=true`
permite processar novos pedidos junto do lote, preservando as fontes e os
perfis já aprovados. O worker registra por pedido os resultados da admissão,
perfil e aquisição; consulte esses motivos antes de concluir que faltam fontes.
Tamanho em GB continua sendo contabilizado para capacidade, mas não existe
piso por minuto por padrão (`QUALITY_MIN_MIB_PER_MIN_*` igual a zero).

O worker combina a busca do episódio com resultados nativos da temporada,
mantendo a identidade da série e do episódio verificada pelo Sonarr. A busca
da temporada é reutilizada por até cinco minutos. Resultados frescos do episódio
prevalecem sobre decisões antigas da temporada.

As fontes habilitadas em `HOMESERVER_RELEASE_INDEXER_PRIORITY` são comparadas
antes da escolha: UIndex e 1337x por padrão, com Amigos Share Club opcional.
O perfil selecionado no Seerr,
Radarr ou Sonarr vale para a busca inicial, episódios, temporadas e trocas manuais.
Cada série usa um único perfil para todas as temporadas, inclusive em pedidos
simultâneos de temporadas diferentes.
`Max Quality` prefere UHD Remux, UHD Blu-ray, UHD WEB-DL, FHD Remux, FHD Blu-ray e
FHD WEB-DL; `Standard` aceita apenas FHD Blu-ray e FHD WEB-DL. `Dublado` usa as
mesmas qualidades do Standard e compara primeiro a ordem de áudio definida em
`HOMESERVER_DUBBED_AUDIO_PREFERENCES`: dual pt-BR + original, pt-BR, original.
Animações respeitam a escolha explícita do perfil.

Todas as candidatas precisam declarar pelo menos
`HOMESERVER_RELEASE_MIN_SEEDERS="5"` seeds. Zero, desconhecido ou abaixo desse
mínimo não permitem aquisição. Entre candidatas viáveis, qualidade precede seeds;
no Dublado, áudio precede qualidade. Seeds precedem a afinidade de família.
Quando o ASC está configurado, o Dublado o pesquisa primeiro e o prefere
entre candidatas de mesma prioridade de áudio e qualidade, antes de comparar
seeds. Uma fonte pública dual continua preferível a uma fonte ASC apenas
dublada ou original, e todas precisam atingir o mínimo de seeds.
No Dublado, fontes sem evidência de áudio para uma das prioridades são puladas.
Nos três perfis, um pack conserva a preferência quando empata com os avulsos
na categoria de qualidade e na prioridade de áudio. Avulsos viáveis dentro da
janela podem superar um pack inferior conforme a ordem do perfil: Max Quality
e Standard comparam qualidade antes de áudio; Dublado compara áudio antes de
qualidade. Assim, no Standard, FHD Blu-ray avulso pode superar um pack FHD WEB-DL.
Alterar o perfil afeta buscas futuras e trocas manuais; não cancela torrents,
substitui arquivos existentes ou inicia upgrades automaticamente. As buscas de
legenda são mantidas nos três perfis, inclusive para áudio dublado.
O prefixo no nome do arquivo não comprova a origem nem a disponibilidade do swarm.

Para fontes Dublado, o worker também pesquisa os títulos brasileiros conhecidos
e combina título/temporada com `DUAL`, `DUBLADO` e `PTBR` nos indexadores configurados.
Com `HOMESERVER_RELEASE_FALLBACK_INDEXERS=1337x`, a busca de packs completos
também tenta `Título Season N` e `Título Sxx` no 1337x, após esgotar as fontes
primárias, nos três perfis. Cada candidata nova precisa corresponder à série,
à temporada completa e a todos os IDs de episódios do Sonarr; a inspeção dos
arquivos, a capacidade, o gateway e a decisão nativa de download continuam
obrigatórios. Essa busca adicional não altera a aquisição de episódios avulsos.
Lê declarações de áudio das melhores candidatas; `MULTI` sozinho, uma legenda
PT-BR ou o idioma que o Radarr inferiu do cadastro não comprovam dublagem.
Resultados de categorias genéricas continuam sujeitos à identidade nativa,
qualidade, tamanho, seeds, inspeção de arquivos e espaço disponível.
No ASC, as etiquetas nativas `Dual-Audio Brazilian Original` e
`Dublado Brazilian` são declarações de áudio PT-BR. A primeira também declara
a faixa original quando o idioma original do título é conhecido. Essas
declarações continuam sujeitas à verificação das faixas do arquivo na importação.
Páginas privadas do ASC são acessadas pelo Prowlarr autenticado; o leitor de
descrições públicas do worker não recebe o cookie do tracker.

`search_incomplete` / `PT-BR discovery incomplete` indica bloqueio, erro ou
limite de inspeção atingido. O worker não trata esse estado como ausência de
dublagem: continua fontes já escolhidas, pode admitir uma candidata PT-BR
comprovada e posterga o fallback original. O cache permite avançar nas consultas
e descrições em ciclos posteriores. Verifique o estado dos indexadores no
Prowlarr e os logs do `control-worker`; não remova a fila para reiniciar a busca.

Um magnet pode anunciar muitos seeds e ter um arquivo `.torrent` ausente ou
corrompido no cache HTTP. Nesse caso, o worker usa uma sessão libtorrent isolada
somente para metadados: até 45 segundos, 16 MiB e nenhum payload de mídia. O hash,
os caminhos e os tamanhos continuam sendo inspecionados antes da admissão.
Falha na recuperação aparece nos logs como `Peer metadata unavailable`.

Com `HOMESERVER_SERIES_PREFER_SEASON_PACK=true`, um pack completo que passe pelos
filtros pode atender novas aquisições. Recuperar automaticamente fontes individuais
sem peers e sem progresso por `HOMESERVER_SOURCE_STALL_SECONDS` exige também
`HOMESERVER_SOURCE_AUTOMATIC_REPLACEMENT_ENABLED=true`. Essa recuperação
exige resposta positiva de disponibilidade dos trackers, confirma novamente o
estado dos originais e conserva seus arquivos parciais. Fontes concluídas,
protegidas, em progresso ou com uma troca em curso são preservadas. A capacidade
inclui o pack inteiro e os compromissos já existentes; episódios duplicados dentro
do pack ocupam espaço e não são apagados automaticamente.
Vídeos e legendas dentro de pastas chamadas exatamente `Extras` ou `Featurettes`
não são vinculados aos episódios da temporada. Todos os arquivos dessas pastas
continuam incluídos no orçamento do torrent; vídeos ambíguos fora delas continuam
reprovando o pack.

A mudança dos vínculos é atômica. Metadados são persistidos antes da troca; após
reinício, uma autorização válida pode ser retomada. Se expirar antes do envio,
uma nova aquisição precisa passar novamente pela verificação de capacidade.
As importações dos episódios continuam sequenciais.

O default `HOMESERVER_SOURCE_AUTOMATIC_REPLACEMENT_ENABLED=false` mantém a fonte
mesmo lenta ou sem peers/progresso, até concluir ou haver uma troca manual pelo
painel. Com a troca automática habilitada,
`HOMESERVER_SOURCE_SLOW_REPLACEMENT_ENABLED=false` impede a troca por lentidão;
a falta de progresso ainda segue a regra de stall. Na avaliação de uma troca
autorizada, um candidato de teste ativo que não recebe nenhum byte é rejeitado
após `HOMESERVER_SOURCE_STALL_SECONDS`; espera por vaga ou pausa manual não conta
como falha do swarm. Essa avaliação não bloqueia os próximos episódios da janela.

Para acompanhar:

```bash
docker compose logs --tail=150 control-worker
docker compose logs --tail=100 download-gateway
```

Compare seeds conectados e progresso no monitor qBit com os seeds anunciados na
busca do Sonarr. Contagens de indexadores e trackers não garantem que um peer
esteja alcançável ou transferindo naquele momento. Não contorne o gateway para
forçar um download: isso perde a validação de espaço e os vínculos de importação.

Referência técnica: [modo de metadados do libtorrent](https://libtorrent.org/manual-ref.html#magnet-links).
