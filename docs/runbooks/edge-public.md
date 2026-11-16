# Entrada pública Jellyfin na edge

## Decisão atual — 16/11/2026

O proprietário aceitou `https://streaming.greis.dev.br:10443/jellyfin/` após
a recusa da operadora em liberar 80/443 para PF. Esta escolha substitui a
preferência anterior pela URL sem porta. A edge usa os headers canônicos
Host/X-Forwarded-Host com `:10443`, X-Forwarded-Port 10443 e redirect HTTP 308
explícito para a mesma autoridade, preservando caminho e query.

**TLS implantado:** permanece WAN 10443 → PVE 443 → edge 443 → Caddy 8443.
Somente `public-proxy` e `edge-tls` foram recriados; o ID e o StartedAt de
`public-security` foram preservados. Portas internas, firewall, guard e mídia
permanecem iguais. A `https_port` do Caddy não deve virar 10443.
O redirect HTTP é útil onde a porta 80 estiver alcançável; não faz a porta 80
bloqueada funcionar. Usuários externos precisam do endereço HTTPS completo.

O Certbot do control emitiu os certificados Let's Encrypt por DNS-01. O Caddy
carrega `fullchain.pem` e `privkey.pem` do certificado estático exclusivo para
`streaming.greis.dev.br`. A edge recebe
somente a cadeia pública e a chave desse certificado, sem conta ACME, chave
de outros domínios ou credenciais DNS. Arquivos ausentes impedem a inicialização;
não há fallback para emissão HTTP-01.

Passaram **33 aceites TLS reais** pelo Windows na LAN até o destino público
na porta 10443, via NAT loopback: login comum, catálogo, Range de 64 KiB,
WebSocket, admin 403, spoof e três logouts. Esses testes não comprovam 4G
nem reprodução completa nesse TLS; o proprietário ainda precisa testar ambos.

O registro A antigo correspondia ao IP dinâmico anterior. O proprietário
corrigiu-o, mantendo somente o destino atual; `a.sec.dns.br`, `b.sec.dns.br`
e o resolvedor público consultado confirmaram a resposta. O resolvedor Windows
ainda mantinha cache antigo. DNS segue no Registro.br, sem Cloudflare/VPS.
**Renovação DNS-01 e DDNS continuam manuais**, sem API integrada do Registro.br.
Mudança de IP ou expiração exigem intervenção; não prometer continuidade automática.

O deploy usou `.env.edge` e backup privado v2; a identidade real do guest foi
conferida separadamente do alias SSH. Conferir esses recibos antes de reutilizar
o helper transitório ou executar rollback.

Administração permanece privada. A plataforma prepara domínios próprios
apontando ao IP Tailscale da VM control, com HTTPS na porta 443 e grants; não adicionar
vhosts administrativos nesta edge. Ver o [plano de execução](../plans/2026-11-16-public-10443.md).

## Evidências anteriores à decisão pela porta 10443

**Estado em 16/11/2026, publicação em ativação:** backend edge → playback:8096 ativo
(HTTP 200). Firewall PVE `enabled/running`, DROP IN/OUT e chains das NICs
conferidos; edge → core:8080, controle:443 e PVE:8006/22 deram timeout.
Proxy: Items sem sessão 401, configuração 403, Users/Public `[]`, acesso direto
ao Nginx 403 e guard indisponível 500. Hostname incorreto no Caddy retornou 200
com corpo vazio, sem Jellyfin; esse teste não é uma negação HTTP 403/444.

Duas regras DNAT TCP 80/443 foram aplicadas no PVE sem reload. HTTP 308 para
HTTPS correto passou pela LAN. O proxy passou em **33 checks reais**: login
comum, identidade, catálogo de 20 itens, Range 206 de 64 KiB, WebSocket 101,
login admin 403, token admin válido 403, GETs administrativos/spoof negados e
três logouts. Caddy definitivo e os outros dois containers estão Running com
rootfs RO, UID 1000, capabilities ALL removidas e limites somados de 448 MiB.
O DNS A foi confirmado e o diretório ACME respondeu 200 até a partir do Caddy.
**Naquele diagnóstico ainda não havia certificado TLS.** ACME e 4G nas portas externas 80/443 deram
timeout. DNS, WAN e vínculo dos mapeamentos foram conferidos; HTTP pelo domínio
retorna 308 no desktop, comprovando NAT loopback nesse cliente.

A regra **temporária WAN TCP 10443 → LAN TCP 443 do PVE** está ativa. O teste
4G retornou `ERR_SSL_PROTOCOL_ERROR`; a captura validada em `nic1` registrou
oito SYN de origem externa distinta para PVE:443, além dos controles LAN.
A porta alternativa chega ao mesmo destino interno, ainda sem TLS. O diagnóstico
de 80/443 externas concentra-se no caminho anterior à edge, sem distinguir
definitivamente roteador de operadora.

A preferência então era **resolver primeiro 80/443 para URL sem porta**,
substituída pela decisão acima. À época, 10443 era somente diagnóstico, sem TLS.
A consulta RO mostrou firewall Off, Attack Protection Enable, controle parental
desmarcado e tabela IP Filter vazia. Drop Upstream no formulário não é regra
aplicada; o menu Access Control do manual não aparece na UI. Próximo passo:
suporte Brava conferir filtro/reserva de 80/443 no equipamento ou a montante,
sem atribuir definitivamente a causa à operadora.

A auditoria privada de **15/10/2026** já registrava timeout externo TCP 80/443 e
resposta em 54443. O proxy antigo era uma TV box Armbian na mesma LAN, não na
nuvem: WAN 54443 → TV box 443, com DNS-01 na configuração histórica. A execução
de 18/10 documentou HTTP 200 externo e TLS válido por esse caminho. Portanto,
o acesso antigo pela porta alta não comprova funcionamento de WAN 443; os
timeouts padrão antecedem a migração.

Texto preparado para chamado, **não enviado**:

> Solicito verificar filtro ou reserva de TCP 80 e 443 no equipamento fornecido
> e na rede de acesso. WAN e mapeamentos foram conferidos. As portas padrão dão
> timeout externo, enquanto um teste temporário em TCP 10443 chega ao mesmo
> destino interno TCP 443, com SYN externo capturado. O servidor e o loopback
> local respondem. Há evidência anterior à migração de timeout em 80/443 e
> funcionamento em porta alta. Precisamos usar 80/443 sem porta alternativa;
> favor informar o ponto da restrição e o procedimento para liberação.

Limpeza dos dispositivos de teste Jellyfin v2/v3: seis registros conferidos,
quatro já ausentes e dois removidos por IDs de recibos, metadados e janela
temporal. Logout próprio verificado; registros v1 não identificados com
segurança não foram alterados. IDs, nomes e tokens permanecem privados.

O túnel temporário de teste foi encerrado. O health privado na porta 18096
respondeu HTTP 200; essa prova não demonstra acesso pela internet.

Os checks anteriores do proxy e o redirect LAN não comprovavam TLS ou
reprodução externa. Manter as proteções do firewall, sem
ampliar portas do PVE ou da edge. O diagnóstico TLS temporário do Caddy usa arquivo
privado 0600 limitado a 1 MiB; logs HTTP continuam desativados. IPs e pacotes
brutos não pertencem à documentação pública. As etapas abaixo conservam a
ordem de implantação.

`compose.edge.yaml` prepara somente `streaming.greis.dev.br`: Caddy TLS →
Nginx → Jellyfin privado na playback, com o guard `public_access` existente.
Este arquivo não comprova DNS, NAT, emissão de certificado ou reprodução externa.
O Compose monolítico e o core não participam da implantação.

O guard revalida a sessão nativa e recusa contas administrativas/desabilitadas,
inclusive quando tentam uma rota comum. A allowlist nega APIs administrativas,
exclusão de mídia, criação de usuário e scans. Login por senha, o prefixo
`/jellyfin`, imagens autorizadas, Range e WebSocket conservam o protocolo nativo.
Quick Connect permanece negado. O relay histórico de apresentação de legendas
não é necessário para o ingresso; este caminho usa a resposta nativa Jellyfin.

## Preparação e limites

- Fixar os três IDs locais completos em `EDGE_CADDY_IMAGE_ID`,
  `EDGE_NGINX_IMAGE_ID` e `EDGE_PYTHON_IMAGE_ID`; Compose usa `pull_policy: never`.
  O Python deve conter as fontes públicas atuais e `/app/.venv/bin/python`.
  Conferir `/usr/sbin/nginx` e a imagem Caddy sem file capabilities produzida pela
  receita `docker/core-caddy/`. Não herdar comandos/ENV de imagens capturadas.
- `EDGE_BIND_IP` deve ser o endereço privado conferido da VM edge. Somente
  TCP 80/443 são publicados; os listeners internos são 8080/8443 para UID 1000.
  Confirmar ausência de conflito de `172.30.248.0/28` antes de criar a rede Docker.
- Preparar `/srv/appdata/edge/caddy`, `/srv/appdata/edge/public-security` e
  `/srv/appdata/edge/tls/streaming` no SSD da edge, UID/GID 1000, modo 0700.
  O primeiro conserva o estado local do Caddy; o segundo conserva apenas o
  limitador de login, sem banco do controlador, fila, mídia ou credenciais admin.
  O terceiro contém somente `fullchain.pem` e `privkey.pem` entregues pelo control,
  UID/GID 1000, modo 0600. O diretório inteiro é montado somente leitura em
  `/etc/homelab-tls/streaming`; não montar a árvore privada do Certbot na edge.
  `create_host_path: false` recusa diretórios ausentes. Os binds de configuração
  são somente leitura e precisam existir.
- Antes da entrega, conferir validade, SAN `streaming.greis.dev.br`, cadeia e
  correspondência certificado/chave. Validar o Caddy com os arquivos instalados.
  A renovação exige nova entrega do par e reinício controlado somente de
  `edge-tls`, pois a API administrativa está desabilitada; trocar arquivos no
  host não recarrega o certificado em memória. Preservar o par anterior privado
  para rollback. Automação de renovação/distribuição ainda depende da plataforma.
- Os limites somam 448 MiB: Caddy 96, Nginx 96, Python 256. Sem swap adicional,
  capabilities ou host networking. `restart: no` mantém a ativação inicial
  explícita. O consumo real e a capacidade de streaming dependem de medição.
- Não há logs de requisições: URLs podem conter tokens nativos. Nginx descarta
  erros e a configuração padrão do Caddy descarta seu logger; Docker usa `none`.
  A exceção temporária de diagnóstico TLS está delimitada no estado acima.
  Capturas excepcionais
  precisam permanecer privadas e redigidas; não habilitar logs brutos na WAN.

## Ordem operacional

1. Conferir imagens, SSD, diretórios, subnet, configuração renderizada e sintaxe
   dos dois proxies. Preparar e testar DNS-01 no Certbot do control, renovação
   e DDNS antes da ativação TLS. Conferir a entrega exclusiva do certificado
   de streaming, suas permissões e o carregamento estático no Caddy.
   Não iniciar antes de instalar e validar `fullchain.pem` e `privkey.pem`.
2. Preservar as permissões mínimas existentes: entrada na edge em TCP 443,
   edge → playback TCP 8096 e as negações para core/gestão. A porta 80 interna
   existente atende apenas o redirect onde alcançável; não depender dela para
   clientes externos ou emissão. Manter administração privada.
3. Com a estratégia de certificado configurada, ativar explicitamente os
   serviços necessários e testar localmente TLS, hostname, autenticação comum,
   negação admin, spoof de headers, Range, WebSocket e guard indisponível.
   Não encaminhar porta diretamente ao Jellyfin.
4. Conferir o mapeamento já criado pelo proprietário: **WAN TCP 10443 → IP LAN
   do PVE TCP 443 → IP privado da edge TCP 443 → Caddy TCP 8443**. Não reaplicar
   regras antigas de encaminhamento WAN 80/443. Preservar os hooks simétricos
   do DNAT existente, sem flush de firewall, reload global, nova unit ou fstab.
5. Conferir certificado válido para o hostname na porta 10443, login/logout
   comum, negação admin e reprodução externa com seek/legendas. Testar também
   a LAN; NAT loopback de 80 não prova o de 10443. Verificar o redirect apenas
   onde HTTP estiver alcançável. Só então anunciar a publicação como operante.

O rollback restaura os arquivos anteriores da edge e, se necessário, fecha
somente as regras DNAT de propriedade desta publicação e para o projeto
`homeserver-edge`, preservando certificados/limites e os demais serviços.
Sem resetar o estado de storage ou remover pausas manuais.

Caddy suporta portas internas alternativas mantendo as portas públicas normais
([opções globais](https://caddyserver.com/docs/caddyfile/options)); o proxy
substitui headers de encaminhamento com a conexão TCP observada
([reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)).
O NAT deve conservar as regras de conntrack já necessárias à rede Proxmox
([documentação de rede](https://github.com/proxmox/pve-docs/blob/master/pve-network.adoc)).
