# Auditoria do primeiro lote de filmes

A lista original contém 123 filmes: 43 avulsos e 80 distribuídos em 24
franquias. A auditoria consultou a lista local, o relatório operacional de
2026-10-03 e o estado atual das APIs em 2026-10-05. O relatório antigo está
no servidor em `/tmp/homeserver-movie-batch-report.md`; os dados privados
da instalação não são incorporados ao Git.

| Resultado do relatório de 2026-10-03 | Quantidade |
|---|---:|
| Candidatos aprovados naquele processamento | 102 |
| Filmes enfileirados | 87 |
| Filmes sem candidato aprovado | 20 |
| Candidatos retidos pela regra de franquia inteira | 15 |
| Filme já disponível | 1 |

Os 102 candidatos somavam 698,30 GiB; os 87 enfileirados, 605,64 GiB.
O total da lista fecha em 87 enfileirados, 35 não enfileirados e um já
disponível. O relatório não registra interrupção de grupos por falta de
espaço. Esses volumes descrevem o processamento antigo, não a capacidade
disponível nem o restante da fila atual.

O preparador antigo cadastrava filmes com `monitored=False` durante a
prévia. Apenas os selecionados para execução recebiam pedidos e entravam
no fluxo de download. Os demais permaneceram no catálogo, sem uma rotina
que reconsiderasse automaticamente os descartados ou retomasse uma
franquia retida. The Truman Show está entre os avulsos e recebeu
`no_qualified_candidate`. A franquia Fast & Furious teve nove candidatos
aprovados e um ausente; pela regra de franquia inteira, nenhum dos dez
entrou na fila naquele processamento. Pedidos manuais posteriores
iniciaram parte dessa franquia.

Na consulta atual, o Radarr continha 197 filmes, 97 sem arquivo. Trinta
estavam simultaneamente sem arquivo e sem monitoramento; nenhum desses
trinta tinha pedido aprovado no Seerr. O cruzamento com o plano antigo
identificou 29 filmes pulados e An Education, que havia entrado na fila,
mas cujo pedido e reserva estão cancelados desde 2026-10-05. Essa
exceção deve ser preservada, sem recriar seu download automaticamente.
The Truman Show não possui pedido, reserva ou tombstone no controlador.
O worker consome pedidos aprovados do Seerr, portanto o
cadastro no Radarr, sozinho, não inicia nem retoma esses downloads.
Alterar somente `monitored` não cria o pedido que falta.

O aviso do Radarr sobre ausência de indexadores para RSS ou busca
automática tem outra causa: essas buscas nativas estão desativadas na
configuração do projeto para que a escolha e a admissão passem pelo
controlador e pelo gateway. A busca interativa permanece disponível nos
indexadores permitidos. Bloqueios temporários HTTP 429/Cloudflare no
Prowlarr são uma condição diferente e precisam ser respeitados antes de
consultar novamente as fontes.

`no_qualified_candidate` não comprova ausência de releases disponíveis.
O JSON privado antigo registra oito tentativas de metadados no uIndex
para The Truman Show, todas agrupadas no motivo
`metadata_manifest_size_or_subtitle`. O preparador limitava a análise
a oito releases por fonte. Esse contador não distingue falha na obtenção
do torrent de rejeição por arquivos, tamanho ou legenda, nem representa
todas as releases disponíveis. A
recuperação requer uma busca atual após o intervalo de bloqueio, seguida
da validação da identidade, qualidade, seeds e tamanho real do torrent.

Para retomar o primeiro lote, devem ser reconsiderados apenas os filmes
faltantes da lista, preservando cancelamentos, os já publicados e a fila existente.
Os avulsos continuam primeiro; as franquias são admitidas por inteiro
considerando apenas seus filmes ainda faltantes. A capacidade precisa
descontar o tamanho restante dos compromissos atuais, incluindo os 63
filmes do lote de animações, sem contar novamente dados já gravados, e
manter 10 GiB livres em cada disco. Pedidos, reservas e
permits só devem ser criados para o conjunto validado que couber; esta
auditoria não iniciou novos downloads.
