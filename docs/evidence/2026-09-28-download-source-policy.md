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

Na busca real, o Sonarr também devolveu episódios individuais e resultados de
outras temporadas. Inspecionar esses itens como pacotes atrasava a retomada.
O worker agora descarta `fullSeason=false` e temporada explicitamente diferente
antes de buscar metadados. Campos ausentes continuam sujeitos à inspeção do
torrent; cobertura e tamanho individual permanecem obrigatórios.
[Campos oficiais do Sonarr](https://raw.githubusercontent.com/Sonarr/Sonarr/develop/src/Sonarr.Api.V3/Indexers/ReleaseResource.cs).

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

A suíte completa passou com 1.075 testes, sem skips, usando o Caddy real nos
contratos do monitor. Ruff, compilação, sintaxe Bash/ShellCheck e a varredura de
segredos passaram. A revisão independente cobriu também exclusão de episódios
de pacotes e proteção de caminhos entre reservas diferentes.

## Ativação

O servidor recebeu a implementação por Git e build local das imagens. O Compose
com o override Intel passou na validação; a montagem ext4 foi verificada com o
UUID esperado antes de ativar os serviços. `config apply` e `config verify`
confirmaram os sete serviços. As duas séries e os oito filmes já cadastrados
foram associados ao perfil `HomeServer` atualizado.

A leitura nativa do qBit confirmou 10 downloads, uploads/total em `-1`, downloads
lentos contados na fila e upload preservado em 2.499.584 bytes/s. Ratio, tempo
total e tempo inativo de seeding ficaram sem teto. Os 43 torrents concluídos
permaneceram presentes; o S02E07 concluído foi retomado para seeding.

Uma tentativa direcionada com o worker parado foi recusada pelo gateway com
`503 admission blocked by worker health`. Após reativar o serviço normal, o
permit de S02E05 foi confirmado e a transferência começou. A guarda de saúde
permaneceu ativa durante toda a operação.

A amostra final mostrou oito downloads simultâneos de The Rookie S02:
E05 e E08–E14, todos UIndex, 1080p, DSNP/playWEB. Tinham peers conectados e
progresso; a soma observada foi 7.856.547 bytes/s, aproximadamente 7,86 MB/s.
E06/E07 permaneciam 100% concluídos e disponíveis para seeding. A transferência
de E08 estava mais avançada que E05, mas os arquivos da biblioteca de E05–E08
ainda não tinham sido importados: a sequência continuava protegida.

O inventário ficou em 51 torrents, preservando os 43 concluídos anteriores.
Os 15 containers esperados estavam rodando, com healthchecks definidos
positivos. Control, telemetry, Jellyfin e Seerr responderam HTTP 200 pelo IP
Tailscale a partir do desktop. Taxas e número de peers descrevem essa amostra,
sem garantir a velocidade futura de cada fonte.
