# Operação diária

- Consultar a fila e o motivo de espera antes de repetir um pedido. Os links
  dos painéis nativos Sonarr, Radarr e qBittorrent estão em
  [Preparação dos serviços](service-setup.md#painéis-web-nativos).
- Tratar `waiting_space`, `waiting_source`, `waiting_subtitles` e estados
  `unknown` como estados operacionais, não como falhas a ignorar.
- Usar sempre o preview de exclusão e confirmar a mesma versão; a exclusão é
  manual e coordenada com monitoramento, Arr, seeding e tombstones.
- Conferir capacidade física, compromissos e idade do snapshot de telemetria.
- Não liberar downloads enquanto UUID, banco, gateway ou backup estiverem
  bloqueados.
- Um pedido só compromete espaço depois que o controlador inspeciona os
  metadados do torrent. A permissão usa o tamanho de todos os arquivos do
  pacote e exige que ele caiba no espaço livre atual, descontados os bytes
  restantes dos torrents na fila e das permissões ainda não iniciadas. Não há
  reserva fixa por filme, episódio ou temporada. A busca prefere remux Blu-ray,
  Blu-ray e só então WEB-DL. WEBRip e HDTV ficam fora da aquisição automática.
- Um filme sem legenda externa explicitamente pt-BR no torrent pode ser
  admitido sem consulta prévia ao SubDL. O finalizador mantém o vídeo fora da
  biblioteca até validar uma legenda ou confirmar áudio original pt-BR; sem
  isso, o pedido fica em `waiting_subtitles`. `pt`, `por` e `pt-PT` não
  comprovam português brasileiro. A preferência por Dolby Vision e Atmos usa
  o nome da release;
  conferir os codecs e a legenda na reprodução quando esses recursos forem
  decisivos. Séries sem sidecar continuam exigindo SRT no preflight.
- Para magnets v1, a busca tenta obter o `.torrent` de `itorrents.net` e
  confere o hash antes de emitir a permissão. O gateway só encaminha um
  magnet cuja permissão tenha metadados inspecionados e reserva ativa; caso o
  cache não responda ou não traga os arquivos requeridos, o pedido aguarda.
- O Bazarr está ligado a Radarr/Sonarr com perfil exclusivo `pb` e provedor
  público Podnapisi para busca após a importação. Para filmes só com vídeo,
  o finalizador busca pelo ID TMDb verificado: pt-BR da mesma release, pt-BR
  de outra release com duração compatível, inglês da mesma release e inglês
  de outra release com duração compatível, nessa ordem. Releases diferentes
  exigem edição/corte compatível e cobertura dos tempos do SRT próxima à
  duração do vídeo, com margem para créditos finais. Conferir o sincronismo
  no Jellyfin. O SubDL V1 usa `EN` e não garante a variante en-US.
- Novas importações de filmes ignoram artefatos de legenda criados pelo
  preflight anterior. A auditoria de produção desta mudança encontrou quatro
  desses artefatos, todos de pedidos já concluídos.
