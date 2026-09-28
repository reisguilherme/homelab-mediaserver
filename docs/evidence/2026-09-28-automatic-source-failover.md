# Trocas automáticas sucessivas de fonte

Release `dbbb85e3f6d505c6da97bcb38af505dbc113f999`, implantada em 28/09/2026.

## Diagnóstico e correção

O S02E02 de *The Rookie* tinha uma fonte original parada em 99,93% e uma
substituição sem seeds em 0%. O worker encerrava o monitoramento depois da
primeira troca, e o registro de permissões também recusava uma segunda troca.

O worker agora monitora todas as substituições e busca outro hash após cinco
minutos sem progresso, com zero seeds conectados e velocidade zero. Uma hora de lentidão
persistente mantém o critério anterior. A seleção consulta trackers UDP públicos,
prefere seeds medidos e trata medições incompletas como desconhecidas. Números
antigos do indexador não impedem usar uma fonte com seeds medidos positivos.

O histórico bloqueia hashes já tentados e protege os caminhos de todas as fontes
preservadas. A troca e cada reenvio exigem evidência recente de que as fontes
anteriores estão paradas e que o tamanho real da alternativa cabe no disco após
contabilizar a fila. Os arquivos parciais permanecem no disco; episódios seguintes
aguardam a importação validada do atual.

## Verificações

- Python 3.12 em WSL, `uv sync --frozen`, Ruff, compilação Python, sintaxe Bash e
  checks disponíveis do alvo lint passaram.
- 227 testes unitários, 124 de contrato, 172 de integração e 9 de sistema passaram.
  Dois testes de portas dependentes de Docker foram pulados em WSL. O Compose dev
  e o Compose de produção com override foram validados no Docker do servidor.
- As regressões cobrem trocas sucessivas de filmes e episódios, seeds anunciados
  incorretamente, alternativas que não cabem, retomada de fontes históricas,
  concorrência no SQLite e continuidade da ordem dos episódios.
- Backup SQLite consistente anterior ao deploy, com `integrity_check=ok`, e cópia
  protegida da configuração em `/srv/backup-staging/automatic-source-failover-2026-09-28/`.
- Artefato SHA-256 `70e716805a1eb754ea58a29f4dd71b47e092e52ecf644a0717bfadaaa7a986f1`
  e manifesto validados por `scripts/deploy.sh`. Imagem identificada pela revisão
  Git; serviço systemd ativo, guarda de UUID e smoke de produção passaram.

## Resultado observado

O worker em segundo plano executou a segunda troca do S02E02 para uma fonte
DSNP WEB-DL 1080p. Não houve inclusão manual de torrent nesta validação.

Às 16:07:30 UTC, a nova fonte tinha 3 seeds conectados, 16 seeds medidos na seleção,
71.965.419 bytes recebidos, progresso de 3,48% e velocidade instantânea
de 533.430 bytes/s. A amostra anterior tinha 1.350.089 bytes recebidos, comprovando
avanço. As duas fontes anteriores e os episódios S02E03/S02E04 estavam parados.
O SQLite confirmou uma única fonte ativa para S02E02, com monitoramento persistido.

O download ainda não havia terminado; a importação e a disponibilidade desse
episódio no Jellyfin não fazem parte da comprovação acima.
