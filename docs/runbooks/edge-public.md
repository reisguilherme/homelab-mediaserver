# Retirada da entrada pública e acesso privado ao Jellyfin

## Decisão vigente — 16/11/2026

O proprietário revogou a publicação WAN. O endereço escolhido é
`https://jellyfin.greis.dev.br`, na porta 443, exclusivamente por Tailscale,
conservando o prefixo `/jellyfin/`. O cliente precisa estar conectado à tailnet
e autorizado pelas regras de acesso da plataforma.

O registro DNS aponta ao endereço Tailscale do control. Seu listener HTTPS
privado encaminha diretamente à playback:8096, com autenticação e autorização
nativas do Jellyfin. O certificado DNS-01 do control cobre esse hostname.
O guard `public_access` da edge não participa desse caminho; não transportar
suas restrições administrativas, headers `:10443` ou redirects para o proxy
privado. A plataforma mantém a configuração de ingresso; o consumidor mantém
o Jellyfin em `compose.playback.yaml`, mídia RO, lease e contenção de scans.

## Publicação encerrada

- `edge-tls`, `public-proxy` e `public-security` estão parados. A parada não
  removeu volumes, certificados ou redes Docker.
- A VM edge foi desligada normalmente, sem parada forçada, com início
  automático desabilitado. Discos e todas as demais configurações foram
  preservados; não religar a VM para atender o novo endereço privado.
- Os dois DNAT públicos TCP 80/443 e seus quatro hooks persistentes foram
  removidos no PVE. Também foram retiradas as três exceções de firewall:
  entrada pública na edge e as duas permissões do caminho edge → playback.
- A conferência do dataplane confirmou a retirada. O caminho control →
  playback, SSH, NFS, NAT de saída e as demais regras foram preservados.
  Não houve alteração de encaminhamentos no roteador nessa operação.
- O código de `compose.edge.yaml`, Caddy, Nginx e guard foi preservado,
  **inativo**. Não executar a sequência histórica de publicação nem reativar
  esses serviços para atender o endereço privado.
- O certificado e a chave exclusivos de `streaming.greis.dev.br` ficaram
  retidos privadamente, sem uso no listener atual. Não foram transferidas
  credenciais DNS ou a conta ACME para a edge.

Os backups e recibos da retirada são privados. O helper removeu apenas as
linhas e regras da publicação original, preservando alterações posteriores
do acesso privado. Seu rollback é explícito e reabriria o caminho antigo;
não executá-lo como recuperação automática de uma falha no acesso Tailscale.

## Aceites e limites atuais

A plataforma registrou 478 testes locais aprovados e um skip. Após desligar
a edge, os nove domínios privados responderam HTTP 200 com TLS válido pelo
Windows/Tailscale e DNS normal; as portas de gestão direta inspecionadas
permaneceram negadas. O Nginx privado
adapta `api_key` legado somente nas rotas WebSocket e Download e somente na
ausência de credencial moderna, preservando a precedência da autenticação
nativa. Essa compatibilidade não exigiu recriar a playback nem modificar
imagem, dados ou configuração do Jellyfin.

Os 14 checks do helper HTTP/API no Windows via Tailscale, DNS normal e HTTPS
verificado passaram: login e identidade comum/admin, negação 403 de endpoint admin à
conta comum, catálogo, Range 206 e Download legado 206, ambos de 65.536 bytes,
WebSocket 101 com `ApiKey` nativo e com `api_key` legado, e dois logouts com
revogação. Esses resultados não comprovam a interface web completa,
reprodução interativa completa pelo novo caminho ou vídeo externo.

DNS permanece no Registro.br, sem Cloudflare/VPS. A renovação DNS-01 do
certificado privado continua manual, sem API integrada. O antigo DDNS para
o endereço WAN deixou de ser requisito desse ingresso privado. DNS correto
não substitui conexão Tailscale, autorização ou validação do certificado.

## Histórico público preservado

A etapa anterior usou `https://streaming.greis.dev.br:10443/jellyfin/`, com
WAN 10443 → PVE 443 → edge 443 → Caddy 8443. O Certbot do control emitiu um
certificado Let's Encrypt exclusivo por DNS-01; o Caddy carregava a cadeia
e a chave estáticas por bind RO. Somente os dois proxies foram recriados
naquela implantação, preservando ID e StartedAt do guard.

Passaram 33 checks TLS reais pelo Windows na LAN via NAT loopback: login
comum, catálogo, Range de 64 KiB, WebSocket, bloqueios administrativos,
spoof e três logouts. Esses resultados nunca comprovaram 4G nem reprodução
completa externa. A correção do registro A após mudança de IP dinâmico e os
diagnósticos de portas da operadora pertencem a essa etapa encerrada.

O [plano público em 10443](../plans/2026-11-16-public-10443.md) conserva as
evidências de implementação. Ele foi substituído pela decisão privada;
não é uma instrução de ativação. A arquitetura geral e o ingresso Tailscale
pertencem ao
[homelab-platform](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/architecture/README.md).
