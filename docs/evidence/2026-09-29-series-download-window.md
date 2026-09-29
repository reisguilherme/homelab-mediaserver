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
