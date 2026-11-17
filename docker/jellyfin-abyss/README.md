# Abyss local para o playback

Este diretório acrescenta CSS e Spotlight à imagem Jellyfin existente. Não
instala outro servidor e não altera bancos, contas, biblioteca, entrypoint,
montagens ou recursos do container. Detalhes da arquitetura estão no
[README do repositório](../../README.md); o CSS deve ser aplicado pelo
Branding nativo do Jellyfin.

O snapshot `vendor/` corresponde ao commit upstream
`2f809171ed7f3a34b5e78297608b5f2b63dd3f0f` de
[AumGupta/abyss-jellyfin](https://github.com/AumGupta/abyss-jellyfin/tree/2f809171ed7f3a34b5e78297608b5f2b63dd3f0f).
`abyss.css` e `LICENSE` vêm da raiz; os três arquivos Spotlight vêm de
`scripts/spotlight/`. Os arquivos originais e a licença MIT permanecem intactos.
Os hashes estão em `manifest.json`.

`fonts/` preserva Google Sans (SIL OFL 1.1) e Material Icons Round do pacote
`material-icons@1.13.12` (Apache 2.0), com licenças, avisos, fontes de download
e hashes fixados. Google Sans inclui Latin e Latin Extended, normal e itálico,
com pesos variáveis 400–700 e o intervalo óptico solicitado pelo CSS original.
Outros alfabetos usam o fallback que o navegador tiver disponível.

`prepare.py` usa somente a biblioteca padrão Python e funciona sem rede. Antes
de gerar qualquer arquivo, confere os bytes de todas as fontes e os pontos de
alteração. `runtime/` é versionado para que o build não precise de Python.

```sh
python3 deploy/abyss/prepare.py --check
docker build --tag homeserver-jellyfin-abyss:12.2-2f809171-r6 deploy/abyss
```

Para regenerar depois de alterar transformações revisadas:

```sh
python3 deploy/abyss/prepare.py
python3 deploy/abyss/prepare.py --check
node deploy/abyss/test-defaults.mjs
node deploy/abyss/test-runtime.mjs deploy/abyss/runtime
```

O contexto Docker contém apenas os assets preparados. O build usa a imagem
base por digest, verifica seus assets e o SHA-256 exato do `index.html` antes
de acrescentar dois scripts: defaults no início do `head`, antes dos bundles,
e Spotlight no fim do `body`. Uma atualização do Jellyfin exige revisão do
digest e do index; a transformação recusa seguir versões desconhecidas.
Nenhum instalador upstream é executado.

Diferenças locais reproduzíveis:

- Google Sans e Material Icons Round originais são servidos localmente. O
  `abyss.css` mantém as famílias, ligaturas e regras visuais upstream; somente
  duas URLs externas são substituídas. `spotlight.css` permanece intacto.
- `html.layout-desktop { overflow-x: clip; }` contém preventivamente a raiz;
  setas e roda continuam movimentando os carrosséis internamente. Não foi
  reproduzido deslocamento horizontal da página por interação real.
- Na home com Spotlight visível, `.backgroundContainer` fica transparente
  para mostrar o backdrop difuso. O seletor exige
  `html.abyss-spotlight-visible`, preservando o fundo das demais páginas.
- A `r6` oculta a barra Início/Favoritos quando `#indexPage.homePage` está
  visível, a pedido do proprietário. Cabeçalho, menu, busca e abas das
  bibliotecas permanecem. O import de Branding usa `?v=2f809171-r6`.
- Aviso e consulta de atualização do Spotlight removidos. CSS e JavaScript
  não dependem de Google, jsDelivr ou GitHub durante o uso.
- Backdrop compartilhado usa o blob que o Spotlight já buscou autenticado.
  O loader aceita somente blob da mesma origem e mensagem do iframe esperado
  com origem correspondente. O proxy não precisa liberar imagens anonimamente.
- O script é carregado de `ui/spotlight-loader.js`; os demais assets estão no
  mesmo diretório. O prefixo Jellyfin, inclusive `/jellyfin`, é preservado.
- `local/abyss-defaults.js` recebe na `r5` uma migração única do layout local.
  Valores ausente, vazio, `auto`, `modern`, `desktop` e `mobile` passam para
  `desktop-legacy` ou `mobile-legacy`, conforme modo e user-agent. TV, Legacy e
  valores desconhecidos são preservados. O marcador estável
  `homeserver.abyss.legacy-layout-v1=1` impede repetir a migração e preserva
  escolhas feitas depois dela. Fundos continuam sendo ativados somente quando
  a preferência local da conta está ausente, inclusive após login.

O tema exige base Dark. A aplicação pela API privada configura Dark e a ordem
da home recomendada pelo upstream, preservando os valores anteriores em recibo
privado e os demais campos. A `r5` mantém o CSS e o import Branding da `r4`, sem
reaplicar preferências às 18 contas; muda a imagem e a URL versionada do bootstrap.
Voltar à imagem anterior não remove layout, marcador ou fundos do navegador.

A `r4` inicializava somente layouts ausentes: um navegador já configurado como
`modern` conservava o cabeçalho anterior. No aceite da `r5`, foi
conferida a migração Modern → desktop Legacy e Auto → mobile Legacy, marcador `1` e
preservação de uma escolha Modern posterior ao recarregar. Fontes, Spotlight,
backdrop e login/logout passaram nos dois contextos, sem erros de página.
O upstream declara testes com Jellyfin 12.1; o uso em 12.2 exige validação no
navegador. Testes locais de empacotamento e JavaScript não comprovam reprodução,
renderização, isolamento de rede ou aceitação no servidor.
