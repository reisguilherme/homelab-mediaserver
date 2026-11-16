# Abyss no cliente web Jellyfin

O Abyss acrescenta CSS e o banner Spotlight ao cliente web servido pela
playback. A aplicação usa a autenticação, o catálogo e a reprodução do Jellyfin
existente. O tema não cria outro serviço e não altera os clientes nativos de TV
ou celular que renderizam sua própria interface.

Em 16/11/2026, o proprietário retomou esta integração e pediu fidelidade aos
previews upstream. A `r4` foi implantada com fontes locais, home ajustada e
backdrop difuso. Seu aceite passou em contextos novos, mas um navegador com
`layout=modern` preservado manteve o cabeçalho anterior; essa diferença foi
reproduzida. A `r5` passou no aceite de migração única do layout
local, incluindo preservação das escolhas posteriores. Nessa revisão, CSS,
Branding e preferências das 18 contas foram preservados.
A `r6` está implantada e oculta a barra Início/Favoritos, conforme solicitado.
Menu lateral, busca e abas das bibliotecas permanecem disponíveis.
A publicação externa depende das portas
80/443 e de TLS, conforme o
[registro operacional](../architecture/homelab-2026/implementation-status.md).

## Artefatos e limites

O [empacotamento](../../deploy/abyss/README.md) fixa o commit
[`2f809171ed7f3a34b5e78297608b5f2b63dd3f0f`](https://github.com/AumGupta/abyss-jellyfin/tree/2f809171ed7f3a34b5e78297608b5f2b63dd3f0f)
do Abyss e a imagem Jellyfin 12.2 por digest. Originais, licenças e hashes ficam
em `deploy/abyss`; `prepare.py` gera os assets locais de forma reproduzível,
sem rede. Google Sans e Material Icons Round são hospedadas junto ao tema.
O verificador de atualizações do Spotlight foi removido.

O build confere os assets e o hash do `index.html` antes de acrescentar dois
scripts: defaults no início do `head`, antes dos bundles, e Spotlight no fim do
`body`. Atualizar Jellyfin ou o snapshot Abyss exige revisar esses pontos e
repetir o aceite. Não executar o instalador remoto upstream ou editar os arquivos
dentro do container em execução.

Use exclusivamente [`compose.playback.yaml`](../../compose.playback.yaml).
Continuam obrigatórios mídia NFS RO, root filesystem RO, UID/GID 1000, guards de
mount e lease, transcode no SSD dedicado e limite de 3 GiB. Scans permanecem
contidos; a integração não ativa automações, GPU nem novas portas.

O Spotlight reutiliza a sessão do usuário no mesmo domínio. O backdrop recebe
o blob obtido pela busca autenticada; o loader valida a origem da mensagem, o
iframe e o blob. A política da edge acrescenta somente GET/HEAD anônimos para
`/Branding/Css` e `/web/ui/spotlight.html`, considerando também o prefixo
`/jellyfin`. CSS, JavaScript e fontes seguem as regras de assets estáticos já
existentes. Outras páginas HTML e APIs administrativas não são liberadas; a
configuração de Branding continua administrativa.

## Preparação e ativação

1. Confira saúde do playback, lease, mounts e ausência de scan. Registre em
   recibo privado a imagem e o Compose atuais. Preserve uma cópia do Compose,
   do contexto de assets e da configuração de Branding antes da alteração.
   Exporte também as preferências visuais anteriores de cada conta, pela API
   administrativa privada. Credenciais e esses recibos ficam fora do Git.
2. Confira e construa os assets no checkout revisado:

   ```bash
   python3 deploy/abyss/prepare.py --check
   node deploy/abyss/test-defaults.mjs
   node deploy/abyss/test-runtime.mjs deploy/abyss/runtime
   docker compose --env-file .env.playback -f compose.playback.yaml config --quiet
   docker build --network=none --tag homeserver-jellyfin-abyss:12.2-2f809171-r6 deploy/abyss
   ```

   A imagem base por digest deve estar disponível localmente para o build
   offline. O contexto não deve conter `.env`, bancos, tokens ou mídia. Não
   apagar a imagem original nem os recibos das revisões anteriores.
3. Recrie somente Jellyfin com a imagem conferida:

   ```bash
   docker compose --env-file .env.playback -f compose.playback.yaml up -d --no-build --pull never jellyfin
   docker compose --env-file .env.playback -f compose.playback.yaml ps
   ```

   Confira o ID efetivo da imagem, healthcheck, binds, rootfs RO, limite de
   memória e lease. Falha de storage exige tratar a causa; não remover o guard
   para concluir uma mudança de aparência.
4. Na primeira aplicação, pela administração privada, preserve os demais campos
   de Branding e configure o CSS local. A revisão `r6` atualiza somente o import
   do bloco gerenciado para invalidar o cache do CSS:

   ```css
   /* HOMELAB ABYSS START */
   @import url('/jellyfin/web/ui/abyss.css?v=2f809171-r6');
   /* HOMELAB ABYSS END */
   ```

   Substitua somente uma versão anterior desse bloco; não empilhe imports nem
   sobrescreva CSS independente. Leia a configuração novamente para conferir.
5. Pela API privada, aplique base Dark e a ordem inicial recomendada pelo
   upstream: continuar
   assistindo, próximos episódios, bibliotecas pequenas e mídia recente, com
   as demais seções desativadas. Preserve as opções anteriores no recibo e
   altere somente as preferências visuais gerenciadas. Políticas de conta,
   senhas, bibliotecas e histórico não participam da aplicação do tema.
   Na atualização `r4` → `r5`, não repetir essas alterações de Branding ou das
   18 contas. O bootstrap versionado da `r5` migra uma única vez o `layout`
   local ausente, vazio, `auto`, `modern`, `desktop` ou `mobile` para Legacy,
   conforme modo e user-agent. Preserva TV, Legacy e valores desconhecidos.
   O marcador `homeserver.abyss.legacy-layout-v1=1` impede nova migração e
   mantém escolhas posteriores do usuário. A mudança ocorre ao carregar a
   página; em uma aba já aberta, use Ctrl+F5. O estado é próprio de cada
   navegador e origem. Fundos da conta só são inicializados
   se `${userId}-enableBackdrops` estiver ausente, inclusive após login.
6. Na primeira integração, publique a política exata de assets na edge e
   confira os testes. A `r5` usa a mesma política; não adicionar exceções amplas
   para HTML, imagens ou configuração.

## Aceite

A `r6` foi conferida em desktop e celular: barra da home com altura zero,
menu lateral abrindo, abas de filmes visíveis e retorno à home com Spotlight.
Login/logout passaram sem erros de página. A aplicação atualizou o import de
CSS e não mudou as preferências das 18 contas. A regra não remove os dados de
favoritos; a página continua em `#/home?tab=1`, sem a barra de seleção.

O aceite da `r5` conferiu desktop 1910×1000 inicialmente em `modern` e Android
390×844 inicialmente em `auto`: ambos migraram para o Legacy correspondente,
com marcador `1`. Uma escolha Modern posterior permaneceu após recarregar nos
dois contextos. Login/logout, Spotlight, fontes Google Sans e Material Icons,
backdrop e home passaram sem erros de página. O cabeçalho Legacy mostrou
Home/Favourites, com bibliotecas no conteúdo, conforme o preview.

A imagem retornou saudável, com rootfs RO, mounts, limite de 3 GiB e guard
conferidos. Ruff e 40 testes focados passaram, com um skip de Node no WSL;
os checks de defaults e runtime passaram com Node no Windows. A regressão
final de playback passou em login comum, Spotlight, fundo da home, detalhes,
remoção do backdrop fora da home, vídeo 1920×1080 (`readyState=4`) e logout,
sem erros de página. A tela reduzida para 390×844 ficou sem overflow horizontal.
Isso não constitui teste físico de cliente de TV ou de publicação externa.

Em 16/11, o aceite da `r4` passou em contextos novos de desktop 1440×1000 e Android 390×844
com toque. Login, Spotlight, Google Sans e Material Icons locais, layout Legacy,
backdrop ativo e logout passaram nos dois contextos, sem erros de página.
No desktop, detalhes e vídeo 1920×1080 (`readyState=4`) passaram; a classe de
transparência saiu ao deixar a home. No celular, busca retornou cards do
catálogo, com `scrollWidth=390` e `scrollX=0`.

Setas e roda dos carrosséis também foram conferidas mantendo a página desktop
em `scrollX=0`; o clip da raiz é contenção preventiva. A transparência do fundo
fica limitada à home com Spotlight para revelar o backdrop difuso.

Branding e preferências visuais das 18 contas foram conferidos por releitura;
a troca final do import para `r4` não alterou essas preferências novamente.
Os valores originais e os anteriores à revisão visual foram preservados
privadamente. O helper privado permite reverter a revisão visual ou retirar o
tema integralmente, tolerando aplicação parcial e retomada; sete testes com
fixtures passaram.

A imagem `r4` retornou saudável, preservando mounts, mídia e rootfs RO,
limite de 3 GiB e guards. `prepare.py --check`, checks JavaScript e Ruff passaram;
a regressão focada dessa revisão teve 43 testes aprovados e um skip de Node no WSL,
suprido pelos checks JavaScript separados no Windows.

Treze checks HTTP passaram no percurso interno Caddy → Nginx → guard, com
requisições originadas no namespace de rede do Caddy. GET/HEAD dos assets
necessários retornaram 200; configuração administrativa, HTML não listado,
POST de Spotlight e imagem anônima retornaram 401/403. Isso testa a política
HTTP interna, sem atestar WAN, certificado ou navegação externa por TLS.

Para repetir o aceite depois de uma atualização:

Inclua navegadores já configurados como Modern/Auto, a migração para Legacy e
uma escolha manual posterior que deve sobreviver a recarregamentos. Confira
também TV/Legacy/valores desconhecidos, fundos explicitamente desativados e
manutenção do CSS/Branding anterior.

Confira pelo navegador em desktop e em tela pequena com toque: login comum,
banner com fontes e ícones corretos, navegação, busca, detalhes e reprodução
com frames decodificados. Compare screenshots com os previews da revisão
upstream, considerando que títulos, artes e disponibilidade de conteúdo vêm
do catálogo local. O tema não fornece as artes dos previews.

Depois da troca, use recarregamento completo para descartar CSS antigo e uma
sessão nova para conferir preferências. Observe erros de console e requisições:
os assets Abyss não devem buscar Google, jsDelivr ou GitHub. Recursos nativos
do Jellyfin, como Chromecast, devem ser distinguidos dos recursos do tema.

Pelo proxy, confira GET/HEAD dos assets necessários e rejeição das APIs
administrativas e de outros HTML. Refaça o login comum e confirme que a
administração continua restrita ao acesso privado. Teste direto pelo túnel SSH
não substitui o aceite do proxy, de TLS ou de reprodução pela internet.

Não executar scan para testar a aparência. Use uma mídia já disponível e
registre o método de teste; qualquer teste que envie progresso de reprodução
pode alterar o histórico da conta. Para o aceite automatizado desta integração,
as notificações de progresso são interceptadas somente no navegador de teste.

## Reversão

Faça a reversão pela conexão administrativa privada, usando os recibos da
revisão escolhida. Não restaure o banco inteiro para remover um tema: isso
sobrescreveria atividades posteriores dos usuários.

1. Compare Branding e cada preferência gerenciada com o valor aplicado e o
   valor anterior. Aceite valores já restaurados; se houver uma terceira
   alteração, preserve-a e resolva a divergência antes de escrever. Faça essa
   conferência completa antes de iniciar a reversão, inclusive após aplicação
   parcial.
2. Remova somente o bloco CSS gerenciado e restaure os campos visuais alterados
   a partir do recibo. Preserve outros campos de Branding, preferências,
   políticas, credenciais e histórico. Leia novamente e registre o resultado.
3. Recupere o Compose preservado e a imagem correspondente. Recrie somente o
   serviço Jellyfin com `up -d --no-build --pull never jellyfin`, usando o
   arquivo de ambiente privado e o manifest de playback. O contexto original
   também deve ser preservado para o próximo build; remover o CSS sozinho não
   retira o script Spotlight da imagem.
4. Se a revisão da edge também for revertida, recupere somente a referência da
   imagem da política preservada, mantendo os demais campos atuais do ambiente.
   Recrie apenas o guard público; não altere firewall, Caddy, Nginx ou portas
   para reverter o tema.
5. Confira saúde, imagem efetiva, guards, mídia RO, login e reprodução. Registre
   os resultados privadamente. Configuração, cache e acervo permanecem nos
   destinos existentes durante todo o procedimento.

Layout migrado, marcador e defaults do localStorage sobrevivem à troca de
imagem. Se também quiser revertê-los, depois de retirar o bootstrap, ajuste layout e fundos nas
preferências de exibição de cada navegador. Para voltar à ausência de um valor,
remova somente a chave `layout` ou `${userId}-enableBackdrops` correspondente,
conforme a escolha do usuário; não limpe todo o armazenamento da origem.
Preserve `homeserver.abyss.legacy-layout-v1` ao manter ou reinstalar a `r5` se
quiser conservar a escolha manual; remover o marcador permite nova migração.

Para voltar à revisão anterior, use seus respectivos recibos de imagem,
Compose e Branding. Para retirar Abyss por completo, use o Compose/imagem e as
preferências anteriores à primeira implantação, sempre preservando alterações
independentes posteriores.
