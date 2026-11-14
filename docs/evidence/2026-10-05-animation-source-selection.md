# Correções na seleção de fontes de animações

O filtro de idiomas agora normaliza `Latvian` e `lav` para `lv`. O Radarr
informava esse idioma para Flow, mas o controlador rejeitava suas releases
por não reconhecer o nome. O fallback para o idioma original permanece
subordinado ao perfil Dublado.

Perfect Blue consta como 1998 no catálogo do Radarr, com 1997 como ano
secundário. Uma consulta operacional por título encontrou fontes de 1997,
com identidade e qualidade verificadas pelo parser nativo. Foi avaliada
a opção `removeYear`, mas as configurações dos indexadores foram restauradas;
essa opção não foi adicionada ao reconciliador permanente do projeto.

A leitura das APIs confirmou fontes Blu-ray 1080p para Flow e Perfect Blue,
com 93 e 202 seeds informados, respectivamente. Os metadados dos torrents
foram obtidos antes da admissão, conferindo hash, arquivos e tamanho real.
Essas contagens não comprovam disponibilidade de peers ou velocidade real.

Validação: a suíte completa após a correção de idioma teve 1.882 aprovados
e 109 ignorados. Ruff aprovou o módulo e seus testes. Não houve teste de
reprodução nem espera pelo término dos downloads nesta etapa.

Consultas posteriores receberam HTTP 429 e código Cloudflare 1015, com
desativação temporária das fontes no Prowlarr. Resultado vazio durante esse
bloqueio não comprova ausência de releases. A busca direta do site também
recebeu bloqueio de verificação. Após o intervalo do bloqueio, consultas
bem-sucedidas recuperaram os cinco títulos: Mulan II, Millennium Actress,
Tokyo Godfathers, Nausicaä e Beauty and the Beast. Os links fornecidos pelo
usuário também confirmaram fontes compatíveis. Nenhum dos 63 filmes do lote
ficou sem candidato aprovado.

O lote totaliza 340.219.538.552 bytes (316,85 GiB) em metadados de torrents
validados. A admissão considerou primeiro os avulsos, depois cada franquia
inteira, o restante da fila existente e 10 GiB de margem permanente por
disco. Todos os candidatos couberam no HD. O áudio indicado pelos candidatos
é o original, último fallback permitido pelo perfil Dublado; isso não
comprova disponibilidade de uma faixa PT-BR.

A verificação final confirmou os 63 hashes no gateway/qBittorrent, com
placement válido no HD e perfil Dublado no Radarr. Nenhum foi deixado
em `stoppedDL` ou `pausedDL`. Na leitura, oito transferiam, 52 estavam
na fila, dois tinham concluído e um estava em `stalledDL`. Os estados
são instantâneos, sem garantia de velocidade ou de peers futuros.

Depois de descontar o restante da fila, a leitura indicou cerca de
10,26 GiB no SSD e 168,16 GiB no HD, preservando o piso de 10 GiB
em ambos. O modo de manutenção usado para preparar a admissão foi
retirado após conferir todas as reservas; o gateway voltou a exigir
e receber o heartbeat normal do worker, sem desativar essa proteção.
