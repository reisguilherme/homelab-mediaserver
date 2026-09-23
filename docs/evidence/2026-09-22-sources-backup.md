# Fontes e backup — ensaio de 22/09/2026

O Prowlarr cadastrou e testou Nyaa.si, YTS, LimeTorrents, The Pirate Bay e
1337x. O 1337x usa Byparr na rede interna do Compose, sem porta publicada no
host; a imagem está fixada por digest.
Suas integrações com Sonarr e Radarr passaram no teste de conexão. Após o
comando de sincronização, havia quatro indexadores em cada Arr, filtrados pelas
categorias respectivas. Nenhum cliente de download foi ativado: o gateway
ainda não instancia o adaptador real do qBittorrent nem conclui o fluxo de
permits. A regra de reserva impede ligar Arr diretamente ao qBittorrent.

O qBittorrent recebeu uma credencial persistente em arquivo root-only no
servidor; o login continuou válido após reiniciar o container e também
funcionou a partir da rede do gateway. O caminho padrão é `/data/torrents`,
novos torrents entram parados, o RSS automático está desligado e o upload
global está limitado a aproximadamente 20 Mbps. A interface HTTP permanece
publicada somente no loopback do servidor.

O próximo marco do cliente de download é capturar o contrato HTTP que os Arr
usam, ligar o adaptador real somente ao gateway e emitir uma permissão após
reserva e inspeção do torrent. Testes devem provar que qualquer adição sem
permissão é recusada e que uma transferência autorizada é reconciliada após
reinício. Só depois disso Sonarr e Radarr podem apontar para o gateway.

EZTV/EZTVL falharam em resolução DNS e showRSS expirou. RuTracker.org e
o feed pessoal showRSS aguardam credenciais. BitSearch, TheRARBG e
TorrentGalaxyClone não aparecem entre as definições da versão instalada do
Prowlarr; clones de identidade incerta não foram cadastrados. O proxy Byparr
e o 1337x passaram no teste do Prowlarr após a migração para o serviço Compose
e novamente após o reinício da pilha.

O servidor gerou snapshots Restic consistentes de configurações, bancos,
segredos criptografados e releases, sem mídia. A tarefa do Windows copiou o
repositório para `backup/restic`, verificou `restic check` sem erros e terminou
com código 0 no Agendador. O ensaio de restauração isolada recuperou o marcador
`COMMIT` (41 bytes). Os timers e a tarefa diária estão habilitados. Ainda falta
provar restauração completa e reconciliação do estado de downloads.
