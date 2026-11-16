# Entrada pública Jellyfin na edge

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
  erros e Caddy descarta seu logger; Docker usa `none`. Capturas excepcionais
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
