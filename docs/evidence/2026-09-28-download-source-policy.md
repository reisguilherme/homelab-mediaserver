# Seleção de fontes e retomada de séries

Ajuste da instalação pessoal em 28/09/2026. As evidências abaixo distinguem
observações do servidor de testes locais; números de peers mudam durante a execução.

## Diagnóstico e limpeza no servidor

Antes da mudança havia 65 torrents: 43 concluídos e 22 incompletos, incluindo
tentativas antigas pausadas e probes. A rede BitTorrent estava conectada,
com 352 nós DHT, sem proxy de peers e com DHT/PEX/LSD habilitados. Isso
não comprovava conectividade ou seeds de cada release individual.

Um link de UIndex para The Rookie S02E05 retornou magnet com sete trackers.
O worker extraía somente o infohash, buscava o torrent no cache e descartava
os parâmetros `tr`. Foi corrigido o merge dos trackers ao torrent verificado,
mantendo os bytes de `info`, arquivos e infohash. O contrato dos magnets
permite múltiplos trackers. [BEP 9](https://www.bittorrent.org/beps/bep_0009.html).

Com o worker parado, a limpeza solicitada removeu os 22 torrents incompletos
e seus dados. Os 43 concluídos foram preservados. Os 56 arquivos da biblioteca
mantiveram device/inode/tamanho; o espaço livre observado depois foi 125,59 GiB.
Os permits sem transferência foram retirados da fila de admissão. Um probe
concluído de S02E07, com metadados verificados, foi adotado como fonte para
importação; S02E06 já estava concluído. Ambos aguardam S02E05.

Prowlarr, Sonarr e Radarr foram lidos após a alteração: somente UIndex e 1337x
estavam habilitados. As configurações dos demais indexadores ficaram preservadas.

## Comportamento configurado

- Até 10 downloads, contando também os lentos. Seeding sem limite de arquivos,
  com limite de upload da instalação preservado; ratio e tempos de seeding sem teto.
- UIndex primeiro; fallback em 1337x quando não há candidato elegível ou a fonte
  principal anuncia menos de cinco seeds. Na resolução permitida, seeds orientam
  a escolha sem remover os filtros de idioma original e qualidade.
- Filmes em 2160p com fallback em 1080p; séries somente em 1080p. Piso por
  duração continua ativo, por vídeo, inclusive em pacotes.
- Velocidade baixa não dispara substituição. Ausência sustentada de progresso
  ainda permite recuperação de fontes individuais; pausas e downloads concluídos
  são preservados. Pacotes confirmados ainda não têm failover automático.
- Família de releases saudável e temporada completa elegível são preferidas
  em séries; download paralelo mantém importação sequencial por episódio.

Os pacotes UIndex encontrados para The Rookie S02 tinham 10,5/14,1 GiB e não
atendiam o piso nativo de 17,6 GiB para a temporada. Não foram escolhidos por
terem mais seeds: a retomada preserva a qualidade por episódio.

## Verificação de implementação

A regressão dos magnets usa cache sem trackers e verifica os sete trackers no
artefato SQLite enviado pelo gateway. Os testes de pacotes verificam uma única
reserva/transferência por hash, cobertura completa, qualidade individual,
identidades por episódio e importação de um arquivo por comando Sonarr.
Uma colisão entre arquivos de um novo pacote e uma fonte preservada impede
a admissão do pacote e permite continuar a busca por episódios individuais.
O comando nativo aceita um caminho de arquivo e o processa individualmente.
[Fonte Sonarr](https://github.com/Sonarr/Sonarr/blob/main/src/NzbDrone.Core/MediaFiles/DownloadedEpisodesCommandService.cs).

Os filtros de indexadores preservam IDs, tags e credenciais nativas. O teste
com credenciais mascaradas reproduziu um erro de validação de indexador
desativado e foi corrigido: mudar flags não testa nem redefine suas credenciais.

O aceite final da suíte e da nova fila no servidor é registrado após ativação.
