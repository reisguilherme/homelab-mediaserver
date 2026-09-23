# Legenda pt-BR externa antes do grab

## Resultado

Pedidos aprovados continuam sujeitos à reserva, à inspeção do torrent e ao gateway. Um torrent com um único vídeo elegível pode ser admitido sem legenda interna somente quando o SubDL fornece previamente um arquivo SRT pt-BR para a **mesma release**. A legenda é baixada, validada e persistida antes de emitir o permit. Sem confirmação, o pedido permanece aguardando fonte. O Bazarr continua procurando e melhorando legendas após a importação.

## Fluxo

1. O controlador classifica a release pelo Arr. `WEB` sem `WEBRip` é aceito como WEB-DL apenas quando o campo de qualidade do Arr também a classifica como WEBDL. Blu-ray e remux mantêm prioridade.
2. O torrent continua sujeito ao hash, ao manifesto, ao tamanho e ao limite de um vídeo. Se contiver sidecar explicitamente pt-BR, o fluxo existente prevalece.
3. Para vídeo sem sidecar, consultar o SubDL por ID TMDb, idioma `BR_PT` e, em série, temporada e episódio. Aceitar somente arquivo SRT avulso cujo nome de release, normalizado apenas por pontuação e espaços, seja igual ao título da release do Arr. Conferir mídia, temporada e episódio no retorno.
4. Baixar o SRT do domínio fixo de download sem seguir URLs arbitrárias, limitar a 1 MB, conferir estrutura de tempos e gravar bytes e SHA-256 no SQLite por reserva, escopo e infohash. Falha de API, erro de formato ou divergência deixam o candidato inelegível.
5. Emitir permit e solicitar grab somente depois de persistir a legenda. O permit seleciona apenas os arquivos do torrent. O finalizador exige o artefato persistido ao validar o download e grava `.pt-BR.srt` atomicamente ao lado do arquivo importado antes de marcar a importação como concluída.

## Limites e segurança

- A chave SubDL fica em arquivo somente no servidor, fora do Git e fora de URLs de log. O worker lê esse arquivo; o gateway e o cliente de download não recebem a chave.
- Nenhuma mudança libera downloads sem reserva ou desliga as validações do gateway. O teto de 80 GB por filme, 5 GB por episódio e 100 GB por temporada permanece.
- Metadados `BR_PT` e estrutura SRT comprovam a fonte e o formato, não a qualidade da tradução nem sincronismo perfeito; uma amostra deve ser conferida no Jellyfin.
- Temporadas e filmes aguardando espaço continuam pendentes até haver capacidade. Esta mudança não promete que todo pedido terá uma release elegível.
