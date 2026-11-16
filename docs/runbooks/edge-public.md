# Entrada pública Jellyfin na edge

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
**Ainda não há certificado TLS.** Let's Encrypt resolveu o hostname/destino
esperado, mas registrou timeout externo TCP 80. O proprietário confirmou a WAN
esperada e mapeamentos TCP 80/443 ativos para o endereço LAN do PVE, com MAC
correto. HTTP e HTTPS deram timeout pelo 4G. Windows resolveu
o A esperado e o domínio respondeu HTTP 308 pelo Caddy, com Location HTTPS
correto; HTTP LAN com Host explícito também retornou 308. NAT loopback funciona
nesse desktop, superando a tentativa anterior de timeout HTTP. HTTPS falhou no
Schannel, ainda sem certificado emitido. Essa prova local não comprova acesso 4G.

As capturas anteriores em `vmbr0` foram invalidadas: nem o controle LAN apareceu.
Elas não provam ausência de tráfego externo. A captura SYN corrigida em `nic1`
validou controles LAN e NAT loopback e cobriu o intervalo da tentativa 4G
relatada. Outros SYN com origem aparente igual à WAN também apareceram; não foi
identificada origem externa distinta atribuível à tentativa 4G. Isso orienta
a investigação do caminho anterior ao servidor, sem concluir bloqueio da
operadora ou falha do roteador: SNAT pode ocultar a origem real. O status WAN
enviado pelo proprietário confirmou a conexão IPv4 ativa com o endereço esperado
pelo DNS e o vínculo correto dos mapeamentos.

O próximo teste orientado usa regra temporária **WAN TCP 10443 → LAN TCP 443
do PVE**, na mesma conexão WAN. O proprietário ainda não confirmou sua criação;
o teste não foi aplicado e não houve mudança adicional no PVE ou na edge.

O túnel temporário de teste foi encerrado. O health privado na porta 18096
respondeu HTTP 200; essa prova não demonstra acesso pela internet.

Não anunciar disponibilidade pública: os checks do proxy e o redirect LAN não
comprovam TLS ou reprodução externa. Manter as proteções do firewall, sem
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
- Preparar exclusivamente `/srv/appdata/edge/caddy` e
  `/srv/appdata/edge/public-security` no SSD da edge, UID/GID 1000, modo 0700.
  O primeiro conserva certificados/chaves ACME; o segundo conserva apenas o
  limitador de login, sem banco do controlador, fila, mídia ou credenciais admin.
  `create_host_path: false` recusa diretórios ausentes. Os binds de configuração
  são somente leitura e precisam existir.
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
   dos dois proxies. Manter os serviços administrativos privados inalterados.
2. Plataforma habilita apenas edge TCP 80/443 de entrada, edge → playback TCP
   8096 e playback ← edge TCP 8096, preservando as negações para core/gestão.
3. Iniciar estes três serviços explicitamente. Testar localmente hostname,
   autenticação comum, negação admin, spoof de headers, Range, WebSocket e guard
   indisponível. Não encaminhar porta diretamente ao Jellyfin.
4. Plataforma aplica DNAT estritamente do IP LAN/bridge de entrada TCP 80/443
   para edge nas mesmas portas, com hooks simétricos no fragmento de rede já
   existente. Sem flush de firewall, reload global, nova unit ou mudança em fstab.
   O roteador encaminha essas portas ao IP LAN do host, não a 8096.
5. Conferir certificado válido para o hostname, HTTP→HTTPS, login/logout comum,
   negação admin e reprodução externa com seek/legendas. Distinguir teste pela
   LAN de acesso externo real. Só então registrar a publicação como operante.

O rollback fecha somente as duas regras DNAT de propriedade desta publicação e
para o projeto `homeserver-edge`, preservando certificados/limites e os demais
serviços. Sem resetar o estado de storage ou remover pausas manuais.

Caddy suporta portas internas alternativas mantendo as portas públicas normais
([opções globais](https://caddyserver.com/docs/caddyfile/options)); o proxy
substitui headers de encaminhamento com a conexão TCP observada
([reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)).
O NAT deve conservar as regras de conntrack já necessárias à rede Proxmox
([documentação de rede](https://github.com/proxmox/pve-docs/blob/master/pve-network.adoc)).
