# Operação diária

- Consultar a fila e o motivo de espera antes de repetir um pedido.
- Tratar `waiting_space`, `waiting_source`, `waiting_subtitles` e estados
  `unknown` como estados operacionais, não como falhas a ignorar.
- Usar sempre o preview de exclusão e confirmar a mesma versão; a exclusão é
  manual e coordenada com monitoramento, Arr, seeding e tombstones.
- Conferir capacidade física, compromissos e idade do snapshot de telemetria.
- Não liberar downloads enquanto UUID, banco, gateway ou backup estiverem
  bloqueados.
- Em filmes, a reserva inicial é de 81 GB: vídeo limitado a 80 GB mais margem
  para arquivos auxiliares. A busca prefere remux Blu-ray, Blu-ray e só então
  WEB-DL. WEBRip e HDTV ficam fora da aquisição automática.
- Um filme sem legenda externa explicitamente pt-BR no torrent permanece
  pendente. `pt`, `por` e `pt-PT` não comprovam português brasileiro. A
  preferência por Dolby Vision e Atmos usa o nome da release; conferir os
  codecs e a legenda na reprodução quando esses recursos forem decisivos.
- Para magnets v1, a busca tenta obter o `.torrent` de `itorrents.net` e
  confere o hash antes de emitir a permissão. O gateway só encaminha um
  magnet cuja permissão tenha metadados inspecionados e reserva ativa; caso o
  cache não responda ou não traga os arquivos requeridos, o pedido aguarda.
- O Bazarr está ligado a Radarr/Sonarr com perfil exclusivo `pb` e provedor
  público Podnapisi. Isso cobre a busca após a importação; a aquisição
  automática ainda exige uma legenda pt-BR identificável no pacote antes do
  download. Para releases só com vídeo, é preciso desenvolver uma verificação
  de legenda anterior à admissão ou um fluxo de importação em quarentena.
