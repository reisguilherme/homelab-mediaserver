# Segurança: estado observado e fronteiras propostas

Revisão documental em 2026-11-14, baseada no working tree, sem executar serviços ou testes.
Não foram lidos `.env`, credenciais, bancos, mídia ou inventários privados.
Este documento não altera publicação, firewall, montagens, usuários ou permissões.

## Como interpretar a evidência

- **Estático:** comportamento ou configuração demonstrável no código inspecionado.
- **Hipótese runtime:** consequência condicionada ao estado efetivo do servidor; precisa de medição.
- **CVE não verificada:** não houve inventário das imagens instaladas nem análise de CVEs por digest; não se declara vulnerabilidade de versão nem ausência dela.
- Severidade representa impacto e condição de exploração; não significa incidente ocorrido.
- A existência de teste no repositório não comprova que passou, nem que o isolamento funciona no servidor.

## Modelo de ameaça

| Ator ou evento | Acesso considerado | Ativo em risco |
| --- | --- | --- |
| Visitante da internet | Login e recursos públicos permitidos | Disponibilidade, contas e superfície dos proxies |
| Usuário Jellyfin autenticado | Reprodução, catálogo e ações pessoais permitidas | CPU, RAM, sessões e privacidade da biblioteca |
| Conta pública comprometida | Permissões nativas dessa conta | Conteúdo autorizado e recursos de reprodução |
| Processo público comprometido | Credenciais, rede e volumes do próprio processo | Mídia, configurações e serviços alcançáveis |
| Administrador privado | Painéis autenticados pela rede administrativa | Controle completo autorizado e operações destrutivas manuais |
| Falha do compute ou HD USB | Um domínio físico atual de falha | Disponibilidade e integridade; três VMs não criam redundância física |

Proxmox, SSH, administração Kubernetes futura, Arr e qBit administrativo pertencem ao domínio privado.
Acesso público autenticado ao Jellyfin não deve conceder acesso de administração do host ou dos serviços.
Uma falha de execução remota é uma premissa para avaliar alcance; não foi demonstrada no Jellyfin ou Seerr.

## Controles presentes no repositório

- O ingresso público valida o peer do proxy, normaliza caminhos e rejeita credenciais ambíguas: [public_policy.py:26](../../../services/control/src/homeserver_control/api/public_policy.py#L26) e [public_access.py:138](../../../services/control/src/homeserver_control/api/public_access.py#L138).
- Sessões públicas Jellyfin devem pertencer a usuário ativo e não administrador; Seerr rejeita proprietário e permissões administrativas: [public_access.py:185](../../../services/control/src/homeserver_control/api/public_access.py#L185).
- O allowlist público não admite exclusão de mídia; o fluxo privado exige administrador e valida identidade/caminho: [public_policy.py:167](../../../services/control/src/homeserver_control/api/public_policy.py#L167) e [deletion_capture.py:226](../../../services/control/src/homeserver_control/api/deletion_capture.py#L226).
- Relay e integração usam upstreams fixos; a integração limita caminhos, campos e peers: [jellyfin_labels.py:90](../../../services/control/src/homeserver_control/api/jellyfin_labels.py#L90) e [integration_proxy.py:88](../../../services/control/src/homeserver_control/api/integration_proxy.py#L88).
- O gateway separa aquisição automatizada da administração manual qBit, com autenticação, admissão e negação de mutações genéricas: [gateway/app.py:109](../../../services/control/src/homeserver_control/gateway/app.py#L109), [gateway/app.py:1322](../../../services/control/src/homeserver_control/gateway/app.py#L1322) e [gateway/app.py:1520](../../../services/control/src/homeserver_control/gateway/app.py#L1520).
- Há política nftables para pares, tráfego roteado, bridges e IPv6, além de preparação de persistência: [public_network.py:64](../../../services/common/src/homeserver_common/public_network.py#L64) e [public_persistence.py:38](../../../services/common/src/homeserver_common/public_persistence.py#L38).

## Riscos priorizados

### R1 — Alto: escrita direta sobre mídia pelo Jellyfin

- **Evidência estática:** [compose.yaml:47](../../../compose.yaml#L47) monta mídia com escrita; [nginx.conf:33](../../../deploy/jellyfin-proxy/nginx.conf#L33) coordena exclusões por rota HTTP.
- **Condição e impacto:** execução de código no processo pode alterar ou remover arquivos sem chamar a API; alteração de conteúdo de um hardlink também pode atingir a fonte que compartilha o inode.
- **Controle e residual:** UID sem root, capabilities removidas e exclusão coordenada limitam outros caminhos, mas não retiram a escrita do volume. A escrita atual é deliberada para edições e legendas.
- **Teste pendente:** em fixture isolada, comparar edição, exclusão por API e escrita direta como UID do serviço; verificar hardlinks sem usar mídia real.

### R2 — Alto condicionado: isolamento lateral depende do firewall efetivo

- **Evidência estática:** [compose.yaml:348](../../../compose.yaml#L348) conecta control-api às redes públicas e privadas; [compose.yaml:77](../../../compose.yaml#L77) faz ponte semelhante no proxy privado Jellyfin. Há binds administrativos em todas as interfaces, por exemplo [compose.yaml:464](../../../compose.yaml#L464).
- **Condição e impacto:** ausência ou divergência das regras permite alcançar interfaces administrativas a partir de redes que deveriam ser limitadas. O bind, sozinho, não prova exposição WAN.
- **Controle e residual:** [public_network.py:64](../../../services/common/src/homeserver_common/public_network.py#L64) restringe pares e destinos; [public_persistence.py:115](../../../services/common/src/homeserver_common/public_persistence.py#L115) apenas instala arquivos, sem ativar regras ou serviços.
- **Teste pendente:** verificar fluxos permitidos e negados após boot e reinício do Docker, incluindo bridge, DNAT, IPv6 e acesso direto às portas; registrar o backend de firewall usado.
- O Docker documenta que portas publicadas podem contornar regras usuais de UFW; a validação deve observar o caminho real dos pacotes. [Docker: packet filtering](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

### R3 — Alto após comprometimento privado: alcance amplo de segredos e estado

- **Evidência estática:** telemetry monta o `.env` inteiro em [compose.yaml:775](../../../compose.yaml#L775); API, worker e gateway montam o projeto e compartilham estado em [compose.yaml:359](../../../compose.yaml#L359), [compose.yaml:513](../../../compose.yaml#L513) e [compose.yaml:635](../../../compose.yaml#L635).
- **Condição e impacto:** um processo privado comprometido pode ler o arquivo montado e atingir credenciais além das selecionadas para seu ambiente, ou alterar estado compartilhado gravável.
- **Controle e residual:** [service_environment.py:27](../../../services/common/src/homeserver_common/service_environment.py#L27) reduz variáveis por papel; essa seleção não impede leitura do arquivo por um processo comprometido. Os papéis públicos não recebem esse mount.
- **Teste pendente:** mapear permissões e arquivos acessíveis com segredos fictícios; demonstrar limites por papel sem imprimir valores. Não introduzir `*_FILE`, contrariando a política atual.

### R4 — Médio: bloqueio global de novos logins induzido por falhas distribuídas

- **Evidência estática:** [public_limits.py:54](../../../services/control/src/homeserver_control/api/public_limits.py#L54) usa um bucket global comum; [public_limits.py:97](../../../services/control/src/homeserver_control/api/public_limits.py#L97) bloqueia após 100 falhas na janela de dez minutos.
- **Condição e impacto:** tentativas distribuídas entre IPs/contas podem atingir o contador global, bloqueando novos logins por 15 minutos, uma hora e depois 24 horas conforme reincidência.
- **Controle e residual:** limites por IP/conta, concorrência e tentativas reduzem brute force; o bloqueio global também afeta usuários legítimos dos dois serviços. Reprodução já autenticada não passa por esse contador.
- **Teste pendente:** usar relógio injetado e upstream fictício para atingir o limite com identidades distintas; medir duração, recuperação e impacto cruzado Jellyfin/Seerr. Não testar força bruta no serviço real.

### R5 — Médio: consumidores e workers sem limites explícitos de recursos

- **Evidência estática:** os blocos Jellyfin, Seerr, control-worker e download-gateway não declaram limites de CPU, memória ou PIDs: [compose.yaml:34](../../../compose.yaml#L34), [compose.yaml:121](../../../compose.yaml#L121), [compose.yaml:503](../../../compose.yaml#L503) e [compose.yaml:629](../../../compose.yaml#L629).
- **Condição e impacto:** transcodes simultâneos, carga anormal ou falha de worker podem disputar o único compute; a inexistência de quotas no Compose não comprova esgotamento ocorrido.
- **Controle e residual:** proxies, relay e guard têm alguns limites; gateway controla capacidade de armazenamento, o que não equivale a quota de CPU/RAM. Limites eventualmente aplicados fora do Compose não foram verificados.
- **Teste pendente:** medir memória, CPU, latência e concorrência com carga representativa; definir quotas preservando execução do controlador e encerramento seguro das operações.

### R6 — Médio após comprometimento público: saída externa ainda permite exfiltração

- **Evidência estática:** [public_network.py:66](../../../services/common/src/homeserver_common/public_network.py#L66) bloqueia destinos privados/local, mas permite saída HTTP/HTTPS pública de Jellyfin e Seerr.
- **Condição e impacto:** processo comprometido pode enviar dados que já consegue ler a um destino externo permitido; a regra de saída não é uma garantia de confidencialidade da mídia.
- **Controle e residual:** allowlist de peers e bloqueio de redes privadas contêm movimento lateral; restringir destinos externos exige compatibilidade com metadados e outros serviços nativos.
- **Teste pendente:** provar negação para gestão/storage e saída apenas nos fluxos aprovados, com endpoints de teste; registrar exceções necessárias sem credenciais ou URLs de sessão.

### R7 — Alto impacto operacional: alimentação assimétrica do compute e do HD

- **Relato do proprietário:** notebook segue na bateria enquanto a fonte AC do USB desliga. Não houve inspeção de logs/hardware; a hipótese é perda abrupta de I/O com escritas pendentes.
- **Evidência estática:** recuperação local opt-in existe, mas a prova de payload em [usb_recovery_sources.py:130](../../../services/common/src/homeserver_common/usb_recovery_sources.py#L130) verifica tamanho/presença, sem hash do conteúdo. Chamadas de filesystem podem bloquear; timeouts de subprocessos não cobrem todo o fluxo.
- **Controle proposto:** nobreak como aquisição pendente, bloqueio de operações dependentes, estado no SSD, watchdog de progresso e reintegração host/guest/NFS. Identidade incerta, corrupção ou processo/mount preso impedem retomada automática.
- **Residual:** software não recupera escrita perdida antes da detecção; fonte/cabo/disco único continuam falíveis. [Contrato e ensaios](power-recovery.md).

### R8 — Alto após comprometimento da gestão: autoridade concentrada

Controle completo de VMs é requisito confirmado. Uma identidade com administração de hipervisor pode alterar guests, rede e acesso aos dados; reunir painéis não reduz esse impacto. Separar identidade humana, coletores e automações; MFA na administração, tokens restritos, cookies/origens separados e confirmação do escopo destrutivo. Grafana e uma playback comprometida não recebem credenciais de criação/exclusão de VMs. Socket Docker `:ro` não torna seus métodos de API somente leitura. A trilha local pode ser alterada por administrador comprometido; maior resistência exige destino independente. [Matriz de autorização e testes](operations.md).

## Fronteira proposta para o hardware de 16 GB

Esta distribuição é uma hipótese de orçamento a medir; não representa máquinas já criadas nem desempenho comprovado.

| Domínio | RAM prevista | Responsabilidade e limite |
| --- | --- | --- |
| VM edge | 1 GB | Entrada TLS, proxies e políticas públicas; sem mídia, `.env` do core ou credenciais de gestão |
| VM playback | 4 GB | Jellyfin, configuração/cache/transcode próprios e biblioteca somente leitura |
| VM core | 6 GB | Storage e automação juntos; controlador, Arr e downloads no domínio privado |
| Host Proxmox | 3 GB | Hipervisor e gestão acessível somente pelo caminho administrativo |
| Margem não alocada | 2 GB | Reserva inicial; não contar essa memória simultaneamente para outras VMs |

O total é 16 GB. Compartilhar compute, energia e HD USB mantém pontos únicos de falha.
O orçamento de observabilidade sai do core existente; o teto experimental conjunto de 1 GiB para Grafana/Prometheus não é RAM adicional e depende de medição. Loki e Immich exigem reavaliação antes de entrar.
O firewall entre domínios deve ser imposto também fora da VM pública; root dentro dela não pode liberar sozinho acesso à gestão.
Edge pode alcançar somente os endpoints de aplicação previstos; playback pode ler a exportação autorizada, sem alcançar painéis Arr, qBit ou administração Proxmox.
Nenhuma VM pública deve receber o socket Docker, o projeto com `.env`, bancos do controlador ou o dispositivo bruto de armazenamento.
A reprodução Jellyfin será pública com login, e a administração continuará privada; manter Seerr privado inicialmente é a proposta de implantação.
O fluxo Seerr → integração limitada → automação permanece no domínio privado; sua publicação futura exigiria revisão explícita dos fluxos.

### Decisão confirmada de desenho: mídia somente leitura na playback

**O usuário confirmou Jellyfin com mídia somente leitura e alterações pela automação privada.** Essa decisão de desenho não autoriza nem comprova qualquer mudança operacional de montagem.
A configuração, cache e transcode do Jellyfin continuariam graváveis em volumes próprios; as consequências para metadados e legendas precisam ser demonstradas.
A proteção deve existir no servidor: exportação RO para playback e nenhuma exportação RW alternativa acessível a esse cliente.
Uma montagem RO apenas dentro da VM playback não basta contra comprometimento da própria VM: ela poderia solicitar escrita ao servidor se este a autorizar.
No NFS, `ro` impede requisições que modificam o filesystem; `root_squash` não substitui uma exportação RO. A conclusão sobre o cliente comprometido é inferência de fronteira de confiança. [nfs-utils: exports(5)](https://www.man7.org/linux/man-pages/man5/exports.5.html).
Validar leitura e tentativas de criar, alterar, renomear e remover uma fixture desde playback, inclusive com privilégio local, sem alcançar dados reais.
A automação privada concentra edição e exclusão coordenadas; validar o fluxo de legendas e metadados antes de retirar a escrita atual, sem criar uma credencial de escrita acessível à playback.

### Contrato de confiança para exclusão privada

No as-is, a captura privada verifica uma identidade administrativa Jellyfin. Para conter uma playback comprometida, o core não pode confiar apenas numa identidade que esse mesmo Jellyfin valida: o servidor comprometido poderia fabricar a resposta. O to-be exige autorização administrativa independente, com confirmação destrutiva em origem separada, credenciais/cookies inacessíveis à playback e proteção de origem/CSRF. A sessão Jellyfin pode dar contexto, mas não é a autoridade destrutiva. Essa é uma lacuna de desenho identificada na revisão, sem exploração demonstrada; o backlog a registra como G19.

## Evidências exigidas antes de declarar isolamento operacional

| Verificação pendente | Resultado esperado |
| --- | --- |
| Internet e usuário comum | Somente login, reprodução e ações públicas aprovadas; administração negada |
| Proxy indisponível ou guard sem resposta | Nenhum encaminhamento autorizado por omissão |
| Headers encaminhados e sessão | Identidade real preservada apenas de proxies conhecidos; spoofing negado |
| Boot, restart Docker e alteração de rede | Políticas preservadas e os mesmos fluxos negativos continuam bloqueados |
| Playback comprometida em laboratório | Gestão e escrita no storage negadas; exportação RO confirmada no servidor |
| Identidade Jellyfin fabricada | Core recusa exclusão sem autorização privada independente e intenção confirmada |
| Carga e limite global de login | Impactos de recursos e lockout conhecidos, com recuperação medida |

Jellyfin requer configuração correta de proxies conhecidos; URLs podem conter tokens e não devem aparecer integralmente em logs. [Jellyfin: reverse proxy](https://jellyfin.org/docs/general/post-install/networking/reverse-proxy/).
O proxy atual desliga access logs e restringe error logs em [nginx.conf:3](../../../deploy/public-proxy/nginx.conf#L3); isso reduz vazamento, mas exige evidência sanitizada para diagnosticar bloqueios e falhas.
Esta revisão não verificou firewall carregado, ACL Tailscale, NAT, TLS, exportações, políticas nativas dos usuários ou capacidade física. Nenhum teste operacional é relatado como aprovado.
