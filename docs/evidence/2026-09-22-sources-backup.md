# Fontes e backup — ensaio de 22/09/2026

O Prowlarr cadastrou e testou Nyaa.si, YTS, LimeTorrents e The Pirate Bay.
Suas integrações com Sonarr e Radarr passaram no teste de conexão. Após o
comando de sincronização, havia três indexadores em cada Arr, filtrados pelas
categorias respectivas. Nenhum cliente de download foi ativado: o gateway
ainda não implementa o adaptador real do qBittorrent nem o fluxo completo de
permits. A regra de reserva impede ligar Arr diretamente ao qBittorrent.

EZTV/EZTVL falharam em resolução DNS, showRSS expirou, e os endereços
disponíveis do 1337x falharam em DNS ou proteção Cloudflare. RuTracker.org e
o feed pessoal showRSS aguardam credenciais. BitSearch, TheRARBG e
TorrentGalaxyClone não aparecem entre as definições da versão instalada do
Prowlarr; clones de identidade incerta não foram cadastrados. O ensaio com
Byparr para o 1337x é separado deste registro e só será habilitado após teste
positivo.

O servidor gerou dois snapshots Restic consistentes de configurações, bancos,
segredos criptografados e releases, sem mídia. A tarefa do Windows copiou o
repositório para `backup/restic`, verificou `restic check` sem erros e terminou
com código 0 no Agendador. O ensaio de restauração isolada recuperou o marcador
`COMMIT` (41 bytes). Os timers e a tarefa diária estão habilitados. Ainda falta
provar restauração completa e reconciliação do estado de downloads.
