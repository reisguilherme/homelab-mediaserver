# Seleção de legenda externa e validação na importação

## Resultado

Pedidos aprovados continuam sujeitos à reserva, à inspeção do torrent e ao gateway. Um torrent de filme com um único vídeo elegível pode ser admitido sem legenda interna: a aquisição do filme não consulta o SubDL. O vídeo baixado permanece fora da biblioteca até que o finalizador confirme uma legenda elegível ou uma faixa de áudio original explicitamente pt-BR. Para séries sem sidecar, permanece o preflight com SRT obtido e persistido antes do permit. O Bazarr continua procurando e melhorando legendas após a importação.

Para filmes, a busca externa usa o ID TMDb do **filme verificado** e escolhe a primeira legenda válida nesta ordem:

| Prioridade | Idioma | Correspondência |
|---|---|---|
| 0 | pt-BR (`BR_PT` no SubDL) | Mesma release |
| 1 | pt-BR (`BR_PT` no SubDL) | Outra release com duração compatível |
| 2 | Inglês (`EN` no SubDL; en-US quando identificável) | Mesma release |
| 3 | Inglês (`EN` no SubDL; en-US quando identificável) | Outra release com duração compatível |

O SubDL V1 usa `EN` e não identifica com confiança o dialeto; a preferência por en-US não pode ser garantida automaticamente. O nome da release é normalizado apenas por pontuação e espaços para identificar uma correspondência exata. Releases diferentes não são excluídas pelo nome: sua elegibilidade depende da edição do filme e da duração. Marcadores como `Extended` e `Director's Cut` precisam ser compatíveis com os do vídeo; uma legenda da edição estendida não serve para a versão comum, nem o inverso. A duração é estimada pela cobertura temporal dos cues do SRT em relação à duração medida do vídeo, com margem limitada para créditos finais. Essa comparação reduz o risco de cortes incompatíveis, mas não comprova sincronismo perfeito; uma amostra deve ser conferida no Jellyfin.

## Fluxo

1. O controlador classifica a release pelo Arr. `WEB` sem `WEBRip` é aceito como WEB-DL apenas quando o campo de qualidade do Arr também a classifica como WEBDL. Blu-ray e remux mantêm prioridade.
2. O torrent continua sujeito ao hash, ao manifesto, ao tamanho e ao limite de um vídeo. Se contiver sidecar explicitamente pt-BR, o fluxo existente prevalece. Para filmes, não há consulta ao SubDL antes do permit; o download ainda depende da reserva e do gateway.
3. Na finalização de filme sem legenda elegível, ignorar artefatos de legendas de filmes deixados pelo preflight anterior e consultar o SubDL pelo ID TMDb verificado do filme e pelos idiomas `BR_PT` e `EN`. Conferir mídia e idioma no retorno, aceitar somente SRT avulso e aplicar a ordem acima. Para outra release, conferir a edição, medir a duração do vídeo baixado e validar a cobertura temporal do SRT com margem para créditos finais.
4. Para séries sem sidecar, manter o preflight por ID TMDb, temporada e episódio: release exata primeiro; subsidiariamente, mesmo título, episódio e família de fonte. O SRT é persistido antes do permit e exigido pelo finalizador. A prioridade de quatro níveis e a comparação de duração acima se aplicam somente a filmes.
5. Baixar o SRT do domínio fixo de download sem seguir URLs arbitrárias, limitar a 1 MB e conferir sua estrutura de tempos. Falha de API, erro de formato, mídia divergente ou duração incompatível não autorizam a importação.
6. O finalizador exige uma legenda validada ou a dispensa pela faixa original explicitamente pt-BR antes de marcar a importação como concluída. A legenda externa é gravada atomicamente ao lado do arquivo importado com o sufixo do idioma escolhido. Se não houver opção elegível, manter o vídeo em `/data/torrents`, fora da biblioteca, e o pedido aguardando legenda.

## Limites e segurança

- A chave SubDL fica em arquivo somente no servidor, fora do Git e fora de URLs de log. O worker lê esse arquivo; o gateway e o cliente de download não recebem a chave.
- Nenhuma mudança libera downloads sem reserva ou desliga as validações do gateway. A admissão usa o tamanho integral do manifesto e o espaço disponível.
- Metadados de idioma e estrutura SRT comprovam a fonte e o formato, não a qualidade da tradução nem sincronismo perfeito.
- Temporadas e filmes aguardando espaço continuam pendentes até haver capacidade. Esta mudança não promete que todo pedido terá uma release elegível.
