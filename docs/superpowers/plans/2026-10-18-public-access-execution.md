# Execução da proteção de acesso público

Estado final em 19/10: abertura pública concluída e commit confirmado pelo
coordenador, com saída 0, configuração persistida verificada e timer de
reversão desativado. O aceite restrito e seu histórico permanecem preservados.
A primeira abertura reverteu automaticamente às 15:20:49 UTC,
restaurando os sete CIDRs, o que explica o timeout posterior relatado em 5G.
Migrações, integridade dos bancos e preservação das contas, permissões, pedidos
e histórico foram conferidas. As senhas foram rotacionadas e as sessões
anteriores revogadas.
Este adendo substitui, para esta execução, a exigência permanente de CIDR e o
bloqueio permanente por tentativas do plano de 15/10. A lista de IPs permanece
durante a implantação e só sai após os critérios abaixo. O desenho de 18/10
registra a revisão de escopo anterior à autorização.

## Limites da entrega

Jellyfin e Seerr mantêm login nativo, com administração privada e Tailscale.
Não há segunda tela obrigatória de autenticação. Não modificar roteador,
montagens, mídia ou fila. Não criar units persistentes do projeto: somente
configuração dos serviços nativos já presentes e timers transitórios de reversão.
Firewall local não contém root comprometido na mesma LAN. VLAN/segmentação
externa, mídia somente leitura e mitigação de DDoS volumétrico são limites
explícitos desta entrega.

## Ordem e critérios

1. Preservar o código sujo atual em worktree isolada; registrar hashes dos
   arquivos de origem. Antes de integrar, recusar arquivos alterados em paralelo
   e guardar cópia pontual dos arquivos substituídos. Nunca copiar `.env`, bancos,
   inventários ou mídia no pacote de código/testes.
2. Validar duas conexões novas de recuperação, chave SSH da borda e temporizador
   independente. Antes de qualquer alteração de rede, armar reversão das tabelas
   próprias e dos arquivos da intervenção, preservando Docker/Tailscale.
3. Criar checkpoint consistente de Jellyfin/Seerr em manutenção privada do
   worker, com integridade SQLite e retorno dos aplicativos anteriores.
   Ensaiar as imagens fixadas em cópias sem rede e sem mounts de mídia.
   Falha no ensaio bloqueia atualização de produção, sem inferir sucesso pelo
   fato de o container iniciar. Rollback sempre restaura imagem e appdata juntos.
4. Entregar os serviços separados: relay de rótulos, guard de sessão/login,
   proxy público e proxy de integração do Seerr. Nenhum recebe o `.env` inteiro,
   Docker socket ou estado do controlador. O guard armazena somente contadores
   pseudonimizados de abuso; senhas/tokens não são persistidos.
5. Aplicar redes e firewall antes de encaminhar tráfego público aos novos
   serviços. No HomeServer são necessárias as tabelas `inet` **e** `bridge`;
   checar IPv6, DNAT Docker e conexões já abertas. Na borda, limitar o UID Caddy
   aos upstreams definidos, DNS e HTTPS público. Não substituir regras de outros
   componentes. Validar configuração persistente e dependências dos serviços
   nativos sem afirmar que houve teste físico de reboot.
6. Migrar os aplicativos após o ensaio. Fazer as integrações privadas usarem
   Authorization moderno; limitar Seerr a rotas exatas do proxy de integração.
   Ativar CSRF/cookies seguros e impedir criação automática de contas, mantendo
   aprovações e cotas dos pedidos. HTTPS privado deve funcionar antes disso.
7. Proteger logs antes de rotacionar: Caddy exclui logs HTTP que podem conter
   tokens; proxies não registram URLs sensíveis nem gravam corpos de login em
   temporários; Jellyfin SessionManager não pode registrar tokens de logout em
   nível Information. Reiniciar e conferir a política antes da revogação.
8. Colocar a entrada pública em manutenção, manter recuperação privada e gerar
   senhas distintas de 24 caracteres por CSPRNG. Gravar TXT privado e journal
   durável com ACL do proprietário **antes** de aplicar. Nunca visualizar seu
   conteúdo. Rotacionar contas humanas, incluindo credenciais locais de contas
   Seerr vinculadas; preservar identidade/histórico. Manter a senha global de
   outros serviços e usar configuração administrativa dedicada do Jellyfin.
   Revogar sessões antigas e verificar login novo e rejeição do token antigo.
9. Executar testes reais: usuário comum entra/reproduz/pede; administrador,
   anônimo, API key de serviço, origem forjada e rota administrativa não passam
   publicamente; Range, WebSocket, legendas, seek e HTTPS privado funcionam.
   Validar navegador e solicitar conferência final de TV/celular ao proprietário.
10. Somente com esses critérios cumpridos, retirar CIDR da borda e do firewall,
    verificar a origem anteriormente bloqueada e readback da persistência.
    Falha crítica mantém CIDR ou manutenção. Entregar caminho do TXT sem anexá-lo
    ou enviá-lo a terceiros e registrar riscos residuais e testes não realizados.

## Evidências e implementação

O ledger operacional privado está em
`.runtime/security-hardening-20261018/progress.md`. Estados de produção são
registrados separadamente dos testes e do código preparado. O sucesso de uma
fixture não comprova implantação nem reprodução em aparelho físico.

Política de login: 20 tentativas/minuto por IP, 10/minuto por identidade e
60/minuto globais; no máximo 2/1/8 verificações concorrentes respectivamente.
Cinco falhas em 10 minutos geram bloqueio temporário por IP de 15 minutos,
escalando para 1 hora e 24 horas; a identidade recebe esperas progressivas de
30/120/900 segundos. Erros de backend não contam como senha errada. Quick Connect
público fica desativado nesta entrega, com login usuário/senha preservado.

A rotação também deve definir `LoginAttemptsBeforeLockout=-1` no Jellyfin e
conferir esse valor após a escrita. No Jellyfin 12.2, zero significa três
tentativas, e atingir um limite nativo desativa a conta permanentemente; isso
conflitaria com o bloqueio temporário acima. Preservar contas já desativadas e
todas as outras permissões. A alteração da política não zera contadores; o
login válido usado na verificação pode zerá-los pelo comportamento nativo.

Os gateways das duas redes privadas de proxy têm prioridade explícita para
que o encaminhamento loopback do Tailscale apresente um peer determinístico.
A confiança vale para esse IP exato, sem aceitar a LAN ou uma rede Docker inteira.

Imagens nativas são fixadas pelo digest após conferência e pull. O Jellyfin 12.x
exige nova varredura após a migração; esta só pode ocorrer com todos os discos
de mídia disponíveis. A ausência de mídia nas fixtures não pode ser confundida
com autorização para limpar o catálogo de produção.

A regra que bloqueia saída para o IP público da residência contém um retrato do
WAN. Mudanças de endereço ou de configuração do roteador exigem atualizar esse
valor e repetir o teste de hairpin. O teste atual não encontrou administração
do roteador em 80/443; isso não garante o resultado após mudanças de rede.

## Validação real em 19/10

O aceite restrito confirmou 49 verificações: 20 na borda, 13 no HomeServer e
16 de login/sessão. Usuário comum entra pela rota pública; administração,
chaves de serviço e cabeçalhos forjados não contornaram as negações testadas.
O acesso administrativo pelo HTTPS privado do Tailscale funcionou. Outros
oito checks comprovaram WebSocket, leituras parciais de vídeo e limpeza das
sessões de teste; uma legenda externa existente foi entregue corretamente.
Essas verificações não equivalem à reprodução física em TV/celular.

As correções do proxy público usam endereços exatos nas redes dedicadas de
guard e relay. O guard confia somente no peer172.30.240.51 do proxy; depender
da resolução DNS de um serviço com várias interfaces escolheu a interface
errada no servidor. O firewall não foi ampliado para corrigir esse problema.

O TXT de credenciais está na área privada local da intervenção, com ACL
restrita; seu conteúdo não foi exibido. O ledger registra seu caminho e as
etapas reais. Nenhuma mídia foi excluída/movida e a fila não foi resetada.

A manutenção do controlador foi encerrada após conferir a integração Radarr,
persistir as correções e verificar os quatro discos. Os 21 serviços estavam em
execução e os healthchecks configurados passaram. A varredura nativa da biblioteca
terminou com sucesso em 19/10 às13:18UTC, após a migração. As telas
de login também carregaram em um contexto anônimo isolado do Chrome. Nessa etapa,
a retirada dos CIDRs ainda aguardava a confirmação física e a prova de uma
origem externa anteriormente bloqueada. Os resultados posteriores estão
registrados abaixo, separando cada tentativa de abertura.

O primeiro teste físico revelou uma lacuna no aceite: Jellyfin12.2 usa
`GET /UserViews` para listar bibliotecas, enquanto o filtro só aceitava a rota
antiga. As duas leituras modernas foram liberadas para sessões comuns, mantendo
as exclusões administrativas e de escrita. A conta relatada passou a receber as
mesmas bibliotecas e o catálogo por ambas as rotas. O aceite passa a comparar
navegação e catálogo públicos, além de reprodução por URL de item.

O Seerr também não tinha vínculo com essa conta, e o cadastro automático estava
desativado conforme a política. A identidade existente foi importada
explicitamente com permissões comuns; login, consulta de pedidos e logout
públicos passaram. O cadastro automático continua desativado.

## Nova tentativa pública em 19/10, após 21:04 UTC

Após as correções, o proprietário confirmou TV, celular e Seerr funcionando
pela rede Wi-Fi residencial. Essa confirmação física é histórica; não constitui
uma nova confirmação de reprodução em 5G. A primeira abertura ficou sem commit
e seu timer restaurou os sete CIDRs às 15:20:49 UTC (12:20:49 em São Paulo).

A transação separada `public-opening-20261019-retry` foi ativada por volta de
21:04 UTC (18:04 em São Paulo). O mesmo nó externo do CheckHost, anteriormente
excluído pelos CIDRs, apresentou timeout nos dois endereços de aplicação antes
da abertura e HTTP 200 em ambos depois, com o destino público esperado. A consulta usou
somente URLs públicas, sem credenciais. Isso comprova alcance HTTP externo
daquela origem, sem afirmar reprodução física na rede celular.

As entradas na raiz também foram verificadas pelo WAN público com TLS válido:
Jellyfin redireciona com 308 para `/jellyfin/` e Seerr com 307 para `/login`.
São redirecionamentos relativos esperados, sem troca de host ou query.

As provas nativas autenticadas foram repetidas após essa ativação: três
bibliotecas e 591 registros no catálogo, WebSocket, leituras no início e no
meio do vídeo, legenda externa existente e login/consulta/logout do Seerr.
Sessões de teste foram revogadas. Acesso privado, negações administrativas,
21 testes de saída de rede, saúde dos 21 serviços e quatro montagens também
passaram. Não houve nova ampliação de permissões ou alteração das proteções
de sessão, origem do proxy e isolamento.

Nesta etapa da segunda tentativa, o commit ainda estava pendente e o timer
independente permanecia como recuperação. O resultado terminal está registrado
ao final deste documento.
Em nova resposta, o proprietário confirmou que o acesso funciona e relatou
capas borradas no Jellyfin e o logotipo do Seerr mostrando apenas o ícone.
A correção limitada de imagens e logotipo foi implantada por volta de 21:18 UTC;
os resultados técnicos e o limite da primeira medição visual estão abaixo.
A rede usada nessa resposta não foi especificada.
Não há nova confirmação humana de reprodução em 5G; a evidência atual combina
o aceite físico anterior com testes externos e nativos automatizados recentes.
Os limites de VLAN/segmentação externa, root comprometido e DDoS volumétrico
permanecem os mesmos.

## Correção de imagens e logotipo em 19/10, por volta de 21:18 UTC

O helper revisado `deploy_public_assets.py`, SHA `00fa33bd…b683a3b`, concluiu
a aplicação de três arquivos técnicos, recriando somente o guard e o proxy
públicos. Os hashes das fontes e do código em execução foram conferidos;
a imagem derivada preservou as correções anteriores de Arr, renderer e
UserViews. Os demais containers e os arquivos Compose permaneceram iguais.
Não houve escrita nas APIs nativas. A suíte de 216 casos da ponte de sessão
passou em Windows e Linux, assim como os 30 testes completos do Nginx real
em Linux e o Ruff.

Antes da correção, a mesma imagem anônima retornava 200 no Jellyfin nativo
e 401 pelo acesso público. Depois, duas capas reais retornaram 200 pelo
cookie de sessão validado, com conteúdo idêntico ao nativo: 38536 e 32087
bytes. O cache dessas respostas é privado e não armazena conteúdo; pedidos
anônimos e com cookie de sessão revogada continuaram recebendo 401.

A primeira verificação no navegador passou no login comum, nas duas
verificações de logotipo e na limpeza da sessão de teste. Porém, selecionou
zero capas porque consultou os cards cedo demais e reconhecia apenas URLs
com prefixo. Esse resultado não comprova a renderização das capas. Uma
segunda medição reconheceu os caminhos com e sem prefixo e aguardou as
imagens reais; o resultado foi positivo, sem nova implantação de assets.

Às 21:21:58 UTC, o segundo teste real no Chrome identificou três capas visíveis
e decodificou as três, confirmou acesso à imagem somente com o cookie validado
e carregou os dois logotipos do Seerr. Login comum, limpeza do dispositivo de
teste e revogação do token passaram; dispositivos anteriores foram preservados.
Esse teste não afirma reprodução física nem login do Seerr pelo navegador.
O primeiro resultado sem candidatos permanece registrado como medição incompleta.

As provas externas foram renovadas após a correção: o mesmo nó antes excluído
recebeu 200 nos dois serviços, com o destino público esperado. Os 21 testes de
saída de rede do Home, acesso administrativo privado e negações públicas de
administração e chaves também passaram novamente. O coordenador revisado passou
em 25 testes antes de iniciar a leitura final da borda e o commit.

## Commit público final concluído em 19/10

A execução real do coordenador terminou com saída 0 e confirmou o commit da
transação `public-opening-20261019-retry`, o timer de reversão desativado e a
configuração persistida verificada. A conferência final validou os arquivos
candidatos, as regras ativas das tabelas próprias, os três drop-ins nativos
preservados e os serviços nativos ativos e habilitados. O histórico do aceite
restrito permaneceu igual; o rollback da primeira abertura continua registrado
como uma transação separada.

As três fontes técnicas da correção, a imagem derivada do guard/tag de
desenvolvimento e a configuração Nginx em execução foram verificadas. O teste
real no Chrome aprovou três capas visíveis e decodificadas e os dois logotipos
do Seerr, com limpeza e revogação da sessão de teste. A administração privada,
as exclusões públicas e o isolamento continuam preservados.

A prova externa automatizada foi confirmada; `user_cellular_network_verified`
permanece falso. O usuário confirmou acesso funcionando sem informar a rede.
O aceite físico histórico pela rede Wi-Fi, os testes automatizados recentes
e esse novo relato permanecem separados, sem afirmar nova reprodução humana
em 5G. Os limites de segmentação externa, root comprometido e DDoS volumétrico
continuam os mesmos.
