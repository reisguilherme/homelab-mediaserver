# Janela de downloads de séries — 29/09/2026

## Problema e causa

O servidor tinha 51 torrents: 50 completos e somente The Rookie S02E05
baixando. S02E06–14 estavam completos no qBit, mas aguardavam a importação
do episódio 5. O Sonarr tinha apenas S02E01–04 na biblioteca. S03 e S04
estavam monitoradas e tinham solicitações ativas; havia cerca de 119 GiB livres.

`SeriesAcquirer` construía a janela com os dez primeiros episódios ainda não
importados. S02E05–14 ocupavam todas as posições, mesmo com nove deles já
baixados. S02E15–20 e as temporadas seguintes não entravam na aquisição.

## Correção

A janela de aquisição passou a excluir torrents confirmados cujo hash tem
zero bytes restantes na evidência atual do gateway/qBit. Esses episódios
continuam pendentes para a importação, mas liberam vagas de download.
Episódios sem evidência, com leitura indisponível ou sem confirmação continuam
ocupando a janela. Um pack só libera os vínculos quando seu torrent físico
está completo.
Enquanto baixa, todos os episódios vinculados ao mesmo parent de pack usam
uma única vaga. Contar cada vínculo como transferência separada impediria
alcançar S04 mesmo com poucas transferências físicas em andamento.

A ordem cronológica de seleção, o limite de dez downloads, o orçamento dos
bytes reais, os filtros de qualidade e a importação sequencial permanecem
ativos. O torrent original de S02E05 é preservado; velocidade baixa não dispara
substituição.

## Verificação

Testes reproduzem S02E05 incompleto com S02E06–14 completos e sem importação:
S02E15 e S03E01 passam a ser consultados quando há vagas. Casos sem provider,
com erro, hash desconhecido e permit apenas autorizado não liberam posições.
Packs completos/parciais têm cobertura específica. A biblioteca e o ledger
de importação não são alterados pela aquisição.

A suíte final passou com 1.103 testes, sem skips, em Linux/WSL com Python 3.12,
dependências do lockfile e Caddy real nos contratos do monitor. Ruff,
compilação Python, sintaxe Bash e ShellCheck passaram. Houve apenas os dois
avisos de depreciação já existentes nas dependências do TestClient.

Na primeira ativação, S02E15 e S02E16 foram adquiridos pelo UIndex na família
DSNP/playWEB. S03 começou como torrent completo 1080p AMZN pela fonte 1337x,
com 25 seeds conectados e cerca de 4,65 MB/s em uma amostra. O hash de S02E05
e os 50 torrents que já estavam completos foram preservados.

Após aplicar a contagem por torrent físico, S04 também foi adquirida como
pack completo 1080p AMZN/Vyndros. A leitura final mostrou nove downloads:
S02E05, S02E15–20, S03PACK e S04PACK, somando 37,17 MB/s naquele instante.
O episódio 5 mantinha um seed conectado; sua lentidão não bloqueava os demais.
O Sonarr continuava com apenas S02E01–04 importados, sem arquivos da S03/S04
publicados fora de ordem. Todos os 50 torrents inicialmente completos estavam
preservados e o disco tinha cerca de 108,5 GiB livres.

O worker estava saudável, sem reinícios inesperados. Controlador, Jellyfin
e Seerr responderam HTTP 200 pelo IP Tailscale. Esses dados são amostras
da validação no servidor, não garantias de velocidade futura.
